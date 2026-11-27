"""Change history: provincial by-law changes between consecutive snapshots, plus web findings.

Written to data/changes_history.json; the Excel "Changes" sheet and the weekly digest read it.
"""
from __future__ import annotations

from common import DATA, read_json, write_json
from opendata import SNAP_DIR, diff

HISTORY = DATA / "changes_history.json"
TRACKED = {"zoning", "devplan", "devplan_amendments"}


def _fmt(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"


def rebuild_opendata() -> list[dict]:
    snaps = sorted(SNAP_DIR.glob("snapshot_2*.json"))
    out = []
    for a, b in zip(snaps, snaps[1:]):
        sa, sb = a.stem.split("_")[1], b.stem.split("_")[1]
        for c in diff(read_json(a), read_json(b)):
            if c["layer"] not in TRACKED:
                continue
            out.append({"detected": _fmt(sb), "source": "Province", "muni": c["muni"], "doc_type": c["doc_type"],
                        "change": c["change"], "bylaw": c["bylaw"], "current_set": c["current_set"],
                        "note": f"between snapshots {_fmt(sa)} and {_fmt(sb)}"})
    return out


def save(web: list[dict] | None = None) -> list[dict]:
    hist = [c for c in read_json(HISTORY, []) if c["source"] != "Province"]
    known = {(c["muni"], c["bylaw"], c.get("url")) for c in hist}
    hist += [w for w in (web or []) if (w["muni"], w["bylaw"], w.get("url")) not in known]
    hist = rebuild_opendata() + hist
    write_json(HISTORY, hist)
    return hist


if __name__ == "__main__":
    h = save()
    print(len(h), "change records")
