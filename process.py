"""
Classify and extract structured data from raw_captures into extracted_items.

Commands:
    python process.py classify    # Session 3: classify + extract unprocessed captures
    python process.py status      # show counts of pending / processed captures
"""

import argparse
import logging
import sys

from dotenv import load_dotenv

from db import get_conn, init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def cmd_status() -> None:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM raw_captures").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM raw_captures WHERE processed = 0").fetchone()[0]
        extracted = conn.execute("SELECT COUNT(*) FROM extracted_items").fetchone()[0]
    log.info("raw_captures: %d total, %d unprocessed", total, pending)
    log.info("extracted_items: %d total", extracted)


def cmd_classify() -> None:
    # Session 3: implement Haiku classification + extraction here
    raise NotImplementedError("classify not yet implemented — coming in Session 3")


def main() -> None:
    init_db()

    parser = argparse.ArgumentParser(description="Process raw captures into extracted items.")
    parser.add_argument("command", choices=["classify", "status"])
    args = parser.parse_args()

    if args.command == "status":
        cmd_status()
    elif args.command == "classify":
        cmd_classify()


if __name__ == "__main__":
    main()
