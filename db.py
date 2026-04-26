import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "db.sqlite"))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
                developer           TEXT,
                architect           TEXT,
                units               INTEGER,
                height              TEXT,
                status              TEXT,
                event_type          TEXT,
                priority            INTEGER DEFAULT 0,
                is_development_item INTEGER NOT NULL DEFAULT 0,
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
                dismiss_reason    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_raw_captures_processed  ON raw_captures(processed);
            CREATE INDEX IF NOT EXISTS idx_raw_captures_captured_at ON raw_captures(captured_at);
            CREATE INDEX IF NOT EXISTS idx_briefs_status            ON briefs(status);
            CREATE INDEX IF NOT EXISTS idx_briefs_created_at        ON briefs(created_at);
        """)


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
