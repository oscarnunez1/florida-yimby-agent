"""
Daily orchestration script for the Florida YIMBY research agent.

Runs in sequence:
  1. scrape.py          — fetch all sources
  2. process.py classify — classify new captures
  3. process.py dedup    — update already_covered flags
  4. process.py draft-briefs — generate briefs for new uncovered items

Logs each run to the daily_log table.
"""

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from db import get_conn, init_db

ROOT = Path(__file__).parent
PYTHON = sys.executable


def _run(args: list[str]) -> tuple[int, str]:
    """Run a subprocess, return (returncode, combined stdout+stderr)."""
    result = subprocess.run(
        args, capture_output=True, text=True, cwd=ROOT
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _count_new_captures(before: int) -> int:
    with get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0]
    return max(0, after - before)


def _count_new_briefs(before: int) -> int:
    with get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]
    return max(0, after - before)


def main() -> None:
    init_db()
    start = time.time()
    run_date = date.today().isoformat()
    errors: dict[str, str] = {}

    with get_conn() as conn:
        captures_before = conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0]
        briefs_before   = conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0]

    print(f"[run_daily] {run_date} — starting pipeline")

    # ── Step 1: scrape ────────────────────────────────────────────────────────
    print("[run_daily] step 1/4: scrape")
    rc, out = _run([PYTHON, "scrape.py"])
    if rc != 0:
        errors["scrape"] = out[-500:] if len(out) > 500 else out
        print(f"[run_daily] scrape FAILED (rc={rc})")
    else:
        print("[run_daily] scrape OK")

    new_captures = _count_new_captures(captures_before)
    print(f"[run_daily] {new_captures} new captures")

    # ── Step 2: classify ─────────────────────────────────────────────────────
    print("[run_daily] step 2/4: classify")
    rc, out = _run([PYTHON, "process.py", "classify"])
    if rc != 0:
        errors["classify"] = out[-500:] if len(out) > 500 else out
        print(f"[run_daily] classify FAILED (rc={rc})")
    else:
        print("[run_daily] classify OK")

    # ── Step 3: dedup ─────────────────────────────────────────────────────────
    print("[run_daily] step 3/4: dedup")
    rc, out = _run([PYTHON, "process.py", "dedup"])
    if rc != 0:
        errors["dedup"] = out[-500:] if len(out) > 500 else out
        print(f"[run_daily] dedup FAILED (rc={rc})")
    else:
        print("[run_daily] dedup OK")

    # ── Step 4: draft-briefs ──────────────────────────────────────────────────
    print("[run_daily] step 4/4: draft-briefs")
    rc, out = _run([PYTHON, "process.py", "draft-briefs"])
    if rc != 0:
        errors["draft-briefs"] = out[-500:] if len(out) > 500 else out
        print(f"[run_daily] draft-briefs FAILED (rc={rc})")
    else:
        print("[run_daily] draft-briefs OK")

    new_briefs = _count_new_briefs(briefs_before)
    duration   = round(time.time() - start, 1)

    print(f"[run_daily] done — {new_captures} new captures, {new_briefs} new briefs, "
          f"{len(errors)} errors, {duration}s")

    # ── Log to daily_log ─────────────────────────────────────────────────────
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO daily_log
                   (run_date, new_captures, new_briefs, errors_json, duration_seconds)
               VALUES (?, ?, ?, ?, ?)""",
            (run_date, new_captures, new_briefs,
             json.dumps(errors) if errors else None,
             duration),
        )

    if errors:
        print("[run_daily] errors:", list(errors.keys()))
        sys.exit(1)


if __name__ == "__main__":
    main()
