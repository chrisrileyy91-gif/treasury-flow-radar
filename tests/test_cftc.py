import json
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from treasury_flow_radar.database import database, get_observations
from treasury_flow_radar.sources.cftc import (
    CATEGORIES,
    CONTRACTS,
    DATASET_ID,
    CftcClient,
    CftcRequestError,
    CftcResponseError,
    ingest_cftc,
    parse_rows,
)

RETRIEVED = datetime(2026, 10, 4, 16, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "cftc_tff_futures_only_treasuries.json"


class Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json", "ETag": '"fixture"'}

    def __init__(self, content):
        self.content = content.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.content


class Opener:
    def __init__(self, payloads):
        self.payloads, self.requests = list(payloads), []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return Response(payload)


def client_for(payloads, *, clock=None, page_size=5000):
    if isinstance(payloads, str):
        payloads = [payloads]
    opener = Opener(payloads)
    return CftcClient(
        opener=opener, clock=clock or (lambda: RETRIEVED), page_size=page_size
    ), opener


def fixture_text():
    return FIXTURE.read_text(encoding="utf-8")


def test_official_codes_cover_requested_treasury_contracts():
    records = parse_rows(fixture_text())
    assert {r.code for r in records} == set(CONTRACTS)
    assert [CONTRACTS[k][0] for k in ("042601", "044601", "043602", "043607", "020601")]


def test_category_long_short_spreading_and_open_interest_are_preserved():
    record = parse_rows(fixture_text())[0]
    measures = {(m.participant, m.metric): m for m in record.metrics}
    assert measures["all_participants", "open_interest"].value == 950000
    assert measures["dealer_intermediary", "long"].value == 124000
    assert measures["dealer_intermediary", "short"].value == 255000
    assert measures["dealer_intermediary", "spreading"].value == 14000
    assert measures["asset_manager_institutional", "spreading"].value == 31000
    assert measures["leveraged_funds", "long"].value == 90000
    assert measures["other_reportables", "short"].value == 32000
    assert measures["nonreportable", "long"].value == 70000
    assert ("nonreportable", "spreading") not in measures
    assert len(measures) == 15


@pytest.mark.parametrize(("raw", "expected"), [("1,234", 1234), ("0", 0), ("-25", -25)])
def test_numeric_counts_preserve_raw_and_distinguish_missing(raw, expected):
    row = json.loads(fixture_text())[0]
    row["dealer_positions_long_all"] = raw
    metric = next(
        m
        for m in parse_rows([row])[0].metrics
        if (m.participant, m.metric) == ("dealer_intermediary", "long")
    )
    assert (metric.value, metric.raw_value) == (expected, raw)
    row["dealer_positions_short_all"] = None
    missing = next(
        m
        for m in parse_rows([row])[0].metrics
        if (m.participant, m.metric) == ("dealer_intermediary", "short")
    )
    assert missing.value is None and missing.raw_value == ""


def test_reporting_date_is_not_retrieval_date():
    r = parse_rows(fixture_text())[0]
    assert r.report_date.isoformat() == "2026-09-29"
    assert r.as_of_date == "260929"
    assert r.report_date != RETRIEVED.date()


def test_malformed_and_conflicting_rows_fail():
    with pytest.raises(CftcResponseError, match="invalid JSON"):
        parse_rows("{bad")
    with pytest.raises(CftcResponseError, match="report date"):
        parse_rows([{"cftc_contract_market_code": "042601", "market_and_exchange_names": "test"}])
    row = json.loads(fixture_text())[0]
    row["dealer_positions_short_all"] = "1.5"
    with pytest.raises(CftcResponseError, match="non-integer"):
        parse_rows([row])
    row = json.loads(fixture_text())[0]
    with pytest.raises(CftcResponseError, match="conflicting duplicate"):
        parse_rows([row, dict(row, dealer_positions_long_all=1)])


def test_public_official_api_query_has_no_credentials():
    client, opener = client_for(fixture_text())
    client.fetch_history()
    request, timeout = opener.requests[0]
    url = urlsplit(request.full_url)
    params = parse_qs(url.query)
    assert url.netloc == "publicreporting.cftc.gov"
    assert url.path == f"/resource/{DATASET_ID}.json"
    assert "042601" in params["$where"][0]
    assert "dealer_positions_spread_all" in params["$select"][0]
    assert "api_key" not in params
    assert request.get_header("Accept") == "application/json" and timeout > 0


def test_history_pages_are_ordered_and_complete():
    rows = json.loads(fixture_text())
    bodies = [json.dumps(rows[:2]), json.dumps(rows[2:4]), json.dumps(rows[4:])]
    client, opener = client_for(bodies, page_size=2)
    pages = client.fetch_history()
    assert [p.offset for p in pages] == [0, 2, 4]
    assert len(opener.requests) == 3


def test_complete_mocked_pipeline_keeps_raw_payload_and_provenance(tmp_path):
    raw = fixture_text()
    result = ingest_cftc(database_path=tmp_path / "cot.sqlite3", client=client_for(raw)[0])
    assert result == {"contracts": 5, "inserted": 75, "unchanged": 0, "missing": 0}
    with database(tmp_path / "cot.sqlite3") as conn:
        rows = get_observations(conn)
        assert len(rows) == 75 and all(r["unit"] == "contracts" for r in rows)
        assert all(r["publication_time"] is None for r in rows)
        row = next(r for r in rows if r["logical_key"] == "2026-09-29|dealer_intermediary|long")
        assert row["value_numeric"] == 124000
        assert row["observation_time"] == "2026-09-29T00:00:00.000000Z"
        assert row["retrieval_time"] == "2026-10-04T16:00:00.000000Z"
        meta = json.loads(row["metadata_json"])
        assert meta["participant_label"] == "Dealer/Intermediary" and meta["metric"] == "long"
        linked = conn.execute(
            """SELECT r.payload,src.identifier,s.identifier,s.metadata_json
               FROM observations o JOIN raw_records r ON r.id=o.raw_record_id
               JOIN sources src ON src.id=o.source_id JOIN series s ON s.id=o.series_id
               WHERE o.id=?""",
            (row["id"],),
        ).fetchone()
        assert linked["payload"] == raw and linked[1] == "CFTC"
        assert "042601" in linked[2]
        assert json.loads(linked[3])["cftc_contract_market_code"] == "042601"


def test_idempotency_duplicate_and_series_registration(tmp_path):
    raw = fixture_text()
    db = tmp_path / "cot.sqlite3"
    first = ingest_cftc(database_path=db, client=client_for(raw)[0])
    second = ingest_cftc(database_path=db, client=client_for(raw)[0])
    assert first["inserted"] == 75 and second["unchanged"] == 75
    with database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 75
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 5


def test_revisions_keep_old_rows_and_link_new_raw_payload(tmp_path):
    old = json.loads(fixture_text())
    new = json.loads(fixture_text())
    new[0]["dealer_positions_long_all"] = "124001"
    ticks = iter([RETRIEVED, datetime(2026, 10, 5, 16, tzinfo=UTC)])
    db = tmp_path / "cot.sqlite3"
    for rows in (old, new):
        ingest_cftc(
            database_path=db,
            client=CftcClient(opener=Opener([json.dumps(rows)]), clock=lambda: next(ticks)),
        )
    with database(db) as conn:
        sid = conn.execute("SELECT id FROM series WHERE identifier LIKE '%042601'").fetchone()[0]
        rows = get_observations(
            conn, series_id=sid, logical_key="2026-09-29|dealer_intermediary|long"
        )
        assert [r["revision"] for r in rows] == [1, 2]
        assert [r["value_numeric"] for r in rows] == [124000, 124001]
        assert rows[1]["revision_of_id"] == rows[0]["id"]
        assert rows[1]["raw_record_id"] != rows[0]["raw_record_id"]


def test_http_failure_is_explicit():
    client, _ = client_for([URLError("offline")])
    with pytest.raises(CftcRequestError, match="request failed"):
        client.fetch_history()


def test_participant_definitions_and_spreading_scope():
    assert CATEGORIES["dealer_intermediary"][1]["spreading"] == "dealer_positions_spread_all"
    assert "spreading" in CATEGORIES["other_reportables"][1]
    assert "spreading" not in CATEGORIES["nonreportable"][1]

