"""
Scrape RSS feeds and HTML pages listed in sources.yaml, inserting new items
into raw_captures. Deduplication is by URL (UNIQUE constraint + INSERT OR IGNORE).

Run standalone:
    python scrape.py               # RSS + HTML
    python scrape.py --rss-only    # RSS only
    python scrape.py --html-only   # HTML scrape only
"""

import argparse
import base64
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import anthropic
import feedparser
import httpx
import yaml
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from db import get_conn, init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).parent / "sources.yaml"
EXTRACT_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract_agenda.md"
REQUEST_TIMEOUT = 20
RSS_USER_AGENT = "FloridaYIMBY-Agent/1.0 (+https://floridayimby.com)"
HTML_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_sources() -> dict:
    with open(SOURCES_PATH) as f:
        return yaml.safe_load(f)


# ── RSS ───────────────────────────────────────────────────────────────────────

RSS_CONTENT_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": RSS_USER_AGENT},
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type not in RSS_CONTENT_TYPES:
        raise ValueError(f"unexpected content-type {content_type!r} — expected RSS/XML")

    return feedparser.parse(resp.text)


def entry_content(entry: feedparser.util.FeedParserDict) -> str:
    if entry.get("content"):
        return entry.content[0].get("value", "")
    return entry.get("summary", "")


def scrape_rss_sources(sources: list[dict]) -> tuple[int, int]:
    """Fetch each RSS source and upsert new entries. Returns (seen, inserted)."""
    total_seen = 0
    total_inserted = 0

    with get_conn() as conn:
        for source in sources:
            name = source["name"]
            url = source["url"]
            log.info("Fetching  %s  (%s)", name, url)

            try:
                feed = fetch_feed(url)
            except Exception as exc:
                log.warning("  SKIP  %s — %s", name, exc)
                continue

            entries = feed.get("entries", [])
            log.info("  %d entries found", len(entries))
            total_seen += len(entries)

            inserted_count = 0
            for entry in entries:
                title = entry.get("title", "").strip()
                entry_url = entry.get("link", "").strip()
                content = entry_content(entry)

                if not entry_url:
                    continue

                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_captures (source, url, title, content) VALUES (?, ?, ?, ?)",
                    (name, entry_url, title, content),
                )
                if cur.rowcount:
                    inserted_count += 1

            total_inserted += inserted_count
            log.info("  %d new  /  %d skipped (already seen)", inserted_count, len(entries) - inserted_count)

    return total_seen, total_inserted


# ── HTML scrape ───────────────────────────────────────────────────────────────

def scrape_html_sources(sources: list[dict]) -> tuple[int, int]:
    """
    Fetch each HTML page and extract project items using configured CSS selectors.
    Sources flagged js_rendered=true are skipped with a warning.
    Returns (seen, inserted).
    """
    total_seen = 0
    total_inserted = 0

    with get_conn() as conn:
        for source in sources:
            name = source["name"]
            url = source["url"]

            if source.get("js_rendered"):
                log.warning("SKIP  %s — JS-rendered, deferred to v2 (Playwright)", name)
                continue

            item_sel = source["item_selector"]
            title_sel = source["title_selector"]
            link_sel = source.get("link_selector", "a")

            log.info("Fetching  %s  (%s)", name, url)

            try:
                r = httpx.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": HTML_USER_AGENT},
                )
                r.raise_for_status()
            except Exception as exc:
                log.warning("  SKIP  %s — %s", name, exc)
                continue

            tree = HTMLParser(r.text)
            items = tree.css(item_sel)
            log.info("  %d items matched %r", len(items), item_sel)

            if not items:
                log.warning("  No items found — page may be JS-rendered or selector is wrong")
                continue

            total_seen += len(items)
            inserted_count = 0

            for item in items:
                title_el = item.css_first(title_sel)
                link_el = item.css_first(link_sel)

                title = title_el.text(strip=True) if title_el else ""
                href = link_el.attributes.get("href", "") if link_el else ""

                if not title or not href:
                    continue

                abs_url = urljoin(url, href)

                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_captures (source, url, title, content) VALUES (?, ?, ?, ?)",
                    (name, abs_url, title, ""),
                )
                if cur.rowcount:
                    inserted_count += 1

            total_inserted += inserted_count
            log.info("  %d new  /  %d skipped (already seen)", inserted_count, len(items) - inserted_count)

    return total_seen, total_inserted


# ── WordPress REST API ────────────────────────────────────────────────────────

def scrape_wp_rest_sources(sources: list[dict]) -> tuple[int, int]:
    """
    Fetch WordPress CPT endpoints and insert project items into raw_captures.
    Returns (seen, inserted).
    """
    total_seen = 0
    total_inserted = 0

    with get_conn() as conn:
        for source in sources:
            name = source["name"]
            base_url = source["url"]
            per_page = source.get("per_page", 100)

            url = f"{base_url}?per_page={per_page}"
            log.info("Fetching  %s  (%s)", name, url)

            try:
                r = httpx.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": HTML_USER_AGENT},
                )
                r.raise_for_status()
                items = r.json()
            except Exception as exc:
                log.warning("  SKIP  %s — %s", name, exc)
                continue

            if not isinstance(items, list):
                log.warning("  Unexpected response format from %s", url)
                continue

            log.info("  %d items found", len(items))
            total_seen += len(items)
            inserted_count = 0

            for item in items:
                title = item.get("title", {}).get("rendered", "").strip()
                item_url = item.get("link", "").strip()

                if not title or not item_url:
                    continue

                cur = conn.execute(
                    "INSERT OR IGNORE INTO raw_captures (source, url, title, content) VALUES (?, ?, ?, ?)",
                    (name, item_url, title, ""),
                )
                if cur.rowcount:
                    inserted_count += 1

            total_inserted += inserted_count
            log.info("  %d new  /  %d skipped (already seen)", inserted_count, len(items) - inserted_count)

    return total_seen, total_inserted


# ── IQM2 government calendar ─────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _extract_agenda_projects(
    text_agenda_url: str,
    board: str,
    meeting_date: Optional[str],
) -> list[dict]:
    """
    Fetch the plain-agenda PDF (Type=14, ~300 KB) and extract individual project
    listings via Haiku's PDF document API.

    Returns a list of project dicts (may be empty if no development items found).
    """
    try:
        r = httpx.get(
            text_agenda_url,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": HTML_USER_AGENT},
        )
        r.raise_for_status()
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ct and ct != "application/pdf":
            log.warning("  Agenda is not a PDF (got %r) — skipping  %s", ct, text_agenda_url)
            return []
    except Exception as exc:
        log.warning("  PDF fetch failed  %s — %s", text_agenda_url, exc)
        return []

    pdf_b64 = base64.standard_b64encode(r.content).decode()
    system_prompt = EXTRACT_PROMPT_PATH.read_text()

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Board: {board}\n"
                            f"Meeting date: {meeting_date or 'unknown'}\n\n"
                            "Extract all development project listings from this agenda."
                        ),
                    },
                ],
            }],
        )
        raw = response.content[0].text.strip()
    except Exception as exc:
        log.error("  Haiku extraction failed — %s", exc)
        return []

    clean = _strip_fences(raw)
    try:
        projects = json.loads(clean)
    except json.JSONDecodeError:
        log.error("  JSON parse error for agenda extraction — raw: %s", raw[:300])
        return []

    if not isinstance(projects, list):
        log.warning("  Unexpected non-list response from agenda extraction")
        return []

    log.info("  Extracted %d project(s) from agenda", len(projects))
    return projects


def scrape_iqm2_sources(sources: list[dict]) -> tuple[int, int]:
    """
    Scrape IQM2-powered government meeting calendar pages (miamifl.iqm2.com layout).

    Tracks each board's meetings in the `meetings` table. Creates a raw_capture only when:
      - A meeting with an agenda packet is newly discovered, OR
      - A previously-tracked meeting just had its agenda posted.

    The raw_capture's metadata_json carries:
      hearing_date, hearing_board, agenda_url, agenda_newly_posted

    agenda_newly_posted: true tells the classifier to force priority=high.

    Returns (meetings_checked, captures_inserted).
    """
    total_checked = 0
    total_inserted = 0

    with get_conn() as conn:
        for source in sources:
            name         = source["name"]
            calendar_url = source["url"]
            board        = source.get("board", name)
            lookahead    = source.get("lookahead_days", 60)

            # Selectors for miamifl.iqm2.com — override per-source if needed.
            row_sel    = source.get("row_selector",    ".MeetingRow")
            detail_sel = source.get("detail_selector", ".RowLink a")
            board_sel  = source.get("board_selector",  ".RowDetails")

            log.info("Fetching  %s  (%s)", name, calendar_url)

            try:
                r = httpx.get(
                    calendar_url,
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": HTML_USER_AGENT},
                )
                r.raise_for_status()
            except Exception as exc:
                log.warning("  SKIP  %s — %s", name, exc)
                continue

            tree = HTMLParser(r.text)
            rows = tree.css(row_sel)
            log.info("  %d total rows on page; filtering for %r within %d days", len(rows), board, lookahead)

            today  = datetime.now(timezone.utc).date()
            cutoff = today + timedelta(days=lookahead)

            for row in rows:
                # Board filter: skip rows that don't belong to this source's board.
                board_el   = row.css_first(board_sel)
                board_text = board_el.text(strip=True) if board_el else ""
                if board not in board_text:
                    continue

                detail_el = row.css_first(detail_sel)
                if not detail_el:
                    continue

                detail_href = detail_el.attributes.get("href", "").strip()
                if not detail_href:
                    continue

                meeting_url = urljoin(calendar_url, detail_href)
                date_text   = detail_el.text(strip=True)

                # Parse "Jan 5, 2026 9:00 AM" → ISO date string and date object.
                meeting_date = None
                meeting_day  = None
                try:
                    dt = datetime.strptime(date_text, "%b %d, %Y %I:%M %p")
                    meeting_day  = dt.date()
                    meeting_date = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

                # Skip meetings beyond the lookahead window.
                if meeting_day and meeting_day > cutoff:
                    continue

                total_checked += 1

                # Agenda Packet (Type=1) for state-diffing; plain Agenda (Type=14) for extraction.
                agenda_url      = None  # full packet — tracked in meetings table
                text_agenda_url = None  # plain agenda PDF (~300 KB) — used for Haiku extraction
                for a in (row.css(".MeetingLinks a") or []):
                    href = a.attributes.get("href", "") or ""
                    if "Type=1&" in href or href.endswith("Type=1"):
                        agenda_url = urljoin(calendar_url, href)
                    elif "Type=14&" in href or href.endswith("Type=14"):
                        abs_href = urljoin(calendar_url, href)
                        # FileView.aspx returns an HTML viewer — rewrite to FileOpen.aspx with Inline=True
                        if "FileView.aspx" in abs_href and "Inline=True" not in abs_href:
                            abs_href = abs_href.replace("FileView.aspx", "FileOpen.aspx") + "&Inline=True"
                        text_agenda_url = abs_href
                    if agenda_url and text_agenda_url:
                        break

                # Diff against prior state.
                prior = conn.execute(
                    "SELECT agenda_url FROM meetings WHERE meeting_url = ?",
                    (meeting_url,)
                ).fetchone()

                agenda_newly_posted = False
                should_capture      = False

                if prior is None:
                    conn.execute(
                        """INSERT INTO meetings
                               (source, meeting_url, board, meeting_date,
                                agenda_url, agenda_posted_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            name, meeting_url, board, meeting_date,
                            agenda_url,
                            datetime.now(timezone.utc).isoformat() if agenda_url else None,
                        ),
                    )
                    if agenda_url:
                        should_capture = True

                elif prior["agenda_url"] is None and agenda_url:
                    agenda_newly_posted = True
                    should_capture      = True
                    conn.execute(
                        """UPDATE meetings
                           SET agenda_url = ?, agenda_posted_at = datetime('now')
                           WHERE meeting_url = ?""",
                        (agenda_url, meeting_url),
                    )
                    log.info("  AGENDA NEWLY POSTED  [%s]  %s", board, meeting_date or "")

                if should_capture and text_agenda_url:
                    projects = _extract_agenda_projects(text_agenda_url, board, meeting_date)
                    for project in projects:
                        item_num  = str(project.get("agenda_item_number") or "")
                        item_url  = f"{meeting_url}#item-{item_num}" if item_num else meeting_url

                        pname   = (project.get("project_name") or "").strip()
                        address = (project.get("address") or "").strip()
                        title   = f"{pname} – {address}" if pname and address else pname or address
                        if not title:
                            title = f"{board} – {meeting_date} – Item {item_num}"

                        metadata = json.dumps({
                            "hearing_date":        meeting_date,
                            "hearing_board":       board,
                            "agenda_url":          agenda_url,
                            "agenda_item_number":  item_num or None,
                            "agenda_newly_posted": agenda_newly_posted,
                        })
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO raw_captures
                                   (source, url, title, content, metadata_json)
                               VALUES (?, ?, ?, ?, ?)""",
                            (
                                name,
                                item_url,
                                title,
                                (project.get("description") or "").strip(),
                                metadata,
                            ),
                        )
                        if cur.rowcount:
                            total_inserted += 1

    return total_checked, total_inserted


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape RSS and HTML sources into raw_captures.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rss-only", action="store_true", help="Run RSS sources only")
    group.add_argument("--html-only", action="store_true", help="Run HTML scrape sources only")
    args = parser.parse_args()

    init_db()
    sources = load_sources()

    run_rss = not args.html_only
    run_html = not args.rss_only

    if run_rss:
        rss_sources = sources.get("rss", [])
        log.info("── RSS scrape — %d sources ──", len(rss_sources))
        seen, inserted = scrape_rss_sources(rss_sources)
        log.info("RSS done — %d seen, %d new", seen, inserted)

    if run_html:
        html_sources = sources.get("html_scrape", [])
        log.info("── HTML scrape — %d sources ──", len(html_sources))
        seen, inserted = scrape_html_sources(html_sources)
        log.info("HTML done — %d seen, %d new", seen, inserted)

        wp_rest_sources = sources.get("wp_rest", [])
        log.info("── WP REST scrape — %d sources ──", len(wp_rest_sources))
        seen, inserted = scrape_wp_rest_sources(wp_rest_sources)
        log.info("WP REST done — %d seen, %d new", seen, inserted)

        iqm2_sources = sources.get("iqm2", [])
        log.info("── IQM2 calendar scrape — %d sources ──", len(iqm2_sources))
        checked, inserted = scrape_iqm2_sources(iqm2_sources)
        log.info("IQM2 done — %d meetings checked, %d new captures", checked, inserted)


if __name__ == "__main__":
    main()
