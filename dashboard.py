"""
Flask dashboard — Session 5.
Views: Today, History, Sources, Index.
"""

from datetime import datetime, date
from typing import Optional

# TODO: implement full Flask app in Session 5


# ── Hearing badge ─────────────────────────────────────────────────────────────

def hearing_badge(hearing_date_str: Optional[str], hearing_board: Optional[str]) -> Optional[str]:
    """
    Return a short badge string for a card's hearing indicator, or None.

    Rules:
      - hearing_date within the next 7 days  → "{board} hearing in N days"
      - hearing_date is today                → "{board} hearing today"
      - hearing_date is in the past          → "{board} heard {Mon D}"
      - hearing_date is more than 7 days out → None (no badge shown)
      - hearing_date missing                 → None
    """
    if not hearing_date_str:
        return None

    try:
        # Accept ISO datetime strings or plain date strings
        hearing_dt = datetime.fromisoformat(hearing_date_str)
        hearing_day = hearing_dt.date()
    except ValueError:
        return None

    board = (hearing_board or "Board").strip()
    today = date.today()
    delta = (hearing_day - today).days

    if delta < 0:
        return f"{board} heard {hearing_day.strftime('%b %-d')}"
    elif delta == 0:
        return f"{board} hearing today"
    elif delta <= 7:
        return f"{board} hearing in {delta} day{'s' if delta != 1 else ''}"
    return None
