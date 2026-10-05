"""Read-only JSON research report CLI."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

from treasury_flow_radar.analytics.research import build_research_report


def load_observations_read_only(database_path: str | Path) -> list[dict[str, object]]:
    """Load latest observation revisions and provenance without opening SQLite for writes."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    uri = f"file:{quote(str(path), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT o.id AS observation_id, o.source_id, o.series_id,
                      s.identifier AS source_identifier, s.url AS source_url,
                      se.identifier AS series_identifier, se.name AS series_name,
                      se.frequency AS series_frequency, o.logical_key, o.revision,
                      o.observation_time, o.publication_time, o.retrieval_time,
                      o.value_numeric, o.value_text, o.unit, o.raw_value,
                      o.raw_record_id, o.metadata_json
                 FROM observations AS o
                 JOIN sources AS s ON s.id=o.source_id
                 JOIN series AS se ON se.id=o.series_id AND se.source_id=o.source_id
                 WHERE o.revision=(
                     SELECT MAX(v.revision) FROM observations AS v
                      WHERE v.source_id=o.source_id AND v.series_id=o.series_id
                        AND v.logical_key=o.logical_key
                 )
                 ORDER BY o.observation_time, s.identifier, se.identifier, o.logical_key"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result.append(item)
        return result
    finally:
        connection.close()


def generate_report(
    database_path: str | Path,
    *,
    start_date: date,
    end_date: date,
    threshold_bps: float = 5.0,
    auction_window_days: int = 3,
) -> dict[str, object]:
    rows = load_observations_read_only(database_path)
    return build_research_report(
        rows,
        start_date=start_date,
        end_date=end_date,
        threshold_bps=threshold_bps,
        auction_window_days=auction_window_days,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a descriptive Treasury research report.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--threshold-bps", type=float, default=5.0)
    parser.add_argument("--auction-window-days", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        report = generate_report(
            args.database,
            start_date=args.start_date,
            end_date=args.end_date,
            threshold_bps=args.threshold_bps,
            auction_window_days=args.auction_window_days,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"research report failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

