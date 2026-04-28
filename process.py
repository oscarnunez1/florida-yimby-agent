"""
Classify and extract structured data from raw_captures into extracted_items.

Commands:
    python process.py classify [--limit N] [--dry-run]
    python process.py status
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

from db import get_conn, init_db

load_dotenv()

LOG_PATH = Path(__file__).parent / "runs.log"
CLASSIFY_PROMPT_PATH = Path(__file__).parent / "prompts" / "classify.md"
PRIORITY_MAP = {"high": 3, "medium": 2, "low": 1}
CONTENT_TRUNCATE = 6000  # chars sent to Haiku; keeps cost low while covering most articles
MAX_TOKENS = 1024


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

    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "classify":
        ids = [int(i.strip()) for i in args.ids.split(",")] if args.ids else None
        cmd_classify(limit=args.limit, dry_run=args.dry_run, ids=ids)


if __name__ == "__main__":
    main()
