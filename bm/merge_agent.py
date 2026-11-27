"""Merge a Claude research pass (data/agent_findings/*.json) into the watched pages.

Each finding lists the pages where a municipality / planning district posts its zoning by-law,
development plan or secondary plans, found by reading the site the way a person would. This:
  - re-fetches every page and checks it loads (status < 400, not bounced to the home page),
  - records whether the weekly script can see its document links itself ("script_readable");
    pages that only build their links with JavaScript keep the links the agent saw,
  - stores them as watched[owner]["agent_pages"], which re-crawls keep,
  - seeds data/watch_state.json so next week's check reports only genuinely new links.

    python merge_agent.py            # dry run: print what would be stored
    python merge_agent.py --yes
"""
from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from common import DATA, now_iso, read_json, session, write_json
from discover import STRONG_RX, WATCHED, doc_links, folder_doc_links
from watch import STATE


def check_page(s, url: str) -> dict:
    try:
        r = s.get(url, timeout=25, allow_redirects=True)
        time.sleep(1)
    except Exception as e:
        return {"ok": False, "why": type(e).__name__}
    if r.status_code >= 400:
        return {"ok": False, "why": f"HTTP {r.status_code}"}
    if urlparse(r.url).path in ("", "/") and urlparse(url).path not in ("", "/"):
        return {"ok": False, "why": f"redirects to home {r.url}"}
    links = doc_links(r.text, r.url) if "html" in r.headers.get("content-type", "") else []
    if links is not None and "html" in r.headers.get("content-type", ""):
        links += [d for d in folder_doc_links(s, r.text, r.url) if d["url"] not in {x["url"] for x in links}]
    return {"ok": True, "final": r.url, "links": links}


def merge(yes: bool) -> None:
    watched = read_json(WATCHED, {})
    state = read_json(STATE, {})
    s = session()
    summary = {"pages_ok": 0, "pages_bad": 0, "owners": 0, "js_only": 0}
    for f in sorted((DATA / "agent_findings").glob("*.json")):
        for it in read_json(f, []):
            owner = it.get("owner")
            if owner not in watched:
                print("  unknown owner", owner)
                continue
            pages = list(it.get("pages") or [])
            if it.get("delegates_to") and not any(p.get("url") == it["delegates_to"] for p in pages):
                pages.append({"url": it["delegates_to"], "what": "planning district page", "docs": []})
            kept = []
            for p in pages:
                url = (p.get("url") or "").strip()
                if not url.startswith("http"):
                    continue
                c = check_page(s, url)
                blocked = not c["ok"] and c["why"] in ("HTTP 403", "HTTP 511") and (p.get("docs") or [])
                if blocked:     # the site refuses scripts (bot check) but the agent read it: keep what it saw
                    c = {"ok": True, "final": url, "links": []}
                if not c["ok"]:
                    summary["pages_bad"] += 1
                    print(f"  SKIP {it.get('name')}: {url} ({c['why']})")
                    continue
                summary["pages_ok"] += 1
                agent_docs = [{"url": d["url"], "text": d.get("text", ""), "bylaw": d.get("bylaw", ""),
                               "strong": bool(STRONG_RX.search(f"{d.get('text', '')} {d['url']}"))}
                              for d in p.get("docs") or [] if str(d.get("url", "")).startswith("http")]
                seen = {d["url"] for d in c["links"]}
                docs = c["links"] + [d for d in agent_docs if d["url"] not in seen]
                readable = bool(c["links"])
                summary["js_only"] += 0 if readable or not agent_docs else 1
                kept.append({"url": c["final"], "what": p.get("what", ""), "found_via": "agent", "docs": docs,
                             "script_readable": readable, "script_blocked": bool(blocked),
                             "found_at": date.today().isoformat()})
                if readable:
                    state.setdefault(owner, {})[c["final"]] = {"links": sorted(seen), "checked": now_iso()}
            if kept:
                summary["owners"] += 1
            print(f"{it.get('name')}: {it.get('status')} - {len(kept)} page(s); {it.get('notes', '')[:110]}")
            if yes:
                watched[owner]["agent_pages"] = kept
                watched[owner]["agent_status"] = it.get("status")
                watched[owner]["agent_notes"] = it.get("notes", "")
                if it.get("home_final") and it.get("home_final") != watched[owner].get("home"):
                    watched[owner]["home_final_agent"] = it["home_final"]
    print(summary)
    if yes:
        write_json(WATCHED, watched)
        write_json(STATE, state)
        print("saved")


if __name__ == "__main__":
    merge("--yes" in sys.argv)
