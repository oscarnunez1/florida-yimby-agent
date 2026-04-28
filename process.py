"""
Classify and extract structured data from raw_captures into extracted_items.

Commands:
    python process.py classify [--limit N] [--dry-run]
    python process.py status
    python process.py ingest-coverage [--limit N]
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import anthropic
import httpx
from dotenv import load_dotenv
from selectolax.parser import HTMLParser

from db import get_conn, init_db

load_dotenv()

LOG_PATH = Path(__file__).parent / "runs.log"
CLASSIFY_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"
INGEST_PROMPT_PATH = Path(__file__).parent / "prompts" / "ingest_archive.md"
PRIORITY_MAP = {"high": 3, "medium": 2, "low": 1}
CONTENT_TRUNCATE = 6000  # chars sent to Haiku; keeps cost low while covering most articles
MAX_TOKENS = 1024

ARCHIVE_BASE_URL = "https://floridayimby.com/author/oscar"
HTML_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 20
ARTICLE_BODY_CHARS = 2000  # enough for developer/architect to appear; keeps token cost low


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that models sometimes add despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]  # drop opening fence line
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()

fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

console_handler = logging.StreamHandler()
console_handler.setFormatter(fmt)

file_handler = logging.FileHandler(LOG_PATH)
file_handler.setFormatter(fmt)

logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])
log = logging.getLogger(__name__)


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    with get_conn() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM raw_captures WHERE processed = 0").fetchone()[0]
        extracted = conn.execute("SELECT COUNT(*) FROM extracted_items").fetchone()[0]
        fl_dev  = conn.execute(
            "SELECT COUNT(*) FROM extracted_items WHERE florida_relevance = 1 AND is_development_item = 1"
        ).fetchone()[0]
    log.info("raw_captures:    %d total, %d unprocessed", total, pending)
    log.info("extracted_items: %d total, %d FL development items", extracted, fl_dev)


# ── classify ──────────────────────────────────────────────────────────────────

def cmd_classify(limit: Optional[int], dry_run: bool, ids: Optional[list] = None) -> None:
    system_prompt = CLASSIFY_PROMPT_PATH.read_text()
    client = anthropic.Anthropic()

    _COLS = "id, source, url, title, content, metadata_json"

    with get_conn() as conn:
        if ids:
            placeholders = ",".join("?" * len(ids))
            query = f"SELECT {_COLS} FROM raw_captures WHERE id IN ({placeholders}) ORDER BY id"
            rows = conn.execute(query, ids).fetchall()
        else:
            query = f"SELECT {_COLS} FROM raw_captures WHERE processed = 0 ORDER BY id"
            if limit:
                query += f" LIMIT {limit}"
            rows = conn.execute(query).fetchall()

    if not rows:
        log.info("No unprocessed captures — nothing to do")
        return

    mode = "DRY RUN" if dry_run else "live"
    log.info("Classifying %d captures (%s)", len(rows), mode)

    ok = skipped = 0

    for row in rows:
        capture_id = row["id"]
        source     = row["source"]
        title      = (row["title"] or "").strip()
        content    = (row["content"] or "").strip()
        url        = row["url"]

        user_text = f"Source: {source}\nTitle: {title}\n\nContent: {content[:CONTENT_TRUNCATE]}"

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_text}],
            )
            raw = response.content[0].text.strip()
        except Exception as exc:
            log.error("API error  id=%d  %r  — %s", capture_id, title[:60], exc)
            skipped += 1
            continue

        clean = _strip_fences(raw)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            log.error("JSON parse error  id=%d  %r  — raw: %s", capture_id, title[:60], raw[:300])
            skipped += 1
            continue

        # Read capture metadata (IQM2 hearing fields, agenda_newly_posted flag)
        cap_meta = {}
        if row["metadata_json"]:
            try:
                cap_meta = json.loads(row["metadata_json"])
            except Exception:
                pass

        # Newly posted agendas are time-sensitive — force high priority
        if cap_meta.get("agenda_newly_posted") and data.get("priority") != "high":
            data["priority"] = "high"
            log.info("  priority → high  (agenda newly posted)")

        # Merge hearing fields into extracted_data_json so they flow to briefs
        if cap_meta.get("hearing_date"):
            data["hearing_date"] = cap_meta["hearing_date"]
        if cap_meta.get("hearing_board"):
            data["hearing_board"] = cap_meta["hearing_board"]

        clean = json.dumps(data)  # re-serialise with merged fields

        is_dev = bool(data.get("is_development_item"))
        fl_rel = bool(data.get("florida_relevance"))
        priority_str = data.get("priority", "low")

        log.info(
            "id=%-4d  dev=%-5s  fl=%-5s  priority=%-6s  [%s] %s",
            capture_id, is_dev, fl_rel, priority_str, source, title[:60],
        )

        if not dry_run:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO extracted_items (
                        raw_capture_id, project_name, address, city,
                        developer, architect, units, height,
                        status, event_type, priority,
                        is_development_item, florida_relevance, extracted_data_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        capture_id,
                        data.get("project_name"),
                        data.get("address"),
                        data.get("city"),
                        data.get("developer"),
                        data.get("architect"),
                        data.get("units"),
                        str(data["height_ft"]) if data.get("height_ft") else None,
                        data.get("status", "unknown"),
                        data.get("event_type", "other"),
                        PRIORITY_MAP.get(priority_str, 1),
                        1 if is_dev else 0,
                        1 if fl_rel else 0,
                        clean,
                    ),
                )
                conn.execute("UPDATE raw_captures SET processed = 1 WHERE id = ?", (capture_id,))

        ok += 1

    log.info("Done — %d classified, %d skipped", ok, skipped)
    if dry_run:
        log.info("Dry run — no writes to database")


# ── coverage index ingestion ──────────────────────────────────────────────────

def _scrape_archive_page(page: int) -> list[dict]:
    """Return article stubs from one page of the author archive."""
    url = ARCHIVE_BASE_URL if page == 1 else f"{ARCHIVE_BASE_URL}/page/{page}"
    r = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True,
                  headers={"User-Agent": HTML_USER_AGENT})
    r.raise_for_status()
    tree = HTMLParser(r.text)
    stubs = []
    for art in tree.css("article"):
        link_el = art.css_first("a[title]")
        time_el = art.css_first("time")
        if not link_el:
            continue
        href  = link_el.attributes.get("href", "").strip()
        title = link_el.attributes.get("title", "").strip()
        date  = (time_el.attributes.get("datetime", "") if time_el else "")[:10] or None
        if href:
            stubs.append({"url": href, "title": title, "published_at": date})
    return stubs


def _fetch_article_body(url: str) -> str:
    r = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True,
                  headers={"User-Agent": HTML_USER_AGENT})
    r.raise_for_status()
    el = HTMLParser(r.text).css_first(".entry-content")
    return el.text(strip=True)[:ARTICLE_BODY_CHARS] if el else ""


def _extract_coverage_fields(title: str, body: str, client: anthropic.Anthropic,
                              system_prompt: str) -> dict:
    user_text = f"Title: {title}\n\nBody:\n{body}"
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    return json.loads(_strip_fences(resp.content[0].text.strip()))


def cmd_ingest_coverage(limit: Optional[int]) -> None:
    """
    Walk floridayimby.com/author/oscar newest-first, extract structured fields
    from each unindexed article via Haiku, and insert into coverage_index.

    Stops early when it hits a fully-indexed archive page (incremental runs are fast).
    Use --limit N to cap API calls per invocation.
    """
    system_prompt = INGEST_PROMPT_PATH.read_text()
    client = anthropic.Anthropic()

    # ── Phase 1: collect unindexed article stubs ──────────────────────────────
    log.info("Crawling archive at %s", ARCHIVE_BASE_URL)
    stubs_to_process: list[dict] = []
    seen_urls: set[str] = set()  # guard against pagination-shift duplicates

    with get_conn() as conn:
        for page in range(1, 500):
            try:
                stubs = _scrape_archive_page(page)
            except Exception as exc:
                log.warning("Archive page %d failed — %s", page, exc)
                break

            if not stubs:
                log.info("Archive exhausted after page %d", page - 1)
                break

            new_on_page = 0
            for stub in stubs:
                if stub["url"] in seen_urls:
                    continue
                seen_urls.add(stub["url"])
                if not conn.execute("SELECT 1 FROM coverage_index WHERE article_url = ?",
                                    (stub["url"],)).fetchone():
                    stubs_to_process.append(stub)
                    new_on_page += 1

            log.info("Page %3d: %d/%d new articles", page, new_on_page, len(stubs))

            # Stop only once we have enough new articles for this run.
            # Do NOT stop on new_on_page == 0 — already-indexed pages are normal
            # during a multi-run backfill and must not abort the crawl.
            if limit and len(stubs_to_process) >= limit:
                break

    if not stubs_to_process:
        log.info("Coverage index is up to date — nothing to ingest")
        return

    batch = stubs_to_process[:limit] if limit else stubs_to_process
    remaining_after = len(stubs_to_process) - len(batch)
    log.info("Ingesting %d articles (%d unindexed total found)", len(batch), len(stubs_to_process))

    # ── Phase 2: fetch + extract + insert ─────────────────────────────────────
    ok = skipped = sparse = 0

    for stub in batch:
        url   = stub["url"]
        title = stub["title"]

        try:
            body = _fetch_article_body(url)
        except Exception as exc:
            log.warning("  SKIP fetch  %s — %s", url, exc)
            skipped += 1
            continue

        try:
            data = _extract_coverage_fields(title, body, client, system_prompt)
        except Exception as exc:
            log.warning("  SKIP extract  %s — %s", url, exc)
            skipped += 1
            continue

        null_count = sum(1 for f in ("project_name", "address", "developer", "architect")
                         if not data.get(f))
        label = "SPARSE" if null_count >= 3 else "ok"
        if label == "SPARSE":
            sparse += 1

        log.info("  %s  nulls=%d  [%s]  %s",
                 label, null_count, stub.get("published_at", ""), title[:60])

        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO coverage_index
                       (project_name, address, developer, architect, article_url, published_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (data.get("project_name"), data.get("address"),
                 data.get("developer"), data.get("architect"),
                 url, stub.get("published_at")),
            )
        ok += 1

    log.info("Done — %d indexed, %d skipped, %d sparse", ok, skipped, sparse)
    if remaining_after:
        log.info("%d articles still unindexed — run again to continue backfill", remaining_after)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    init_db()

    parser = argparse.ArgumentParser(description="Process raw captures into extracted items.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show capture and extraction counts")

    p_classify = sub.add_parser("classify", help="Classify unprocessed captures with Haiku")
    p_classify.add_argument("--limit", type=int, default=None, metavar="N",
                            help="Process at most N items (for test runs)")
    p_classify.add_argument("--ids", type=str, default=None, metavar="ID,ID,...",
                            help="Comma-separated raw_capture IDs to (re-)classify, ignoring processed flag")
    p_classify.add_argument("--dry-run", action="store_true",
                            help="Classify without writing to the database")

    p_ingest = sub.add_parser("ingest-coverage",
                               help="Backfill coverage_index from floridayimby.com/author/oscar")
    p_ingest.add_argument("--limit", type=int, default=None, metavar="N",
                          help="Process at most N articles per run (omit to ingest all)")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "classify":
        ids = [int(i.strip()) for i in args.ids.split(",")] if args.ids else None
        cmd_classify(limit=args.limit, dry_run=args.dry_run, ids=ids)
    elif args.command == "ingest-coverage":
        cmd_ingest_coverage(limit=args.limit)


if __name__ == "__main__":
    main()
