"""Shared report construction for live dashboard and static HTML snapshots."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from treasury_flow_radar.analytics.report import load_observations_read_only
from treasury_flow_radar.analytics.research import build_research_report
from treasury_flow_radar.events import load_deals, load_fomc_decisions

# Every section except the basis-trade setup reads from the first date of continuous daily
# collection forward, exactly as before any history was backfilled. The basis-trade setup
# ranks positioning, funding, and volatility against everything stored.
DAILY_COLLECTION_START = date(2024, 10, 7)


def _day(row: dict[str, Any]) -> date:
    return date.fromisoformat(str(row["observation_time"])[:10])


def load_dashboard_report(database_path: str | Path,
                          window_start: date = DAILY_COLLECTION_START) -> dict[str, Any]:
    """Load current immutable revisions and build the common read-only view model."""
    try:
        rows = load_observations_read_only(database_path)
    except (OSError, ValueError):
        rows = []
    dates = [_day(row) for row in rows]
    end = max(dates) if dates else datetime.now(UTC).date()
    recent = [row for row in rows if _day(row) >= window_start]
    start = min((_day(row) for row in recent), default=end)
    return build_research_report(recent, start_date=start, end_date=end, deals=load_deals(),
                                 fomc_decisions=load_fomc_decisions(), history_rows=rows)
