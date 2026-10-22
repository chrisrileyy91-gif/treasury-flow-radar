"""Read one publish run's ingest report (the JSON `treasury_flow_radar.ingest` prints).

Used by the page export (the "Inspect FRED." flag) and the Pheme alert, so both agree on
what counts as a failed FRED pull: any source whose name starts with "fred" (the yield,
spread and funding series and the release calendar) that the run marked FAILED.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def load_ingest_report(path: str | Path) -> dict[str, Any] | None:
    """The report as a dict, or None when the file is missing, unreadable or not an object."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def fred_failures(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The FRED sources this run failed to pull, in report order."""
    if not report:
        return []
    sources = report.get("sources") or []
    return [dict(s) for s in sources if isinstance(s, Mapping) and str(s.get("source", "")).startswith("fred")
            and s.get("status") == "FAILED"]


def fred_needs_inspection(report: Mapping[str, Any] | None) -> bool:
    """True when a FRED source failed, or when there is no readable report to say it didn't.

    A report that simply has no FRED sources (a backfill run limited to, say, CFTC) is fine.
    """
    return report is None or bool(fred_failures(report))
