"""Snapshot the province's zoning and development-plan by-law attributes, and diff against the last snapshot.

The province publishes no amendment or adoption dates, so the only signal is a
by-law number appearing (or disappearing) between two snapshots. Each snapshot is
the distinct (municipality, former municipality, by-law) groups with polygon counts,
which is a few hundred rows instead of the 60 MB geometry download.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from common import DATA, DEVPLAN_URL, ZONING_URL, norm_bylaw, norm_muni, now_iso, read_json, session, today, write_json

SNAP_DIR = DATA / "opendata"


def _layer_edit_date(s, url: str) -> str | None:
    info = s.get(url, params={"f": "json"}, timeout=60).json()
    ms = (info.get("editingInfo") or {}).get("dataLastEditDate")
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def _group(s, url: str, fields: list[str]) -> list[dict]:
    """Distinct groups of `fields` with a polygon count (paged)."""
    rows, offset = [], 0
    stats = '[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"}]'
    while True:
        r = s.get(f"{url}/query", params={
            "where": "1=1", "groupByFieldsForStatistics": ",".join(fields),
            "outStatistics": stats, "orderByFields": ",".join(fields),
            "resultOffset": offset, "resultRecordCount": 2000, "f": "json",
        }, timeout=120)
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"{url}: {d['error']}")
        feats = [f["attributes"] for f in d.get("features", [])]
        rows += feats
        if not d.get("exceededTransferLimit") or not feats:
            return rows
        offset += len(feats)


def take_snapshot() -> dict:
    s = session()
    snap = {
        "retrieved_at": now_iso(),
        "zoning_last_edit": _layer_edit_date(s, ZONING_URL),
        "devplan_last_edit": _layer_edit_date(s, DEVPLAN_URL),
        # Base zoning by-law per (current muni, former muni). One by-law area = one row.
        "zoning": _group(s, ZONING_URL, ["MUNI_NO", "MUNI_NAME", "MUNI_NAME_2014", "PLANNINGDISTRICT", "ZBL"]),
        # Amending by-laws: not tracked yet (future option) but kept so history exists when it is.
        "zoning_amendments": _group(s, ZONING_URL, ["MUNI_NO", "MUNI_NAME", "MUNI_NAME_2014", "ZBL", "ZBL_A", "AMENDMENT_DESCRIPTION"]),
        "devplan": _group(s, DEVPLAN_URL, ["PLANNINGDISTRICT", "MUNI_NO", "MUNI_NAME", "DP_BYLAW"]),
        "devplan_amendments": _group(s, DEVPLAN_URL, ["PLANNINGDISTRICT", "MUNI_NO", "MUNI_NAME", "DP_BYLAW", "DPA_BYLAW"]),
    }
    return snap


def _group_local(features: list[dict], fields: list[str]) -> list[dict]:
    counts: dict[tuple, int] = {}
    for f in features:
        p = dict(f.get("properties") or {})
        if isinstance(p.get("MUNI_NO"), float):
            p["MUNI_NO"] = int(p["MUNI_NO"])
        k = tuple(p.get(x) for x in fields)
        counts[k] = counts.get(k, 0) + 1
    return [{"n": n, **dict(zip(fields, k))} for k, n in counts.items()]


def backfill(zoning_geojson: str, devplan_geojson: str, stamp: str) -> None:
    """Build a snapshot from an archived geojson pair (MAOSnapshots) so the diff history starts earlier."""
    import json
    z = json.load(open(zoning_geojson, encoding="utf-8"))["features"]
    d = json.load(open(devplan_geojson, encoding="utf-8"))["features"]
    write_json(SNAP_DIR / f"snapshot_{stamp}.json", {
        "retrieved_at": f"backfilled from {zoning_geojson} + {devplan_geojson}",
        "zoning_last_edit": None, "devplan_last_edit": None,
        "zoning": _group_local(z, ["MUNI_NO", "MUNI_NAME", "MUNI_NAME_2014", "PLANNINGDISTRICT", "ZBL"]),
        "zoning_amendments": _group_local(z, ["MUNI_NO", "MUNI_NAME", "MUNI_NAME_2014", "ZBL", "ZBL_A", "AMENDMENT_DESCRIPTION"]),
        "devplan": _group_local(d, ["PLANNINGDISTRICT", "MUNI_NO", "MUNI_NAME", "DP_BYLAW"]),
        "devplan_amendments": _group_local(d, ["PLANNINGDISTRICT", "MUNI_NO", "MUNI_NAME", "DP_BYLAW", "DPA_BYLAW"]),
    })


def latest_snapshot(before: str | None = None) -> tuple[str, dict] | tuple[None, None]:
    files = sorted(SNAP_DIR.glob("snapshot_*.json"))
    if before:
        files = [f for f in files if f.stem.split("_")[1] < before]
    if not files:
        return None, None
    return files[-1].stem.split("_")[1], read_json(files[-1])


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _numbers(rows: list[dict], num: str) -> dict[str, dict]:
    """{owner: {"name": display name, "nums": {normalized by-law: raw by-law}}}.

    Owner is MUNI_NO when present: the province spells one municipality several ways
    ("Selkirk (City)", "City of Selkirk"). Keyed on the by-law NUMBER only, not on the
    former-municipality tag, which the province re-tags freely.
    """
    out: dict[str, dict] = {}
    for r in rows:
        n, name = r.get(num), r.get("MUNI_NAME")
        if _blank(n) or (_blank(name) and not r.get("MUNI_NO")):
            continue
        owner = f"#{r['MUNI_NO']}" if r.get("MUNI_NO") else norm_muni(name)
        e = out.setdefault(owner, {"name": (name or "").strip(), "nums": {}})
        e["nums"][norm_bylaw(n)] = n.strip()
    return out


def diff(old: dict, new: dict) -> list[dict]:
    """By-law numbers that appeared in / disappeared from a municipality (or PD) between two snapshots."""
    out = []
    specs = [
        ("zoning", "ZBL", "Zoning by-law"),
        ("devplan", "DP_BYLAW", "Development plan"),
        ("devplan_amendments", "DPA_BYLAW", "Development plan amendment"),
        ("zoning_amendments", "ZBL_A", "Rezoning (not tracked)"),
    ]
    for layer, num, label in specs:
        a, b = _numbers(old.get(layer, []), num), _numbers(new.get(layer, []), num)
        for o in sorted(a.keys() | b.keys()):
            was, now = a.get(o, {"nums": {}}), b.get(o, {"nums": {}})
            name = now.get("name") or was.get("name")
            for change, src, other in (("added", now, was), ("removed", was, now)):
                for k in src["nums"].keys() - other["nums"].keys():
                    out.append({"layer": layer, "change": change, "doc_type": label, "muni_no": o, "muni": name,
                                "bylaw": src["nums"][k], "current_set": sorted(now["nums"].values())})
    return out


def run() -> dict:
    stamp = today()
    snap = take_snapshot()
    prev_stamp, prev = latest_snapshot(before=stamp)
    write_json(SNAP_DIR / f"snapshot_{stamp}.json", snap)
    changes = diff(prev, snap) if prev else []
    result = {
        "snapshot": stamp, "previous": prev_stamp,
        "zoning_last_edit": snap["zoning_last_edit"], "devplan_last_edit": snap["devplan_last_edit"],
        "changes": changes,
    }
    write_json(SNAP_DIR / f"changes_{stamp}.json", result)
    return result


if __name__ == "__main__":
    res = run()
    tracked = [c for c in res["changes"] if c["layer"] != "zoning_amendments"]
    print(f"snapshot {res['snapshot']} (previous {res['previous']}); zoning edited {res['zoning_last_edit']}, "
          f"dev plan edited {res['devplan_last_edit']}; {len(tracked)} tracked changes, "
          f"{len(res['changes']) - len(tracked)} rezonings")
    for c in tracked:
        print("  ", c)
    sys.exit(0)
