"""
Flask dashboard for the Florida YIMBY research agent.

Routes:
  GET  /                    — Today view (last 24h briefs + upcoming hearings)
  GET  /history             — Paginated brief history with filters
  GET  /sources             — Source health table
  GET  /coverage            — Coverage index with search
  GET  /briefs/<id>         — Single-brief detail page
  GET  /logs                — Pipeline run history + latest log file
  POST /briefs/<id>/use     — Mark brief as used
  POST /briefs/<id>/dismiss — Dismiss brief with reason
  POST /briefs/<id>/snooze  — Snooze brief 24 h
"""

import math
import yaml
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from flask import Flask, render_template, request, jsonify, abort, redirect, url_for

from db import get_conn, init_db

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCES_YAML = Path(__file__).parent / "sources.yaml"
PER_PAGE      = 25
COV_PER_PAGE  = 30

app = Flask(__name__)
app.secret_key = "fl-yimby-dashboard-dev"


# ── Helpers ───────────────────────────────────────────────────────────────────

def hearing_badge(hearing_date_str: Optional[str],
                  hearing_board: Optional[str]) -> Optional[str]:
    if not hearing_date_str:
        return None
    try:
        hearing_day = datetime.fromisoformat(hearing_date_str).date()
    except ValueError:
        return None
    board = (hearing_board or "Board").strip()
    delta = (hearing_day - date.today()).days
    if delta < 0:
        return f"{board} heard {hearing_day.strftime('%b %-d')}"
    if delta == 0:
        return f"{board} hearing today"
    if delta <= 7:
        return f"{board} hearing in {delta} day{'s' if delta != 1 else ''}"
    return None


def _load_sources_config() -> dict[str, list[str]]:
    """Return {type_key: [source_name, ...]} from sources.yaml."""
    cfg = yaml.safe_load(SOURCES_YAML.read_text())
    result: dict[str, list[str]] = {}
    for section in ("rss", "html_scrape", "wp_rest", "iqm2"):
        result[section] = [s["name"] for s in cfg.get(section, [])]
    return result


def _source_type_map() -> dict[str, str]:
    """Return {source_name: type_key} for all configured sources."""
    cfg = yaml.safe_load(SOURCES_YAML.read_text())
    m: dict[str, str] = {}
    for section in ("rss", "html_scrape", "wp_rest", "iqm2"):
        for s in cfg.get(section, []):
            m[s["name"]] = section
    return m


def _brief_query_base() -> str:
    return """
        SELECT b.*, rc.source, rc.captured_at, rc.og_image_url, rc.published_at,
               ei.priority, ei.event_type
        FROM briefs b
        JOIN extracted_items ei ON ei.id = b.extracted_item_id
        JOIN raw_captures    rc ON rc.id = ei.raw_capture_id
    """


def source_placeholder_color(source: str) -> str:
    s = source.lower()
    if "urban development" in s:
        return "#7f1d1d"
    if "planning" in s and ("zoning" in s or "appeals" in s):
        return "#14532d"
    if "historic" in s or "preservation" in s:
        return "#4c1d95"
    if "wynwood" in s:
        return "#9a3412"
    return "#1e3a5f"


# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return {
        "hearing_badge":            hearing_badge,
        "source_placeholder_color": source_placeholder_color,
        "active_page":              None,
    }


# ── Today ─────────────────────────────────────────────────────────────────────

@app.route("/")
def today():
    from_date = request.args.get("from_date", "").strip()
    to_date   = request.args.get("to_date",   "").strip()

    effective_from = from_date or (date.today() - timedelta(days=7)).isoformat()
    effective_to   = to_date   or date.today().isoformat()

    with get_conn() as conn:
        briefs = conn.execute(
            _brief_query_base() + """
            WHERE (
                (date(rc.captured_at) >= ? AND date(rc.captured_at) <= ?)
                OR (b.hearing_date IS NOT NULL
                    AND date(b.hearing_date) >= date('now')
                    AND date(b.hearing_date) <= date('now', '+7 days'))
            )
            AND b.status NOT IN ('dismissed')
            AND (b.snoozed_until IS NULL OR b.snoozed_until <= datetime('now'))
            ORDER BY ei.priority DESC, b.created_at DESC
            """,
            (effective_from, effective_to),
        ).fetchall()
    return render_template(
        "today.html", briefs=briefs, active_page="today",
        from_date=effective_from, to_date=effective_to,
    )


# ── History ───────────────────────────────────────────────────────────────────

@app.route("/history")
def history():
    page       = max(1, request.args.get("page", 1, int))
    src_type   = request.args.get("src_type",  "").strip()
    priority   = request.args.get("priority",  "").strip()
    status     = request.args.get("status",    "").strip()
    board      = request.args.get("board",     "").strip()
    from_date  = request.args.get("from_date", "").strip()
    to_date    = request.args.get("to_date",   "").strip()

    filters = dict(src_type=src_type, priority=priority, status=status,
                   board=board, from_date=from_date, to_date=to_date)

    where_clauses = []
    params: list = []

    if src_type:
        src_names = _load_sources_config().get(src_type, [])
        if src_names:
            placeholders = ",".join("?" * len(src_names))
            where_clauses.append(f"rc.source IN ({placeholders})")
            params.extend(src_names)
        else:
            where_clauses.append("1=0")   # unknown type → no results

    if priority:
        where_clauses.append("ei.priority = ?")
        params.append(int(priority))

    if status:
        if status == "new":
            where_clauses.append("b.status IN ('new', 'pending')")
        else:
            where_clauses.append("b.status = ?")
            params.append(status)

    if board:
        where_clauses.append("b.hearing_board LIKE ?")
        params.append(f"%{board}%")

    if from_date:
        where_clauses.append("date(b.created_at) >= ?")
        params.append(from_date)

    if to_date:
        where_clauses.append("date(b.created_at) <= ?")
        params.append(to_date)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    order_sql  = "ORDER BY b.created_at DESC"
    offset     = (page - 1) * PER_PAGE

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM briefs b "
            f"JOIN extracted_items ei ON ei.id = b.extracted_item_id "
            f"JOIN raw_captures rc ON rc.id = ei.raw_capture_id "
            f"{where_sql}", params
        ).fetchone()[0]

        briefs = conn.execute(
            _brief_query_base() + f"{where_sql} {order_sql} LIMIT ? OFFSET ?",
            params + [PER_PAGE, offset]
        ).fetchall()

    total_pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "history.html",
        briefs=briefs, total=total,
        page=page, total_pages=total_pages,
        filters=filters, active_page="history",
    )


# ── Sources ───────────────────────────────────────────────────────────────────

@app.route("/sources")
def sources():
    cfg = yaml.safe_load(SOURCES_YAML.read_text())
    stale_cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    with get_conn() as conn:
        for section in ("rss", "html_scrape", "wp_rest", "iqm2"):
            for src in cfg.get(section, []):
                stats = conn.execute("""
                    SELECT
                        MAX(captured_at) AS last_captured,
                        COUNT(*)         AS total_items,
                        SUM(CASE WHEN captured_at >= datetime('now', '-7 days')
                                 THEN 1 ELSE 0 END) AS recent_items
                    FROM raw_captures
                    WHERE source = ?
                """, (src["name"],)).fetchone()

                last = stats["last_captured"]
                stale = bool(last and last < stale_cutoff) or (
                    stats["total_items"] == 0
                )
                rows.append({
                    "name":         src["name"],
                    "type":         section,
                    "last_captured": last,
                    "total_items":  stats["total_items"] or 0,
                    "recent_items": stats["recent_items"] or 0,
                    "stale":        stale,
                })

    return render_template("sources.html", sources=rows, active_page="sources")


# ── Coverage Index ────────────────────────────────────────────────────────────

@app.route("/coverage")
def coverage():
    q    = request.args.get("q", "").strip()
    page = max(1, request.args.get("page", 1, int))
    offset = (page - 1) * COV_PER_PAGE

    with get_conn() as conn:
        if q:
            like = f"%{q}%"
            total = conn.execute(
                "SELECT COUNT(*) FROM coverage_index "
                "WHERE project_name LIKE ? OR address LIKE ?",
                (like, like)
            ).fetchone()[0]
            entries = conn.execute(
                "SELECT * FROM coverage_index "
                "WHERE project_name LIKE ? OR address LIKE ? "
                "ORDER BY published_at DESC LIMIT ? OFFSET ?",
                (like, like, COV_PER_PAGE, offset)
            ).fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM coverage_index"
            ).fetchone()[0]
            entries = conn.execute(
                "SELECT * FROM coverage_index "
                "ORDER BY published_at DESC LIMIT ? OFFSET ?",
                (COV_PER_PAGE, offset)
            ).fetchall()

    total_pages = max(1, math.ceil(total / COV_PER_PAGE))
    return render_template(
        "coverage.html",
        entries=entries, total=total, q=q,
        page=page, total_pages=total_pages,
        active_page="coverage",
    )


# ── Brief detail ──────────────────────────────────────────────────────────────

@app.route("/briefs/<int:brief_id>")
def brief_detail(brief_id: int):
    with get_conn() as conn:
        b = conn.execute(
            _brief_query_base() + "WHERE b.id = ?", (brief_id,)
        ).fetchone()
    if not b:
        abort(404)
    return render_template("brief_detail.html", b=b, active_page="history")


# ── Card actions (JSON API) ───────────────────────────────────────────────────

@app.route("/briefs/<int:brief_id>/use", methods=["POST"])
def brief_use(brief_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE briefs SET status='used' WHERE id=?", (brief_id,)
        ).rowcount
    if not rows:
        return jsonify(ok=False, error="Brief not found"), 404
    return jsonify(ok=True)


@app.route("/briefs/<int:brief_id>/dismiss", methods=["POST"])
def brief_dismiss(brief_id: int):
    data   = request.get_json(silent=True) or {}
    reason = data.get("reason", "other")
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE briefs SET status='dismissed', dismiss_reason=? WHERE id=?",
            (reason, brief_id)
        ).rowcount
    if not rows:
        return jsonify(ok=False, error="Brief not found"), 404
    return jsonify(ok=True)


@app.route("/briefs/<int:brief_id>/snooze", methods=["POST"])
def brief_snooze(brief_id: int):
    until = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE briefs SET status='snoozed', snoozed_until=? WHERE id=?",
            (until, brief_id)
        ).rowcount
    if not rows:
        return jsonify(ok=False, error="Brief not found"), 404
    return jsonify(ok=True)


# ── Logs ─────────────────────────────────────────────────────────────────────

LOGS_DIR = Path(__file__).parent / "logs"

@app.route("/logs")
def logs():
    with get_conn() as conn:
        runs = conn.execute(
            "SELECT * FROM daily_log ORDER BY run_date DESC, id DESC LIMIT 30"
        ).fetchall()

    # Find the most recent log file in logs/
    log_lines: list[str] = []
    log_filename: Optional[str] = None
    if LOGS_DIR.exists():
        log_files = sorted(LOGS_DIR.glob("daily_*.log"), reverse=True)
        if log_files:
            latest = log_files[0]
            log_filename = latest.name
            text = latest.read_text(errors="replace")
            all_lines = text.splitlines()
            log_lines = all_lines[-100:]  # last 100 lines

    return render_template(
        "logs.html",
        runs=runs,
        log_lines=log_lines,
        log_filename=log_filename,
        active_page="logs",
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
