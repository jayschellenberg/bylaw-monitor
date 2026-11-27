"""Weekly check of watched pages: new document links + page health.

For each owner (municipality / planning district) in watched_pages.json:
  - re-fetch each watched page, list its zoning / plan / by-law document links
  - NEW links (not seen last week) become candidates for download + review
  - health check: a page is BROKEN if it errors, 404s, redirects off-site or to the home page,
    or lost more than half of its document links (or all of them). Broken owners get a
    targeted re-discovery crawl straight away.
  - rolling re-crawl: 1/13 of owners are fully re-crawled every week (each one quarterly),
    which also picks up newly added pages.
State: data/watch_state.json  {owner: {page_url: {"links": [...], "checked": ...}}}
"""
from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlparse

from common import DATA, now_iso, read_json, session, write_json
from discover import WATCHED, crawl, doc_links, folder_doc_links

STATE = DATA / "watch_state.json"
ROTATION_WEEKS = 13


def _rotation_due(owner: str, week: int) -> bool:
    return int(hashlib.md5(owner.encode()).hexdigest(), 16) % ROTATION_WEEKS == week % ROTATION_WEEKS


def check_owner(owner: str, entry: dict, state: dict) -> dict:
    s = session()
    pages = ([p["url"] for p in entry.get("pages", [])[:4]] + entry.get("locked_pages", [])
             + [p["url"] for p in entry.get("agent_pages", []) if not p.get("script_blocked")])
    # script_blocked pages (bot checks) are left to the quarterly Claude re-check, not flagged as broken weekly
    out = {"owner": owner, "name": entry.get("name"), "new_links": [], "broken": [], "pages": {}}
    for url in dict.fromkeys(pages):
        prev = state.get(url, {})
        try:
            r = s.get(url, timeout=25, allow_redirects=True)
            time.sleep(1)
        except Exception as e:
            out["broken"].append({"url": url, "why": f"{type(e).__name__}"})
            continue
        final = urlparse(r.url)
        if r.status_code >= 400:
            out["broken"].append({"url": url, "why": f"HTTP {r.status_code}"})
            continue
        if final.netloc.removeprefix("www.") != urlparse(url).netloc.removeprefix("www.") or final.path in ("", "/") and urlparse(url).path not in ("", "/"):
            out["broken"].append({"url": url, "why": f"redirects to {r.url}"})
            continue
        links = doc_links(r.text, r.url)
        links += [d for d in folder_doc_links(s, r.text, r.url) if d["url"] not in {x["url"] for x in links}]
        urls = {d["url"] for d in links}
        before = set(prev.get("links", []))
        if before and (not urls or len(urls) < len(before) / 2):
            out["broken"].append({"url": url, "why": f"document links dropped {len(before)} -> {len(urls)}"})
        if before:
            out["new_links"] += [d | {"page": url} for d in links if d["url"] not in before]
        out["pages"][url] = {"links": sorted(urls), "checked": now_iso()}
        out.setdefault("docs", {})[url] = links
    return out


def refresh_locked_docs(watched: dict, results: list[dict]) -> None:
    """Keep the document list of hand-added (locked) pages, so the register can match against them."""
    for r in results:
        e = watched.get(r["owner"])
        if not e:
            continue
        for url in e.get("locked_pages", []):
            if url in r.get("docs", {}):
                e.setdefault("locked_docs", {})[url] = r["docs"][url]


def run(workers: int = 10) -> dict:
    watched = read_json(WATCHED, {})
    state = read_json(STATE, {})
    week = date.today().isocalendar().week
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda kv: check_owner(kv[0], kv[1], state.get(kv[0], {})), watched.items()))
    recrawl = {r["owner"] for r in results if r["broken"]} | {o for o in watched if _rotation_due(o, week)}
    recrawled = {}
    for o in recrawl:
        e = watched[o]
        res = crawl(o, e["home"])
        res["name"], res["locked_pages"] = e.get("name"), e.get("locked_pages", [])
        res["agent_pages"] = e.get("agent_pages", [])
        res["locked_docs"] = e.get("locked_docs", {})
        old_pages = {p["url"] for p in e.get("pages", [])}
        res["recrawl_reason"] = "broken page" if any(r["owner"] == o and r["broken"] for r in results) else "quarterly rotation"
        res["pages_changed"] = sorted({p["url"] for p in res["pages"]} ^ old_pages)
        watched[o] = res
        recrawled[o] = {"reason": res["recrawl_reason"], "pages_changed": res["pages_changed"], "error": res.get("error")}
    refresh_locked_docs(watched, results)
    for r in results:
        state.setdefault(r["owner"], {}).update(r["pages"])
    write_json(STATE, state)
    write_json(WATCHED, watched)
    summary = {"checked": now_iso(), "owners": len(results),
               "new_links": [l | {"owner": r["owner"], "name": r["name"]} for r in results for l in r["new_links"]],
               "broken": [{"owner": r["owner"], "name": r["name"], **b} for r in results for b in r["broken"]],
               "recrawled": recrawled}
    write_json(DATA / f"watch_{date.today():%Y%m%d}.json", summary)
    return summary


if __name__ == "__main__":
    s = run()
    print(f"{s['owners']} owners checked; {len(s['new_links'])} new links; {len(s['broken'])} broken pages; "
          f"{len(s['recrawled'])} re-crawled")
