"""Download new by-law documents to Staging, and approve / reject them.

Candidates (collected weekly):
  - register rows flagged "Newer online" (website posts a newer by-law than province + held copy)
  - rows that are Outdated / Missing / Check where the province's current by-law is online
  - new document links on watched pages (watch.py) that look like a zoning / plan document
Each candidate is downloaded once (deduped on URL and file hash) to
  Maps\\$Bylaw-Monitor\\Staging\\<Municipality>\\<Doc type> <by-law> (<date>)\\<file>
and recorded in data/pending.json with status "pending".

Approve (python stage.py approve <id> [--yes]):
  - moves the staged file into the standard folder: <muni folder>\\Zoning | Development Plan | Secondary Plans
    (planning-district documents go to $Planning Districts\\<PD>\\<type>, with a shortcut in each
    member municipality's folder)
  - moves the superseded files of that type into <type>\\Archives (never another area's
    still-current by-law). Without --yes it only prints the plan.
Reject (python stage.py reject <id> [reason]): the staged file moves to Staging\\_rejected.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

from common import DATA, MAPS, STAGING, bylaw_variants, norm_bylaw, now_iso, read_json, session, write_json

PENDING = DATA / "pending.json"
FILED_LABEL = {"Zoning by-law": "Zoning By-law", "Development plan": "Development Plan By-law",
               "Secondary plan": "Secondary Plan By-law"}
TYPE_FOLDER = {"Zoning by-law": "Zoning", "Development plan": "Development Plan", "Secondary plan": "Secondary Plans"}
MAX_BYTES = 250 * 1024 * 1024
SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(s: str, n: int = 80) -> str:
    return SAFE.sub("_", s).strip(" .")[:n]


def load() -> list[dict]:
    return read_json(PENDING, [])


def save(items: list[dict]) -> None:
    write_json(PENDING, items)


def download(url: str, dest_dir: Path) -> tuple[Path | None, str | None, str | None]:
    """Fetch a document. Returns (path, sha256, error). Only PDF / Word files are kept."""
    s = session()
    try:
        r = s.get(url, timeout=120, stream=True, allow_redirects=True)
        if r.status_code >= 400:
            return None, None, f"HTTP {r.status_code}"
        cd = r.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        name = unquote(m.group(1)) if m else unquote(Path(urlparse(r.url).path).name) or "document"
        h, chunks, size = hashlib.sha256(), [], 0
        for c in r.iter_content(1 << 16):
            size += len(c)
            if size > MAX_BYTES:
                return None, None, "file too large"
            h.update(c)
            chunks.append(c)
        data = b"".join(chunks)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:100]}"
    if data[:4] == b"%PDF":
        ext = ".pdf"
    elif data[:2] == b"PK" and b"word/" in data[:4000]:
        ext = ".docx"
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        ext = ".doc"
    else:
        return None, None, f"not a PDF/Word document ({r.headers.get('content-type', '?')})"
    name = _safe(Path(name).stem, 100) + ext
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    path.write_bytes(data)
    return path, h.hexdigest(), None


def candidates(reg: dict, watch_summary: dict | None) -> list[dict]:
    out = []
    for r in reg["rows"]:
        web = r.get("web") or {}
        if not web.get("url"):
            continue
        if r["status"] == "Newer online":
            out.append({"reason": f"Newer on website than province ({r['od_bylaw'] or '-'}) and your copy",
                        "bylaw": web.get("newer", ""), "row": r, "url": web["url"], "page": web.get("page"),
                        "link_text": web.get("text")})
        elif r["status"] in ("Outdated", "Missing", "Check") and web.get("note") == "current by-law found online":
            out.append({"reason": f"Province lists {r['od_bylaw']}; your copy is {r['status'].lower()}",
                        "bylaw": r["od_bylaw"], "row": r, "url": web["url"], "page": web.get("page"),
                        "link_text": web.get("text")})
    rows_by_no = {}
    for r in reg["rows"]:
        rows_by_no.setdefault(r["muni_no"], []).append(r)
    from web import NOT_DOC, TYPE_RX, link_numbers, _label
    for link in (watch_summary or {}).get("new_links", []):
        label = f"{link.get('text', '')} {link['url']}"
        if NOT_DOC.search(link.get("text") or "") or not link.get("strong"):
            continue
        dtype = next((t for t, rx in TYPE_RX.items() if rx.search(label)), None)
        owner = link["owner"]
        if not dtype or not owner.startswith("muni:"):
            continue      # PD-page links reach members through the register rows above
        row = next((r for r in rows_by_no.get(owner[5:], []) if r["doc_type"] == dtype), None)
        if row:
            nums = sorted(link_numbers(_label(link)))
            out.append({"reason": "New document posted on a watched page", "bylaw": nums[0] if nums else "",
                        "row": row, "url": link["url"], "page": link.get("page"), "link_text": link.get("text")})
    return out


def collect(reg: dict, watch_summary: dict | None = None) -> list[dict]:
    """Download new candidates; returns the items staged this run."""
    from inventory import read_pdf
    items = load()
    # A rejected or failed URL is tried again: municipalities often replace a first-reading copy with
    # the adopted one at the same address. The hash check below drops it if nothing changed.
    seen_urls = {i["url"] for i in items if i["status"] in ("pending", "approved", "filed_by_hand")}
    seen_hash = {i["sha256"] for i in items if i.get("sha256")}
    staged, today = [], date.today().isoformat()
    for c in candidates(reg, watch_summary):
        if c["url"] in seen_urls:
            continue
        seen_urls.add(c["url"])
        r = c["row"]
        folder = STAGING / _safe(r["municipality"]) / _safe(f"{r['doc_type']} {c['bylaw'] or 'unnumbered'} ({today})")
        path, sha, err = download(c["url"], folder)
        if not path and any(i["url"] == c["url"] for i in items):
            continue      # still failing / unreachable: already on record
        n_before = sum(1 for i in items if i["url"] == c["url"])
        item = {"id": hashlib.md5(f"{c['url']}#{n_before}".encode() if n_before else c["url"].encode()).hexdigest()[:8],
                "status": "pending" if path else "failed",
                "staged_at": now_iso(), "municipality": r["municipality"], "muni_no": r["muni_no"],
                "doc_type": r["doc_type"], "area": r["area"], "bylaw": c["bylaw"], "province_bylaw": r["od_bylaw"],
                "reason": c["reason"], "url": c["url"], "page": c["page"], "link_text": c["link_text"],
                "path": str(path) if path else None, "sha256": sha, "error": err}
        if sha and sha in seen_hash:
            path.unlink()
            folder.rmdir() if not any(folder.iterdir()) else None
            continue
        if path and path.suffix == ".pdf":
            info = read_pdf(path)
            item["pdf"] = {k: info.get(k) for k in ("pages", "has_text", "bylaw_numbers", "third_reading",
                                                    "consolidation", "text_type", "title", "reading_stage")}
            if not item["bylaw"] and info.get("bylaw_numbers"):
                item["bylaw"] = info["bylaw_numbers"][0]
        seen_hash.add(sha)
        items.append(item)
        staged.append(item)
    save(items)
    return staged


# ---------------------------------------------------------------- approval

def _targets(item: dict) -> tuple[Path, list[Path]]:
    """Destination folder for an approved document + member folders that get a shortcut."""
    from munis import load_munis, load_pds
    m = next(x for x in load_munis() if str(x["muni_no"]) == str(item["muni_no"]))
    sub = TYPE_FOLDER[item["doc_type"]]
    pd = next((p for p in load_pds() if p["pd_key"] == m["pd_key"]), None)
    says_pd = re.search(r"(?i)planning\s*district|\bp\.?d\.?\b", f"{item.get('link_text', '')} {Path(item.get('path') or '').name}")
    if item["doc_type"] == "Zoning by-law":     # zoning is usually municipal; district-wide only when it says so
        pd_wide = bool(pd and pd["folder"] and says_pd)
    else:
        pd_wide = bool(pd and pd["folder"] and (says_pd or "/pd" in (item.get("page") or "").lower()
                                                 or len(pd["members"].split(";")) > 1))
    if pd_wide:
        dest = MAPS / "$Planning Districts" / pd["folder"].split(";")[0] / sub
        members = [x for x in load_munis() if x["pd_key"] == m["pd_key"] and x["folders"]]
        return dest, [MAPS / x["folders"].split(";")[0] for x in members]
    if not m["folders"]:
        raise SystemExit(f"{m['name']} has no folder under Maps yet - create one (or set it in data/munis.csv) and retry")
    return MAPS / m["folders"].split(";")[0] / sub, []


def _superseded(item: dict, dest: Path) -> list[Path]:
    """Held files of this type that the new document replaces. Never another area's current by-law."""
    reg = read_json(DATA / "register.json")
    rows = [r for r in reg["rows"] if r["muni_no"] == str(item["muni_no"]) and r["doc_type"] == item["doc_type"]]
    keep = {v for r in rows if norm_bylaw(r["od_bylaw"]) != norm_bylaw(item.get("province_bylaw"))
            for v in bylaw_variants(r["od_bylaw"])}
    new = bylaw_variants(item["bylaw"])
    out = []
    for r in rows:
        if r["area"] != item["area"] and item["doc_type"] != "Secondary plan":
            continue
        for f in r.get("files", []) + r.get("other_files", []):
            nums = {v for n in f["numbers"] for v in bylaw_variants(n)}
            p = Path(f["path"])
            if f["archived"] or (nums & new) or (nums & keep) or not p.exists():
                continue
            if item["doc_type"] == "Secondary plan" and not (nums or Path(item["path"]).stem.lower()[:15] in p.stem.lower()):
                continue
            out.append(p)
    return sorted(set(out))


def _shortcut(target: Path, link: Path) -> None:
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{str(link).replace(chr(39), chr(39) * 2)}');"
          f"$s.TargetPath='{str(target).replace(chr(39), chr(39) * 2)}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)


def _archive_dir(p: Path, doc_type: str) -> Path:
    """The file's own municipality (or PD) folder / Zoning | Development Plan | Secondary Plans / Archives."""
    rel = p.relative_to(MAPS).parts
    top = MAPS / rel[0] / rel[1] if rel[0] == "$Planning Districts" else MAPS / rel[0]
    return top / TYPE_FOLDER[doc_type] / "Archives"


def approve(item_id: str, yes: bool = False, archive: bool = True, keep: tuple[str, ...] = (),
            label: str = "") -> dict:
    """archive=False: file without archiving anything (a map schedule, a companion document).
    keep: never archive a file whose name contains any of these strings (e.g. a newer draft).
    label: extra word in the filed name, e.g. "Maps"."""
    items = load()
    item = next(i for i in items if i["id"] == item_id)
    if item["status"] != "pending":
        raise SystemExit(f"{item_id} is {item['status']}, not pending")
    src = Path(item["path"])
    dest, members = _targets(item)
    num = item["bylaw"] or "unnumbered"
    name = f"{FILED_LABEL[item['doc_type']]} {num}{' ' + label if label else ''} - {src.stem}"
    if (item.get("pdf") or {}).get("reading_stage") == "first reading only" or (item.get("claude_check") or {}).get("is_draft"):
        name = "DRAFT - " + name             # Jason's rule: first reading only = draft, not the final version
    new_path = (dest / _safe(name, 140)).with_suffix(src.suffix)
    old = [p for p in _superseded(item, dest) if not any(k.lower() in p.name.lower() for k in keep)] if archive else []
    plan = {"move_new": [str(src), str(new_path)],
            "archive": [[str(p), str(_archive_dir(p, item["doc_type"]) / p.name)] for p in old],
            "shortcuts": [str(m / TYPE_FOLDER[item["doc_type"]] / (new_path.stem + ".lnk")) for m in members]}
    if not yes:
        return {"dry_run": True, **plan}
    dest.mkdir(parents=True, exist_ok=True)
    for a, b in plan["archive"]:
        Path(b).parent.mkdir(parents=True, exist_ok=True)
        if not Path(b).exists():
            shutil.move(a, b)
    shutil.move(str(src), str(new_path))
    for lnk in plan["shortcuts"]:
        Path(lnk).parent.mkdir(parents=True, exist_ok=True)
        _shortcut(new_path, Path(lnk))
    try:
        src.parent.rmdir()
    except OSError:
        pass
    item.update(status="approved", approved_at=now_iso(), filed_as=str(new_path), archived=plan["archive"])
    save(items)
    return {"dry_run": False, **plan}


def reject(item_id: str, reason: str = "") -> None:
    items = load()
    item = next(i for i in items if i["id"] == item_id)
    if item.get("path") and Path(item["path"]).exists():
        dest = STAGING / "_rejected" / Path(item["path"]).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(item["path"], dest)
        item["path"] = str(dest)
    item.update(status="rejected", rejected_at=now_iso(), reject_reason=reason)
    save(items)


def apply_to_register(reg: dict) -> None:
    """Rows with a pending staged document show status 'Pending review'."""
    pend = [i for i in load() if i["status"] == "pending"]
    for r in reg["rows"]:
        hit = [i for i in pend if i["muni_no"] == r["muni_no"] and i["doc_type"] == r["doc_type"]
               and (i["area"] == r["area"] or r["doc_type"] != "Zoning by-law")]
        if hit:
            r["status_before_pending"] = r["status"]
            r["status"] = "Pending review"
            r["pending"] = [{"id": i["id"], "bylaw": i["bylaw"], "path": i["path"], "url": i["url"]} for i in hit]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for i in load():
            if i["status"] == "pending":
                print(f"{i['id']}  {i['municipality']:<24} {i['doc_type']:<17} {i['bylaw']:<12} {i['reason']}\n"
                      f"          {i['path']}")
    elif cmd == "approve":
        a = sys.argv[3:]
        res = approve(sys.argv[2], yes="--yes" in a, archive="--no-archive" not in a,
                      keep=tuple(a[i + 1] for i, x in enumerate(a) if x == "--keep" and i + 1 < len(a)),
                      label=next((a[i + 1] for i, x in enumerate(a) if x == "--label" and i + 1 < len(a)), ""))
        print(("DRY RUN - add --yes to do it\n" if res["dry_run"] else "Done\n")
              + f"file new:  {res['move_new'][0]}\n       ->  {res['move_new'][1]}\n"
              + "".join(f"archive:   {a}\n       ->  {b}\n" for a, b in res["archive"])
              + "".join(f"shortcut:  {s}\n" for s in res["shortcuts"]))
    elif cmd == "reject":
        reject(sys.argv[2], " ".join(sys.argv[3:]))
        print("rejected", sys.argv[2])
