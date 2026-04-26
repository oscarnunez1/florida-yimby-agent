"""
Scrape RSS feeds and HTML pages listed in sources.yaml, inserting new items
into raw_captures. Deduplication is by URL (UNIQUE constraint + INSERT OR IGNORE).

Run standalone:
    python scrape.py               # RSS + HTML
    python scrape.py --rss-only    # RSS only
    python scrape.py --html-only   # HTML scrape only
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

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


if __name__ == "__main__":
    main()
