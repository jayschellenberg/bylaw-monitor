"""Inventory the by-law and map files already held under the Maps folder.

For every municipality folder (and every planning district under `$Planning Districts`)
lists each document, classifies it (zoning / development plan / secondary plan / map / other),
and for PDFs reads the cover pages and signature pages for the by-law number, the
third-reading (adoption) date and a consolidation date. Results are cached on
path + size + mtime, so re-runs only read new or changed files.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

from common import DATA, MAPS, NON_MUNI_FOLDERS, now_iso, read_json, write_json

CACHE = DATA / "inventory_cache.json"
OUT = DATA / "inventory.json"

DOC_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".doc", ".docx", ".url", ".kmz"}
SKIP_DIRS = re.compile(r"(?i)minutes|news|building permit|transit|pictures|brochure|newsletter|tax info|"
                       r"housing starts|lidar|press release|economic development")
ARCHIVE_DIR = re.compile(r"(?i)^z?archives?$|^old|-old$|^zarchive")

MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
MON_ABBR = "jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov|dec"
DATE_PATTERNS = [
    # "this 11th day of May, 2021" / "11 day of May A.D. 2021"
    re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+day\s+of\s+({MONTHS}),?\s+(?:a\.?\s?d\.?,?\s+)?(\d{{4}})", re.I),
    # "May 11, 2021" / "May 11th 2021"
    re.compile(rf"\b({MONTHS}|{MON_ABBR})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.I),
    # "11 May 2021"
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS}|{MON_ABBR})\.?,?\s+(\d{{4}})", re.I),
    # 2021-05-11
    re.compile(r"\b(20\d\d|19\d\d)-(\d\d)-(\d\d)\b"),
]
BYLAW_RE = re.compile(
    r"by-?\s?law\s*(?:no\.?|number|#)?\s*:?\s*"
    r"(\d{1,5}\s?[-/.]\s?\d{2,4}(?:\s?[-/]\s?\d{2,4})?|\d{2,5}(?![\d,]))", re.I)
THIRD_RE = re.compile(r"third\s+(?:and\s+final\s+)?reading|read\s+a\s+third\s+time|3rd\s+reading|"
                      r"done\s+and\s+passed|given\s+third", re.I)
CONSOL_RE = re.compile(r"consolidat|office\s+consolidation|updated\s+(?:to|as\s+of|on)|amended\s+to\s+(?:include|date)|"
                       r"includes\s+amendments", re.I)


READING_RX = {
    n: re.compile(rf"read\s+a\s+{w}\s+time|(?:received|given)\s+{w}\s+reading|{w}\s+reading", re.I)
    for n, w in ((1, "first"), (2, "second"), (3, "third"))
}
# Blank: "this _____ day of ___", "this day of , ."  (but "this __13th__day" is filled in)
BLANK_AFTER = re.compile(r"\s*(?:this|on)?\s*(?:_{2,}\s*(?:day|of)\b|day\s+of\s*[,_.]|day\s+of\s+_)", re.I)
FILLED_AFTER = re.compile(rf"\s*(?:this|on)?\s*_*\s*(?:the\s+)?(?:\d{{1,2}}(?:st|nd|rd|th)?\s*(?:\)|day)|[a-z-]+\s*\(\d{{1,2}}\w*\)\s*day"
                          rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:day\s+)?of\s+(?:{MONTHS})"
                          rf"|(?:{MONTHS})\s+\d{{1,2}}|\d{{1,2}}\s+(?:{MONTHS})|\d{{4}}-\d\d-\d\d)", re.I)


def reading_stage(text: str) -> str | None:
    """'first reading only' when the readings block shows a first-reading date but blank second and
    third readings (a draft, not the adopted by-law); 'adopted' when third reading has a date."""
    filled, blank = set(), set()
    for n, rx in READING_RX.items():
        for m in rx.finditer(text):
            after = text[m.end(): m.end() + 70]
            if BLANK_AFTER.match(after):
                blank.add(n)
            elif FILLED_AFTER.match(after):
                filled.add(n)
    if 3 in filled:
        return "adopted"
    if 1 in filled and not ({2, 3} & filled) and ({2, 3} & blank):
        return "first reading only"
    if {1, 2} <= filled and 3 in blank:
        return "second reading"
    return None


def _month(m: str) -> int:
    m = m.lower()[:3]
    return ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m) + 1


def find_dates(text: str) -> list[tuple[int, str]]:
    """[(char offset, 'YYYY-MM-DD')] for every parseable date in text."""
    out = []
    for i, pat in enumerate(DATE_PATTERNS):
        for m in pat.finditer(text):
            g = m.groups()
            try:
                if i == 0 or i == 2:
                    d = datetime(int(g[2]), _month(g[1]), int(g[0]))
                elif i == 1:
                    d = datetime(int(g[2]), _month(g[0]), int(g[1]))
                else:
                    d = datetime(int(g[0]), int(g[1]), int(g[2]))
            except (ValueError, IndexError):
                continue
            if 1950 <= d.year <= datetime.now().year + 1:
                out.append((m.start(), d.strftime("%Y-%m-%d")))
    return sorted(out)


def _date_near(text: str, anchor: re.Pattern, window: int = 400) -> str | None:
    """Latest date within `window` chars after (or shortly before) any anchor match."""
    dates = find_dates(text)
    hits = []
    for m in anchor.finditer(text):
        for pos, d in dates:
            if m.start() - 120 <= pos <= m.end() + window:
                hits.append(d)
    return max(hits) if hits else None


def classify_name(path: Path) -> str:
    s = (str(path.parent.name) + " " + path.stem).lower()
    s = re.sub(r"[_\-.]+", " ", s)              # 'Developmen-Plan' -> 'developmen plan'
    s = re.sub(r"\bdevelopmen\b", "development", s)
    if re.search(r"secondary\s*plan|area\s*structure\s*plan|neighbourhood\s*plan", s):
        return "Secondary plan"
    if re.search(r"develop(ment)?\s*plan|dev\s*plan|\bdp\b|\bmdp\b", s):
        return "Development plan"
    if re.search(r"zoning|\bzbl\b|zone", s):
        return "Zoning by-law"
    if re.search(r"map|parcel|ownership|aerial|soil|plan\b", s) or path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".kmz"}:
        return "Map"
    return "Other"


def classify_text(text: str) -> str | None:
    head = text[:3000].lower()
    if re.search(r"secondary\s+plan", head):
        return "Secondary plan"
    if re.search(r"development\s+plan", head) and not re.search(r"zoning\s+by-?\s?law", head[:800]):
        return "Development plan"
    if re.search(r"zoning\s+by-?\s?law", head):
        return "Zoning by-law"
    return None


def read_pdf(path: Path) -> dict:
    """Cover + signature pages: by-law numbers, third-reading date, consolidation date."""
    info: dict = {}
    try:
        doc = fitz.open(path)
    except Exception as e:  # corrupt / encrypted
        return {"error": f"open failed: {e}"}
    with doc:
        n = doc.page_count
        info["pages"] = n
        idx = list(range(min(4, n))) + list(range(max(4, n - 4), n))
        head = "\n".join(doc[i].get_text() for i in idx if i < 4)
        tail = "\n".join(doc[i].get_text() for i in idx if i >= 4)
        meta = doc.metadata or {}
    text = head + "\n" + tail
    info["has_text"] = len(text.strip()) > 200
    if not info["has_text"]:
        return info
    nums = Counter(re.sub(r"\s", "", m.group(1)) for m in BYLAW_RE.finditer(head[:6000]))
    info["bylaw_numbers"] = [k for k, _ in nums.most_common(5)]
    info["text_type"] = classify_text(head)
    info["third_reading"] = _date_near(tail, THIRD_RE) or _date_near(head, THIRD_RE)
    info["consolidation"] = _date_near(head[:5000], CONSOL_RE, window=200)
    info["reading_stage"] = reading_stage(re.sub(r"\s+", " ", text))
    info["title"] = re.sub(r"\s+", " ", head[:400]).strip()[:200]
    for k in ("creationDate", "modDate"):
        v = meta.get(k) or ""
        if v.startswith("D:") and len(v) >= 10:
            info["pdf_" + k] = f"{v[2:6]}-{v[6:8]}-{v[8:10]}"
    return info


def owners() -> list[dict]:
    """Municipality folders + planning-district folders."""
    out = []
    for d in sorted(MAPS.iterdir()):
        if not d.is_dir() or d.name in NON_MUNI_FOLDERS or d.name.startswith("$"):
            continue
        out.append({"kind": "muni", "folder": d.name, "path": d})
    pd_root = MAPS / "$Planning Districts"
    for d in sorted(pd_root.iterdir()):
        if d.is_dir():
            out.append({"kind": "pd", "folder": d.name, "path": d})
    return out


def scan_owner(o: dict, cache: dict) -> list[dict]:
    docs = []
    root: Path = o["path"]
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in DOC_EXT:
            continue
        rel = p.relative_to(root)
        if any(SKIP_DIRS.search(part) for part in rel.parts[:-1]):
            continue
        st = p.stat()
        key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        rec = {
            "file": str(rel), "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            "archived": any(ARCHIVE_DIR.search(part) for part in rel.parts[:-1]),
            "name_type": classify_name(p),
        }
        if p.suffix.lower() == ".pdf" and rec["name_type"] != "Map" or (
                p.suffix.lower() == ".pdf" and re.search(r"(?i)by-?law|bylaw|zon|plan", p.name)):
            if key not in cache or (cache[key].get("has_text") and "reading_stage" not in cache[key]):
                cache[key] = read_pdf(p)
            rec.update(cache[key])
        t = rec.get("text_type")
        rec["doc_type"] = t if t and rec["name_type"] in ("Other", "Map", t) else rec["name_type"]
        docs.append(rec)
    return docs


def run() -> dict:
    cache = read_json(CACHE, {})
    result = {"generated": now_iso(), "owners": []}
    for i, o in enumerate(owners()):
        docs = scan_owner(o, cache)
        result["owners"].append({"kind": o["kind"], "folder": o["folder"], "docs": docs})
        if i % 20 == 0:
            write_json(CACHE, cache)
            print(f"  {i} folders scanned ({o['folder']})", flush=True)
    write_json(CACHE, cache)
    write_json(OUT, result)
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    r = run()
    docs = [d for o in r["owners"] for d in o["docs"]]
    print(f"{len(r['owners'])} folders, {len(docs)} documents")
    print(Counter(d["doc_type"] for d in docs))
    print("PDFs without text (scanned):", sum(1 for d in docs if d.get("has_text") is False))
