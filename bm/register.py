"""Build the by-law register: what the province says is current vs what is held locally.

One row per by-law area:
  - Zoning: one row per distinct base zoning by-law (ZBL) in a municipality. Amalgamated
    municipalities can have several (one per former municipality, MUNI_NAME_2014).
  - Development plan: one row per distinct DP_BYLAW covering the municipality.
  - Secondary plans: one row per secondary-plan document held (the province has no layer).

Status values:
  Current           a held, non-archived file is the by-law the province lists
  Current (misfiled) the matching file is only in an Archives folder
  Outdated          you hold this type of document, but for a different by-law number
  Missing           nothing of this type is held
  Check             files are held but none has a readable by-law number (scans etc.)
  Pending review    a newer version was found and staged, awaiting approval
  Held only         held locally but not in the provincial layer (e.g. secondary plans)
"""
from __future__ import annotations

import re
from collections import defaultdict

from common import DATA, MAPS, bylaw_variants, norm_bylaw, now_iso, read_json, write_json
from munis import load_munis, load_pds, norm_pd

REGISTER = DATA / "register.json"
PD_ROOT = "$Planning Districts"

FNAME_NUM = [
    re.compile(r"(?<![\d])(\d{1,5})\s?[-/]\s?(\d{2,4})(?![\d])"),
    re.compile(r"(?i)(?:no\.?|by-?law|zbl|bl)\s*_?\s*(\d{3,5})(?![\d])"),
]


def filename_numbers(name: str) -> set[str]:
    stem = re.sub(r"(?i)\.(pdf|docx?|jpe?g|png|gif|tiff?)$", "", name)
    stem = re.sub(r"_[A-Za-z0-9]{6}$", "", stem)            # CMS upload suffixes like _9aXkPA
    out = set()
    for rx in FNAME_NUM:
        for m in rx.finditer(stem):
            out.add(norm_bylaw("-".join(g for g in m.groups() if g)))
    return {n for n in out if n and not re.fullmatch(r"(19|20)\d\d-\d{1,2}", n) or re.fullmatch(r"\d{4}-\d{2}", n)}


def doc_numbers(d: dict) -> set[str]:
    """By-law numbers a document IS (not merely mentions): filename numbers + the top number in its text."""
    nums = filename_numbers(d["file"].split("\\")[-1])
    if d.get("bylaw_numbers"):
        nums.add(norm_bylaw(d["bylaw_numbers"][0]))
    return nums


def _matches(d: dict, bylaw: str) -> bool:
    return bool(bylaw_variants(bylaw) & {v for n in doc_numbers(d) for v in bylaw_variants(n)})


def _is_main(d: dict) -> bool:
    return (d.get("pages") or 0) >= 8 or not re.search(r"(?i)map|schedule|sch-?[a-z]\b", d["file"])


def _file_entry(folder: str, d: dict) -> dict:
    return {"path": str(MAPS / folder / d["file"]), "file": d["file"], "folder": folder,
            "archived": d["archived"], "pages": d.get("pages"), "has_text": d.get("has_text"),
            "third_reading": d.get("third_reading"), "consolidation": d.get("consolidation"),
            "numbers": sorted(doc_numbers(d)), "modified": d["modified"], "doc_type": d["doc_type"],
            "draft": d.get("reading_stage") == "first reading only" or "draft" in d["file"].lower()}


def _held_summary(files: list[dict]) -> dict:
    """Pick the best main document among matching files and summarize its dates."""
    if not files:
        return {}
    live = [f for f in files if not f["archived"]] or files
    live = [f for f in live if not f.get("draft")] or live      # a final copy beats a draft
    mains = [f for f in live if (f["pages"] or 0) >= 8] or live
    best = max(mains, key=lambda f: ((f["consolidation"] or ""), (f["third_reading"] or ""), f["pages"] or 0, f["modified"]))
    return {"best_file": best["path"], "third_reading": best["third_reading"],
            "held_note": "DRAFT (first reading only)" if best.get("draft") else "",
            "consolidation": max((f["consolidation"] for f in live if f["consolidation"]), default=None),
            "downloaded": best["modified"]}


def build() -> dict:
    inv = read_json(DATA / "inventory.json")
    snaps = sorted((DATA / "opendata").glob("snapshot_2*.json"))
    snap = read_json(snaps[-1])
    hist = {s.stem.split("_")[1]: read_json(s) for s in snaps[:-1]}
    docs_by_folder = {(o["kind"], o["folder"]): o["docs"] for o in inv["owners"]}
    pds = {p["pd_key"]: p for p in load_pds()}

    def docs_for(m: dict, include_pd: bool) -> list[tuple[str, dict]]:
        out = []
        for f in filter(None, m["folders"].split(";")):
            out += [(f, d) for d in docs_by_folder.get(("muni", f), [])]
        pd = pds.get(m["pd_key"])
        if include_pd and pd and pd["folder"]:
            for pf in pd["folder"].split(";"):     # "new;old": every listed PD folder counts as held
                out += [(f"{PD_ROOT}\\{pf}", d) for d in docs_by_folder.get(("pd", pf), [])]
        return out

    def first_seen(layer: str, field: str, muni_no: str, bylaw: str) -> str | None:
        """Earliest snapshot listing this by-law for the muni (None if it predates our history)."""
        seen = None
        for stamp in sorted(hist):
            rows = hist[stamp].get(layer, [])
            if any(str(r.get("MUNI_NO")) == muni_no and norm_bylaw(r.get(field)) == norm_bylaw(bylaw) for r in rows):
                return seen if seen else None   # present in the oldest snapshot we have -> predates history
            seen = stamp
        return seen or "20261030"

    rows = []
    for m in load_munis():
        no = str(m["muni_no"])
        base = {"muni_no": no, "municipality": m["name"], "muni_type": m["type"], "name_with_type": m["name_with_type"],
                "planning_district": m["planning_district"], "website": m["website"],
                "pd_website": pds.get(m["pd_key"], {}).get("website", ""), "folders": m["folders"]}

        specs = [
            ("Zoning by-law", "zoning", "ZBL", "zoning_amendments", "ZBL_A", False),
            ("Development plan", "devplan", "DP_BYLAW", "devplan_amendments", "DPA_BYLAW", True),
        ]
        for doc_type, layer, field, alayer, afield, use_pd in specs:
            areas: dict[str, dict] = {}
            for r in snap[layer]:
                if str(r.get("MUNI_NO")) != no or not norm_bylaw(r.get(field)):
                    continue
                k = norm_bylaw(r[field])
                a = areas.setdefault(k, {"bylaw": r[field].strip(), "former": set(), "n": 0})
                a["n"] += r.get("n", 1)
                if (r.get("MUNI_NAME_2014") or "").strip():
                    a["former"].add(r["MUNI_NAME_2014"].strip())
            amends = defaultdict(set)
            for r in snap[alayer]:
                if str(r.get("MUNI_NO")) == no and (r.get(afield) or "").strip() and norm_bylaw(r.get(afield)) != norm_bylaw(r.get(field)):
                    amends[norm_bylaw(r.get(field))].add(r[afield].strip())

            held = [(f, d) for f, d in docs_for(m, use_pd) if d["doc_type"] == doc_type]
            # District-wide zoning by-laws are filed in the PD folder: they count as held when the
            # number matches, but a PD folder's other members' by-laws must not make this one "Outdated".
            pd_held = [] if use_pd else [(f, d) for f, d in docs_for(m, True) if d["doc_type"] == doc_type
                                         and f.startswith(PD_ROOT)]
            if not areas:
                areas = {"": {"bylaw": "", "former": set(), "n": 0}}
            for k, a in sorted(areas.items(), key=lambda kv: -kv[1]["n"]):
                files = [_file_entry(f, d) for f, d in held + pd_held if a["bylaw"] and _matches(d, a["bylaw"])]
                other = [_file_entry(f, d) for f, d in held if not (a["bylaw"] and _matches(d, a["bylaw"]))]
                other_nums = sorted({n for f in other for n in f["numbers"] if (f["pages"] or 0) >= 8})
                if files:
                    finals = [f for f in files if not f.get("draft")]
                    if not finals:
                        status = "Draft only"           # only first-reading copies of this by-law are held
                    else:
                        status = "Current" if any(not f["archived"] for f in finals) else "Current (misfiled)"
                elif not held:
                    status = "Missing"
                elif other_nums:
                    status = "Outdated"
                else:
                    status = "Check"
                if not a["bylaw"] and held:
                    status = "Held only"
                rows.append({**base, "doc_type": doc_type, "area": ", ".join(sorted(a["former"])),
                             "od_bylaw": a["bylaw"], "od_first_seen": first_seen(layer, field, no, a["bylaw"]) if a["bylaw"] else None,
                             "od_amendments": sorted(amends.get(k, set())),
                             "status": status, **_held_summary(files),
                             "held_other_numbers": other_nums if status in ("Outdated", "Check") else [],
                             "files": files, "other_files": other})

        # Secondary plans: held only (no provincial layer).
        for f, d in docs_for(m, True):
            if d["doc_type"] == "Secondary plan" and _is_main(d):
                fe = _file_entry(f, d)
                rows.append({**base, "doc_type": "Secondary plan", "area": d["file"].split("\\")[-1][:-4],
                             "od_bylaw": "", "od_first_seen": None, "od_amendments": [],
                             "status": "Held only", **_held_summary([fe]), "held_other_numbers": [],
                             "files": [fe], "other_files": []})

    # Maps and other documents per municipality, for the lookup sheet.
    library = {}
    for m in load_munis():
        library[str(m["muni_no"])] = [_file_entry(f, d) for f, d in docs_for(m, True)]
    reg = {"generated": now_iso(), "snapshot": snaps[-1].stem.split("_")[1], "rows": rows, "library": library}
    write_json(REGISTER, reg)
    return reg


if __name__ == "__main__":
    from collections import Counter
    reg = build()
    rows = reg["rows"]
    print(len(rows), "rows")
    for t in ("Zoning by-law", "Development plan", "Secondary plan"):
        print(t, Counter(r["status"] for r in rows if r["doc_type"] == t))
