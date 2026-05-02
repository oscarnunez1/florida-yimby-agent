import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "db.sqlite"))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS raw_captures (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT    NOT NULL,
                url          TEXT    NOT NULL UNIQUE,
                title        TEXT,
                content      TEXT,
                captured_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                processed    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS extracted_items (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_capture_id      INTEGER NOT NULL REFERENCES raw_captures(id),
                project_name        TEXT,
                address             TEXT,
                city                TEXT,
                developer           TEXT,
                architect           TEXT,
                units               INTEGER,
                height              TEXT,
                status              TEXT,
                event_type          TEXT,
                priority            INTEGER DEFAULT 0,
                is_development_item INTEGER NOT NULL DEFAULT 0,
                florida_relevance   INTEGER NOT NULL DEFAULT 0,
                extracted_data_json TEXT
            );

            CREATE TABLE IF NOT EXISTS coverage_index (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT,
                address      TEXT,
                developer    TEXT,
                architect    TEXT,
                article_url  TEXT    NOT NULL UNIQUE,
                published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS briefs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                extracted_item_id INTEGER NOT NULL REFERENCES extracted_items(id),
                headline          TEXT,
                lede              TEXT,
                body              TEXT,
                fact_sheet_json   TEXT,
                sources           TEXT,
                open_questions    TEXT,
                accuracy_score    REAL,
                status            TEXT    NOT NULL DEFAULT 'pending',
                created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
                used_url          TEXT,
                dismiss_reason    TEXT,
                hearing_date      TEXT,
                hearing_board     TEXT
            );

            -- Tracks IQM2 meeting state between daily scraper runs.
            -- Used to detect when an agenda packet is newly posted.
            CREATE TABLE IF NOT EXISTS meetings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source           TEXT    NOT NULL,
                meeting_url      TEXT    NOT NULL UNIQUE,
                board            TEXT,
                meeting_date     TEXT,
                agenda_url       TEXT,
                first_seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                agenda_posted_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_raw_captures_processed    ON raw_captures(processed);
            CREATE INDEX IF NOT EXISTS idx_raw_captures_captured_at  ON raw_captures(captured_at);
            CREATE INDEX IF NOT EXISTS idx_briefs_status              ON briefs(status);
            CREATE INDEX IF NOT EXISTS idx_briefs_created_at          ON briefs(created_at);
        """)

        # Migrations for columns added after initial schema.
        migrations = [
            ("extracted_items", "city",                 "TEXT"),
            ("extracted_items", "florida_relevance",    "INTEGER NOT NULL DEFAULT 0"),
            ("extracted_items", "already_covered",      "INTEGER NOT NULL DEFAULT 0"),
            ("extracted_items", "coverage_match_url",   "TEXT"),
            ("extracted_items", "market",               "TEXT"),
            ("raw_captures",    "metadata_json",        "TEXT"),
            ("raw_captures",    "og_image_url",         "TEXT"),
            ("raw_captures",    "published_at",         "TEXT"),
            ("briefs",          "hearing_date",         "TEXT"),
            ("briefs",          "hearing_board",        "TEXT"),
            ("briefs",          "confirmed_vs_pending", "TEXT"),
            ("briefs",          "snoozed_until",        "TEXT"),
            ("extracted_items", "county",               "TEXT"),
            ("extracted_items", "region",               "TEXT"),
            ("meetings",        "municipality",          "TEXT"),
        ]

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date         TEXT    NOT NULL,
                new_captures     INTEGER NOT NULL DEFAULT 0,
                new_briefs       INTEGER NOT NULL DEFAULT 0,
                errors_json      TEXT,
                duration_seconds REAL,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        for table, col, definition in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists

        # Index depends on florida_relevance existing — create after migration.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extracted_florida"
            " ON extracted_items(florida_relevance, is_development_item)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extracted_market"
            " ON extracted_items(market)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extracted_county"
            " ON extracted_items(county)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extracted_region"
            " ON extracted_items(region)"
        )

        # Backfill market for any extracted_items where market IS NULL.
        # Short-circuits when all rows already have a value.
        needs_backfill = conn.execute(
            "SELECT 1 FROM extracted_items WHERE market IS NULL LIMIT 1"
        ).fetchone()
        if needs_backfill:
            # json_extract pass: IQM2 items carry hearing_board in JSON → always MIAMI.
            # Isolated so city/address pass still runs on older SQLite without json_extract.
            try:
                conn.execute("""
                    UPDATE extracted_items SET market = 'MIAMI'
                    WHERE market IS NULL
                      AND extracted_data_json IS NOT NULL
                      AND json_extract(extracted_data_json, '$.hearing_board') IS NOT NULL
                """)
            except Exception:
                pass

            conn.executescript("""
                UPDATE extracted_items SET market = 'MIAMI'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%miami%'
                    OR lower(coalesce(address,'')) LIKE '%miami%'
                );
                UPDATE extracted_items SET market = 'TAMPA'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%tampa%'
                    OR lower(coalesce(address,'')) LIKE '%tampa%'
                );
                UPDATE extracted_items SET market = 'ST. PETE'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%st. pete%'
                    OR lower(coalesce(city,''))    LIKE '%st pete%'
                    OR lower(coalesce(city,''))    LIKE '%saint pete%'
                    OR lower(coalesce(city,''))    LIKE '%pinellas%'
                    OR lower(coalesce(city,''))    LIKE '%clearwater%'
                    OR lower(coalesce(address,'')) LIKE '%st. pete%'
                    OR lower(coalesce(address,'')) LIKE '%st pete%'
                    OR lower(coalesce(address,'')) LIKE '%pinellas%'
                    OR lower(coalesce(address,'')) LIKE '%clearwater%'
                );
                UPDATE extracted_items SET market = 'ORLANDO'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%orlando%'
                    OR lower(coalesce(address,'')) LIKE '%orlando%'
                    OR lower(coalesce(city,''))    LIKE '%kissimmee%'
                    OR lower(coalesce(address,'')) LIKE '%kissimmee%'
                );
                UPDATE extracted_items SET market = 'WEST PALM'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%west palm%'
                    OR lower(coalesce(city,''))    LIKE '%palm beach%'
                    OR lower(coalesce(city,''))    LIKE '%boca raton%'
                    OR lower(coalesce(city,''))    LIKE '%delray%'
                    OR lower(coalesce(address,'')) LIKE '%west palm%'
                    OR lower(coalesce(address,'')) LIKE '%palm beach%'
                    OR lower(coalesce(address,'')) LIKE '%boca raton%'
                );
                UPDATE extracted_items SET market = 'FORT LAUDERDALE'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%fort lauderdale%'
                    OR lower(coalesce(city,''))    LIKE '%ft. lauderdale%'
                    OR lower(coalesce(city,''))    LIKE '%ft lauderdale%'
                    OR lower(coalesce(address,'')) LIKE '%fort lauderdale%'
                    OR lower(coalesce(address,'')) LIKE '%ft. lauderdale%'
                );
                UPDATE extracted_items SET market = 'BROWARD'
                WHERE market IS NULL AND (
                    lower(coalesce(city,''))    LIKE '%broward%'
                    OR lower(coalesce(city,''))    LIKE '%pompano%'
                    OR lower(coalesce(city,''))    LIKE '%pembroke pines%'
                    OR lower(coalesce(city,''))    LIKE '%coral springs%'
                    OR lower(coalesce(city,''))    LIKE '%deerfield beach%'
                    OR lower(coalesce(city,''))    LIKE '%miramar%'
                    OR lower(coalesce(city,''))    LIKE '%hallandale%'
                    OR lower(coalesce(address,'')) LIKE '%broward%'
                    OR lower(coalesce(address,'')) LIKE '%pompano%'
                    OR lower(coalesce(address,'')) LIKE '%pembroke pines%'
                );
                UPDATE extracted_items SET market = 'OTHER'
                WHERE market IS NULL;
            """)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
