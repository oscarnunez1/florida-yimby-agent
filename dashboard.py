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
from collections import Counter
from datetime import datetime, date, timedelta
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _sources_yaml() -> dict:
    return yaml.safe_load(SOURCES_YAML.read_text())


def _load_sources_config() -> dict[str, list[str]]:
    """Return {type_key: [source_name, ...]} from sources.yaml."""
    cfg = _sources_yaml()
    return {
        section: [s["name"] for s in cfg.get(section, [])]
        for section in ("rss", "html_scrape", "wp_rest", "iqm2")
    }


def _source_type_map() -> dict[str, str]:
    """Return {source_name: type_key} for all configured sources."""
    cfg = _sources_yaml()
    return {
        s["name"]: section
        for section in ("rss", "html_scrape", "wp_rest", "iqm2")
        for s in cfg.get(section, [])
    }


def _brief_query_base() -> str:
    return """
        SELECT b.*, rc.source, rc.captured_at, rc.og_image_url, rc.published_at,
               ei.priority, ei.event_type, ei.market
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


_MIAMI_DADE = {
    "MIAMI", "MIAMI BEACH", "CORAL GABLES", "HIALEAH", "HOMESTEAD", "AVENTURA",
    "DORAL", "SUNNY ISLES", "BAL HARBOUR", "SURFSIDE", "NORTH MIAMI BEACH",
    "NORTH MIAMI", "OPA-LOCKA", "SWEETWATER", "CUTLER BAY", "PALMETTO BAY",
    "PINECREST", "SOUTH MIAMI", "KEY BISCAYNE",
}
_BROWARD = {
    "FT. LAUDERDALE", "MIRAMAR", "PEMBROKE PINES", "HALLANDALE BEACH", "HALLANDALE",
    "POMPANO BEACH", "DEERFIELD BEACH", "CORAL SPRINGS", "SUNRISE", "PLANTATION",
    "DAVIE", "WESTON", "COOPER CITY", "DANIA BEACH", "LAUDERHILL", "TAMARAC",
    "MARGATE", "COCONUT CREEK", "LIGHTHOUSE POINT", "OAKLAND PARK", "WILTON MANORS",
    "HOLLYWOOD",
}
_PALM_BEACH = {
    "WEST PALM BEACH", "BOCA RATON", "DELRAY BEACH", "BOYNTON BEACH", "LAKE WORTH",
    "PB GARDENS", "PALM BEACH", "JUPITER", "RIVIERA BEACH", "WELLINGTON",
}
_TAMPA_BAY = {"TAMPA", "ST. PETE", "CLEARWATER", "LARGO", "BRADENTON", "SARASOTA"}
_ORLANDO   = {"ORLANDO", "WINTER PARK", "KISSIMMEE", "SANFORD"}


def market_color(market: str) -> str:
    m = (market or "FLORIDA").upper()
    if m in _MIAMI_DADE:  return "#1e3a5f"
    if m in _BROWARD:     return "#713f12"
    if m in _PALM_BEACH:  return "#7f1d1d"
    if m in _TAMPA_BAY:   return "#065f46"
    if m in _ORLANDO:     return "#4c1d95"
    return "#1f2937"


def market_display(market: str) -> str:
    return market or "FLORIDA"


# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return {
        "hearing_badge":            hearing_badge,
        "source_placeholder_color": source_placeholder_color,
        "market_color":             market_color,
        "market_display":           market_display,
        "active_page":              None,
    }


# ── Today ─────────────────────────────────────────────────────────────────────

@app.route("/")
def today():
    from_date     = request.args.get("from_date",     "").strip()
    to_date       = request.args.get("to_date",       "").strip()
    market_filter = request.args.get("market",        "").strip().upper()

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
            AND (b.status IN ('new', 'pending')
                 OR (b.status = 'snoozed' AND b.snoozed_until <= datetime('now')))
            AND ei.already_covered = 0
            ORDER BY ei.priority DESC, b.created_at DESC
            """,
            (effective_from, effective_to),
        ).fetchall()

    market_counts = Counter(b["market"] or "OTHER" for b in briefs)
    return render_template(
        "today.html", briefs=briefs, active_page="today",
        from_date=effective_from, to_date=effective_to,
        market_counts=market_counts, market_filter=market_filter,
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
    market     = request.args.get("market",    "").strip().upper()

    filters = dict(src_type=src_type, priority=priority, status=status,
                   board=board, from_date=from_date, to_date=to_date, market=market)

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

    if market:
        where_clauses.append("ei.market = ?")
        params.append(market)

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

    with get_conn() as conn:
        all_markets = [
            row["market"] for row in conn.execute(
                "SELECT DISTINCT market FROM extracted_items "
                "WHERE market IS NOT NULL ORDER BY market"
            ).fetchall()
        ]

    total_pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "history.html",
        briefs=briefs, total=total,
        page=page, total_pages=total_pages,
        filters=filters, active_page="history",
        all_markets=all_markets,
    )


# ── Sources ───────────────────────────────────────────────────────────────────

@app.route("/sources")
def sources():
    cfg = _sources_yaml()
    stale_cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        agg = {
            row["source"]: row
            for row in conn.execute("""
                SELECT
                    source,
                    MAX(captured_at) AS last_captured,
                    COUNT(*)         AS total_items,
                    SUM(CASE WHEN captured_at >= datetime('now', '-7 days')
                             THEN 1 ELSE 0 END) AS recent_items
                FROM raw_captures
                GROUP BY source
            """).fetchall()
        }

    rows = []
    for section in ("rss", "html_scrape", "wp_rest", "iqm2"):
        for src in cfg.get(section, []):
            stats = agg.get(src["name"])
            last  = stats["last_captured"] if stats else None
            total = stats["total_items"]   if stats else 0
            recent = stats["recent_items"] if stats else 0
            stale = bool(last and last < stale_cutoff) or (total == 0)
            rows.append({
                "name":          src["name"],
                "type":          section,
                "last_captured": last,
                "total_items":   total,
                "recent_items":  recent,
                "stale":         stale,
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


@app.route("/briefs/<int:brief_id>/undo", methods=["POST"])
def brief_undo(brief_id: int):
    data            = request.get_json(silent=True) or {}
    previous_status = data.get("previous_status", "new")
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE briefs SET status=?, snoozed_until=NULL, dismiss_reason=NULL WHERE id=?",
            (previous_status, brief_id)
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
