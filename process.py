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

import re

import anthropic
import httpx
from dotenv import load_dotenv
from rapidfuzz import fuzz
from rapidfuzz import process as rfprocess
from selectolax.parser import HTMLParser

from db import get_conn, init_db

load_dotenv()

LOG_PATH = Path(__file__).parent / "runs.log"
CLASSIFY_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"
INGEST_PROMPT_PATH = Path(__file__).parent / "prompts" / "ingest_archive.md"
PRIORITY_MAP = {"high": 3, "medium": 2, "low": 1}
CONTENT_TRUNCATE = 6000  # chars sent to Haiku; keeps cost low while covering most articles
MAX_TOKENS = 1024

DRAFT_PROMPT_PATH = Path(__file__).parent / "prompts" / "draft_brief.md"
WP_API_URL = "https://floridayimby.com/wp-json/wp/v2/posts"

# ── Market detection ──────────────────────────────────────────────────────────

_MARKET_PATTERNS: list[tuple[str, list[str]]] = [
    ("MIAMI",           ["miami"]),
    ("TAMPA",           ["tampa"]),
    ("ST. PETE",        ["st. pete", "st pete", "saint pete", "pinellas", "clearwater", "dunedin", "largo"]),
    ("ORLANDO",         ["orlando", "kissimmee", "sanford", "lake nona"]),
    ("WEST PALM",       ["west palm", "palm beach", "boca raton", "delray beach", "boynton beach"]),
    ("FORT LAUDERDALE", ["fort lauderdale", "ft. lauderdale", "ft lauderdale"]),
    ("BROWARD",         ["broward", "pompano", "deerfield beach", "coral springs", "davie, fl", "miramar, fl",
                          "pembroke pines", "hallandale", "sunrise, fl", "plantation, fl", "tamarac", "weston, fl"]),
    ("STATEWIDE",       ["statewide"]),
]

_STATEWIDE_SOURCES = {"floridian development"}


def detect_market(city: Optional[str], address: Optional[str],
                  source: Optional[str] = None,
                  hearing_board: Optional[str] = None) -> str:
    """Return one of the canonical market values for an extracted item."""
    # All current IQM2 boards are Miami
    if hearing_board:
        return "MIAMI"

    text = " ".join(x.lower() for x in [city or "", address or ""] if x).strip()

    for market, patterns in _MARKET_PATTERNS:
        if any(p in text for p in patterns):
            return market

    if source and source.lower() in _STATEWIDE_SOURCES:
        return "STATEWIDE"

    return "OTHER"
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
        market = detect_market(
            data.get("city"),
            data.get("address"),
            source=source,
            hearing_board=data.get("hearing_board"),
        )

        log.info(
            "id=%-4d  dev=%-5s  fl=%-5s  priority=%-6s  market=%-15s  [%s] %s",
            capture_id, is_dev, fl_rel, priority_str, market, source, title[:60],
        )

        if not dry_run:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO extracted_items (
                        raw_capture_id, project_name, address, city,
                        developer, architect, units, height,
                        status, event_type, priority,
                        is_development_item, florida_relevance, extracted_data_json,
                        market
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        market,
                    ),
                )
                conn.execute("UPDATE raw_captures SET processed = 1 WHERE id = ?", (capture_id,))

        ok += 1

    log.info("Done — %d classified, %d skipped", ok, skipped)
    if dry_run:
        log.info("Dry run — no writes to database")


# ── dedup ─────────────────────────────────────────────────────────────────────

def _addr_score(a: str, b: str) -> float:
    """
    Address similarity that handles multi-parcel assemblages.
    token_sort_ratio fails when one side lists several parcels ("2401, 2405, and 2525 Lake Drive")
    and the other names only one ("2525 Lake Drive"). partial_ratio catches that case because
    the shorter string is a substring of the longer one. We take the max of both.
    """
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b))


def cmd_dedup() -> None:
    """
    Fuzzy-match every FL dev extracted_item against coverage_index.
    Sets already_covered=1 + coverage_match_url when a match is found.

    Matching strategy:
    - Primary: project_name token_sort_ratio >= 85, then address check if both sides have one.
    - Address check: max(token_sort_ratio, partial_ratio) >= 85. partial_ratio catches
      multi-parcel assemblages where one address is a substring of the other.
    - Fallback: if name score < 85 but address-alone score >= 90, match on address.
      Catches projects where the source uses a different name than Oscar's published article.
    """
    with get_conn() as conn:
        coverage = conn.execute(
            "SELECT project_name, address, article_url FROM coverage_index WHERE project_name IS NOT NULL"
        ).fetchall()

        if not coverage:
            log.info("Coverage index is empty — run ingest-coverage first")
            return

        cov_names   = [r["project_name"] for r in coverage]
        cov_addrs   = [(r["address"] or "").strip() for r in coverage]

        items = conn.execute("""
            SELECT id, project_name, address
            FROM extracted_items
            WHERE is_development_item = 1 AND florida_relevance = 1
            ORDER BY id
        """).fetchall()

        log.info("Deduping %d FL dev items against %d coverage entries", len(items), len(coverage))

        matched = unmatched = skipped = 0

        for item in items:
            pname   = (item["project_name"] or "").strip()
            address = (item["address"] or "").strip()

            if not pname:
                conn.execute("UPDATE extracted_items SET already_covered=0 WHERE id=?", (item["id"],))
                skipped += 1
                continue

            # ── Primary path: name match ──────────────────────────────────────
            result = rfprocess.extractOne(
                pname, cov_names,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=85,
            )

            if result is not None:
                _, name_score, idx = result
                cov_row  = coverage[idx]
                cov_addr = cov_addrs[idx]

                # Confirm with address when both sides have one.
                if address and cov_addr:
                    ascore = _addr_score(address, cov_addr)
                    if ascore < 85:
                        conn.execute("UPDATE extracted_items SET already_covered=0 WHERE id=?", (item["id"],))
                        unmatched += 1
                        log.info("  near-miss  id=%-4d  name=%d  addr=%d  [%s]",
                                 item["id"], name_score, ascore, pname[:50])
                        continue

                conn.execute(
                    "UPDATE extracted_items SET already_covered=1, coverage_match_url=? WHERE id=?",
                    (cov_row["article_url"], item["id"]),
                )
                log.info("  MATCH(name)  id=%-4d  score=%d  [%s]  →  %s",
                         item["id"], name_score, pname[:40], cov_row["article_url"].split("/")[-1][:60])
                matched += 1
                continue

            # ── Fallback: address-alone match ─────────────────────────────────
            # Catches projects where source uses a different name than Oscar's article.
            if address:
                best_addr_score = 0.0
                best_idx = -1
                for idx, cov_addr in enumerate(cov_addrs):
                    if not cov_addr:
                        continue
                    s = _addr_score(address, cov_addr)
                    if s > best_addr_score:
                        best_addr_score = s
                        best_idx = idx

                if best_addr_score >= 90 and best_idx >= 0:
                    cov_row = coverage[best_idx]
                    conn.execute(
                        "UPDATE extracted_items SET already_covered=1, coverage_match_url=? WHERE id=?",
                        (cov_row["article_url"], item["id"]),
                    )
                    log.info("  MATCH(addr)  id=%-4d  addr_score=%d  [%s]  →  %s",
                             item["id"], best_addr_score, pname[:40], cov_row["article_url"].split("/")[-1][:60])
                    matched += 1
                    continue

            conn.execute("UPDATE extracted_items SET already_covered=0 WHERE id=?", (item["id"],))
            unmatched += 1

    log.info("Dedup done — %d already covered, %d new, %d skipped (no project_name)",
             matched, unmatched, skipped)


# ── brief drafting ────────────────────────────────────────────────────────────

_SECTION_RE = re.compile(
    r"^##\s*(HEADLINE|LEDE|BODY|FACT SHEET|SOURCES|CONFIRMED VS PENDING|OPEN QUESTIONS|ACCURACY SCORE)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_brief_sections(text: str) -> dict[str, str]:
    """Split Opus output on ## SECTION headers into a dict keyed by header name."""
    parts = _SECTION_RE.split(text)
    # parts = [preamble, key1, body1, key2, body2, ...]
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip().upper()] = parts[i + 1].strip()
    return sections


def _extract_accuracy_score(text: str) -> Optional[float]:
    m = re.search(r"\b(\d{1,3})\b", text or "")
    return float(m.group(1)) if m else None


def cmd_draft_briefs(limit: Optional[int]) -> None:
    """
    For each unmatched FL dev item with no existing brief, call Opus to generate
    an 8-section research brief in Michael Young's editorial voice.
    """
    system_prompt = DRAFT_PROMPT_PATH.read_text()
    client = anthropic.Anthropic()

    with get_conn() as conn:
        query = """
            SELECT ei.id, ei.project_name, ei.address, ei.city, ei.developer,
                   ei.architect, ei.units, ei.height, ei.status, ei.event_type,
                   ei.priority, ei.extracted_data_json,
                   rc.content  AS source_content,
                   rc.url      AS source_url,
                   rc.source   AS source_name
            FROM extracted_items ei
            JOIN raw_captures rc ON rc.id = ei.raw_capture_id
            WHERE ei.is_development_item  = 1
              AND ei.florida_relevance    = 1
              AND ei.already_covered      = 0
              AND NOT EXISTS (
                  SELECT 1 FROM briefs b WHERE b.extracted_item_id = ei.id
              )
            ORDER BY ei.priority DESC, ei.id
        """
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()

    if not rows:
        log.info("No eligible items — run dedup first or all briefs already drafted")
        return

    log.info("Drafting briefs for %d items", len(rows))
    ok = skipped = 0

    for row in rows:
        item_id = row["id"]
        data    = json.loads(row["extracted_data_json"] or "{}")

        # Build the user message: structured data + source content
        user_parts = [
            f"Project: {row['project_name'] or 'Unknown'}",
            f"Address: {row['address'] or 'Unknown'}",
            f"City: {row['city'] or data.get('city') or 'Unknown'}",
            f"Developer: {row['developer'] or 'Unknown'}",
            f"Architect: {row['architect'] or 'Unknown'}",
            f"Units: {row['units'] or data.get('units') or 'Unknown'}",
            f"Height: {row['height'] or data.get('height_ft') or 'Unknown'} ft",
            f"Status: {row['status'] or 'Unknown'}",
            f"Event type: {row['event_type'] or 'Unknown'}",
            f"Source: {row['source_name']}",
            f"Source URL: {row['source_url']}",
        ]
        hearing_date  = data.get("hearing_date")
        hearing_board = data.get("hearing_board")
        if hearing_date:
            user_parts.append(f"Hearing date: {hearing_date}")
        if hearing_board:
            user_parts.append(f"Hearing board: {hearing_board}")

        source_content = (row["source_content"] or "").strip()
        if source_content:
            user_parts.append(f"\nSource content:\n{source_content[:4000]}")

        user_text = "\n".join(user_parts)

        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=2048,
                system=[{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_text}],
            )
            raw = response.content[0].text.strip()
        except Exception as exc:
            log.error("API error  id=%d  %r  — %s", item_id, row["project_name"], exc)
            skipped += 1
            continue

        sections = _parse_brief_sections(raw)
        if not sections:
            log.error("Parse error  id=%d — no sections found in:\n%s", item_id, raw[:300])
            skipped += 1
            continue

        headline = sections.get("HEADLINE", "")
        lede     = sections.get("LEDE", "")
        log.info("  id=%-4d  [%s]  %s", item_id, row["project_name"] or "?", headline[:70])

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO briefs
                       (extracted_item_id, headline, lede, body, fact_sheet_json,
                        sources, confirmed_vs_pending, open_questions, accuracy_score,
                        hearing_date, hearing_board, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')""",
                (
                    item_id,
                    headline,
                    lede,
                    sections.get("BODY", ""),
                    sections.get("FACT SHEET", ""),
                    sections.get("SOURCES", ""),
                    sections.get("CONFIRMED VS PENDING", ""),
                    sections.get("OPEN QUESTIONS", ""),
                    _extract_accuracy_score(sections.get("ACCURACY SCORE", "")),
                    hearing_date,
                    hearing_board,
                ),
            )
        ok += 1

    log.info("Done — %d briefs drafted, %d skipped", ok, skipped)


# ── coverage index ingestion ──────────────────────────────────────────────────

def _fetch_wp_api_page(page: int) -> tuple[list[dict], int]:
    """Fetch one page of all posts from the WordPress REST API.
    Returns (stubs, total_pages). Content is extracted from content.rendered (HTML stripped).
    The API returns posts newest-first, includes all authors.
    """
    r = httpx.get(
        WP_API_URL,
        params={"per_page": 100, "page": page, "_fields": "id,link,title,date,content"},
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": HTML_USER_AGENT},
    )
    r.raise_for_status()
    total_pages = int(r.headers.get("X-WP-TotalPages", 1))
    stubs = []
    for post in r.json():
        raw_html = (post.get("content") or {}).get("rendered") or ""
        body     = HTMLParser(raw_html).text(strip=True)[:ARTICLE_BODY_CHARS]
        title_html = (post.get("title") or {}).get("rendered") or ""
        stubs.append({
            "url":          (post.get("link") or "").strip(),
            "title":        HTMLParser(title_html).text(strip=True),
            "published_at": ((post.get("date") or "")[:10]) or None,
            "body":         body,
        })
    return stubs, total_pages


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
    raw = json.loads(_strip_fences(resp.content[0].text.strip()))
    if isinstance(raw, list):
        # Haiku occasionally wraps the object in an array — take the first element.
        if not raw:
            raise ValueError("Haiku returned an empty JSON array")
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ValueError(f"Unexpected JSON type from Haiku: {type(raw).__name__}")
    return raw


def cmd_ingest_coverage(limit: Optional[int]) -> None:
    """
    Walk the full floridayimby.com post archive via the WordPress REST API,
    extract structured fields from each unindexed article via Haiku, and insert
    into coverage_index. Covers all staff writers, not just Oscar's byline.
    Article body comes inline from the API — no per-article HTTP fetch needed.
    """
    system_prompt = INGEST_PROMPT_PATH.read_text()
    client = anthropic.Anthropic()

    # ── Phase 1: collect unindexed article stubs ──────────────────────────────
    log.info("Crawling site archive via WordPress REST API at %s", WP_API_URL)
    stubs_to_process: list[dict] = []
    seen_urls: set[str] = set()
    total_pages: Optional[int] = None

    with get_conn() as conn:
        for page in range(1, 10_000):
            try:
                stubs, total_pages = _fetch_wp_api_page(page)
            except Exception as exc:
                log.warning("API page %d failed — %s", page, exc)
                break

            if not stubs:
                log.info("Archive exhausted after page %d", page - 1)
                break

            new_on_page = 0
            for stub in stubs:
                if not stub["url"] or stub["url"] in seen_urls:
                    continue
                seen_urls.add(stub["url"])
                if not conn.execute("SELECT 1 FROM coverage_index WHERE article_url = ?",
                                    (stub["url"],)).fetchone():
                    stubs_to_process.append(stub)
                    new_on_page += 1

            log.info("Page %3d/%s: %d/%d new articles",
                     page, total_pages or "?", new_on_page, len(stubs))

            if total_pages and page >= total_pages:
                break
            if limit and len(stubs_to_process) >= limit:
                break

    if not stubs_to_process:
        log.info("Coverage index is up to date — nothing to ingest")
        return

    batch = stubs_to_process[:limit] if limit else stubs_to_process
    remaining_after = len(stubs_to_process) - len(batch)
    log.info("Ingesting %d articles (%d unindexed total found)", len(batch), len(stubs_to_process))

    # ── Phase 2: extract fields + insert ──────────────────────────────────────
    # Body is already in the stub from the REST API — no per-article fetch needed.
    ok = skipped = sparse = 0

    for stub in batch:
        url   = stub["url"]
        title = stub["title"]
        body  = stub["body"]

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

    sub.add_parser("dedup", help="Fuzzy-match extracted_items against coverage_index")

    p_draft = sub.add_parser("draft-briefs", help="Draft briefs for uncovered FL dev items using Opus")
    p_draft.add_argument("--limit", type=int, default=None, metavar="N",
                         help="Draft at most N briefs per run")

    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "classify":
        ids = [int(i.strip()) for i in args.ids.split(",")] if args.ids else None
        cmd_classify(limit=args.limit, dry_run=args.dry_run, ids=ids)
    elif args.command == "ingest-coverage":
        cmd_ingest_coverage(limit=args.limit)
    elif args.command == "dedup":
        cmd_dedup()
    elif args.command == "draft-briefs":
        cmd_draft_briefs(limit=args.limit)


if __name__ == "__main__":
    main()
