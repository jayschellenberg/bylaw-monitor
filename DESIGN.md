# Manitoba By-law Monitor — Design

Goal: never more than 1 week behind when a municipality (non-Winnipeg) or planning district
releases a new zoning by-law, zoning consolidation, development plan (or amendment), or
secondary plan. Keep every past version in an `Archives` subfolder. Maintain a master
hyperlinked register.

Decided 2026-10-30.

## Scope

| Tracked now | Future option |
|---|---|
| Base zoning by-law + consolidations | Individual site-specific rezonings (`ZBL_A`) |
| Development plan + amendments (`DP_BYLAW` / `DPA_BYLAW`) | |
| Secondary plans | |

Winnipeg excluded.

## Sources (checked in this order each week)

1. **Manitoba geoPortal — Manitoba Zoning By-Laws** (and the Development Plan layer).
   Query the ArcGIS REST service for distinct `MUNI_NAME`, `MUNI_NAME_2014`, `ZBL`,
   `PLANNINGDISTRICT` (+ `DP_BYLAW`, `DPA_BYLAW`). Compare with last week's snapshot.
   The province publishes no dates, so a *new by-law number* is the signal.
   Existing monthly archive: a local folder of province snapshots, one subfolder per year.
2. **Municipal websites** — seed URLs from `MUNI_WEBSITES` in
   `web\src\main.js` of the manitoba-opendata-parcelsearch app (`mb_parcelsearch_dir` in `config.local.json`).
3. **Planning district websites** — seed URLs from `PD_WEBSITES` in the same file.

Sources 2–3 catch by-laws posted before they reach open data.

## Engine: hybrid

- **Local script (free)**, Windows Task Scheduler, **Friday afternoon**, same pattern as
  `mao-assembly\schedule_refresh.ps1`.
  - Pulls the open-data attributes and diffs them.
  - Fetches each *watched page* (see below), extracts the list of document links
    (href + link text + Last-Modified/ETag/size), and diffs against last week.
  - Runs page-health checks (see "Website changes").
  - Writes `changes-YYYYMMDD.json`. If it is empty, it sends a heartbeat digest and stops.
- **Claude agent (only on changes)** reads the changed pages and documents. It identifies
  the by-law number, type, third-reading date and consolidation date, downloads the PDFs to
  staging and drafts the digest.

## On a new find: auto-download + flag

1. Download to `Maps\$Bylaw-Monitor\Staging\<Muni>\<by-law #>\` (multi-file by-laws stay together).
2. Flag it as **Pending review** in the register and the digest.
3. When you approve, move the new version into the standard folder, move the superseded
   version to `Archives\`, and set the register status to Current.

## Folder convention (standardize gradually)

```
<Muni>\Zoning\                      current by-law (one or many files)
<Muni>\Zoning\Archives\
<Muni>\Development Plan\  (+ Archives\)
<Muni>\Secondary Plans\   (+ Archives\)
$Planning Districts\<PD>\...        PD-wide documents live here once;
                                    member munis get a .lnk + register link
```

Existing files are recorded where they sit. A municipality moves to the convention only when
it next gets an update. Multi-document by-laws go in a subfolder named
`<By-law #> (<3rd reading yyyy-mm-dd>)`.

## Register (one row per by-law area)

Rows are keyed on current municipality + former municipality (`MUNI_NAME_2014`), so an
amalgamated municipality can have several active by-laws.

Fields: municipality, former muni / area, planning district, document type, by-law #,
title, 3rd-reading / adoption date, consolidation date (if any), open-data by-law #,
match status (Current / Outdated / Missing / Pending review / Unknown), local path(s),
source URL, watched page URL(s), date downloaded, date last verified, notes.

Outputs, regenerated after every run and every review:

- **Excel** `Maps\$Bylaw-Monitor\Bylaw Register.xlsx`
  - *Lookup* sheet: municipality drop-down showing all zoning by-laws, development plans,
    secondary plans and maps held, with hyperlinks to local files and dates.
  - *Register* sheet: full filterable table.
  - *Changes* sheet: weekly log.
- **Dashboard** (private claude.ai page): same drop-down and status at a glance. It links
  to source URLs, because a web page can't open local D:\ files.
- **Email digest** via Gmail every Friday: new or changed items, pending reviews, broken
  watched pages, plus a heartbeat line when nothing changed.

## Website changes / redesigns (re-crawl strategy)

Watched pages are learned once during the baseline. They are kept honest by four layers:

1. **Weekly health check (free, script).** A page is flagged *Needs re-discovery* if it
   returns 404 or 410, redirects to the homepage or another domain, loses most of its
   document links (>50% drop), or has no PDF links at all. The homepage domain is also
   checked so moved sites are caught.
2. **Targeted re-discovery (agent, only for flagged municipalities).** Claude crawls that
   site from the homepage (by-laws / planning / documents / council-minutes sections plus
   site search). It updates the watched URLs and logs what changed.
3. **Rolling full re-crawl.** Every municipality and planning district is fully re-crawled
   once a quarter, staggered at about 1/13 of them per week so the cost stays flat. This
   also catches new pages, e.g. a newly added "Secondary Plans" page.
4. **Cross-source watchdog.** If open data shows a new by-law number but no watched page has
   shown it within 2 weekly runs, that municipality gets an immediate re-discovery plus a web
   search. It is also flagged if a watched page hasn't changed in 18+ months while the
   provincial layer has.

Every re-discovery result is logged so you can see why a URL changed.

## Build phases

1. Project skeleton, config, open-data fetch + diff.
2. **Baseline.** Inventory every folder, reading file names plus PDF cover/last pages for
   by-law #, 3rd-reading date and consolidation date. Compare against open data and discover
   the watched pages. Produce a draft register marking each row Current, Outdated or Missing
   for you to review.
3. Excel register + dashboard generation.
4. Weekly script, health checks, staging/approval flow, Gmail digest, Task Scheduler entry.
5. Re-crawl rotation + watchdog.
