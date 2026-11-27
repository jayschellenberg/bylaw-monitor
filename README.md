# bylaw-monitor

Status: Active | Next: tbd | Blocked: none | Updated: 2026-11-27

Weekly monitor for Manitoba (non-Winnipeg) zoning by-laws, development plans and secondary plans.
Design and decisions: [DESIGN.md](DESIGN.md).

Output (user-facing): `$Bylaw-Monitor\Bylaw Register.xlsx` inside the by-law library folder (`maps_dir` in `config.local.json`; called `Maps` below).

## Setup

Copy `config.example.json` to `config.local.json` (kept out of git) and set:

| Key | What it is |
|---|---|
| `maps_dir` | The by-law library: one folder per municipality, plus `$Planning Districts`. The register and Staging are written to `$Bylaw-Monitor` inside it. |
| `mb_parcelsearch_dir` | A checkout of [manitoba-opendata-parcelsearch](https://github.com/jayschellenberg/manitoba-opendata-parcelsearch): the municipality list, the website list and the email alert helper (`alert-lib.ps1`) are read from it. |
| `ntfy_topic` | ntfy topic for failure alerts. Anyone who knows the name can read the alerts, so keep it private. |

A relative path is taken from this folder. `data/pending.json` and `data/renames.json` hold
this machine's file paths and are not in git; they are created as the monitor runs.

Licence: MIT (see `LICENSE`). The by-laws and the province's open data keep their own terms.

## Pipeline

Run from `bm\` (Python 3.14; PyMuPDF, openpyxl, requests, beautifulsoup4):

| Step | Script | Writes | Notes |
|---|---|---|---|
| 1 | `python opendata.py` | `data/opendata/snapshot_YYYYMMDD.json`, `changes_YYYYMMDD.json` | Province zoning + dev-plan layers, distinct by-law numbers per municipality. Diffed by `MUNI_NO` + by-law number. |
| 2 | `python munis.py` | `data/munis.csv`, `data/pds.csv` | Municipality / planning-district master list: folders, websites, PD. **Hand-editable**: set `locked=1` on a row to keep your edits. |
| 3 | `python inventory.py` | `data/inventory.json` (+ cache) | Every file in the Maps folders, classified; PDFs read for by-law #, 3rd reading, consolidation. Cached on path+size+mtime. |
| 4 | `python discover.py [owner ...]` | `data/watched_pages.json` | Finds each site's by-law pages. `owner` = `muni:<MUNI_NO>` or `pd:<PD KEY>`; no args = all. `locked_pages` survive re-crawls. |
| 5 | `python register.py` | `data/register.json` | Province vs held, one row per by-law area, with status. |
| 6 | `python web.py` | updates `register.json` | Online copy of the current by-law; flags **Newer online**. |
| 7 | `python changes.py` | `data/changes_history.json` | Province changes between snapshots + website findings. |
| 8 | `python excel.py` | `Bylaw Register.xlsx` | Lookup (drop-down) / Register / Files / Changes. |

`python baseline.py` runs 2–8 (and 1 when `--fetch` is given).

**Sites the crawler can't read** (JavaScript menus, bot checks, odd labels) are mapped by a Claude
research pass: agents read each site like a person and write `data/agent_findings/*.json`, then
`python merge_agent.py --yes` re-checks every page and stores it as `agent_pages` in
`watched_pages.json` (kept across re-crawls). Pages whose links only appear with JavaScript keep the
links the agent saw; pages behind a bot check (`script_blocked`) are skipped by the weekly check and
need another research pass to refresh. First pass 2026-11-05: 67 sites, 60 mapped; 161 of 166
municipalities/districts now have a watched page.
**Folder viewers.** Many municipal sites list documents in a JavaScript folder viewer. `folder_doc_links()` in
`discover.py` reads two such platforms straight from their endpoints, with no browser: Catalis/GovOffice "docman"
(`/admin/dm/Public/GetFolderById/`) and municipalwebsites.ca (`/Home/Documents?dirId=`).
**Hand-added pages** go in `locked_pages` in `watched_pages.json`. Their document lists are kept in `locked_docs`
and refreshed by the Friday check.

`data/web_ignore.json` lists links reviewed by hand that only look like newer by-laws.

## Weekly routine

| When | What | Where |
|---|---|---|
| Fri 13:00 | Windows task `bylaw-monitor-weekly` runs `weekly.ps1` → `bm\weekly.py`: province snapshot, watched-page check (+ re-crawl of broken sites and 1/13 of sites on rotation), register rebuild, watchdog, downloads to Staging, Excel, digest. Emails the HTML digest via the shared Gmail SMTP config (`MBOpenData\mb-parcelsearch\alert-email.local.txt`); a failed run sends a failure alert (email + ntfy topic from `config.local.json`). | `logs\weekly-*.log` (ends `COMPLETE`), `Maps\$Bylaw-Monitor\Digests\` |
| Fri 15:00 | Claude desktop scheduled task `bylaw-monitor-friday-review`: re-runs the weekly job if it was missed, reads each new download and records a `claude_check` (adopted? 3rd reading, consolidation, approve/reject recommendation) in `data\pending.json`, refreshes the dashboard, emails a short verification note when there was something to check. Runs while the Claude app is open (or at next launch). | dashboard page (private) |

Register / re-register the Windows task: `powershell -ExecutionPolicy Bypass -File schedule_weekly.ps1` — from an **elevated** prompt to get S4U (runs while logged off); unelevated it is Interactive.
Manual runs: `weekly.ps1` (full), `weekly.ps1 -NoFetch` (rebuild + email only), `-NoEmail`.

## Approving downloads

New documents wait in `Maps\$Bylaw-Monitor\Staging\<Municipality>\…` until you approve them.
Ask Claude "review the pending by-laws", or from `bm\`:

```
python stage.py list
python stage.py approve <ID>          # dry run: shows what would move where
python stage.py approve <ID> --yes    # do it
python stage.py reject <ID> [reason]  # moves the file to Staging\_rejected
```

Approving files the document as `<muni folder>\Zoning|Development Plan|Secondary Plans\<type> By-law <#> - <name>`
(district-wide documents go to `$Planning Districts\<PD>\…` with a shortcut in each member's folder) and moves the
superseded files of that by-law area into `…\<type>\Archives\` in the folder they came from. Another area's
still-current by-law is never archived.

## Status meanings

| Status | Meaning |
|---|---|
| Current | A held, non-archived file is the by-law the province lists |
| Current (misfiled) | The matching file is only in an Archives folder |
| Outdated | You hold this type of document, but a different by-law number |
| Missing | Nothing of this type is held |
| Check | Files held, but none has a readable by-law number (scans, number not in text) |
| Draft only | The only copies held of the province's by-law show first reading only |
| Current (ahead of province) | You hold a final copy of a by-law newer than the one the province lists |

**DRAFT rule.** A document whose readings block shows only first reading (second and third blank) is
still useful but not the final version: `drafts.py` (run every Friday) prefixes its file name with
`DRAFT - ` and the register notes "DRAFT (first reading only)". Office consolidations are skipped, and
any file listed in `data/draft_exceptions.json` (`{path: reason}`) is left alone. Every rename is logged
in `data/renames.json`. Approving a first-reading copy from Staging also files it with the prefix.
| Newer online | The municipality / PD website posts a newer by-law than the province and your copy |
| Pending review | A newer version was downloaded to `Staging` and awaits approval |
| Held only | Held locally, not in the provincial layer (secondary plans) |

## Data sources

- Zoning: `https://services.arcgis.com/mMUesHYPkXjaFGfS/arcgis/rest/services/Manitoba_Zoning_By_Laws/FeatureServer/0`
- Development plans: `.../Manitoba_Development_Plan_Designations/FeatureServer/0`
- Municipality numbers/types: `web\public\mb-municipalities.geojson` in a checkout of [manitoba-opendata-parcelsearch](https://github.com/jayschellenberg/manitoba-opendata-parcelsearch) (`mb_parcelsearch_dir`)
- Websites: `MUNI_WEBSITES` / `PD_WEBSITES` in `web\src\main.js` in the same checkout
- History backfilled from a local archive of monthly province snapshots (2025 and 2026).

## Adapting this to another jurisdiction

The code is written for Manitoba and is not jurisdiction-neutral. The method carries over;
the sources do not. The method:

1. Take an **official index** of which by-law is in force for each municipality, and compare
   it with last week's copy. A by-law number that changes is the signal.
2. **Watch each municipality's by-law page** for documents the index has not caught up with.
3. **Inventory your own library** of by-law PDFs and read each one's number, adoption date
   and consolidation date.
4. Build a **register**: what the index says is current, what you hold, what is newer online.
5. **Stage** new downloads for a person to approve before anything is filed.

To point it at another province or state you need to supply four things. The Manitoba
versions are in the files named here.

| You supply | Manitoba version | Where it lives |
|---|---|---|
| An official index of by-law numbers by municipality | Two provincial ArcGIS layers (zoning, development plans) | Layer addresses in `bm/common.py`; field names (`MUNI_NO`, `ZBL`, `DP_BYLAW`, ...) in `bm/opendata.py` |
| A municipality list with websites, and any planning bodies above them | The municipality boundary file and website lists from the parcel search app; planning districts | `bm/munis.py`, including the name fixes for amalgamated and renamed municipalities |
| A by-law library on disk | One folder per municipality, plus `$Planning Districts` | `maps_dir` in `config.local.json`; folder rules in `bm/munis.py` and `bm/inventory.py` |
| The wording by-laws use for adoption | "Third reading", "office consolidation" | The patterns at the top of `bm/inventory.py` |

Things that will need rework:

- **No official index.** Many jurisdictions publish nothing like the provincial layers. Steps
  2 to 5 still work with the websites as the only signal, but `bm/opendata.py`,
  `bm/register.py` and `bm/changes.py` assume the index exists.
- **Website platforms.** `bm/discover.py` reads two document-viewer platforms common on
  Manitoba municipal sites (Catalis/GovOffice and municipalwebsites.ca). Other regions will
  have their own.
- **Adoption process.** Three readings is the Canadian municipal convention. Elsewhere the
  adoption date is worded differently, and the draft detection in `bm/inventory.py` and
  `bm/drafts.py` depends on it.
- **Document types.** Zoning by-laws, development plans and secondary plans are Manitoba's
  planning documents; the classifier in `bm/inventory.py` looks for those names.
- **Alerts and the weekly review.** `weekly.ps1` uses the email helper from the parcel search
  app, and the Friday review is a Claude scheduled task. Both are optional.

`data/munis.csv`, `data/pds.csv` and `data/watched_pages.json` are Manitoba's municipality
list and watched pages. They show the shape of the data; start yours empty.
