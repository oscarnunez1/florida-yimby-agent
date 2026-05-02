# Florida YIMBY Intel

A personal research agent that monitors Florida real estate development news, municipal hearing agendas, and editorial sources, then drafts publication-ready briefs for a journalist covering the Florida development beat. Instead of spending an hour each morning scanning feeds and board calendars, the journalist opens a dashboard, reviews pre-written briefs filtered to their coverage area, and either sends them to WordPress or dismisses them. The agent runs automatically every morning at 10 AM and costs roughly a dollar a day to operate.

---

## What It Does

### Full pipeline

```
Sources → Scraper → raw_captures → Classifier → extracted_items → Dedup → Briefs → Dashboard
```

**1. Scrape** (`scrape.py`)
Pulls new items from every configured source:
- **RSS feeds** — fetched via feedparser; OG images fetched per article and stored alongside the capture. Floridian Development images are downloaded and embedded as base64 data URLs because that site blocks hotlinking.
- **IQM2 municipal calendars** — Miami boards on `miamifl.iqm2.com` are polled for upcoming meetings and newly posted agenda packets. When a new agenda appears, it is downloaded as a PDF and sent to Claude Haiku for project extraction. One `raw_capture` is created per development item found in the agenda.
- **Legistar municipal calendars** — Tampa, St. Petersburg, Coral Gables, and Pompano Beach boards are monitored via Legistar's `Calendar.aspx` pages using the same detect-and-extract pattern as IQM2. Coral Gables is currently accessible; Tampa, St. Pete, and Pompano Beach are blocked by Cloudflare WAF (infrastructure in place, access pending Playwright integration). Agenda PDFs are extracted via Claude Haiku in the same way as IQM2.

New items land in `raw_captures`. Deduplication is by URL — already-seen items are silently skipped.

**2. Classify** (`process.py classify`)
Every unprocessed capture is sent to Claude Haiku with a structured prompt. Haiku decides: Is this a Florida development item? How newsworthy is it (high / medium / low)? It extracts whatever structured fields it can find — project name, address, city, developer, architect, unit count, height, status, event type. Items flagged as newly-posted agenda packets are automatically promoted to high priority. Results go into `extracted_items` with a `market` (city), `county`, and `region` value derived from the city field using word-boundary regex matching.

**3. Dedup** (`process.py dedup`)
Every extracted Florida development item is fuzzy-matched against the coverage index — a catalogue of every article published on floridayimby.com. Matching uses token sort ratio on project names (threshold: 85) with an address confirmation step. If the journalist has already written about a project, the item is flagged `already_covered = 1` and excluded from brief generation.

**4. Draft briefs** (`process.py draft-briefs`)
For each uncovered FL development item, Claude Opus drafts an 8-section research brief in the publication's editorial voice: Headline, Lede, Body, Fact Sheet, Sources, Confirmed vs Pending, Open Questions (with a recommended next step), and an Accuracy Score. Briefs are written to be publication-ready: fact-dense, zero promotional language, no speculation beyond what the source states.

**5. Dashboard** (`dashboard.py`)
A Flask web app on `localhost:5000`. The journalist opens it, reads the Inbox (today's unread briefs), dismisses irrelevant ones, marks used ones, and copies the WordPress HTML for briefs they want to publish.

---

## Architecture

```
sources.yaml
    │
    ▼
scrape.py ──────────────────────────────────────────────────────────────────┐
│  RSS feeds (feedparser)                                                    │
│  IQM2 calendar + agenda PDF (httpx + Haiku PDF extraction)                │
│  Legistar calendar + agenda PDF (httpx + Haiku PDF extraction)            │
    │
    ▼
raw_captures  (SQLite)
    │
    ▼
process.py classify  (Claude Haiku 4.5)
    │  Structured JSON extraction per item
    │  City → county → region detection (word-boundary regex)
    ▼
extracted_items  (SQLite)
    │
    ├──► process.py dedup  (rapidfuzz fuzzy match)
    │        │
    │        ▼
    │    coverage_index  (floridayimby.com archive, 2,897 articles)
    │        │
    │        └──► already_covered flag set on extracted_items
    │
    ▼
process.py draft-briefs  (Claude Opus 4.5)
    │
    ▼
briefs  (SQLite)
    │
    ▼
dashboard.py  (Flask + Jinja2)
    │
    ├── /inbox    — unread briefs, cascading geo filters
    ├── /archive  — all briefs, full filter bar
    ├── /briefs/<id>  — full brief detail + WordPress copy
    ├── /sources  — source health table
    ├── /coverage — coverage index search
    └── /logs     — pipeline run history
```

---

## Source Inventory

### RSS feeds
| Source | URL | Notes |
|---|---|---|
| The Real Deal Miami | therealdeal.com/miami/feed/ | Miami-only real estate news |
| Floridian Development | floridiandevelopment.com/feed/ | Florida construction + development |
| Commercial Observer | commercialobserver.com/feed/ | National CRE, filtered for FL |
| Construction Dive | constructiondive.com/feeds/news/ | National construction, filtered for FL |
| Bisnow | bisnow.com/rss/ | Global CRE feed, filtered for FL relevance during classification |
| St Pete Rising | feeds.feedburner.com/StPeteRising | Tampa Bay development news (FeedBurner) |
| Tampa Bay Business & Wealth | tbbwmag.com/feed/ | Tampa Bay business and real estate |
| Business Observer Florida | businessobserverfl.com/rss/headlines/all/ | Statewide Florida business news |

### IQM2 municipal boards
| Source | Board | Lookahead |
|---|---|---|
| Miami Urban Development Review Board | Urban Development Review Board | 60 days |
| Miami Planning Zoning and Appeals Board | Planning, Zoning and Appeals Board | 60 days |
| Miami Historic and Environmental Preservation Board | Historic and Environmental Preservation Board | 60 days |
| Wynwood Design Review Committee | Wynwood Design Review Committee | 60 days |

All four boards use `miamifl.iqm2.com`. The scraper polls each board's calendar, detects newly posted agenda PDFs, extracts individual project listings from the PDF via Haiku, and creates one `raw_capture` per project line item.

### Legistar municipal boards
| Municipality | Board | Status |
|---|---|---|
| Coral Gables | Planning and Zoning Board | Active |
| Coral Gables | Board of Architects | Active |
| Tampa | Planning Commission | WAF-blocked |
| Tampa | City Council | WAF-blocked |
| Tampa | Variance Review Board | WAF-blocked |
| Tampa | Architectural Review Committee | WAF-blocked |
| St. Petersburg | Planning Commission | WAF-blocked |
| St. Petersburg | City Council | WAF-blocked |
| St. Petersburg | Board of Adjustment | WAF-blocked |
| Pompano Beach | Community Redevelopment Agency | WAF-blocked |
| Pompano Beach | Planning and Zoning Board | WAF-blocked |
| Pompano Beach | City Commission | WAF-blocked |
| Pompano Beach | Architectural Appearance Review Board | WAF-blocked |

Coral Gables (`coralgables.legistar.com`) is accessible and actively monitored. Tampa, St. Petersburg, and Pompano Beach Legistar portals return 19-byte Cloudflare WAF responses to automated requests. The scraper infrastructure is fully in place for all 13 boards — accessing the blocked sites requires a Playwright-based headless browser (see Roadmap). Column indices are discovered dynamically from table headers, so layout changes by any municipality won't break parsing.

---

## Tech Stack

- **Python 3.9.6**
- **httpx** — HTTP client for all web requests (RSS, HTML, IQM2, Legistar, OG images)
- **feedparser** — RSS/Atom feed parsing
- **selectolax** — fast HTML parsing with CSS selectors
- **flask** — web framework for the dashboard
- **python-dotenv** — `.env` file loading
- **pyyaml** — `sources.yaml` parsing
- **rapidfuzz** — fuzzy string matching for deduplication
- **apscheduler** — used by the cron shell wrapper
- **anthropic** — Anthropic Python SDK
- **SQLite** with WAL mode + 5 s busy timeout — single-file database, concurrent read-safe
- **Claude Haiku 4.5** — classification, agenda PDF extraction, coverage index ingestion
- **Claude Opus 4.5** — brief drafting (8-section editorial briefs)

---

## Reliability & Performance Features

Improvements made after the initial build to harden the pipeline for daily unattended operation:

- **SQLite WAL mode + 5 s busy timeout** — the scraper and dashboard can run concurrently without "database is locked" errors; SQLite will retry for up to 5 seconds before raising an exception.
- **Centralised geo data** (`utils.py`) — `CITY_TO_COUNTY` and `COUNTY_TO_REGION` are defined once in `utils.py` and imported everywhere; no duplication between `process.py` and `dashboard.py`.
- **Cached sidebar unread count** — `inject_globals()` stores the unread count in Flask's `g` object so the `COUNT(*)` query runs at most once per request regardless of how many templates reference it.
- **Resilient Legistar column detection** — the scraper discovers board-name and date column indices by scanning `<th>` header text rather than using hardcoded positional indices, so a municipality's layout change won't silently produce wrong data.
- **Word-boundary market detection** — `detect_market()` uses `\b` regex boundaries when scanning address fields, preventing partial-word matches (e.g. `daviesfield` no longer triggers the `DAVIE` market).
- **Explicit Anthropic API error handling** — all three `client.messages.create()` call sites catch `anthropic.APIError` explicitly before the general `Exception` fallback, producing a clean log line and continuing the pipeline rather than crashing the run.
- **Secure Flask secret key** — loaded from `FLASK_SECRET_KEY` env var; never hardcoded.

---

## Project Structure

```
florida-yimby-agent/
├── scrape.py              # All source scrapers: RSS, IQM2, Legistar
├── process.py             # Classification, dedup, brief drafting, market detection
├── dashboard.py           # Flask app: all routes, filter logic, template context
├── db.py                  # SQLite schema, migrations, connection helper
├── utils.py               # Shared utilities and single source of truth for geo data
│                          #   (CITY_TO_COUNTY, COUNTY_TO_REGION mappings + helpers)
├── run_daily.py           # Pipeline orchestrator: runs all 4 steps in sequence
├── run_daily.sh           # Bash wrapper: activates venv, writes dated log file
├── backfill_og_images.py  # One-shot: backfill OG images for existing RSS captures
├── sources.yaml           # All monitored sources with selectors and config
├── style_guide.md         # Editorial voice reference (placeholder)
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── prompts/
│   ├── classify.md        # Haiku system prompt: classify + extract structured fields
│   ├── draft_brief.md     # Opus system prompt: write 8-section editorial brief
│   ├── extract_agenda.md  # Haiku system prompt: extract projects from agenda PDFs
│   ├── ingest_archive.md  # Haiku system prompt: extract fields from published articles
│   └── extract.md        # Stub (not yet wired up)
├── templates/
│   ├── base.html          # Layout, CSS design system, sidebar nav, JS utilities
│   ├── inbox.html         # Today's unread briefs with cascading geo filter bar
│   ├── archive.html       # All briefs, full filter bar
│   ├── brief_detail.html  # Single brief: all sections + WordPress copy button
│   ├── sources.html       # Source health table
│   ├── coverage.html      # Coverage index search
│   └── logs.html          # Run history table + latest log file viewer
├── logs/                  # Daily log files (daily_YYYY-MM-DD.log), gitignored
├── db.sqlite              # SQLite database, gitignored
└── runs.log               # Legacy log file
```

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url>
cd florida-yimby-agent
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and add your keys:

```
ANTHROPIC_API_KEY=sk-ant-...

# Flask session security — generate with: python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=your-secret-key-here
```

`FLASK_SECRET_KEY` is used to sign Flask session cookies. Without it the dashboard falls back to a generic dev key, which is insecure if the port is ever exposed outside localhost.

### 5. Initialize the database

```bash
python db.py
```

This creates `db.sqlite` with all tables and indexes. Safe to re-run — all migrations are idempotent.

### 6. Run the first scrape

```bash
python run_daily.py
```

This runs all four pipeline steps: scrape → classify → dedup → draft-briefs. On the first run, the coverage index is empty so nothing will be flagged as already covered. It takes 5–15 minutes depending on how many new items the AI needs to process.

### 7. (Optional) Backfill the coverage index

The dedup step compares against `coverage_index`, which starts empty. To populate it with the full floridayimby.com archive:

```bash
python process.py ingest-coverage
```

This fetches all ~2,900 published articles via the WordPress REST API and sends each one to Haiku to extract project name, address, developer, and architect. Costs roughly $2.50 and takes 20–30 minutes. Only needs to be done once; subsequent runs only fetch new articles.

### 8. Start the dashboard

```bash
python dashboard.py
```

Open `http://localhost:5000/inbox` in a browser.

---

## Daily Operations

### Automatic schedule

The pipeline runs automatically at **10:00 AM every day** via cron:

```
0 10 * * * /Users/oscarnunez/florida-yimby-agent/run_daily.sh
```

`run_daily.sh` activates the virtual environment, runs `run_daily.py`, and appends all output to `logs/daily_YYYY-MM-DD.log`. The cron job requires the laptop to be awake at 10 AM.

To install the cron job on a new machine:

```bash
crontab -e
# Add: 0 10 * * * /path/to/florida-yimby-agent/run_daily.sh
```

### Running manually

```bash
# Full pipeline
python run_daily.py

# Individual steps
python scrape.py                        # scrape all sources (RSS + IQM2 + Legistar)
python scrape.py --rss-only             # RSS only
python scrape.py --legistar-only        # Legistar calendar sources only
python process.py classify              # classify new captures
python process.py classify --limit 5    # classify at most 5 (for testing)
python process.py classify --dry-run    # classify without writing to DB
python process.py dedup                 # update already_covered flags
python process.py draft-briefs          # generate briefs
python process.py draft-briefs --limit 3
python process.py update-markets        # re-detect city/county/region for all items
python process.py status                # show DB counts
```

### Checking logs

Dashboard Logs page at `/logs` shows the last 30 run records and the last 100 lines of today's log file.

Command line:

```bash
tail -f logs/daily_$(date +%Y-%m-%d).log   # follow today's log
ls -lt logs/                                # list all log files
```

The `daily_log` table in the database stores run date, new captures count, new briefs count, duration, and any errors per run.

---

## Dashboard Guide

### Inbox (`/inbox`)

Shows unread briefs (status = new or pending, not snoozed past their wake time) captured in the last 7 days by default. The filter bar lets you narrow by:

- **Region** → **County** → **City** — cascading geo dropdowns. Changing Region filters the County options; changing County filters the City options. All client-side, no extra server round-trips.
- **Date** — presets: Today, Last 7 days, Last 30 days, Last 90 days, All Time, or Custom range.
- **Source** — individual source selection from all configured sources.
- **Status** — Unread (default), All, Used, Snoozed, Dismissed.
- **⚡ Upcoming hearings** — toggle to show only briefs with a board hearing in the next 14 days.

Active filters appear as removable chips below the filter bar. Click × on a chip to remove that one filter. "Clear all" resets everything.

The sidebar shows an unread count badge next to Briefs.

### Archive (`/archive`)

All briefs ever created with the same filter bar (status defaults to All). Useful for finding a specific brief, checking what was covered in a past date range, or reviewing dismissed items.

### Sources (`/sources`)

A health table for every configured source. Shows last captured date, total item count, items in the last 7 days, and a green/red status dot. A source goes stale if it hasn't produced any captures in the past 3 days.

### Coverage Index (`/coverage`)

The deduplication catalogue: 2,897 published Florida YIMBY articles with extracted project name, address, developer, and architect. Searchable by project name or address. When a new brief's project matches an entry here, it's automatically marked `already_covered` and excluded from the Inbox.

### Logs (`/logs`)

Run history table (last 30 runs) with new captures, new briefs, duration, and error details. Also shows the last 100 lines of the most recent daily log file in a dark monospace viewer.

### Three-dot menu

Every card has a `⋯` button in the top-right corner. Options:

- **Mark as used** — moves the brief to used status and removes it from Inbox.
- **Snooze 24h** — hides the brief until the same time tomorrow.
- **Dismiss** — marks the brief dismissed with a reason: Not relevant, Already covered, Wrong market, Low priority, Duplicate, or Other.

All three actions show an **Undo** toast at the bottom of the screen. The toast stays for 5 seconds with a countdown bar. Clicking Undo calls the `/briefs/<id>/undo` endpoint and re-inserts the card at its original position.

### Card navigation

Clicking anywhere on a card (outside the three-dot menu) navigates to the full brief detail page. Browsers that support the View Transitions API (Chrome 111+, Safari 18+) get a shared-element morph animation. The back button returns to the previous page with the same animation in reverse.

### Brief detail page

Shows all 8 sections of the brief: Headline, Lede, Body, Fact Sheet, Sources, Confirmed vs Pending, Open Questions, Accuracy Score. The **Copy as WordPress HTML** button copies `<h2>` + `<p>` formatted output to the clipboard.

---

## Adding New Sources

### New RSS feed

Add an entry under `rss:` in `sources.yaml`:

```yaml
rss:
  - name: Source Display Name
    url: https://example.com/feed/
    tags: [florida, development]
```

The `name` field becomes the `source` value in `raw_captures`. Tags are metadata only — not used by the pipeline. Run `python scrape.py --rss-only` to test.

### New IQM2 board

Add an entry under `iqm2:` in `sources.yaml`:

```yaml
iqm2:
  - name: City of Example – Planning Board
    url: https://example.iqm2.com/Citizens/Calendar.aspx?cat=12
    board: Planning Board
    lookahead_days: 60
```

The `board` value must match the exact board name as it appears in the IQM2 HTML row. The default IQM2 selectors work for all standard `*.iqm2.com` installs; use the optional selector overrides if a municipality has customized its layout (documented in `sources.yaml` comments).

### New Legistar board

Add an entry under `legistar:` in `sources.yaml`:

```yaml
legistar:
  - name: Example City Planning Board
    url: https://examplecity.legistar.com/Calendar.aspx
    municipality: Example City
    board: Planning Board
    lookahead_days: 60
```

The `board` value is matched case-insensitively as a substring against the first column of each calendar row. The scraper auto-discovers column positions from table headers, so it is resilient to minor layout changes. Sites protected by Cloudflare WAF will be detected and skipped gracefully with a warning log.

---

## Prompt Files

### `prompts/classify.md`

System prompt for Claude Haiku 4.5. Receives the title and content of each raw capture and returns a JSON object with: `is_development_item`, `florida_relevance`, `project_name`, `address`, `city`, `developer`, `architect`, `units`, `height_ft`, `status`, `event_type`, `priority`, and a `reasoning` field. Includes detailed rules for what qualifies as a Florida development item (land deals, construction loans, rezoning applications, etc.) and what does not (resales, market analysis, corporate news). Priority thresholds: high = new project filing, major approval, or groundbreaking; medium = amendment or update; low = non-Florida or tangential mention.

### `prompts/draft_brief.md`

System prompt for Claude Opus 4.5. Receives structured fields from `extracted_items` plus the source content and writes a full 8-section research brief. Voice rules enforce the publication's style: lead with the observable fact, present tense for current status, no adjectives that aren't measurements, no enabling phrases ("clears the way," "paves the way"), cite specific numbers. The Accuracy Score section requires Opus to self-assess factual confidence on four dimensions. For IQM2 and Legistar items, the lede must include board name, hearing date, and application number.

### `prompts/extract_agenda.md`

System prompt for Haiku's PDF document API. Receives a municipal board meeting agenda as a PDF and returns a JSON array of individual project listings — one object per development item with agenda item number, project name, address, developer, architect, and description. Procedural items (approval of minutes, roll call, public comment) are filtered out. Used for both IQM2 and Legistar agenda PDFs.

### `prompts/ingest_archive.md`

System prompt for coverage index ingestion. Receives the title and body of a published Florida YIMBY article and extracts: `project_name`, `address`, `developer`, `architect`. Used by `process.py ingest-coverage` to build the deduplication catalogue from the site archive.

---

## Database Schema

### `raw_captures`
The raw intake table. One row per unique URL.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `source` | TEXT | Display name from `sources.yaml` |
| `url` | TEXT | Unique constraint — dedup happens here |
| `title` | TEXT | Article headline or project name |
| `content` | TEXT | Article body or project description |
| `captured_at` | TEXT | ISO datetime, defaults to now |
| `published_at` | TEXT | ISO date parsed from RSS feed |
| `processed` | INTEGER | 0 = not yet classified |
| `og_image_url` | TEXT | OG/Twitter card image URL or base64 data URL |
| `metadata_json` | TEXT | IQM2/Legistar hearing metadata: hearing_date, hearing_board, agenda_url, agenda_newly_posted |

### `extracted_items`
One row per classified item. Only FL development items flow into briefs.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `raw_capture_id` | INTEGER | FK → raw_captures |
| `project_name` | TEXT | Extracted by Haiku |
| `address` | TEXT | |
| `city` | TEXT | As stated in source text |
| `developer` | TEXT | |
| `architect` | TEXT | |
| `units` | INTEGER | Residential unit count |
| `height` | TEXT | Height in feet |
| `status` | TEXT | proposed / filed / approved / permitted / under_construction / topped_off / completed / unknown |
| `event_type` | TEXT | new_filing / approval / construction_milestone / amendment / completion / profile / other |
| `priority` | INTEGER | 3=high, 2=medium, 1=low |
| `is_development_item` | INTEGER | 0/1 |
| `florida_relevance` | INTEGER | 0/1 |
| `extracted_data_json` | TEXT | Full Haiku JSON output |
| `already_covered` | INTEGER | 0/1 — set by dedup step |
| `coverage_match_url` | TEXT | URL of matched published article |
| `market` | TEXT | City-level market: MIAMI, MIRAMAR, BOCA RATON, etc. |
| `county` | TEXT | Miami-Dade, Broward, Palm Beach, Hillsborough, etc. — drives the cascading county filter |
| `region` | TEXT | South Florida, Tampa Bay, Orlando Metro — drives the top-level region filter |

### `briefs`
One row per drafted brief.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `extracted_item_id` | INTEGER | FK → extracted_items |
| `headline` | TEXT | 8–12 word declarative sentence |
| `lede` | TEXT | 3–5 sentence paragraph |
| `body` | TEXT | 2–3 paragraphs |
| `fact_sheet_json` | TEXT | Bullet list of hard numbers |
| `sources` | TEXT | Newline-separated URLs |
| `confirmed_vs_pending` | TEXT | Two sub-lists |
| `open_questions` | TEXT | Missing facts + recommended next step |
| `accuracy_score` | REAL | 0–100 self-assessment by Opus |
| `status` | TEXT | new / used / dismissed / snoozed / pending |
| `created_at` | TEXT | ISO datetime |
| `hearing_date` | TEXT | ISO date from IQM2/Legistar metadata |
| `hearing_board` | TEXT | Board name from IQM2/Legistar metadata |
| `snoozed_until` | TEXT | ISO datetime when snooze expires |
| `dismiss_reason` | TEXT | not_relevant / already_covered / wrong_market / low_priority / duplicate / other |

### `coverage_index`
Catalogue of published articles used for deduplication.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `project_name` | TEXT | Extracted by Haiku |
| `address` | TEXT | |
| `developer` | TEXT | |
| `architect` | TEXT | |
| `article_url` | TEXT | Unique — the canonical published URL |
| `published_at` | TEXT | ISO date |

### `meetings`
State tracking for IQM2 and Legistar board meeting calendar entries.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `source` | TEXT | Source name from sources.yaml |
| `meeting_url` | TEXT | Unique — meeting detail URL |
| `board` | TEXT | Board name |
| `meeting_date` | TEXT | ISO date |
| `agenda_url` | TEXT | Full agenda packet URL |
| `municipality` | TEXT | City/county display name (e.g. "Tampa", "Coral Gables") |
| `first_seen_at` | TEXT | When the meeting was first discovered |
| `agenda_posted_at` | TEXT | When the agenda URL first appeared |

### `daily_log`
One row per pipeline run.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key |
| `run_date` | TEXT | ISO date |
| `new_captures` | INTEGER | Items added to raw_captures |
| `new_briefs` | INTEGER | Briefs generated |
| `errors_json` | TEXT | JSON object of step→error for any failures |
| `duration_seconds` | REAL | Wall-clock time for the full run |

---

## Cost Estimates

All estimates assume Claude Haiku 4.5 at $0.80/M input + $4.00/M output tokens and Claude Opus 4.5 at $15.00/M input + $75.00/M output tokens. Prompt caching (active on both Haiku and Opus system prompts) reduces repeated system prompt costs significantly.

### Daily steady-state (approximate)

| Step | Model | Typical calls/day | Estimated cost |
|---|---|---|---|
| Classification | Haiku | ~10 new items | ~$0.02 |
| Agenda extraction | Haiku | 0–2 per new agenda | ~$0.05 |
| Brief drafting | Opus | ~3 briefs | ~$0.30 |
| **Total** | | | **~$0.40/day** |

On heavy news days (multiple board agendas + several major stories), the daily cost can reach $1–2. Most days it is under $0.50.

> **Note:** Costs scale with the number of municipal agendas processed each day. Legistar boards can post large agenda packets (50–200 pages); when multiple boards post simultaneously, Haiku token usage increases proportionally. Days with 3+ active agendas from Coral Gables, Tampa, or St. Pete could add $0.20–0.50 to the daily total.

### One-time setup costs

| Task | Model | Notes | Cost |
|---|---|---|---|
| Coverage index backfill | Haiku | 2,897 articles, ~600 tokens each | ~$2.50 |

---

## Known Limitations

- **JavaScript-rendered sources** cannot be scraped without Playwright. The Melo Group projects page is in `html_scrape_deferred` for this reason. iBuild (Miami-Dade permit portal) and EnerGov (Miami-Dade pre-application portal) are also JavaScript-rendered.
- **Tampa, St. Petersburg, and Pompano Beach Legistar portals** are protected by Cloudflare WAF and currently inaccessible via automated scraping. The scraper infrastructure is fully in place for all 13 boards; accessing these sources requires Playwright-based headless browser automation (see Roadmap).
- **Coverage index scope**: The dedup catalogue is based on the full floridayimby.com archive, not just Oscar's byline — this is intentional but means it may suppress items covered by other staff writers that Oscar might want to cover independently.
- **Cron requires the laptop to be awake** at 10 AM. If the machine is asleep, the pipeline skips that day. Migration to a VPS would eliminate this.
- **Classified but not extracted**: Items where Haiku detects `florida_relevance=true` and `is_development_item=true` but cannot extract any structured fields will have null values throughout. These still get briefs drafted; the brief will have weaker fact sheets.
- **No real-time alerts**: The pipeline runs once a day. Breaking news between runs is not captured until the next morning.

---

## Roadmap

- **Playwright-based Legistar scraper** — Tampa, St. Petersburg, and Pompano Beach Legistar portals are blocked by Cloudflare WAF. A headless browser session using Playwright could bypass this; all 11 remaining boards would activate immediately.
- **EnerGov pre-application scraper** — Miami-Dade's pre-application portal (MiamiDade.gov/building) shows projects before any formal filing. Requires Playwright for JavaScript rendering.
- **Fort Lauderdale DRC** — The Fort Lauderdale Development Review Committee publishes meeting packets as PDFs directly on the city website. A targeted scraper with direct PDF monitoring is planned.
- **Event and asset type classification** — Add dedicated fields for `asset_type` (residential, office, hotel, retail, mixed-use, industrial) and `event_type` refinement to enable better inbox filtering without relying on text search.
- **Project and company knowledge graph** — A relationship layer linking developer entities, architect firms, and project addresses across briefs to enable cross-project intelligence ("this developer has 4 active projects in Brickell").
- **Metrics dashboard** — Conversion rate by source (briefs published vs. generated), source value scoring, market distribution charts.
- **VPS deployment** — Move the cron job to a Linux VPS for 24/7 operation, add a daily digest email, and expose the dashboard on a private URL rather than localhost.
