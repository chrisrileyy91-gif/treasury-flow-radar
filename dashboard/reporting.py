"""Shared report construction for live dashboard and static HTML snapshots."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from treasury_flow_radar.analytics.report import load_observations_read_only
from treasury_flow_radar.analytics.research import build_research_report
from treasury_flow_radar.events import load_deals, load_fomc_decisions


def load_dashboard_report(database_path: str | Path) -> dict[str, Any]:
    """Load current immutable revisions and build the common read-only view model."""
    try:
        rows = load_observations_read_only(database_path)
    except (OSError, ValueError):
        rows = []
    dates = [date.fromisoformat(str(row["observation_time"])[:10]) for row in rows]
    start = min(dates) if dates else datetime.now(UTC).date()
    end = max(dates) if dates else start
    return build_research_report(rows, start_date=start, end_date=end, deals=load_deals(),
                                 fomc_decisions=load_fomc_decisions())

