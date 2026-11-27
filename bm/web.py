"""Attach website findings to register rows.

For each register row, looks at the documents linked from the municipality's and its
planning district's watched pages and records:
  web.url / web.text   the online copy of the province's current by-law (if found), else the
                       most relevant zoning / plan document on the site
  web.newer            a linked document whose by-law number looks NEWER than both the
                       province's number and the held copy (municipality posted ahead of the
                       provincial layer); the row status becomes "Newer online"
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from common import DATA, bylaw_variants, norm_bylaw, read_json, write_json

TYPE_RX = {
    "Zoning by-law": re.compile(r"(?i)zoning|zbl"),
    "Development plan": re.compile(r"(?i)development[\s_-]*plan|dev[\s_-]*plan|\bdp\b"),
    "Secondary plan": re.compile(r"(?i)secondary[\s_-]*plan"),
}
NOT_DOC = re.compile(r"(?i)application|form\b|fee|notice|hearing|agenda|minutes|variance|conditional use|"
                     r"subdivision|amend\w*|\bzba\b|re-?\s?zon|reserve|report|review|comments|draft|1st[\s_]reading|first[\s_]reading|"
                     r"fact\s*sheet|brochure|presentation|survey|map\b|schedule")
# The look-arounds skip pieces of a date such as 7.25.18 or 05-21-25 ('7-25' is not a by-law).
NUM_RX = re.compile(r"(?i)(?:by-?\s?law|no\.?|#|zbl)?\s*(?<![\d][-/.])(?<!\d)(\d{1,5})\s?[-/._ ]\s?(\d{2,4})(?!\d)(?![-/.]\d)")


def _label(d: dict) -> str:
    """Link text + the URL's file name. Not the whole URL: CMS document ids (GUIDs, upload dates)
    read as by-law numbers ('docId=...-47...' -> '1-47')."""
    name = d["url"].rstrip("/").split("/")[-1].split("?")[0]
    if re.search(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}|docid|^\d+$", name):
        name = ""
    return f"{d['text']} {re.sub(r'[_+]|%20', ' ', name)}"


# Right after "By-law" / "No." / "#", also read three-part numbers ("By-Law 109/02/26") and
# year-first ones ("Zoning By-Law 2026-05"); elsewhere those shapes are usually dates.
KEYED_RX = re.compile(r"(?i)(?:by-?\s?law|no\.?|#|(?<![a-z])zbl|(?<![a-z])bl)[\s_]*(?:no\.?\s*)?(\d{1,4}[-/]\d{2}[-/]\d{2,4}|(?:19|20)\d\d[-/]\d{1,3})(?![\d])")
# Plain numbers: "By-Law 1381", "No 1977", or a file name that starts with one ("4968-Zoning-By-Law-…").
# Never a bare year ("Zoning Bylaw 2026 Consolidation").
PLAIN_RX = re.compile(r"(?i)(?:by-?\s?law|\bno\.?|#|(?<![a-z])zbl|(?<![a-z])bl)[\s_#]*(?:no\.?\s*)?(\d{3,5})(?!\d)(?![/.-]\d)|(?:^|[\s/])(\d{3,5})[-_ ](?=[a-z])")


CONSOL_RX = re.compile(r"(?i)consolidat|office\s+consol|with\s+amendments|as\s+amended|incl(?:uding|\.)?\s+amendments|"
                       r"updated\s+(?:to|as\s+of)|amended\s+to\b|\bconsol\b")


def consol_date(label: str) -> str | None:
    """Latest date a consolidation link shows: 'June 23, 2020', '2020-06-23', '20200623', '202010',
    or a year ('up to ZBA 22-2024' -> 2024-01-01). None if it shows none."""
    from inventory import find_dates
    text = re.sub(r"[_+]|%20", " ", label)
    dates = [d for _, d in find_dates(text)]
    for y, m, d in re.findall(r"(?<!\d)(20\d\d)[-.]?(0[1-9]|1[0-2])[-.]?(0[1-9]|[12]\d|3[01])(?!\d)", text):
        dates.append(f"{y}-{m}-{d}")
    if not dates:
        for y, m in re.findall(r"(?<!\d)(20\d\d)(0[1-9]|1[0-2])(?!\d)", text):
            dates.append(f"{y}-{m}-01")
    if not dates:
        years = [int(y) for y in re.findall(r"(?<!\d)(?:19|20)(\d\d)(?!\d)", text)]
        years = [2000 + y if y < 50 else 1900 + y for y in years]
        if years:
            dates.append(f"{max(years)}-01-01")
    dates = [d for d in dates if d <= date.today().isoformat()]
    return max(dates) if dates else None


def link_numbers(label: str) -> set[str]:
    out = set()
    for a, b in NUM_RX.findall(label):
        n = norm_bylaw(f"{a}-{b}")
        if not re.fullmatch(r"(19|20)\d\d-\d{1,2}", n):   # skip upload-path dates like 2025/12
            out.add(n)
    for m in KEYED_RX.finditer(label):
        out.add(norm_bylaw(m.group(1)))
    for m in PLAIN_RX.finditer(label):
        n = m.group(1) or m.group(2)
        if not re.fullmatch(r"(19|20)\d\d", n):
            out.add(norm_bylaw(n))
    return out


def year_of(num: str) -> int | None:
    """Adoption year implied by a by-law number ('8-18' -> 2018, '1709-2013' -> 2013, '2021-04' -> 2021)."""
    parts = norm_bylaw(num).split("-")
    years = []
    for p in parts:
        if len(p) == 4 and p.isdigit() and 1950 <= int(p) <= 2100:
            years.append(int(p))
    if not years and len(parts) >= 2 and len(parts[-1]) == 2 and parts[-1].isdigit():
        y = int(parts[-1])
        years.append(2000 + y if y < 50 else 1900 + y)
    years = [y for y in years if y <= date.today().year + 1]
    return max(years) if years else None


def _about_this_muni(label: str, row: dict, others: list[str]) -> bool:
    """Planning-district and neighbour pages list several municipalities' by-laws: keep a link
    only if it names no OTHER municipality, or names this one (with a matching type)."""
    low = norm_text(label)
    me = norm_text(row["municipality"])
    named_other = [o for o in others if o != me and len(o) > 3 and o in low]
    if me in low:
        if row["muni_type"] == "RM" and re.search(rf"town of {re.escape(me)}", low):
            return False
        if row["muni_type"] == "TOWN" and re.search(rf"(rm|r m|rural municipality) of {re.escape(me)}", low):
            return False
        return True
    return not named_other


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\bsaint\b", "st", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def attach(reg: dict, watched: dict) -> list[dict]:
    """Mutates register rows; returns web change records for the Changes sheet."""
    from munis import load_munis
    munis = load_munis()
    pd_of = {str(m["muni_no"]): m["pd_key"] for m in munis}
    others = [norm_text(m["name"]) for m in munis]
    # Links reviewed by hand that only look newer (a date read as a by-law number): {url: reason}
    ignore = read_json(DATA / "web_ignore.json", {})
    areas_of: dict[tuple, set] = {}
    for x in reg["rows"]:
        areas_of.setdefault((x["muni_no"], x["doc_type"]), set()).add(x["od_bylaw"])
    findings = []
    for r in reg["rows"]:
        owners = [f"muni:{r['muni_no']}"]
        if r["doc_type"] != "Zoning by-law" or not watched.get(owners[0], {}).get("pages"):
            owners.append(f"pd:{pd_of.get(r['muni_no'], '')}")
        links = {}
        for o in owners:
            w = watched.get(o) or {}
            # agent_pages: found by a Claude research pass where the crawler could not read the site
            for p in w.get("pages", []) + w.get("agent_pages", []) + [
                    {"url": u, "docs": w.get("locked_docs", {}).get(u, [])} for u in w.get("locked_pages", [])]:
                for d in p.get("docs", []):
                    links.setdefault(d["url"], {**d, "page": p["url"]})
        trx = TYPE_RX[r["doc_type"]]
        typed = [d for d in links.values() if trx.search(f"{d['text']} {d['url']}")]
        if r["doc_type"] == "Zoning by-law":
            typed = [d for d in typed if _about_this_muni(_label(d), r, others)]
        web = {}
        if r["od_bylaw"]:
            v = bylaw_variants(r["od_bylaw"])
            hit = [d for d in typed if v & {x for n in link_numbers(_label(d)) for x in bylaw_variants(n)}]
            if hit:
                web = {"url": hit[0]["url"], "text": hit[0]["text"] or hit[0]["url"], "page": hit[0]["page"],
                       "note": "current by-law found online"}
        # Consolidations of the current by-law. Jason's rule: a consolidation, when one exists, is the
        # current version; the newest one is downloaded and replaces the old (stage.collect_consolidations).
        od_v = bylaw_variants(r["od_bylaw"]) if r["od_bylaw"] else set()
        consol = []
        for d in typed:
            lab = _label(d)
            if not CONSOL_RX.search(lab) or re.search(r"(?i)draft|1st[\s_]reading|first[\s_]reading", lab):
                continue
            nums = {v for x in link_numbers(lab) for v in bylaw_variants(x)}
            if od_v and nums and not (od_v & nums):
                continue      # a consolidation of some other by-law
            if not (od_v & nums) and len(areas_of.get((r["muni_no"], r["doc_type"]), ())) > 1 and not any(
                    norm_text(a) in norm_text(lab) for a in r["area"].split(", ") if a):
                continue      # several by-law areas: an unnumbered consolidation must name this one
            cdate = consol_date(lab)
            od_year = year_of(r["od_bylaw"]) if r["od_bylaw"] else None
            if cdate and od_year and int(cdate[:4]) < od_year:
                continue      # older than the current by-law itself, so not a consolidation of it
            consol.append({"url": d["url"], "text": d["text"] or d["url"], "page": d["page"], "date": cdate})
        consol.sort(key=lambda c: c["date"] or "", reverse=True)
        r["web_consolidations"] = consol
        if consol:
            c = consol[0]
            web = {"url": c["url"], "text": c["text"], "page": c["page"],
                   "note": f"consolidation online{' (' + c['date'] + ')' if c['date'] else ''}"}
        main_docs = [d for d in typed if not NOT_DOC.search(_label(d))]   # link text + file name
        if not web and main_docs:
            d = main_docs[0]
            web = {"url": d["url"], "text": d["text"] or d["url"], "page": d["page"],
                   "note": "zoning/plan document on site (number not shown in link)"}
        # Newer than both the province and the held copy?
        known = {norm_bylaw(r["od_bylaw"])} | {norm_bylaw(n) for f in r.get("files", []) for n in f["numbers"]}
        # everything of this type you hold, including by-laws newer than the province's
        held_files = [f for f in r.get("files", []) + r.get("other_files", []) if not f["archived"]]
        held_all = {v for f in held_files if not f.get("draft") for n in f["numbers"] for v in bylaw_variants(n)}
        held_drafts = {v for f in held_files if f.get("draft") for n in f["numbers"] for v in bylaw_variants(n)}
        base_year = max(filter(None, [year_of(r["od_bylaw"]) if r["od_bylaw"] else None,
                                      *(year_of(n) for f in r.get("files", []) for n in f["numbers"])]), default=None)
        for d in (main_docs if r["doc_type"] != "Secondary plan" else []):
            if d["url"] in ignore or CONSOL_RX.search(_label(d)):
                continue     # consolidations are handled above, never counted as a new by-law
            nums_here = link_numbers(_label(d))
            if r["od_bylaw"] and bylaw_variants(r["od_bylaw"]) & {v for x in nums_here for v in bylaw_variants(x)}:
                continue     # names the province's own by-law, e.g. "ZBL 5-05 consolidated up to ZBA 22-2024"
            for n in nums_here:
                y = year_of(n)
                if n in known or not y or not base_year or y <= base_year:
                    continue
                if bylaw_variants(n) & held_drafts and not bylaw_variants(n) & held_all:
                    web = {"url": d["url"], "text": d["text"] or d["url"], "page": d["page"], "newer": n, "held": True,
                           "note": f"You hold a DRAFT (first reading only) of {n}; province: {r['od_bylaw'] or '-'}"}
                    break
                if bylaw_variants(n) & held_all:     # the newer one is already in your folders
                    web = {"url": d["url"], "text": d["text"] or d["url"], "page": d["page"], "newer": n, "held": True,
                           "note": f"You hold {n} ({y}), newer than the province's {r['od_bylaw'] or '-'}"}
                    r["status_before_web"] = r["status"]
                    r["status"] = "Current (ahead of province)"
                    break
                web = {"url": d["url"], "text": d["text"] or d["url"], "page": d["page"],
                       "note": f"NEWER on website: by-law {n} ({y}) vs province {r['od_bylaw'] or '-'}", "newer": n}
                if r["status"] in ("Current", "Current (misfiled)", "Outdated", "Check", "Missing"):
                    r["status_before_web"] = r["status"]
                    r["status"] = "Newer online"
                findings.append({"detected": reg["generated"][:10], "source": "Website", "muni": r["municipality"],
                                 "doc_type": r["doc_type"], "change": "posted", "bylaw": n,
                                 "current_set": [r["od_bylaw"]] if r["od_bylaw"] else [],
                                 "note": f"{d['text']} (on {d['page']})", "url": d["url"]})
                break
            if web.get("newer"):
                break
        r["web"] = web
    return findings


def run() -> list[dict]:
    reg = read_json(DATA / "register.json")
    watched = read_json(DATA / "watched_pages.json", {})
    findings = attach(reg, watched)
    write_json(DATA / "register.json", reg)
    return findings


if __name__ == "__main__":
    for f in run():
        print(f["muni"], "|", f["doc_type"], "|", f["bylaw"], "|", f["note"][:100])
