"""
One-shot backfill: fetch og:image for the 200 most recent RSS raw_captures
that are missing og_image_url. Safe to re-run; skips rows already populated.
"""
import time
import yaml
from pathlib import Path
from db import get_conn
from scrape import fetch_og_image

RSS_SOURCES = [s["name"] for s in yaml.safe_load(
    (Path(__file__).parent / "sources.yaml").read_text()
).get("rss", [])]

LIMIT = 200
DELAY = 1.0

def main():
    if not RSS_SOURCES:
        print("No RSS sources found in sources.yaml")
        return

    placeholders = ",".join("?" * len(RSS_SOURCES))
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, url FROM raw_captures
            WHERE og_image_url IS NULL
              AND source IN ({placeholders})
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            RSS_SOURCES + [LIMIT],
        ).fetchall()

    print(f"Found {len(rows)} RSS captures missing og_image_url (capped at {LIMIT})")

    ok = 0
    skip = 0
    for i, row in enumerate(rows, 1):
        og = fetch_og_image(row["url"])
        if og:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE raw_captures SET og_image_url = ? WHERE id = ?",
                    (og, row["id"]),
                )
            ok += 1
            print(f"[{i}/{len(rows)}] ✓  {og[:80]}")
        else:
            skip += 1
            print(f"[{i}/{len(rows)}] –  no image  {row['url'][:70]}")

        if i < len(rows):
            time.sleep(DELAY)

    print(f"\nDone. {ok} images saved, {skip} skipped (no og:image found).")

if __name__ == "__main__":
    main()
