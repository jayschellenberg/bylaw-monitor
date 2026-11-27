"""Find the page(s) on each municipal / planning-district website where by-laws are posted.

From each home page, follows links whose text or URL suggests by-laws, zoning or planning
(two levels deep, capped), and keeps the pages that link to zoning / development-plan /
secondary-plan documents. These become the "watched pages" the weekly check diffs.

Also used for targeted re-discovery when a watched page breaks, and for the rolling
quarterly re-crawl (see DESIGN.md "Website changes").
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup

from common import DATA, now_iso, read_json, session, write_json
from munis import load_munis, load_pds

WATCHED = DATA / "watched_pages.json"

NAV_RX = re.compile(r"(?i)by-?\s?laws?|zoning|planning|development|land\s*use|secondary|"
                    r"permits?|documents|publications|government|council|municipal\s*office|policies")
DOC_RX = re.compile(r"(?i)zoning|zbl|development[\s_-]*plan|dev[\s_-]*plan|secondary[\s_-]*plan|"
                    r"\bdp\b|land[\s_-]*use|by-?\s?law")
STRONG_RX = re.compile(r"(?i)zoning|development[\s_-]*plan|secondary[\s_-]*plan")
DOC_EXT = re.compile(r"(?i)\.(pdf|docx?)(\?|$)|/download|/documents?/|/file/|filedownload|/media/|/wp-content/uploads/")
SKIP_URL = re.compile(r"(?i)\.(jpe?g|png|gif|zip|mp4|ics)$|facebook|twitter|instagram|youtube|linkedin|mailto:|tel:|"
                      r"/(events?|calendar|news|jobs|careers|tenders|recreation|parks)/")

MAX_PAGES = 18      # fetched pages per site
DELAY = 1.0         # seconds between requests to the same site


def _links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base, a["href"].strip()))[0]
        if href.startswith("http"):
            out.append((href, " ".join(a.get_text(" ").split())[:200]))
    for f in soup.find_all(["iframe", "frame"], src=True):   # framesets (e.g. tcwpd.ca)
        out.append((urljoin(base, f["src"]), "frame"))
    return out


def _same_site(u: str, home: str) -> bool:
    a, b = urlparse(u).netloc.lower().removeprefix("www."), urlparse(home).netloc.lower().removeprefix("www.")
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def doc_links(html: str, base: str) -> list[dict]:
    """Links on a page that look like zoning / plan / by-law documents."""
    seen, out = set(), []
    for href, text in _links(html, base):
        if href in seen:
            continue
        label = f"{text} {urlparse(href).path}"
        if DOC_EXT.search(href) and DOC_RX.search(label):
            seen.add(href)
            out.append({"url": href, "text": text, "strong": bool(STRONG_RX.search(label))})
    return out


FOLDER_SKIP = re.compile(r"(?i)minutes|agenda|newsletter|audited|financial\s*statement|budget|payroll|tender|"
                         r"job|employment|recreation|tax\s*levies")
MAX_FOLDER_CALLS = 80


def folder_doc_links(s, html: str, base: str) -> list[dict]:
    """Document links held in a site's JavaScript folder viewer, read straight from its JSON/HTML endpoint.

    Two platforms common on Manitoba municipal sites:
      - Catalis / GovOffice "docman": <div class="docmanContainer" id=.. sec={..}> ->
        /admin/dm/Public/GetFolderById/?folderid=&sec= (JSON), files at /uploads/dm/<fileId>/<fileName>
      - municipalwebsites.ca: <li data-directory=..> + #main-dir-id -> /Home/Documents?dirId=&mainDirId=
        (HTML fragment), files at /Home/DownloadDocument?docId=
    """
    out, calls = [], 0
    m = re.search(r'class="docmanContainer"\s+id="(\d+)"\s+sec="(\{[^"]+\})"', html)
    if m:
        root = urljoin(base, "/")
        todo = [(int(m.group(1)), "")]
        while todo and calls < MAX_FOLDER_CALLS:
            fid, path = todo.pop(0)
            try:
                f = s.get(urljoin(root, "/admin/dm/Public/GetFolderById/"),
                          params={"folderid": fid, "sec": m.group(2)}, timeout=25).json()
            except Exception:
                break
            calls += 1
            time.sleep(0.3)
            for x in f.get("dmfiles") or []:
                title = x.get("FileTitle") or x.get("fileName") or ""
                out.append({"url": urljoin(root, f"/uploads/dm/{x['fileId']}/{x['fileName']}"),
                            "text": title, "folder": path})
            todo += [(d["folderId"], f"{path}/{d['folderName']}") for d in f.get("dmfolders") or []
                     if not FOLDER_SKIP.search(d.get("folderName") or "")]
    main = re.search(r'id="main-dir-id"\s+data-directory="([0-9a-f-]{36})?"', html)   # may be empty (Gillam)
    if main:
        todo = [(d, "") for d in dict.fromkeys(re.findall(r'<li data-directory="([0-9a-f-]{36})"', html))]
        seen = set()
        while todo and calls < MAX_FOLDER_CALLS:
            did, path = todo.pop(0)
            if did in seen:
                continue
            seen.add(did)
            try:
                frag = s.get(urljoin(base, "/Home/Documents"), params={"dirId": did, "mainDirId": main.group(1) or ""},
                             timeout=25).text
            except Exception:
                break
            calls += 1
            time.sleep(0.3)
            for href, text in re.findall(r'href="(/Home/DownloadDocument\?docId=[0-9a-f-]{36})"[^>]*>\s*([^<]{1,200})', frag):
                out.append({"url": urljoin(base, href), "text": " ".join(text.split()), "folder": path})
            todo += [(d, path) for d in re.findall(r'<li data-directory="([0-9a-f-]{36})"', frag) if d not in seen]
    seen_u, docs = set(), []
    for d in out:
        label = f"{d['text']} {urlparse(d['url']).path}"
        if d["url"] in seen_u or not DOC_RX.search(f"{label} {d['folder']}"):
            continue
        seen_u.add(d["url"])
        docs.append({"url": d["url"], "text": d["text"], "strong": bool(STRONG_RX.search(label)), "via": "folder viewer"})
    return docs


def crawl(owner_id: str, home: str) -> dict:
    s = session()
    res = {"owner": owner_id, "home": home, "checked": now_iso(), "pages": [], "error": None}
    queue, seen, fetched = [(home, 0, "home")], set(), 0
    final_home = home
    while queue and fetched < MAX_PAGES:
        url, depth, why = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = s.get(url, timeout=25, allow_redirects=True)
            fetched += 1
            time.sleep(DELAY)
        except Exception as e:
            if url == home:
                res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        if url == home:
            final_home = r.url
            res["home_status"] = r.status_code
            res["home_final"] = r.url
            if r.status_code >= 400:
                res["error"] = f"HTTP {r.status_code}"
                break
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            continue
        docs = doc_links(r.text, r.url)
        seen_docs = {d["url"] for d in docs}
        docs += [d for d in folder_doc_links(s, r.text, r.url) if d["url"] not in seen_docs]
        strong = [d for d in docs if d["strong"]]
        if strong:
            res["pages"].append({"url": r.url, "found_via": why, "docs": docs, "n_strong": len(strong)})
        if depth >= 2:
            continue
        scored = []
        for href, text in _links(r.text, r.url):
            if href in seen or SKIP_URL.search(href) or DOC_EXT.search(href) or not _same_site(href, final_home):
                continue
            label = f"{text} {urlparse(href).path}"
            if NAV_RX.search(label):
                score = 3 if STRONG_RX.search(label) else 2 if re.search(r"(?i)by-?\s?law", label) else 1
                scored.append((score, href, text))
        for score, href, text in sorted(scored, key=lambda x: -x[0])[:12]:
            queue.append((href, depth + 1, text[:60]))
        queue.sort(key=lambda q: q[1])
    res["fetched"] = fetched
    res["pages"].sort(key=lambda p: -p["n_strong"])
    return res


def owners() -> list[tuple[str, str, str]]:
    out = [(f"muni:{m['muni_no']}", m["name_with_type"], m["website"]) for m in load_munis() if m["website"]]
    seen_pd = set()
    for p in load_pds():
        if p["website"] and p["website"] not in seen_pd:
            seen_pd.add(p["website"])
            out.append((f"pd:{p['pd_key']}", p["pd_key"], p["website"]))
    return out


def run(only: set[str] | None = None, workers: int = 10) -> dict:
    watched = read_json(WATCHED, {})
    todo = [o for o in owners() if not only or o[0] in only]
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(crawl, oid, home): (oid, name) for oid, name, home in todo}
        for i, f in enumerate(as_completed(futs), 1):
            oid, name = futs[f]
            try:
                res = f.result()
            except Exception as e:
                res = {"owner": oid, "error": repr(e), "pages": []}
            res["name"] = name
            prev = watched.get(oid, {})
            res["locked_pages"] = prev.get("locked_pages", [])   # hand-added pages survive re-crawls
            res["agent_pages"] = prev.get("agent_pages", [])     # so do pages a Claude research pass found
            res["locked_docs"] = prev.get("locked_docs", {})
            watched[oid] = res
            print(f"[{i}/{len(todo)}] {name}: {len(res['pages'])} pages"
                  + (f"  ERROR {res['error']}" if res.get("error") else ""), flush=True)
            if i % 10 == 0:
                write_json(WATCHED, watched)
    write_json(WATCHED, watched)
    return watched


if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    run(only)
