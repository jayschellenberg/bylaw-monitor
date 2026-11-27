"""Rebuild the register and workbook from current data (optionally fetching a new province snapshot).

    python baseline.py            # munis -> inventory -> register -> web -> changes -> excel
    python baseline.py --fetch    # take a new province snapshot first
"""
from __future__ import annotations

import sys
from collections import Counter

import changes
import excel
import inventory
import munis
import opendata
import register
import web

if __name__ == "__main__":
    if "--fetch" in sys.argv:
        print("province snapshot:", opendata.run()["snapshot"])
    munis.build()
    inventory.run()
    reg = register.build()
    findings = web.run()
    changes.save(findings)
    path = excel.build()
    reg = register.read_json(register.REGISTER)
    for t in ("Zoning by-law", "Development plan", "Secondary plan"):
        print(t, dict(Counter(r["status"] for r in reg["rows"] if r["doc_type"] == t)))
    print(len(findings), "website findings;", path)
