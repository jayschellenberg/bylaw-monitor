"""Keep the newest consolidation of each current by-law, replacing the old one.

Jason's rule (2026-11-05): a consolidation, when one exists, is the most current version of a
by-law. When a municipality / planning district posts a newer consolidation than the one held, it
is downloaded and filed automatically, and the previous consolidation of that by-law moves to
<type>\\Archives in the folder it came from. The original (unconsolidated) by-law is left alone.

For each register row whose watched pages show a consolidation of the province's current by-law
(web.py -> row["web_consolidations"], newest first), the newest one is downloaded and compared:
  - same bytes as a held file                      -> nothing to do
  - no consolidation of this by-law held           -> file it
  - both dated (PDF text / link / file name)       -> file it only if the posted one is newer
  - undated                                        -> compare the site's Last-Modified with the held
                                                      file's date; if that can't decide, leave it in
                                                      Staging as "pending" for review
First-reading copies are never treated as consolidations (see drafts.py).
Every action is recorded in data/pending.json (kind "consolidation").

    python consolidations.py          # dry run: what would be filed / archived
    python consolidations.py --yes
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from common import DATA, STAGING, bylaw_variants, now_iso, read_json, session
from inventory import read_pdf
from stage import FILED_LABEL, _archive_dir, _safe, _shortcut, _targets, download, load, save
from web import CONSOL_RX, consol_date


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_consolidation(f: dict) -> bool:
    return bool(f.get("consolidation")) or bool(CONSOL_RX.search(Path(f["file"]).name))


def _held_date(f: dict) -> str | None:
    return max(filter(None, [f.get("consolidation"), consol_date(Path(f["file"]).name)]), default=None)


def _last_modified(s, url: str) -> str | None:
    try:
        r = s.head(url, timeout=20, allow_redirects=True)
        lm = r.headers.get("last-modified")
        return parsedate_to_datetime(lm).date().isoformat() if lm else None
    except Exception:
        return None


def collect(reg: dict, apply: bool = False) -> list[dict]:
    s = session()
    items = load()
    done_urls = {i["url"] for i in items if i.get("kind") == "consolidation" and i.get("status") in ("auto_filed", "pending")}
    results = []
    seen_rows = set()
    for r in reg["rows"]:
        cons = r.get("web_consolidations") or []
        if not cons or not r["od_bylaw"]:
            continue
        key = (r["muni_no"], r["doc_type"], r["od_bylaw"])
        if key in seen_rows:
            continue
        seen_rows.add(key)
        c = cons[0]
        held = [f for f in r.get("files", []) if not f["archived"] and not f.get("draft")]
        held_cons = [f for f in held if _is_consolidation(f)]
        held_date = max(filter(None, (_held_date(f) for f in held_cons)), default=None)
        rec = {"municipality": r["municipality"], "doc_type": r["doc_type"], "bylaw": r["od_bylaw"],
               "url": c["url"], "link_text": c["text"], "page": c["page"], "link_date": c["date"],
               "held_consolidations": [f["path"] for f in held_cons], "held_date": held_date}
        if c["url"] in done_urls:
            rec["decision"] = "already handled"
            results.append(rec)
            continue
        done_urls.add(c["url"])      # the same district document can appear under several members
        folder = STAGING / "_consolidations" / _safe(f"{r['municipality']} {r['doc_type']} {r['od_bylaw']}")
        path, sha, err = download(c["url"], folder)
        if not path:
            rec["decision"] = f"download failed: {err}"
            results.append(rec)
            continue
        held_hashes = {_sha(Path(f["path"])) for f in held if Path(f["path"]).exists()}
        if sha in held_hashes:
            path.unlink()
            rec["decision"] = "same file already held"
            results.append(rec)
            continue
        info = read_pdf(path) if path.suffix == ".pdf" else {}
        if info.get("reading_stage") == "first reading only":
            path.unlink()
            rec["decision"] = "first-reading copy, not a consolidation"
            results.append(rec)
            continue
        pdf_nums = {v for n in (info.get("bylaw_numbers") or [])[:5] for v in bylaw_variants(n)}
        if pdf_nums and not (pdf_nums & bylaw_variants(r["od_bylaw"])):
            path.unlink()
            rec["decision"] = f"PDF is a different by-law ({', '.join(info['bylaw_numbers'][:3])})"
            results.append(rec)
            continue
        new_date = max(filter(None, [info.get("consolidation"), c["date"]]), default=None)
        rec.update(new_date=new_date, sha256=sha)
        if not held_cons:
            decision = "file: no consolidation held yet"
        elif new_date and held_date:
            decision = "file: newer consolidation" if new_date > held_date else "same or older than held"
        else:
            lm = _last_modified(s, c["url"])
            newest_held = max((f["modified"] for f in held_cons), default=None)
            rec["last_modified"] = lm
            if lm and newest_held:
                decision = "file: newer on website (last modified)" if lm > newest_held else "same or older than held"
            else:
                decision = "review: cannot tell which is newer"
        rec["decision"] = decision
        if not decision.startswith(("file", "review")):
            path.unlink()
            results.append(rec)
            continue
        item = {"id": hashlib.md5(c["url"].encode()).hexdigest()[:8], "kind": "consolidation",
                "staged_at": now_iso(), "municipality": r["municipality"], "muni_no": r["muni_no"],
                "doc_type": r["doc_type"], "area": r["area"], "bylaw": r["od_bylaw"], "province_bylaw": r["od_bylaw"],
                "reason": decision, "url": c["url"], "page": c["page"], "link_text": c["text"], "path": str(path),
                "sha256": sha, "pdf": {k: info.get(k) for k in ("pages", "third_reading", "consolidation", "reading_stage")},
                "consolidation_date": new_date, "status": "pending"}
        if decision.startswith("file") and apply:
            dest, members = _targets(item)
            name = f"{FILED_LABEL[r['doc_type']]} {r['od_bylaw']} - Consolidated {new_date or 'undated'} - {path.stem}"
            new_path = dest / (_safe(name, 150) + path.suffix)     # not with_suffix: names like "St._Laurent" hold dots
            dest.mkdir(parents=True, exist_ok=True)
            archived = []
            for f in held_cons:
                p = Path(f["path"])
                if p.exists():
                    tgt = _archive_dir(p, r["doc_type"]) / p.name
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    if not tgt.exists():
                        shutil.move(str(p), str(tgt))
                        archived.append([str(p), str(tgt)])
            shutil.move(str(path), str(new_path))
            for m in members:
                lnk = m / dest.name / (new_path.stem + ".lnk")
                lnk.parent.mkdir(parents=True, exist_ok=True)
                _shortcut(new_path, lnk)
            item.update(status="auto_filed", filed_as=str(new_path), archived=archived, approved_at=now_iso())
            rec.update(filed_as=str(new_path), archived=archived)
        elif decision.startswith("file"):
            rec["would_archive"] = [f["path"] for f in held_cons]
            path.unlink()            # dry run: leave nothing behind
        if apply:
            items.append(item)
        results.append(rec)
    if apply:
        save(items)
    return results


if __name__ == "__main__":
    from common import read_json as _rj
    import register
    res = collect(_rj(register.REGISTER), apply="--yes" in sys.argv)
    for x in res:
        print(f"{x['municipality']:<20} {x['doc_type']:<17} {x['bylaw']:<11} {x['decision']:<42} "
              f"new={x.get('new_date')} held={x.get('held_date')}")
        for p in x.get("would_archive", []) + [a for a, _ in x.get("archived", [])]:
            print(f"{'':>20}   archive: {p}")
        if x.get("filed_as"):
            print(f"{'':>20}   filed:   {x['filed_as']}")
