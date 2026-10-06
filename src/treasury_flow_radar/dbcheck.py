"""Safety check for a stored database before it replaces the published copy.

Used by the publishing workflow: the database must open, pass SQLite's integrity
check, and contain at least as many observations as before ingestion. Observations
are immutable revisions, so a correct ingestion run can only add rows; fewer rows
means something went wrong and the previous copy must be kept.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def check_database(path: str | Path, *, min_observations: int = 0) -> dict[str, object]:
    """Return integrity and row counts; raise ValueError if the database is unsafe to keep."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"database not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity check failed: {integrity}")
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sources", "series", "raw_records", "observations")}
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"not a usable Treasury Flow Radar database: {exc}") from exc
    finally:
        connection.close()
    if counts["observations"] < min_observations:
        raise ValueError(
            f"observation count fell from {min_observations} to {counts['observations']}; "
            "keeping the previous database")
    return {"integrity": integrity, **counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Treasury Flow Radar SQLite database.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--min-observations", type=int, default=0,
                        help="fail if the database holds fewer observations than this")
    args = parser.parse_args(argv)
    try:
        result = check_database(args.database, min_observations=args.min_observations)
    except ValueError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
