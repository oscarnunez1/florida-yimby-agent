# Changelog

## Session 9 — 2026-05-01

**Inbox/Archive rename, cascading geo filter bar, county/region data model**

- Renamed Today → Inbox (`/inbox`), History → Archive (`/archive`). `/history` redirects to `/archive` with 301.
- Added `county` and `region` columns to `extracted_items` with indexes. `CITY_TO_COUNTY` (80+ entries) and `COUNTY_TO_REGION` dicts added to `process.py`. All existing rows backfilled.
- New filter bar on both Inbox and Archive: Region → County → City (cascading, client-side via GEO JSON), Date (Today / 7d / 30d / 90d / All Time), Source (News vs Municipal), Status (Unread default on Inbox, All default on Archive), Hearings toggle (shows briefs with upcoming hearing in 14 days).
- Active filter chips below the filter bar — each shows a clear-URL × button; "Clear all" resets everything.
- Unread count badge (coral pill) next to Inbox in sidebar.
- `_apply_common_filters` helper centralizes WHERE clause building for both routes.

---

## Session 8 — 2025 (multiple commits)

**UI redesign, city-level market detection, View Transitions, undo toast**

- Complete template rewrite: Playfair Display headlines, DM Sans UI, DM Mono data, dark sidebar, card grid layout with OG images.
- Renamed dashboard to "Florida YIMBY Intel".
- Replaced county-level market patterns (`BROWARD`, `WEST PALM`) with `FLORIDA_CITIES` dict (80+ entries) for city-level detection (`MIRAMAR`, `BOCA RATON`). Longest-match-first sort prevents `miami` matching `miami beach`.
- View Transitions API: `@view-transition { navigation: auto }` plus matching `view-transition-name: card-{id}` on cards and detail page for shared-element morph animation.
- Undo toast: 5-second countdown bar, stores card HTML + DOM position before removal, `/briefs/<id>/undo` endpoint restores status.
- Three-dot menu with Mark as used / Snooze 24h / Dismiss (6 reasons).
- OG image fetch on RSS captures with base64 embedding for Floridian Development (hotlink protection).
- Backfilled OG images for 181 existing RSS captures.
- Tampa (`tampagov.net/iqm2`) and St. Pete (`stpete.org/iqm2`) confirmed as Legistar, not IQM2 — deferred to Legistar scraper.
- `utils.py` created to share `_strip_fences()` between `scrape.py` and `process.py`.
- N+1 query on Sources page replaced with single `GROUP BY source` aggregation.
- `lru_cache` on `_sources_yaml()` to avoid repeated YAML disk reads.

---

## Session 7 — 2025

**Cron job, log viewer, run_daily.py orchestrator**

- `run_daily.py` — four-step pipeline orchestrator that runs scrape → classify → dedup → draft-briefs in sequence and writes to `daily_log`.
- `run_daily.sh` — bash wrapper activates `.venv`, runs `run_daily.py`, appends output to `logs/daily_YYYY-MM-DD.log`.
- Cron job installed: `0 10 * * *` runs `run_daily.sh` daily at 10 AM.
- Logs page (`/logs`) in dashboard: run history table + last 100 lines of latest log file in dark monospace viewer.
- Confirmed full daily run clean end-to-end.

---

## Session 6 — 2025

**Flask dashboard live**

- `dashboard.py` — Flask app with all routes: Today (`/`), History (`/history`), Sources (`/sources`), Coverage (`/coverage`), Brief detail (`/briefs/<id>`), Logs (`/logs`).
- All six Jinja2 templates written.
- `run_daily.py` orchestrator wired up and tested.
- Coverage index at 2,897 articles after full site-wide backfill.
- POST endpoints: `/briefs/<id>/use`, `/briefs/<id>/dismiss`, `/briefs/<id>/snooze`.
- Market filter pills on Today view, date range filter, paginated History.

---

## Session 5 — 2025

**Coverage index backfill, deduplication working**

- `process.py ingest-coverage` — crawls full floridayimby.com archive via WordPress REST API (site-wide, not just Oscar's byline), extracts project fields via Haiku, inserts into `coverage_index`.
- Coverage index populated to 2,897 articles.
- `process.py dedup` — fuzzy match extracted items against coverage index using rapidfuzz `token_sort_ratio` (threshold 85) with address confirmation step.
- Address fallback: `partial_ratio` catches multi-parcel assemblage addresses where one string is a substring of the other.
- 24 items flagged as already covered, 98 new items eligible for brief drafting.

---

## Session 4 — 2025

**IQM2 municipal board pipeline**

- IQM2 scraper added to `scrape.py`: polls `miamifl.iqm2.com` calendar for four Miami boards (UDRB, PZAB, HEPB, Wynwood DRC).
- Meeting state tracked in `meetings` table to detect newly posted agenda packets.
- Agenda PDF extraction via Haiku's document API: sends plain agenda PDF (Type=14) and returns JSON array of project line items.
- Each project item creates a `raw_capture` with `metadata_json` carrying `hearing_date`, `hearing_board`, `agenda_item_number`, `agenda_newly_posted`.
- `agenda_newly_posted` flag forces Haiku to assign `priority=high` during classification.
- 90 municipal captures created across 4 boards.
- Coverage index initial backfill started (105 articles at end of session).
- `meetings` table added to `db.py` schema.

---

## Session 3 — 2025

**Classification pipeline, 258 items classified**

- `process.py classify` — sends each unprocessed raw capture to Claude Haiku 4.5 with `classify.md` system prompt.
- Haiku outputs structured JSON: `is_development_item`, `florida_relevance`, `project_name`, `address`, `city`, `developer`, `architect`, `units`, `height_ft`, `status`, `event_type`, `priority`.
- Prompt engineering for Florida relevance: certain sources are treated as FL-only by definition (The Real Deal Miami, Floridian Development).
- `extracted_items` table populated; 79 FL development items identified out of 258 classified.
- `process.py status` command shows capture and extraction counts.
- `process.py dedup` stub added (full implementation in Session 5).

---

## Sessions 1 & 2 — 2025

**Core scraper: RSS, HTML, WP REST**

- `db.py` — SQLite schema with `raw_captures`, `extracted_items`, `coverage_index`, `briefs`, `meetings`, `daily_log`. WAL mode enabled. Migration-safe: all column additions use `ALTER TABLE … ADD COLUMN` in a try/except loop.
- `scrape.py` — three scraper types:
  - RSS via feedparser: The Real Deal Miami, Floridian Development, Commercial Observer, Construction Dive, Bisnow (5 feeds).
  - HTML scraper via selectolax: Arquitectonica Projects.
  - WordPress REST API: Kobi Karp Projects.
- `sources.yaml` — declarative source config: URL, selectors, category, tags.
- Deduplication by URL using SQLite `INSERT OR IGNORE` with UNIQUE constraint.
- `.env.example` with `ANTHROPIC_API_KEY`.
- `requirements.txt` with all dependencies.
- Initial scrape: 548 raw captures across all sources.
