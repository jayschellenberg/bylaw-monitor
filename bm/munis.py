"""Master list of municipalities and planning districts: number, name, folder(s), websites.

Writes data/munis.csv and data/pds.csv. Both are auto-generated but meant to be hand-corrected:
a row with `locked=1` is never overwritten on re-generation.

Sources:
- mb-municipalities.geojson (MBOpenData) -> MUNI_NO + "NAME (TYPE)"
- MUNI_WEBSITES / PD_WEBSITES in mb-parcelsearch main.js -> home pages
- latest open-data snapshot -> each municipality's planning district
- Maps folder names -> local folder(s), including folders of pre-2015 municipalities
"""
from __future__ import annotations

import csv
import difflib
import json
import re
from collections import Counter
from pathlib import Path

from common import DATA, MAIN_JS, MAPS, MUNI_GEOJSON, NON_MUNI_FOLDERS, norm_muni, read_json

MUNIS_CSV = DATA / "munis.csv"
PDS_CSV = DATA / "pds.csv"
PD_ROOT = MAPS / "$Planning Districts"

IN_SCOPE_TYPES = {"CITY", "TOWN", "RM", "RURAL MUNICIPALITY", "MUNICIPALITY", "VILLAGE", "LGD",
                  "LOCAL GOVERNMENT DISTRICT"}
EXCLUDE = {"WINNIPEG"}

# Folders named for a pre-2015 municipality -> the current municipality that absorbed it.
FORMER = {
    "morton": "boissevainmorton", "langford": "northcypresslangford", "lawrence": "lakeshore",
    "southcypress": "glenborosouthcypress", "glenboro": "glenborosouthcypress", "somerset": "lorne",
    "treherne": "norfolktreherne", "southnorfolk": "norfolktreherne", "wawanesa": "oaklandwawanesa",
    "oakland": "oaklandwawanesa", "swanriver": "swanvalleywest", "manitou": "pembina",
    "souris": "sourisglenwood", "riverside": "prairielakes", "riverton": "bifrostriverton",
    "franklin": "emersonfranklin", "siglunes": "westinterlake", "macgregor": "northnorfolk",
    "stpierrejolys": "stpierrejolys", "stclaude": "grey", "plumcoulee": "rhineland", "gretna": "rhineland",
    "crystalcity": "louise", "pilotmound": "louise", "twoborders": "twoborders",
    "clanwilliamerickson": "clanwilliamerickson", "glenellalansdown": "glenellalansdowne",
    "lacdobonnet": "lacdubonnet", "springfiels": "springfield",
}
# Exact folder name -> municipality key, for folders the rules above get wrong.
FOLDER_OVERRIDES = {
    "Swan River RM": "swanvalleywest", "Swan River Town": "swanriver", "Birtle, RM": "prairieview",
    "Miniota, RM": "prairieview", "Cameron, RM": "grassland", "Lakeview, RM": "westlakegladstone",
    "Killarney-Turtle Mtn": "killarneyturtlemountain", "Turtle Mountain RM": "killarneyturtlemountain",
    "Notre Dame de Lourdes, Village": "lorne",
}
PD_ALIASES = {"PORTAGE LA PAIRIE": "PORTAGE LA PRAIRIE", "WEST INTERLAKE": "WESTERN INTERLAKE",
              "TRANS-CANADA WEST": "TRANS CANADA WEST", "BRANDON AND AREA": "BRANDON & AREA",
              "RHINELAND, PLUM COULEE, GRETNA, ALTONA": "RPGA"}
# Planning district -> folder under $Planning Districts. Explicit only: fuzzy matching linked
# e.g. TRANS CANADA WEST to the wrong folder. Add a row when a new PD folder is created.
PD_FOLDERS = {
    "BRANDON & AREA": "Brandon BAPD", "BROKENHEAD RIVER": "Brokenhead River",
    # "new;old": new filings go to the first folder, documents in all of them count as held.
    "CARMAN-DUFFERIN": "Carman-Dufferin-Grey;Carman-Dufferin",
    "GREY-ST. CLAUDE": "Carman-Dufferin-Grey;Grey-St Claude Planning District",
    "WHITE HORSE PLAINS": "Whitehorse Plains Planning District WHPPD", "MID-WEST": "Ellice-Archie", "INLAND PORT SPECIAL PLANNING AREA": "CentrePort",
    "CYPRESS": "Cypress Planning District", "EASTERN INTERLAKE": "East Interlake - Gimli Bifrost Arborg Wpg Beach",
    "MACDONALD-RITCHOT": "MacDonald-Ritchot", "MSTW": "MSTW - Morden Stanley Winkler",
    "PORTAGE LA PRAIRIE": "Portage la Prairie", "RPGA": "RPGA - Rhineland Plum Coulee Gretna Altona",
    "RED RIVER": "RRPD - Red River Planning - Selkirk St Clements St Andrews East West St Paul Dunnotar",
    "SOUTH CENTRAL": "South Central", "SOUTH INTERLAKE": "South Interlake-SIPD", "WESTERN INTERLAKE": "West Interlake",
    "WHITEMOUTH REYNOLDS": "Whitemouth Reynolds", "WINNIPEG RIVER": "Winnipeg River",
}
TYPE_HINTS = [(r"\bcity\b", "CITY"), (r"\btown\b", "TOWN"), (r"\bvillage\b", "VILLAGE"),
              (r"\brm\b|rural", "RM"), (r"\bmun\b|municipality", "MUNICIPALITY"), (r"\blgd\b", "LGD")]


def parse_js_dict(name: str) -> dict[str, str]:
    js = MAIN_JS.read_text(encoding="utf-8")
    body = js[js.index(f"const {name} = {{"):]
    body = body[:body.index("\n};")]
    pairs = re.findall(r"""^\s*(['"])(.+?)\1\s*:\s*['"](https?://[^'"]+)['"]""", body, re.M)
    return {k: url for _, k, url in pairs}


def norm_pd(name: str | None) -> str:
    """Same normalization as normalizePdKey() in main.js."""
    if not name:
        return ""
    s = re.sub(r"\s+", " ", str(name).upper()).strip()
    if s in ("<NULL>", "NULL", "N/A"):
        return ""
    s = re.sub(r"\s*\([^()]*\)$", "", s)
    s = re.sub(r"\s+(?:PLANNING\s+DISTRICT|P\.?\s*D\.?)$", "", s).strip()
    return PD_ALIASES.get(s, s)


def _type_of(t: str) -> str:
    t = (t or "").upper()
    return {"RURAL MUNICIPALITY": "RM", "LOCAL GOVERNMENT DISTRICT": "LGD"}.get(t, t)


def _read_locked(path: Path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r[key]: r for r in csv.DictReader(f) if r.get("locked") == "1"}


def build() -> tuple[list[dict], list[dict]]:
    gj = json.load(open(MUNI_GEOJSON, encoding="utf-8"))["features"]
    muni_sites = {k.upper().replace(".", ""): v for k, v in parse_js_dict("MUNI_WEBSITES").items()}
    pd_sites = parse_js_dict("PD_WEBSITES")

    snaps = sorted((DATA / "opendata").glob("snapshot_2*.json"))
    snap = read_json(snaps[-1]) if snaps else {}
    pd_votes: dict[int, Counter] = {}
    for r in snap.get("devplan", []) + snap.get("zoning", []):
        if r.get("MUNI_NO") and r.get("PLANNINGDISTRICT") and r["PLANNINGDISTRICT"].strip():
            pd_votes.setdefault(r["MUNI_NO"], Counter())[r["PLANNINGDISTRICT"].strip()] += r.get("n", 1)

    munis = []
    for f in gj:
        p = f["properties"]
        typ = _type_of(p.get("MUNI_TYPE"))
        if typ not in {_type_of(t) for t in IN_SCOPE_TYPES} or p["MUNI_LIST_NAME"].upper() in EXCLUDE:
            continue
        nwt = p["MUNI_LIST_NAME_WITH_TYPE"]
        pd = pd_votes.get(p["MUNI_NO"], Counter()).most_common(1)
        pd = pd[0][0] if pd else ""
        munis.append({
            "muni_no": p["MUNI_NO"], "name": p["MUNI_LIST_NAME"].strip().title(), "type": typ, "name_with_type": re.sub(r"\s+", " ", nwt),
            "key": norm_muni(p["MUNI_LIST_NAME"]), "planning_district": pd,
            "pd_key": norm_pd(pd), "website": muni_sites.get(re.sub(r"\s+", " ", nwt).upper().replace(".", ""), ""),
            "folders": "", "locked": "",
        })
    munis.sort(key=lambda m: m["name"])

    # ---- folders -> municipalities
    by_key: dict[str, list[dict]] = {}
    for m in munis:
        by_key.setdefault(m["key"], []).append(m)
    unmatched = []
    for d in sorted(MAPS.iterdir()):
        if not d.is_dir() or d.name.startswith("$") or d.name in NON_MUNI_FOLDERS:
            continue
        key = norm_muni(re.sub(r"(?i)\s*-\s*(now|see)\b.*", "", d.name))
        if d.name in FOLDER_OVERRIDES:
            key = FOLDER_OVERRIDES[d.name]
        elif key not in by_key:
            key = FORMER.get(key, key)
        cands = by_key.get(key) or by_key.get(FORMER.get(key, ""), [])
        if not cands:
            close = difflib.get_close_matches(key, by_key.keys(), n=1, cutoff=0.85)
            cands = by_key.get(close[0], []) if close else []
        if len(cands) > 1:
            hint = next((t for rx, t in TYPE_HINTS if re.search(rx, d.name, re.I)), None)
            typed = [c for c in cands if c["type"] == hint]
            # No type in the folder name: the urban municipality (town/city) is the usual subject.
            cands = typed or [c for c in cands if c["type"] in ("CITY", "TOWN")] or cands[:1]
        if not cands:
            unmatched.append(d.name)
            continue
        m = cands[0]
        m["folders"] = ";".join(filter(None, [m["folders"], d.name]))

    # ---- planning districts
        pds: dict[str, dict] = {}
    for m in munis:
        if not m["pd_key"]:
            continue
        e = pds.setdefault(m["pd_key"], {"pd_key": m["pd_key"], "name": m["planning_district"],
                                         "website": pd_sites.get(m["pd_key"], ""), "members": "", "folder": "",
                                         "locked": ""})
        e["members"] = ";".join(filter(None, [e["members"], m["name"]]))
    for e in pds.values():
        e["folder"] = PD_FOLDERS.get(e["pd_key"], "")
        e["website"] = e["website"] or pd_sites.get(next((k for k, v in PD_ALIASES.items() if v == e["pd_key"]), ""), "")

    locked_m, locked_p = _read_locked(MUNIS_CSV, "muni_no"), _read_locked(PDS_CSV, "pd_key")
    munis = [locked_m.get(str(m["muni_no"]), m) for m in munis]
    pd_rows = [locked_p.get(k, v) for k, v in sorted(pds.items())]
    for path, rows in ((MUNIS_CSV, munis), (PDS_CSV, pd_rows)):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return munis, pd_rows, unmatched


def load_munis() -> list[dict]:
    with open(MUNIS_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_pds() -> list[dict]:
    with open(PDS_CSV, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


if __name__ == "__main__":
    munis, pds, unmatched = build()
    print(f"{len(munis)} municipalities, {sum(1 for m in munis if m['folders'])} with a folder, "
          f"{sum(1 for m in munis if m['website'])} with a website; {len(pds)} planning districts "
          f"({sum(1 for p in pds if p['folder'])} with a folder, {sum(1 for p in pds if p['website'])} with a website)")
    print("Folders not matched:", unmatched)
    print("Munis without folder:", [m["name_with_type"] for m in munis if not m["folders"]])
    print("PDs without folder:", [p["pd_key"] for p in pds if not p["folder"]])
    print("PD folders unused:", sorted({d.name for d in PD_ROOT.iterdir() if d.is_dir()} - {f for p in pds for f in p["folder"].split(";")}))
