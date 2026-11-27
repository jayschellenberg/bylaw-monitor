"""Friday run: province snapshot, page checks, register rebuild, downloads to Staging, digest.

    python weekly.py            # full run
    python weekly.py --no-fetch # skip network-heavy steps (re-render from current data)

Writes:
  data/weekly_latest.json                          summary (also read by the Claude follow-up task)
  Maps\\$Bylaw-Monitor\\Digests\\Digest YYYY-MM-DD.html   the email body (weekly.ps1 sends it)
  Maps\\$Bylaw-Monitor\\Bylaw Register.xlsx
  data/dash/{register,changes,pending}.json        data for the dashboard page
"""
from __future__ import annotations

import html
import sys
import traceback
from collections import Counter
from datetime import date, datetime

import changes
import discover
import drafts
import excel
import inventory
import munis
import opendata
import register
import stage
import watch
import web
from common import DATA, OUTPUT, now_iso, read_json, write_json

WATCHDOG = DATA / "watchdog.json"
DIGESTS = OUTPUT / "Digests"
WATCHDOG_WEEKS = 2


def update_watchdog(reg: dict) -> list[dict]:
    """Province by-laws first seen in a snapshot we took, not yet found online -> overdue after 2 weeks."""
    wd = read_json(WATCHDOG, {})
    today = date.today()
    for r in reg["rows"]:
        if not r.get("od_first_seen") or not r["od_bylaw"]:
            continue
        key = f"{r['muni_no']}|{r['doc_type']}|{r['od_bylaw']}"
        e = wd.setdefault(key, {"municipality": r["municipality"], "doc_type": r["doc_type"], "bylaw": r["od_bylaw"],
                                "province_first_seen": r["od_first_seen"], "watch_started": today.isoformat(),
                                "found_online": None})
        if (r.get("web") or {}).get("note") == "current by-law found online" and not e["found_online"]:
            e["found_online"] = today.isoformat()
        if r["status"] in ("Current", "Current (misfiled)"):
            e["held"] = True
    write_json(WATCHDOG, wd)
    overdue = []
    for k, e in wd.items():
        started = date.fromisoformat(e["watch_started"])
        if not e["found_online"] and not e.get("held") and (today - started).days >= 7 * WATCHDOG_WEEKS:
            overdue.append({"key": k, **e})
    return overdue


def _rows_html(rows: list[list[str]], head: list[str]) -> str:
    th = "".join(f"<th style='text-align:left;padding:4px 8px;background:#305496;color:#fff'>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top'>{c}</td>"
                                   for c in r) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse;font-size:13px'>{th}{trs}</table>"


def _a(url: str | None, text: str | None = None) -> str:
    if not url:
        return ""
    return f"<a href='{html.escape(url, quote=True)}'>{html.escape(text or url)}</a>"


def digest(summary: dict, reg: dict) -> tuple[str, str]:
    e = html.escape
    pend = [i for i in stage.load() if i["status"] == "pending"]
    new_staged = summary["staged"]
    prov = [c for c in summary["province_changes"] if c["layer"] in ("zoning", "devplan")]
    prov_amend = [c for c in summary["province_changes"] if c["layer"] == "devplan_amendments"]
    newer = [r for r in reg["rows"] if r["status"] == "Newer online"]
    ahead = [r for r in reg["rows"] if r["status"] == "Current (ahead of province)"]
    newer_new = [f for f in summary["web_findings"]]
    parts = [f"<h2 style='font-family:Segoe UI,Arial'>By-law monitor &middot; {date.today():%A %B %d, %Y}</h2>",
             (f"<p style='font-family:Segoe UI,Arial;font-size:13px;color:#555'>Province zoning layer last edited "
              f"{e(str(summary['zoning_last_edit'])[:10])}, development-plan layer {e(str(summary['devplan_last_edit'])[:10])}. "
              f"{summary['owners_checked']} municipal / planning-district sites checked.</p>")
             if summary["zoning_last_edit"] else
             "<p style='font-family:Segoe UI,Arial;font-size:13px;color:#555'>Quick run: province data and websites "
             "were not checked this time; the register was rebuilt from existing data.</p>"]
    quiet = not (new_staged or prov or newer_new or summary["overdue"] or summary["broken"] or summary.get("drafts_renamed"))
    if quiet:
        parts.append("<p style='font-family:Segoe UI,Arial'><b>No changes this week.</b> Nothing new from the province "
                     "or on the watched municipal pages.</p>")

    def section(title: str, body: str) -> None:
        parts.append(f"<h3 style='font-family:Segoe UI,Arial;margin-top:22px'>{title}</h3>{body}")

    if pend:
        section(f"Waiting for your review ({len(pend)}, {len(new_staged)} new this week)", _rows_html(
            [[e(i["id"]), e(i["municipality"]), e(i["doc_type"]), e(i["bylaw"] or "?"),
              e(((i.get("pdf") or {}).get("third_reading")) or ""), e(i["reason"]),
              _a(i["url"], "source") + "<br><span style='color:#777;font-size:11px'>" + e(i["path"] or i.get("error") or "") + "</span>"]
             for i in pend], ["ID", "Municipality", "Document", "By-law", "3rd reading", "Why", "Source / staged file"])
            + "<p style='font-size:12px;color:#555'>To file one: ask Claude &ldquo;review the pending by-laws&rdquo;, "
              "or run <code>python stage.py approve &lt;ID&gt;</code> in the bm folder.</p>")
    if prov:
        section("Province changes this week", _rows_html(
            [[e(c["muni"]), e(c["doc_type"]), e(c["change"]), e(c["bylaw"]), e(", ".join(c["current_set"]))] for c in prov],
            ["Municipality", "Document", "Change", "By-law", "Province now lists"]))
    if prov_amend:
        section("Development plan amendments (province)", "<p style='font-size:13px'>" + e(
            "; ".join(f"{c['muni']} {c['change']} {c['bylaw']}" for c in prov_amend)) + "</p>")
    if newer:
        section(f"Posted on websites ahead of the province ({len(newer)})", _rows_html(
            [[e(r["municipality"]), e(r["doc_type"]), e(r["od_bylaw"] or "-"), e((r["web"] or {}).get("newer", "")),
              _a(r["web"].get("url"), r["web"].get("text"))] for r in newer],
            ["Municipality", "Document", "Province", "Website", "Link"]))
    if ahead:
        section(f"You hold by-laws newer than the province lists ({len(ahead)})", "<p style='font-size:13px'>" + e(
            "; ".join(f"{r['municipality']} {r['doc_type'].lower()} {r['web'].get('newer')} (province: {r['od_bylaw']})"
                      for r in ahead)) + "</p>")
    if summary.get("drafts_renamed"):
        section(f"Renamed as DRAFT (first reading only) ({len(summary['drafts_renamed'])})", "<p style='font-size:13px'>" + "<br>".join(
            e(r["to"]) for r in summary["drafts_renamed"]) + "</p><p style='font-size:12px;color:#555'>To keep a file's original "
            "name, add its path to data/draft_exceptions.json and rename it back (data/renames.json has the old names).</p>")
    if summary["overdue"]:
        section("Watchdog: new provincial by-law not found online after 2+ weeks", _rows_html(
            [[e(o["municipality"]), e(o["doc_type"]), e(o["bylaw"]), e(o["watch_started"])] for o in summary["overdue"]],
            ["Municipality", "Document", "By-law", "Watching since"])
            + "<p style='font-size:12px;color:#555'>These sites were re-crawled this run. Still missing usually means "
              "the municipality hasn't posted it; ask the office or check the planning district.</p>")
    if summary["broken"] or summary["recrawled"]:
        section("Website health", _rows_html(
            [[e(b["name"] or b["owner"]), _a(b["url"]), e(b["why"])] for b in summary["broken"]]
            + [[e(o), "", e(f"re-crawled ({v['reason']})" + (f"; pages changed: {len(v['pages_changed'])}" if v["pages_changed"] else "")
                         + (f"; ERROR {v['error']}" if v.get("error") else ""))] for o, v in summary["recrawled"].items()],
            ["Site", "Page", "Issue / action"]))
    totals = summary["totals"]
    section("Register status", _rows_html(
        [[e(t)] + [str(totals.get(t, {}).get(s, 0)) for s in ("Current", "Pending review", "Newer online", "Outdated", "Missing", "Check")]
         for t in ("Zoning by-law", "Development plan")],
        ["", "Current", "Pending", "Newer online", "Outdated", "Missing", "Check"])
        + f"<p style='font-size:12px;color:#555'>Full register: {e(str(OUTPUT / 'Bylaw Register.xlsx'))}</p>")
    if summary.get("errors"):
        section("Step errors", "<pre style='font-size:11px'>" + e("\n".join(summary["errors"])) + "</pre>")
    n = len(new_staged) + len(prov) + len(newer_new)
    subject = (f"By-law monitor {date.today():%b %d}: " + ("no changes" if quiet else
               f"{len(new_staged)} new download(s), {len(prov)} province change(s), {len(summary['overdue'])} overdue"))
    return subject, "<div style='font-family:Segoe UI,Arial'>" + "".join(parts) + "</div>"


DASH = DATA / "dash"


def _day(stamp: str | None) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}" if stamp else ""


def dashboard_files(reg: dict) -> None:
    """Row files for the dashboard page (uploaded by the Friday Claude task): register, changes, pending."""
    rows = []
    for r in reg["rows"]:
        w = r.get("web") or {}
        link = w.get("url", "")
        rows.append({
            "key": f"{r['muni_no']}|{r['doc_type']}|{r['area']}|{r['od_bylaw']}",
            "municipality": r["municipality"], "type": r["muni_type"], "planning_district": r["planning_district"],
            "document": r["doc_type"], "area": r["area"], "province_bylaw": r["od_bylaw"],
            "new_in_province": _day(r.get("od_first_seen")), "status": r["status"],
            "third_reading": r.get("third_reading") or "", "consolidation": r.get("consolidation") or "",
            "held_file": (r.get("best_file") or "").split("\\")[-1], "online": w.get("text", "") if link else "",
            "online_link": link if link.startswith(("http://", "https://")) else "",
            "note": "; ".join(filter(None, [r.get("held_note"), w.get("note", "")])),
            "municipal_site": r["website"], "pd_site": r["pd_website"]})
    hist = sorted(read_json(changes.HISTORY, []), key=lambda c: c["detected"], reverse=True)[:200]
    chg = [{"id": f"{i}", "detected": c["detected"], "source": c["source"], "municipality": c["muni"],
            "document": c["doc_type"], "change": c["change"], "bylaw": c["bylaw"],
            "province_now": ", ".join(c.get("current_set", [])), "note": c.get("note", ""),
            "link": c.get("url") or ""} for i, c in enumerate(hist)]
    pend = [{"id": i["id"], "municipality": i["municipality"], "document": i["doc_type"], "bylaw": i["bylaw"] or "",
             "third_reading": (i.get("pdf") or {}).get("third_reading") or "", "reason": i["reason"],
             "staged": i["staged_at"][:10], "link": i["url"],
             "recommend": (i.get("claude_check") or {}).get("recommend", "not checked yet"),
             "check_notes": (i.get("claude_check") or {}).get("notes", ""), "status": i["status"]}
            # Every pending item plus the latest 50 others: the dashboard shows only "pending" rows, but an
            # empty file would read as a failed load there.
            for i in sorted(stage.load(), key=lambda i: (i["status"] != "pending", i["staged_at"]), reverse=False)][:200]
    pend = [p for p in pend if p["status"] == "pending"] + [p for p in pend if p["status"] != "pending"][-50:]
    for name, data in (("register", rows), ("changes", chg), ("pending", pend)):
        write_json(DASH / f"{name}.json", data)


def run(fetch: bool = True) -> dict:
    errors = []

    def step(name, fn, default=None):
        try:
            return fn()
        except Exception:
            errors.append(f"{name}: {traceback.format_exc(limit=3)}")
            print(f"STEP FAILED: {name}", file=sys.stderr)
            return default

    od = step("province snapshot", opendata.run, {}) if fetch else {}
    step("municipality list", munis.build)
    step("folder inventory", inventory.run)
    # Jason's rule: a first-reading-only copy gets "DRAFT - " in front of its file name.
    renamed = step("draft rename", lambda: drafts.rename(drafts.candidates()), []) or []
    if renamed:
        step("folder inventory (after renames)", inventory.run)
    ws = step("watched pages", watch.run, {}) if fetch else {}
    reg = register.build()
    step("web attach", web.run)
    reg = read_json(register.REGISTER)
    overdue = step("watchdog", lambda: update_watchdog(reg), []) or []
    if fetch and overdue:      # targeted re-discovery for overdue municipalities, then re-attach
        def recrawl():
            pd_of = {str(m["muni_no"]): m["pd_key"] for m in munis.load_munis()}
            owners = {f"muni:{o['key'].split('|')[0]}" for o in overdue} | {f"pd:{pd_of.get(o['key'].split('|')[0])}" for o in overdue}
            discover.run(only=owners)
            web.run()
        step("watchdog re-crawl", recrawl)
        reg = read_json(register.REGISTER)
    findings = [r for r in reg["rows"] if r["status"] == "Newer online"]
    web_records = []
    for r in findings:
        w = r["web"]
        web_records.append({"detected": date.today().isoformat(), "source": "Website", "muni": r["municipality"],
                            "doc_type": r["doc_type"], "change": "posted", "bylaw": w.get("newer", ""),
                            "current_set": [r["od_bylaw"]] if r["od_bylaw"] else [], "note": w.get("text", ""),
                            "url": w.get("url")})
    staged = (step("download to staging", lambda: stage.collect(reg, ws), []) or []) if fetch else []
    stage.apply_to_register(reg)
    write_json(register.REGISTER, reg)
    step("change history", lambda: changes.save(web_records))
    step("excel", excel.build)
    totals = {t: dict(Counter(r["status"] for r in reg["rows"] if r["doc_type"] == t))
              for t in ("Zoning by-law", "Development plan", "Secondary plan")}
    summary = {"ran_at": now_iso(), "zoning_last_edit": od.get("zoning_last_edit"),
               "devplan_last_edit": od.get("devplan_last_edit"), "province_changes": od.get("changes", []),
               "owners_checked": ws.get("owners", 0), "broken": ws.get("broken", []), "recrawled": ws.get("recrawled", {}),
               "new_links": len(ws.get("new_links", [])), "staged": staged, "web_findings": web_records,
               "overdue": overdue, "totals": totals, "errors": errors, "drafts_renamed": renamed}
    subject, body = digest(summary, reg)
    DIGESTS.mkdir(parents=True, exist_ok=True)
    path = DIGESTS / f"Digest {date.today():%Y-%m-%d}.html"
    path.write_text(f"<html><head><meta charset='utf-8'><title>{html.escape(subject)}</title></head><body>{body}</body></html>",
                    encoding="utf-8")
    summary.update(subject=subject, digest_path=str(path))
    step("dashboard files", lambda: dashboard_files(reg))
    write_json(DATA / "weekly_latest.json", summary)
    write_json(DATA / f"weekly_{date.today():%Y%m%d}.json", summary)
    return summary


if __name__ == "__main__":
    s = run(fetch="--no-fetch" not in sys.argv)
    print(s["subject"])
    print("digest:", s["digest_path"])
    if s["errors"]:
        print("ERRORS:\n" + "\n".join(s["errors"]))
        sys.exit(2)
