"""Controlled orchestration for the project's existing historical data adapters."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from treasury_flow_radar.database import DEFAULT_DB_PATH
from treasury_flow_radar.sources.cftc import ingest_cftc
from treasury_flow_radar.sources.fred import ingest_fred
from treasury_flow_radar.sources.fred_releases import ingest_release_dates
from treasury_flow_radar.sources.nyfed import ingest_nyfed
from treasury_flow_radar.sources.treasury_auctions import ingest_treasury_auctions

DEFAULT_WINDOW_DAYS = 730
FRED_SOURCES = {
    "fred-dgs2": "DGS2", "fred-dgs5": "DGS5", "fred-dgs7": "DGS7",
    "fred-dgs10": "DGS10", "fred-dgs30": "DGS30",
    "fred-dfii10": "DFII10", "fred-t10yie": "T10YIE", "fred-threefytp10": "THREEFYTP10",
}
SOURCE_NAMES = (*FRED_SOURCES, "fred-releases", "nyfed", "cftc", "treasury-auctions")
SOURCE_LABELS = {
    **{key: f"FRED {series}" for key, series in FRED_SOURCES.items()},
    "fred-releases": "FRED release calendar (CPI, jobs, PCE, FOMC)",
    "nyfed": "NY Fed Primary Dealer Treasury positioning",
    "cftc": "CFTC Treasury futures positioning",
    "treasury-auctions": "U.S. Treasury auctions",
}


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    message: str
    inserted: int = 0
    unchanged: int = 0
    missing: int = 0
    raw_records_added: int = 0
    observations_total: int = 0
    raw_records_total: int = 0


@dataclass(frozen=True)
class IngestionSummary:
    database: str
    start_date: str
    end_date: str
    results: tuple[SourceResult, ...]

    @property
    def has_failures(self) -> bool:
        return any(result.status == "FAILED" for result in self.results)

    def as_dict(self) -> dict[str, Any]:
        if self.has_failures:
            status = "FAILED"
        elif any(result.status == "SKIPPED" for result in self.results):
            status = "COMPLETE_WITH_SKIPS"
        else:
            status = "COMPLETE"
        return {
            "database": self.database,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "status": status,
            "sources": [asdict(result) for result in self.results],
        }


def _counts(
    database_path: str | Path,
    source_identifier: str,
    series_identifier: str | None = None,
) -> tuple[int, int]:
    """Return normalized observation and raw payload totals for one source."""
    path = Path(database_path)
    if not path.is_file():
        return 0, 0
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sources", "observations", "raw_records"}.issubset(tables):
            return 0, 0
        row = conn.execute(
            """SELECT s.id,
                      COUNT(DISTINCT CASE WHEN ? IS NULL OR se.identifier=? THEN o.id END),
                      COUNT(DISTINCT r.id)
               FROM sources s LEFT JOIN observations o ON o.source_id=s.id
               LEFT JOIN series se ON se.id=o.series_id
               LEFT JOIN raw_records r ON r.source_id=s.id
               WHERE s.identifier=? GROUP BY s.id""",
            (series_identifier, series_identifier, source_identifier),
        ).fetchone()
        return (0, 0) if row is None else (int(row[1]), int(row[2]))
    finally:
        conn.close()


def _normalized_counts(value: Any) -> tuple[int, int, int]:
    if isinstance(value, list):
        rows = value
    else:
        rows = [value]
    totals = {key: 0 for key in ("inserted", "unchanged", "missing")}
    for row in rows:
        if isinstance(row, Mapping):
            for key in totals:
                totals[key] += int(row.get(key, 0))
        else:
            for key in totals:
                totals[key] += int(getattr(row, key, 0))
    return totals["inserted"], totals["unchanged"], totals["missing"]


def _safe_error(exc: Exception) -> str:
    message = str(exc) or type(exc).__name__
    secret = os.getenv("FRED_API_KEY")
    if secret:
        message = message.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {message}"


def run_ingestion(
    *,
    database_path: str | Path = DEFAULT_DB_PATH,
    sources: tuple[str, ...] | list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    environment: Mapping[str, str] | None = None,
    adapter_functions: Mapping[str, Callable[..., Any]] | None = None,
) -> IngestionSummary:
    """Run each selected existing adapter independently and report every outcome.

    The default window is the latest 730 calendar days. Each adapter performs its
    own existing transaction; one provider failure cannot roll back another.
    ``adapter_functions`` is an offline-test seam, not an alternate source path.
    """
    env = os.environ if environment is None else environment
    end = end_date or datetime.now(UTC).date()
    start = start_date or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start > end:
        raise ValueError("start_date must not be after end_date")
    selected = tuple(SOURCE_NAMES if sources is None else sources)
    if not selected:
        raise ValueError("select at least one source")
    if len(set(selected)) != len(selected):
        raise ValueError("source selections must not be repeated")
    unknown = sorted(set(selected) - set(SOURCE_NAMES))
    if unknown:
        raise ValueError(f"unknown source selection(s): {', '.join(unknown)}")

    adapters = {
        **{
            key: (lambda series=series: ingest_fred(
                [series], database_path=database_path,
                observation_start=start, observation_end=end,
            ))
            for key, series in FRED_SOURCES.items()
        },
        "fred-releases": lambda: ingest_release_dates(
            database_path=database_path, start_date=start, end_date=end,
        ),
        "nyfed": lambda: ingest_nyfed(
            database_path=database_path, observation_start=start, observation_end=end,
        ),
        "cftc": lambda: ingest_cftc(
            database_path=database_path, start_date=start, end_date=end,
        ),
        "treasury-auctions": lambda: ingest_treasury_auctions(
            database_path=database_path, start_date=start, end_date=end,
        ),
    }
    if adapter_functions:
        adapters.update({key: value for key, value in adapter_functions.items() if key in adapters})

    results: list[SourceResult] = []
    for key in selected:
        if key.startswith("fred-") and not env.get("FRED_API_KEY", "").strip():
            results.append(SourceResult(
                key, "SKIPPED", "FRED_API_KEY is not configured; no FRED request was made."
            ))
            continue
        source_identifier = {
            **dict.fromkeys(FRED_SOURCES, "FRED"), "fred-releases": "FRED", "nyfed": "NYFED",
            "cftc": "CFTC", "treasury-auctions": "U.S. Treasury Fiscal Data",
        }[key]
        series_identifier = FRED_SOURCES.get(key)
        old_observations = old_raw = 0
        try:
            old_observations, old_raw = _counts(database_path, source_identifier, series_identifier)
            result = adapters[key]()
            inserted, unchanged, missing = _normalized_counts(result)
            new_observations, new_raw = _counts(database_path, source_identifier, series_identifier)
            results.append(SourceResult(
                source=key,
                status="SUCCESS",
                message="Adapter completed; observations retain source-native units and provenance.",
                inserted=inserted,
                unchanged=unchanged,
                missing=missing,
                raw_records_added=max(0, new_raw - old_raw),
                observations_total=new_observations,
                raw_records_total=new_raw,
            ))
        except Exception as exc:  # noqa: BLE001  # isolate and report each independent source failure
            try:
                new_observations, new_raw = _counts(database_path, source_identifier, series_identifier)
                message = _safe_error(exc)
            except sqlite3.Error as count_error:
                new_observations, new_raw = old_observations, old_raw
                message = f"{_safe_error(exc)}; unable to read post-run counts: {_safe_error(count_error)}"
            results.append(SourceResult(
                source=key,
                status="FAILED",
                message=message,
                observations_total=new_observations,
                raw_records_total=new_raw,
            ))
    return IngestionSummary(str(database_path), start.isoformat(), end.isoformat(), tuple(results))


def _cli_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update Treasury Flow Radar using its existing official-source adapters."
    )
    parser.add_argument(
        "--database",
        default=os.getenv("TREASURY_FLOW_RADAR_DB", str(DEFAULT_DB_PATH)),
        help="SQLite database path (default: TREASURY_FLOW_RADAR_DB or project data path)",
    )
    parser.add_argument(
        "--source", action="append", choices=SOURCE_NAMES, dest="sources",
        help="source to run; repeat for multiple sources (default: all)",
    )
    parser.add_argument("--start-date", type=_cli_date, help="inclusive YYYY-MM-DD start date")
    parser.add_argument("--end-date", type=_cli_date, help="inclusive YYYY-MM-DD end date")
    args = parser.parse_args(argv)
    try:
        summary = run_ingestion(
            database_path=args.database,
            sources=args.sources,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summary.as_dict(), indent=2))
    return 1 if summary.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

