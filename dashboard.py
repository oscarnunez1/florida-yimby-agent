"""
Flask dashboard for the Florida YIMBY research agent.

Routes:
  GET  /  /inbox              — Inbox view (unread briefs)
  GET  /archive               — Paginated brief archive with filters
  GET  /history               — Redirect → /archive
  GET  /sources               — Source health table
  GET  /coverage              — Coverage index with search
  GET  /briefs/<id>           — Single-brief detail page
  GET  /logs                  — Pipeline run history + latest log file
  POST /briefs/<id>/use       — Mark brief as used
  POST /briefs/<id>/dismiss   — Dismiss brief with reason
  POST /briefs/<id>/snooze    — Snooze brief 24 h
  POST /briefs/<id>/undo      — Undo last action
"""

import json
import math
import yaml
from collections import Counter
from datetime import datetime, date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from flask import Flask, render_template, request, jsonify, abort, redirect, url_for

from db import get_conn, init_db
from process import CITY_TO_COUNTY, COUNTY_TO_REGION

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCES_YAML = Path(__file__).parent / "sources.yaml"
PER_PAGE      = 25
COV_PER_PAGE  = 30

app = Flask(__name__)
app.secret_key = "fl-yimby-dashboard-dev"

# ── Geo data (static, built from process.py mappings) ────────────────────────

_COUNTY_TO_CITIES: dict[str, list[str]] = {}
for _city, _county in CITY_TO_COUNTY.items():
    _COUNTY_TO_CITIES.setdefault(_county, []).append(_city)

_REGION_TO_COUNTIES: dict[str, list[str]] = {}
for _county, _region in COUNTY_TO_REGION.items():
    _REGION_TO_COUNTIES.setdefault(_region, []).append(_county)


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
    cfg = _sources_yaml()
    return {
        section: [s["name"] for s in cfg.get(section, [])]
        for section in ("rss", "html_scrape", "wp_rest", "iqm2")
    }


def _source_type_map() -> dict[str, str]:
    cfg = _sources_yaml()
    return {
        s["name"]: section
        for section in ("rss", "html_scrape", "wp_rest", "iqm2")
        for s in cfg.get(section, [])
    }


def _brief_query_base() -> str:
    return """
        SELECT b.*, rc.source, rc.captured_at, rc.og_image_url, rc.published_at,
               ei.priority, ei.event_type, ei.market, ei.county, ei.region
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


# ── Filter helpers ────────────────────────────────────────────────────────────

def _date_range_to_bounds(date_range: str) -> tuple[Optional[str], Optional[str]]:
    today = date.today().isoformat()
    if date_range == "today":
        return today, today
    if date_range == "last_7":
        return (date.today() - timedelta(days=7)).isoformat(), today
    if date_range == "last_30":
        return (date.today() - timedelta(days=30)).isoformat(), today
    if date_range == "last_90":
        return (date.today() - timedelta(days=90)).isoformat(), today
    return None, None


def _apply_common_filters(where: list, params: list, filters: dict) -> None:
    """Append region/county/city/date/source_type/hearings clauses in-place."""
    if filters.get("region"):
        where.append("ei.region = ?")
        params.append(filters["region"])

    if filters.get("county"):
        where.append("ei.county = ?")
        params.append(filters["county"])

    if filters.get("city"):
        where.append("ei.market = ?")
        params.append(filters["city"])

    date_val = filters.get("date", "")
    if date_val == "custom":
        from_d = filters.get("from_date", "")
        to_d   = filters.get("to_date", "")
        if from_d and to_d:
            where.append("date(rc.captured_at) BETWEEN ? AND ?")
            params.extend([from_d, to_d])
    else:
        date_from, date_to = _date_range_to_bounds(date_val)
        if date_from:
            where.append("date(rc.captured_at) >= ?")
            params.append(date_from)
        if date_to:
            where.append("date(rc.captured_at) <= ?")
            params.append(date_to)

    if filters.get("source"):
        where.append("rc.source = ?")
        params.append(filters["source"])

    if filters.get("hearings") == "1":
        where.append(
            "b.hearing_date IS NOT NULL"
            " AND date(b.hearing_date) >= date('now')"
            " AND date(b.hearing_date) <= date('now', '+14 days')"
        )


_DATE_LABELS = {
    "today": "Today", "last_7": "Last 7 days",
    "last_30": "Last 30 days", "last_90": "Last 90 days",
}
_SOURCE_LABELS  = {"news": "News", "municipal": "Municipal"}
_STATUS_LABELS  = {"all": "All", "used": "Used", "snoozed": "Snoozed",
                   "dismissed": "Dismissed", "new": "New"}


def _active_chips(path: str, filters: dict, default_status: str) -> list[dict]:
    chips = []

    def url_without(*keys):
        p = {k: v for k, v in filters.items() if v and k not in keys}
        return f"{path}?{urlencode(p)}" if p else path

    if filters.get("region"):
        chips.append({"label": filters["region"], "href": url_without("region", "county", "city")})
    if filters.get("county"):
        chips.append({"label": filters["county"], "href": url_without("county", "city")})
    if filters.get("city"):
        chips.append({"label": filters["city"], "href": url_without("city")})
    if filters.get("date"):
        if filters["date"] == "custom":
            from_d = filters.get("from_date", "")
            to_d   = filters.get("to_date", "")
            if from_d and to_d:
                try:
                    f_str = datetime.strptime(from_d, "%Y-%m-%d").strftime("%b %-d")
                    t_str = datetime.strptime(to_d,   "%Y-%m-%d").strftime("%b %-d")
                    label = f"{f_str} – {t_str}"
                except ValueError:
                    label = f"{from_d} – {to_d}"
                chips.append({"label": label, "href": url_without("date", "from_date", "to_date")})
        else:
            chips.append({"label": _DATE_LABELS.get(filters["date"], filters["date"]),
                          "href": url_without("date")})
    if filters.get("source"):
        chips.append({"label": filters["source"], "href": url_without("source")})
    status = filters.get("status", "")
    if status and status != default_status:
        chips.append({"label": _STATUS_LABELS.get(status, status),
                      "href": url_without("status")})
    if filters.get("hearings") == "1":
        chips.append({"label": "⚡ Upcoming hearings", "href": url_without("hearings")})
    return chips


def _geo_json_for_template(all_counties: list, all_cities: list) -> str:
    return json.dumps({
        "city_to_county":     CITY_TO_COUNTY,
        "county_to_region":   COUNTY_TO_REGION,
        "region_to_counties": _REGION_TO_COUNTIES,
        "county_to_cities":   _COUNTY_TO_CITIES,
        "all_counties":       all_counties,
        "all_cities":         all_cities,
    })


def _geo_lookups() -> tuple[list, list]:
    with get_conn() as conn:
        all_counties = [r["county"] for r in conn.execute(
            "SELECT DISTINCT county FROM extracted_items WHERE county IS NOT NULL ORDER BY county"
        ).fetchall()]
        all_cities = [r["market"] for r in conn.execute(
            "SELECT DISTINCT market FROM extracted_items WHERE market IS NOT NULL ORDER BY market"
        ).fetchall()]
    return all_counties, all_cities


# ── Template context ──────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    with get_conn() as conn:
        unread_count = conn.execute(
            "SELECT COUNT(*) FROM briefs WHERE status IN ('new', 'pending')"
        ).fetchone()[0]
    return {
        "hearing_badge":            hearing_badge,
        "source_placeholder_color": source_placeholder_color,
        "market_color":             market_color,
        "market_display":           market_display,
        "unread_count":             unread_count,
        "active_page":              None,
    }


# ── Inbox ─────────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/inbox")
def inbox():
    filters = {
        "region":   request.args.get("region",   "").strip(),
        "county":   request.args.get("county",   "").strip(),
        "city":     request.args.get("city",     "").strip(),
        "date":     request.args.get("date",     "last_7").strip(),
        "from_date":request.args.get("from_date","").strip(),
        "to_date":  request.args.get("to_date",  "").strip(),
        "source":   request.args.get("source",   "").strip(),
        "status":   request.args.get("status",   "").strip(),
        "hearings": request.args.get("hearings", "").strip(),
    }

    where: list = []
    params: list = []

    _apply_common_filters(where, params, filters)

    status = filters["status"]
    if status == "used":
        where.append("b.status = 'used'")
    elif status == "snoozed":
        where.append("b.status = 'snoozed'")
    elif status == "dismissed":
        where.append("b.status = 'dismissed'")
    elif status == "all":
        pass  # no status filter
    else:
        # default: unread
        where.append(
            "(b.status IN ('new', 'pending')"
            " OR (b.status = 'snoozed' AND b.snoozed_until <= datetime('now')))"
        )
        where.append("ei.already_covered = 0")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as conn:
        briefs = conn.execute(
            _brief_query_base() + f"""
            {where_sql}
            ORDER BY ei.priority DESC, b.created_at DESC
            """,
            params,
        ).fetchall()
        all_sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT rc.source FROM briefs b"
            " JOIN extracted_items ei ON b.extracted_item_id = ei.id"
            " JOIN raw_captures rc ON ei.raw_capture_id = rc.id"
            " ORDER BY rc.source"
        ).fetchall()]

    all_counties, all_cities = _geo_lookups()
    active_filters = _active_chips("/inbox", filters, default_status="")

    return render_template(
        "inbox.html",
        briefs=briefs,
        filters=filters,
        active_filters=active_filters,
        all_counties=all_counties,
        all_cities=all_cities,
        all_sources=all_sources,
        geo_json=_geo_json_for_template(all_counties, all_cities),
        market_counts=Counter(b["market"] or "FLORIDA" for b in briefs),
        active_page="inbox",
    )


# ── Archive ───────────────────────────────────────────────────────────────────

@app.route("/history")
def history_redirect():
    return redirect(url_for("archive"), 301)


@app.route("/archive")
def archive():
    page = max(1, request.args.get("page", 1, int))
    filters = {
        "region":   request.args.get("region",   "").strip(),
        "county":   request.args.get("county",   "").strip(),
        "city":     request.args.get("city",     "").strip(),
        "date":     request.args.get("date",     "").strip(),
        "from_date":request.args.get("from_date","").strip(),
        "to_date":  request.args.get("to_date",  "").strip(),
        "source":   request.args.get("source",   "").strip(),
        "status":   request.args.get("status",   "").strip(),
        "hearings": request.args.get("hearings", "").strip(),
        # legacy params kept for pagination URL building
        "board":    request.args.get("board",    "").strip(),
        "priority": request.args.get("priority", "").strip(),
    }

    where: list = []
    params: list = []

    _apply_common_filters(where, params, filters)

    # Legacy board filter
    if filters.get("board"):
        where.append("b.hearing_board LIKE ?")
        params.append(f"%{filters['board']}%")

    # Legacy priority filter
    if filters.get("priority"):
        where.append("ei.priority = ?")
        params.append(int(filters["priority"]))

    status = filters["status"]
    if status == "new":
        where.append("b.status IN ('new', 'pending')")
    elif status in ("used", "dismissed", "snoozed"):
        where.append("b.status = ?")
        params.append(status)
    # else: "" or "all" = no status filter

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * PER_PAGE

    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM briefs b"
            " JOIN extracted_items ei ON ei.id = b.extracted_item_id"
            " JOIN raw_captures rc ON rc.id = ei.raw_capture_id"
            f" {where_sql}",
            params,
        ).fetchone()[0]

        briefs = conn.execute(
            _brief_query_base() + f"{where_sql} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
            params + [PER_PAGE, offset],
        ).fetchall()
        all_sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT rc.source FROM briefs b"
            " JOIN extracted_items ei ON b.extracted_item_id = ei.id"
            " JOIN raw_captures rc ON ei.raw_capture_id = rc.id"
            " ORDER BY rc.source"
        ).fetchall()]

    all_counties, all_cities = _geo_lookups()
    active_filters = _active_chips("/archive", filters, default_status="")

    total_pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "archive.html",
        briefs=briefs, total=total,
        page=page, total_pages=total_pages,
        filters=filters,
        active_filters=active_filters,
        all_counties=all_counties,
        all_cities=all_cities,
        all_sources=all_sources,
        geo_json=_geo_json_for_template(all_counties, all_cities),
        active_page="archive",
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
            last   = stats["last_captured"] if stats else None
            total  = stats["total_items"]   if stats else 0
            recent = stats["recent_items"]  if stats else 0
            stale  = bool(last and last < stale_cutoff) or (total == 0)
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
            total = conn.execute("SELECT COUNT(*) FROM coverage_index").fetchone()[0]
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
    return render_template("brief_detail.html", b=b, active_page="archive")


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

    log_lines: list[str] = []
    log_filename: Optional[str] = None
    if LOGS_DIR.exists():
        log_files = sorted(LOGS_DIR.glob("daily_*.log"), reverse=True)
        if log_files:
            latest = log_files[0]
            log_filename = latest.name
            text = latest.read_text(errors="replace")
            log_lines = text.splitlines()[-100:]

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
