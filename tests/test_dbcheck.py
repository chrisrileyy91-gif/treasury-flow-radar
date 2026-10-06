from __future__ import annotations

import pytest

from treasury_flow_radar.database import initialize_database
from treasury_flow_radar.dbcheck import check_database, main


def test_healthy_database_reports_counts(tmp_path):
    db = tmp_path / "ok.sqlite3"
    initialize_database(db)
    result = check_database(db)
    assert result["integrity"] == "ok" and result["observations"] == 0


def test_shrinking_observation_count_is_refused(tmp_path):
    db = tmp_path / "ok.sqlite3"
    initialize_database(db)
    with pytest.raises(ValueError, match="observation count fell from 5 to 0"):
        check_database(db, min_observations=5)
    assert main(["--database", str(db), "--min-observations", "5"]) == 1


def test_missing_or_corrupt_database_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        check_database(tmp_path / "absent.sqlite3")
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(ValueError, match="not a usable"):
        check_database(junk)
