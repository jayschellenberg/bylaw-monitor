"""Write the master register workbook: Maps\\$Bylaw-Monitor\\Bylaw Register.xlsx

Sheets
  Lookup    pick a municipality from the drop-down: its by-law status rows and every
            document held (zoning, development plan, secondary plans, maps), all hyperlinked
  Register  every by-law area, filterable, colour-coded by status
  Files     every held document
  Changes   provincial by-law changes between snapshots, plus web findings
The Lookup sheet uses plain INDEX/MATCH formulas (no FILTER) so its links stay clickable
in any Excel version.
"""
from __future__ import annotations

import os
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from common import DATA, MAPS, OUTPUT, read_json

XLSX = OUTPUT / "Bylaw Register.xlsx"
STATUS_FILL = {
    "Current": "C6EFCE", "Current (misfiled)": "E2EFDA", "Current (ahead of province)": "A9D08E", "Outdated": "FFC7CE", "Missing": "F8CBAD",
    "Check": "FFEB9C", "Draft only": "FFEB9C", "Pending review": "BDD7EE", "Held only": "EDEDED", "Newer online": "FFC7CE",
}
HEAD = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="305496")
LINK = Font(color="0563C1", underline="single")
LOOKUP_STATUS_ROWS = 15
LOOKUP_FILE_ROWS = 250


def rel(path: str) -> str:
    """Link target relative to the workbook, so links survive a different Dropbox drive letter."""
    try:
        return os.path.relpath(path, OUTPUT)
    except ValueError:
        return path


def _header(ws, row: int, cols: list[tuple[str, int]]) -> None:
    for i, (name, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=name)
        c.font, c.fill = HEAD, HEAD_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
        if width:
            ws.column_dimensions[get_column_letter(i)].width = width


def _status_colors(ws, rng: str, col_letter: str, first_row: int) -> None:
    for status, color in STATUS_FILL.items():
        ws.conditional_formatting.add(rng, FormulaRule(
            formula=[f'${col_letter}{first_row}="{status}"'], fill=PatternFill("solid", fgColor=color)))


def _d(s: str | None) -> str:
    return s or ""


def build() -> str:
    reg = read_json(DATA / "register.json")
    rows = reg["rows"]
    changes = read_json(DATA / "changes_history.json", [])
    munis = sorted({r["municipality"] for r in rows})

    wb = Workbook()
    lk = wb.active
    lk.title = "Lookup"
    rg = wb.create_sheet("Register")
    fl = wb.create_sheet("Files")
    ch = wb.create_sheet("Changes")
    ls = wb.create_sheet("Lists")
    ls.sheet_state = "hidden"
    for i, m in enumerate(munis, 1):
        ls.cell(row=i, column=1, value=m)

    # ---------------- Register
    rcols = [("Municipality", 24), ("Type", 10), ("Planning district", 26), ("Document", 17), ("Area (former muni)", 24),
             ("Province by-law #", 14), ("New in province since", 13), ("Status", 15), ("Held: 3rd reading", 12),
             ("Held: consolidation", 13), ("Held: file date", 11), ("Held file", 50), ("Other numbers held", 22),
             ("Online (latest found)", 40), ("Online note", 34), ("Municipal site", 30), ("PD site", 30),
             ("Province amendments (DPA/ZBL_A) count", 12), ("Lookup key", 4)]
    _header(rg, 1, rcols)
    seen: dict[str, int] = {}
    order = {"Zoning by-law": 0, "Development plan": 1, "Secondary plan": 2}
    rows.sort(key=lambda r: (r["municipality"], order.get(r["doc_type"], 9), r["area"]))
    for i, r in enumerate(rows, 2):
        seen[r["municipality"]] = seen.get(r["municipality"], 0) + 1
        fs = r.get("od_first_seen")
        web = r.get("web") or {}
        vals = [r["municipality"], r["muni_type"], r["planning_district"], r["doc_type"], r["area"], r["od_bylaw"],
                f"{fs[:4]}-{fs[4:6]}-{fs[6:]}" if fs else "", r["status"], _d(r.get("third_reading")),
                _d(r.get("consolidation")), _d(r.get("downloaded")), None, ", ".join(r["held_other_numbers"]),
                None, "; ".join(filter(None, [r.get("held_note"), web.get("note", "")])), None, None, len(r["od_amendments"]), f"{r['municipality']}|{seen[r['municipality']]}"]
        for j, v in enumerate(vals, 1):
            rg.cell(row=i, column=j, value=v)
        if r.get("best_file"):
            c = rg.cell(row=i, column=12, value=os.path.basename(r["best_file"]))
            c.hyperlink, c.font = rel(r["best_file"]), LINK
        if web.get("url"):
            c = rg.cell(row=i, column=14, value=web.get("text") or web["url"])
            c.hyperlink, c.font = web["url"], LINK
        for col, url in ((16, r["website"]), (17, r["pd_website"])):
            if url:
                c = rg.cell(row=i, column=col, value=url)
                c.hyperlink, c.font = url, LINK
    nreg = len(rows) + 1
    rg.freeze_panes = "B2"
    rg.auto_filter.ref = f"A1:{get_column_letter(len(rcols))}{nreg}"
    _status_colors(rg, f"A2:{get_column_letter(len(rcols) - 1)}{nreg}", "H", 2)
    rg.column_dimensions["S"].hidden = True

    # ---------------- Files
    fcols = [("Municipality", 24), ("Category", 16), ("File", 60), ("Folder", 30), ("In Archives", 9), ("Pages", 7),
             ("By-law # read", 18), ("3rd reading", 12), ("Consolidation", 12), ("File date", 11), ("Text layer", 8),
             ("Lookup key", 4)]
    _header(fl, 1, fcols)
    muni_by_no = {r["muni_no"]: r["municipality"] for r in rows}
    cat_order = {"Zoning by-law": 0, "Development plan": 1, "Secondary plan": 2, "Map": 3, "Other": 4}
    lib = []
    for no, files in reg["library"].items():
        name = muni_by_no.get(no)
        if not name:
            continue
        for f in files:
            lib.append((name, f))
    lib.sort(key=lambda x: (x[0], cat_order.get(x[1]["doc_type"], 9), x[1]["archived"], x[1]["file"].lower()))
    seen = {}
    for i, (name, f) in enumerate(lib, 2):
        seen[name] = seen.get(name, 0) + 1
        cat = f["doc_type"]
        if cat in ("Zoning by-law", "Development plan", "Secondary plan") and (f["pages"] or 0) and f["pages"] <= 3:
            cat += " (map/page)"
        elif f.get("draft"):
            cat += " (DRAFT)"
        vals = [name, cat, None, f["folder"], "yes" if f["archived"] else "", f["pages"],
                ", ".join(f["numbers"]), _d(f["third_reading"]), _d(f["consolidation"]), f["modified"],
                {True: "yes", False: "NO (scan)", None: ""}[f["has_text"]], f"{name}|{seen[name]}"]
        for j, v in enumerate(vals, 1):
            fl.cell(row=i, column=j, value=v)
        c = fl.cell(row=i, column=3, value=f["file"])
        c.hyperlink, c.font = rel(f["path"]), LINK
    nfl = len(lib) + 1
    fl.freeze_panes = "B2"
    fl.auto_filter.ref = f"A1:K{nfl}"
    fl.column_dimensions["L"].hidden = True

    # ---------------- Lookup
    lk["A1"] = "Manitoba By-law Register"
    lk["A1"].font = Font(bold=True, size=16)
    lk["A2"] = "Municipality:"
    lk["A2"].font = Font(bold=True)
    lk["B2"] = munis[0]
    lk["B2"].fill = PatternFill("solid", fgColor="FFF2CC")
    lk["B2"].font = Font(bold=True, size=13)
    dv = DataValidation(type="list", formula1=f"=Lists!$A$1:$A${len(munis)}", allow_blank=False)
    lk.add_data_validation(dv)
    dv.add("B2")
    lk["D2"] = f"Generated {datetime.now():%Y-%m-%d %H:%M}  ·  province snapshot {reg['snapshot']}"
    lk["D2"].font = Font(italic=True, color="808080")
    lk["A3"] = '=IFERROR("Municipal site: "&INDEX(Register!$P:$P,MATCH($B$2,Register!$A:$A,0)),"")'
    lk["D3"] = '=IFERROR("Planning district: "&INDEX(Register!$C:$C,MATCH($B$2,Register!$A:$A,0)),"")'

    top = 5
    lk.cell(row=top - 1, column=1, value="By-law status").font = Font(bold=True, size=12)
    scols = [("Document", 18), ("Area (former muni)", 24), ("Province by-law #", 14), ("Status", 15),
             ("Held: 3rd reading", 13), ("Held: consolidation", 13), ("Held file", 48), ("Online (latest found)", 40),
             ("Online note", 34), ("", 0)]
    _header(lk, top, scols)
    src = {1: "D", 2: "E", 3: "F", 4: "H", 5: "I", 6: "J", 9: "O"}
    for k in range(1, LOOKUP_STATUS_ROWS + 1):
        r = top + k
        m = f"MATCH($B$2&\"|{k}\",Register!$S:$S,0)"
        for col, letter in src.items():
            lk.cell(row=r, column=col, value=f'=IFERROR(INDEX(Register!${letter}:${letter},{m})&"","")')
        lk.cell(row=r, column=7, value=f'=IFERROR(IF(INDEX(Register!$L:$L,{m})="","",HYPERLINK(INDEX(Register!$X:$X,{m}),INDEX(Register!$L:$L,{m}))),"")')
        lk.cell(row=r, column=8, value=f'=IFERROR(IF(INDEX(Register!$Y:$Y,{m})="","",HYPERLINK(INDEX(Register!$Y:$Y,{m}),INDEX(Register!$N:$N,{m}))),"")')
        lk.cell(row=r, column=7).font = LINK
        lk.cell(row=r, column=8).font = LINK
    _status_colors(lk, f"A{top + 1}:I{top + LOOKUP_STATUS_ROWS}", "D", top + 1)

    ftop = top + LOOKUP_STATUS_ROWS + 3
    lk.cell(row=ftop - 1, column=1, value="Documents held (zoning, development plans, secondary plans, maps, other)").font = Font(bold=True, size=12)
    _header(lk, ftop, [("Category", 0), ("File", 0), ("Folder", 0), ("In Archives", 0), ("Pages", 0),
                       ("By-law # read", 0), ("3rd reading", 0), ("Consolidation", 0), ("File date", 0)])
    fsrc = {1: "B", 3: "D", 4: "E", 5: "F", 6: "G", 7: "H", 8: "I", 9: "J"}
    for k in range(1, LOOKUP_FILE_ROWS + 1):
        r = ftop + k
        m = f"MATCH($B$2&\"|{k}\",Files!$L:$L,0)"
        for col, letter in fsrc.items():
            lk.cell(row=r, column=col, value=f'=IFERROR(INDEX(Files!${letter}:${letter},{m})&"","")')
        c = lk.cell(row=r, column=2, value=f'=IFERROR(HYPERLINK(INDEX(Files!$M:$M,{m}),INDEX(Files!$C:$C,{m})),"")')
        c.font = LINK
    for col, w in zip("ABCDEFGHI", (18, 46, 22, 15, 13, 16, 44, 40, 34)):
        lk.column_dimensions[col].width = w
    lk.freeze_panes = "A4"

    # Hidden link-target columns (HYPERLINK() needs the target as a cell value).
    for i, r in enumerate(rows, 2):
        rg.cell(row=i, column=24, value=rel(r["best_file"]) if r.get("best_file") else "")
        rg.cell(row=i, column=25, value=(r.get("web") or {}).get("url", ""))
    rg.column_dimensions["X"].hidden = rg.column_dimensions["Y"].hidden = True
    for i, (name, f) in enumerate(lib, 2):
        fl.cell(row=i, column=13, value=rel(f["path"]))
    fl.column_dimensions["M"].hidden = True

    # ---------------- Changes
    ccols = [("Detected", 12), ("Source", 12), ("Municipality", 26), ("Document", 26), ("Change", 10),
             ("By-law #", 16), ("Province now lists", 30), ("Link / note", 60)]
    _header(ch, 1, ccols)
    for i, c in enumerate(sorted(changes, key=lambda c: c["detected"], reverse=True), 2):
        vals = [c["detected"], c["source"], c["muni"], c["doc_type"], c["change"], c["bylaw"],
                ", ".join(c.get("current_set", [])), c.get("note", "")]
        for j, v in enumerate(vals, 1):
            ch.cell(row=i, column=j, value=v)
        if c.get("url"):
            ch.cell(row=i, column=8).hyperlink = c["url"]
            ch.cell(row=i, column=8).font = LINK
    ch.freeze_panes = "A2"
    ch.auto_filter.ref = f"A1:H{len(changes) + 1}"

    wb.calculation.fullCalcOnLoad = True
    OUTPUT.mkdir(parents=True, exist_ok=True)
    wb.save(XLSX)
    return str(XLSX)


if __name__ == "__main__":
    print(build())
