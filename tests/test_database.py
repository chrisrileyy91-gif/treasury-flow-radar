from datetime import datetime, timezone
import sqlite3

import pytest

from treasury_flow_radar.database import (
    DuplicateObservationError, connect, database, get_observations,
    get_observations_as_of, initialize_database, insert_observation,
    insert_raw_record, register_series, register_source,
)

UTC = timezone.utc
TUE = datetime(2026, 10, 5, 16, tzinfo=UTC)
FRI = datetime(2026, 10, 7, 12, tzinfo=UTC)
SAT = datetime(2026, 10, 8, 12, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "data" / "test.sqlite3"
    initialize_database(path)
    return path


def setup_source(conn):
    source = register_source(conn, identifier="example", name="Example provider",
                             source_type="public_data")
    series = register_series(conn, source_id=source, identifier="TEST",
                             name="Test series", frequency="daily", default_unit="index")
    return source, series


def test_initialization_and_registration(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        assert source > 0 and series > 0
    with connect(db_path) as conn:
        assert {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} >= {"sources", "series", "observations", "raw_records"}


def test_observation_insert_and_retrieval(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        raw_id = insert_raw_record(conn, source_id=source, payload='{"value":"5.2"}',
                                   retrieval_time=FRI, external_record_id="row-1",
                                   content_type="application/json")
        obs_id = insert_observation(
            conn, source_id=source, series_id=series, logical_key="2026-10-05",
            observation_time=TUE, publication_time=FRI, retrieval_time=FRI,
            value_numeric=5.2, unit="percent", raw_value="5.2", raw_record_id=raw_id)
        rows = get_observations(conn, series_id=series)
        assert rows[0]["id"] == obs_id
        assert rows[0]["raw_record_id"] == raw_id
        assert rows[0]["value_numeric"] == 5.2


def test_foreign_keys_are_enforced(db_path):
    with database(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            register_series(conn, source_id=999, identifier="X", name="X")
        source, series = setup_source(conn)
        with pytest.raises(sqlite3.IntegrityError):
            insert_observation(conn, source_id=source, series_id=999, logical_key="x",
                               observation_time=TUE, retrieval_time=FRI)


def test_exact_duplicate_is_idempotent_and_conflict_rejected(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        args = dict(source_id=source, series_id=series, logical_key="k",
                    observation_time=TUE, retrieval_time=FRI, publication_time=FRI,
                    value_text="five")
        first = insert_observation(conn, **args)
        assert insert_observation(conn, **args) == first
        with pytest.raises(DuplicateObservationError):
            insert_observation(conn, **{**args, "value_text": "six"})


def test_revisions_preserve_history_and_as_of_selects_known_revision(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        original = insert_observation(
            conn, source_id=source, series_id=series, logical_key="period-1",
            observation_time=TUE, publication_time=FRI, retrieval_time=FRI,
            value_numeric=100.0, revision=1)
        revised_at = datetime(2026, 10, 9, 12, tzinfo=UTC)
        revised = insert_observation(
            conn, source_id=source, series_id=series, logical_key="period-1",
            observation_time=TUE, publication_time=revised_at, retrieval_time=revised_at,
            value_numeric=95.0, revision=2, revision_of_id=original)
        assert [r["id"] for r in get_observations(conn, logical_key="period-1")] == [
            original, revised]
        assert get_observations_as_of(conn, SAT)[0]["id"] == original
        assert get_observations_as_of(conn, revised_at)[0]["id"] == revised


def test_as_of_excludes_not_yet_published_or_retrieved(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        insert_observation(
            conn, source_id=source, series_id=series, logical_key="future",
            observation_time=TUE, publication_time=FRI, retrieval_time=FRI,
            value_text="released")
        before = datetime(2026, 10, 6, 12, tzinfo=UTC)
        assert get_observations_as_of(conn, before) == []
        assert len(get_observations_as_of(conn, SAT)) == 1
        # Publication known, but retrieval has not yet happened.
        late_retrieval = datetime(2026, 10, 9, 12, tzinfo=UTC)
        insert_observation(
            conn, source_id=source, series_id=series, logical_key="retrieved-late",
            observation_time=TUE, publication_time=FRI, retrieval_time=late_retrieval,
            value_text="not yet retrieved")
        assert len(get_observations_as_of(conn, SAT)) == 1


def test_timestamps_are_normalized_to_utc_and_naive_values_rejected(db_path):
    eastern_offset = datetime.fromisoformat("2026-10-07T08:00:00-04:00")
    with database(db_path) as conn:
        source, series = setup_source(conn)
        insert_observation(conn, source_id=source, series_id=series, logical_key="zone",
                           observation_time=TUE, publication_time=eastern_offset,
                           retrieval_time=FRI, value_text="v")
        row = get_observations(conn)[0]
        assert row["publication_time"] == "2026-10-07T12:00:00.000000Z"
        with pytest.raises(ValueError, match="timezone-aware"):
            insert_observation(conn, source_id=source, series_id=series, logical_key="bad",
                               observation_time=datetime(2026, 10, 5), retrieval_time=FRI)


def test_publication_time_may_be_unknown(db_path):
    with database(db_path) as conn:
        source, series = setup_source(conn)
        insert_observation(conn, source_id=source, series_id=series, logical_key="unknown-pub",
                           observation_time=TUE, retrieval_time=FRI, value_text="v")
        assert len(get_observations_as_of(conn, SAT)) == 1
        assert get_observations_as_of(conn, datetime(2026, 10, 6, tzinfo=UTC)) == []
