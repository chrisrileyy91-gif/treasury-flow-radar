"""SQLite persistence for observations and their provenance."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "treasury_flow_radar.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    identifier TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    instrument_type TEXT,
    frequency TEXT,
    default_unit TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (source_id, identifier),
    UNIQUE (source_id, id)
);

CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    external_record_id TEXT,
    payload TEXT NOT NULL,
    content_type TEXT,
    payload_sha256 TEXT NOT NULL,
    retrieval_time TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_id, external_record_id, payload_sha256),
    UNIQUE (source_id, id)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    series_id INTEGER NOT NULL,
    logical_key TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    revision_of_id INTEGER REFERENCES observations(id) ON DELETE RESTRICT,
    observation_time TEXT NOT NULL,
    publication_time TEXT,
    retrieval_time TEXT NOT NULL,
    value_text TEXT,
    value_numeric REAL,
    unit TEXT,
    raw_value TEXT,
    raw_record_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id, series_id) REFERENCES series(source_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (source_id, raw_record_id) REFERENCES raw_records(source_id, id) ON DELETE RESTRICT,
    UNIQUE (source_id, series_id, logical_key, revision)
);

CREATE INDEX IF NOT EXISTS idx_observations_time
    ON observations(series_id, observation_time);
CREATE INDEX IF NOT EXISTS idx_observations_logical_revision
    ON observations(source_id, series_id, logical_key, revision);
CREATE INDEX IF NOT EXISTS idx_observations_knowledge_time
    ON observations(retrieval_time, publication_time);
CREATE INDEX IF NOT EXISTS idx_raw_records_source_retrieval
    ON raw_records(source_id, retrieval_time);
"""

class DuplicateObservationError(ValueError):
    """Raised when an existing logical revision is submitted with different contents."""


def _timestamp(value: datetime | str | None, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError("timestamp is required")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a configured database connection with foreign keys enabled."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database(path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Yield a connection and commit on success, roll back on error, then close it."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_database(path: str | Path = DEFAULT_DB_PATH) -> None:
    """Create the database file and schema; safe to call repeatedly."""
    with database(path) as conn:
        conn.executescript(_SCHEMA)


def register_source(
    conn: sqlite3.Connection,
    *,
    identifier: str,
    name: str,
    source_type: str,
    url: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Register a provider. Re-registering its identifier returns the existing id."""
    now = _timestamp(datetime.now(timezone.utc))
    conn.execute(
        """INSERT INTO sources(identifier, name, source_type, url, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(identifier) DO NOTHING""",
        (identifier, name, source_type, url, _json(metadata), now),
    )
    row = conn.execute("SELECT id FROM sources WHERE identifier = ?", (identifier,)).fetchone()
    assert row is not None
    return int(row["id"])


def register_series(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    identifier: str,
    name: str,
    description: str | None = None,
    instrument_type: str | None = None,
    frequency: str | None = None,
    default_unit: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Register a provider series or instrument and return its id."""
    now = _timestamp(datetime.now(timezone.utc))
    conn.execute(
        """INSERT INTO series
        (source_id, identifier, name, description, instrument_type, frequency,
         default_unit, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, identifier) DO NOTHING""",
        (source_id, identifier, name, description, instrument_type, frequency,
         default_unit, _json(metadata), now),
    )
    row = conn.execute(
        "SELECT id FROM series WHERE source_id = ? AND identifier = ?",
        (source_id, identifier),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def insert_raw_record(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    payload: str,
    retrieval_time: datetime | str,
    external_record_id: str | None = None,
    content_type: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Preserve one raw response/record, deduplicated by provider ID and payload hash."""
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO raw_records
        (source_id, external_record_id, payload, content_type, payload_sha256,
         retrieval_time, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, external_record_id, payload_sha256) DO NOTHING""",
        (source_id, external_record_id, payload, content_type, digest,
         _timestamp(retrieval_time), _json(metadata)),
    )
    row = conn.execute(
        """SELECT id FROM raw_records
        WHERE source_id = ? AND external_record_id IS ? AND payload_sha256 = ?""",
        (source_id, external_record_id, digest),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def insert_observation(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    series_id: int,
    logical_key: str,
    observation_time: datetime | str,
    retrieval_time: datetime | str,
    publication_time: datetime | str | None = None,
    value_text: str | None = None,
    value_numeric: float | None = None,
    unit: str | None = None,
    raw_value: str | None = None,
    raw_record_id: int | None = None,
    revision: int = 1,
    revision_of_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert an immutable observation version; exact duplicate submissions are idempotent."""
    if revision < 1:
        raise ValueError("revision must be at least 1")
    if (revision == 1) != (revision_of_id is None):
        raise ValueError("revision 1 has no predecessor; later revisions require revision_of_id")
    observation_ts = _timestamp(observation_time)
    publication_ts = _timestamp(publication_time, optional=True)
    retrieval_ts = _timestamp(retrieval_time)
    created_ts = _timestamp(datetime.now(timezone.utc))
    normalized_metadata = _json(metadata)
    identity = (source_id, series_id, logical_key, revision)
    existing = conn.execute(
        """SELECT * FROM observations
        WHERE source_id = ? AND series_id = ? AND logical_key = ? AND revision = ?""",
        identity,
    ).fetchone()
    fields = (revision_of_id, observation_ts, publication_ts, retrieval_ts, value_text,
              value_numeric, unit, raw_value, raw_record_id, normalized_metadata)
    if existing is not None:
        prior = tuple(existing[k] for k in (
            "revision_of_id", "observation_time", "publication_time", "retrieval_time",
            "value_text", "value_numeric", "unit", "raw_value", "raw_record_id", "metadata_json"
        ))
        if prior != fields:
            raise DuplicateObservationError(
                "this logical observation revision already exists with different contents"
            )
        return int(existing["id"])
    if revision_of_id is not None:
        predecessor = conn.execute(
            """SELECT source_id, series_id, logical_key, revision FROM observations WHERE id = ?""",
            (revision_of_id,),
        ).fetchone()
        if predecessor is None or (
            predecessor["source_id"], predecessor["series_id"], predecessor["logical_key"],
            predecessor["revision"] + 1
        ) != (source_id, series_id, logical_key, revision):
            raise ValueError("revision predecessor must be the previous revision of this observation")
    cursor = conn.execute(
        """INSERT INTO observations
        (source_id, series_id, logical_key, revision, revision_of_id, observation_time,
         publication_time, retrieval_time, value_text, value_numeric, unit, raw_value,
         raw_record_id, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*identity, revision_of_id, observation_ts, publication_ts, retrieval_ts, value_text,
         value_numeric, unit, raw_value, raw_record_id, normalized_metadata, created_ts),
    )
    return int(cursor.lastrowid)


def get_observations(
    conn: sqlite3.Connection,
    *,
    source_id: int | None = None,
    series_id: int | None = None,
    logical_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return observation versions, optionally filtered, in deterministic time order."""
    clauses, parameters = [], []
    for column, value in (("source_id", source_id), ("series_id", series_id),
                          ("logical_key", logical_key)):
        if value is not None:
            clauses.append(f"{column} = ?")
            parameters.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM observations" + where +
        " ORDER BY observation_time, logical_key, revision, retrieval_time",
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def get_observations_as_of(
    conn: sqlite3.Connection,
    as_of: datetime | str,
    *,
    source_id: int | None = None,
    series_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return latest revisions knowable at as_of; both publication and retrieval are gated."""
    cutoff = _timestamp(as_of)
    filters, parameters = [], [cutoff, cutoff]
    if source_id is not None:
        filters.append("o.source_id = ?")
        parameters.append(source_id)
    if series_id is not None:
        filters.append("o.series_id = ?")
        parameters.append(series_id)
    extra = (" AND " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        f"""SELECT o.* FROM observations AS o
        WHERE o.retrieval_time <= ?
          AND (o.publication_time IS NULL OR o.publication_time <= ?)
          {extra}
          AND NOT EXISTS (
            SELECT 1 FROM observations AS newer
            WHERE newer.source_id = o.source_id
              AND newer.series_id = o.series_id
              AND newer.logical_key = o.logical_key
              AND newer.revision > o.revision
              AND newer.retrieval_time <= ?
              AND (newer.publication_time IS NULL OR newer.publication_time <= ?)
          )
        ORDER BY o.observation_time, o.logical_key""",
        [*parameters, cutoff, cutoff],
    ).fetchall()
    return [dict(row) for row in rows]
