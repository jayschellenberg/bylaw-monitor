"""Mark first-reading-only copies as drafts: prefix the file name with "DRAFT - ".

Jason's rule (2026-10-30): a by-law or plan whose readings block shows only first reading is
still useful but is not the final version, so its file name starts with "DRAFT - " and the
register notes it. inventory.py detects it (reading_stage == "first reading only").

Skipped: names already containing "draft", office consolidations (they keep the original
unsigned readings block), and anything listed in data/draft_exceptions.json ({path: reason}).
Every rename is appended to data/renames.json so it can be undone.

    python drafts.py          # list what would be renamed
    python drafts.py --yes    # rename
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from common import DATA, MAPS, now_iso, read_json, write_json

PREFIX = "DRAFT - "
RENAMES = DATA / "renames.json"
EXCEPTIONS = DATA / "draft_exceptions.json"


def is_draft_name(name: str) -> bool:
    return bool(re.search(r"(?i)draft", name))


def candidates(inv: dict | None = None) -> list[Path]:
    inv = inv or read_json(DATA / "inventory.json")
    skip = read_json(EXCEPTIONS, {})
    out = []
    for o in inv["owners"]:
        base = MAPS / "$Planning Districts" / o["folder"] if o["kind"] == "pd" else MAPS / o["folder"]
        for d in o["docs"]:
            if d.get("reading_stage") != "first reading only":
                continue
            p = base / d["file"]
            if is_draft_name(p.name) or re.search(r"(?i)consolidat", p.name) or str(p) in skip or not p.exists():
                continue
            out.append(p)
    return out


def rename(paths: list[Path]) -> list[dict]:
    log = read_json(RENAMES, [])
    done = []
    for p in paths:
        new = p.with_name(PREFIX + p.name)
        if new.exists():
            continue
        p.rename(new)
        rec = {"at": now_iso(), "from": str(p), "to": str(new), "why": "first reading only (2nd/3rd readings blank)"}
        log.append(rec)
        done.append(rec)
    write_json(RENAMES, log)
    return done


if __name__ == "__main__":
    c = candidates()
    if "--yes" not in sys.argv:
        print("\n".join(str(p) for p in c) or "nothing to rename")
        print(f"{len(c)} file(s); add --yes to rename")
    else:
        for r in rename(c):
            print(f"{r['from']}\n  -> {Path(r['to']).name}")
