import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from treasury_flow_radar.database import database, get_observations, get_observations_as_of
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

RETRIEVED = datetime(2026, 10, 9, 16, tzinfo=UTC)
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
    assert len(records) == 10
    assert all(r.contract_name for r in records)
    assert {CONTRACTS[k][0] for k in ("042601", "044601", "043602", "043607", "020601")} == {
        "ust_2_year_note_042601",
        "ust_5_year_note_044601",
        "ust_10_year_note_043602",
        "ust_ultra_10_year_note_043607",
        "ust_30_year_bond_020601",
    }
    decoy = dict(
        json.loads(fixture_text())[0],
        cftc_contract_market_code="999999",
        market_and_exchange_names="TREASURY GENERIC INDEX - EXCHANGE",
    )
    assert parse_rows([decoy]) == ()


def test_category_long_short_spreading_and_open_interest_are_preserved():
    record = parse_rows(fixture_text())[0]
    measures = {(m.participant, m.metric): m for m in record.metrics}
    assert measures["all_participants", "open_interest"].value == 950000
    assert measures["dealer_intermediary", "long"].value == 124000
    assert measures["dealer_intermediary", "long"].source_field == "dealer_positions_long_all"
    assert record.contract_name == "2-YEAR U.S. TREASURY NOTES"
    assert measures["dealer_intermediary", "short"].value == 255000
    assert measures["dealer_intermediary", "spreading"].value == 14000
    assert measures["asset_manager_institutional", "spreading"].value == 31000
    assert measures["leveraged_funds", "long"].value == 90000
    assert measures["other_reportables", "short"].value == 32000
    assert measures["nonreportable", "long"].value == 70000
    assert ("nonreportable", "spreading") not in measures
    assert len(measures) == 15
    expected_fields = {
        "dealer_positions_long_all",
        "dealer_positions_short_all",
        "dealer_positions_spread_all",
        "asset_mgr_positions_long",
        "asset_mgr_positions_short",
        "asset_mgr_positions_spread",
        "lev_money_positions_long",
        "lev_money_positions_short",
        "lev_money_positions_spread",
        "other_rept_positions_long",
        "other_rept_positions_short",
        "other_rept_positions_spread",
        "nonrept_positions_long_all",
        "nonrept_positions_short_all",
    }
    assert {
        m.source_field for m in record.metrics if m.metric != "open_interest"
    } == expected_fields
    assert (
        measures["dealer_intermediary", "long"].value
        != measures["dealer_intermediary", "short"].value
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234", 1234), ("0", 0), ("-25", -25), ("1.0", 1)],
)
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


@pytest.mark.parametrize("marker", ["", ".", "*", "NA", "N/A", "NULL", "-"])
def test_provider_missing_markers_are_not_zero(marker):
    row = json.loads(fixture_text())[0]
    row["dealer_positions_long_all"] = marker
    measure = next(
        m
        for m in parse_rows([row])[0].metrics
        if (m.participant, m.metric) == ("dealer_intermediary", "long")
    )
    assert measure.value is None
    assert measure.raw_value == marker


def test_reporting_date_is_not_retrieval_date():
    r = parse_rows(fixture_text())[0]
    assert r.report_date.isoformat() == "2026-09-29"
    assert r.as_of_date == "260929"
    assert r.report_date != RETRIEVED.date()
    assert (
        parse_rows(
            [
                dict(
                    json.loads(fixture_text())[0],
                    report_date_as_yyyy_mm_dd="2026-09-29T23:30:00-05:00",
                )
            ]
        )[0].report_date.isoformat()
        == "2026-09-29"
    )
    with pytest.raises(CftcResponseError, match="invalid report date"):
        parse_rows(
            [dict(json.loads(fixture_text())[0], report_date_as_yyyy_mm_dd="2026-09-29garbage")]
        )


def test_malformed_and_conflicting_rows_fail():
    with pytest.raises(CftcResponseError, match="invalid JSON"):
        parse_rows("{bad")
    with pytest.raises(CftcResponseError, match="report date"):
        parse_rows(
            [
                {
                    "cftc_contract_market_code": "042601",
                    "market_and_exchange_names": "test",
                    "contract_market_name": "test",
                }
            ]
        )
    row = json.loads(fixture_text())[0]
    row["dealer_positions_short_all"] = "1.5"
    with pytest.raises(CftcResponseError, match="non-integer"):
        parse_rows([row])
    row = json.loads(fixture_text())[0]
    del row["dealer_positions_short_all"]
    with pytest.raises(CftcResponseError, match="missing source field"):
        parse_rows([row])
    row = json.loads(fixture_text())[0]
    del row["contract_market_name"]
    with pytest.raises(CftcResponseError, match="contract_market_name"):
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
    assert "contract_market_name" in params["$select"][0]
    assert "api_key" not in params
    assert request.get_header("Accept") == "application/json" and timeout > 0


def test_cftc_query_limits_history_without_changing_weekly_observations():
    client, opener = client_for(fixture_text())
    page = client.fetch_history(start_date=date(2025, 10, 1), end_date=date(2026, 10, 1))[0]
    params = parse_qs(urlsplit(opener.requests[0][0].full_url).query)
    where = params["$where"][0]
    assert "2025-10-01T00:00:00.000" in where
    assert "2026-10-01T23:59:59.999" in where
    assert page.records[0].report_date == date(2026, 9, 29)


def test_history_pages_are_ordered_and_complete():
    rows = json.loads(fixture_text())
    bodies = [json.dumps(rows[i : i + 3]) for i in range(0, len(rows), 3)]
    client, opener = client_for(bodies, page_size=3)
    pages = client.fetch_history()
    assert [p.offset for p in pages] == [0, 3, 6, 9]
    assert sum(len(p.records) for p in pages) == 10
    assert len(opener.requests) == 4


def test_identical_rows_repeated_across_pages_are_deduplicated():
    rows = json.loads(fixture_text())
    client, _ = client_for([json.dumps(rows[:3]), json.dumps(rows[2:4])], page_size=3)
    pages = client.fetch_history()
    assert sum(len(p.records) for p in pages) == 4
    assert len(pages[1].records) == 1


def test_conflicting_rows_repeated_across_pages_fail_before_ingestion():
    rows = json.loads(fixture_text())
    conflicting = dict(rows[2], dealer_positions_long_all="999999")
    client, _ = client_for([json.dumps(rows[:3]), json.dumps([conflicting, rows[3]])], page_size=3)
    with pytest.raises(CftcResponseError, match="conflicting duplicate across pages"):
        client.fetch_history()


def test_complete_mocked_pipeline_keeps_raw_payload_and_provenance(tmp_path):
    raw = fixture_text()
    result = ingest_cftc(database_path=tmp_path / "cot.sqlite3", client=client_for(raw)[0])
    assert result == {"contracts": 5, "inserted": 150, "unchanged": 0, "missing": 3}
    with database(tmp_path / "cot.sqlite3") as conn:
        rows = get_observations(conn)
        assert len(rows) == 150 and all(r["unit"] == "contracts" for r in rows)
        assert all(r["publication_time"] is None for r in rows)
        row = next(r for r in rows if r["logical_key"] == "2026-09-29|dealer_intermediary|long")
        assert row["value_numeric"] == 124000
        assert row["observation_time"] == "2026-09-29T00:00:00.000000Z"
        assert row["retrieval_time"] == "2026-10-09T16:00:00.000000Z"
        meta = json.loads(row["metadata_json"])
        assert meta["participant_label"] == "Dealer/Intermediary" and meta["metric"] == "long"
        assert meta["source_field"] == "dealer_positions_long_all"
        assert meta["source_dataset_id"] == DATASET_ID
        assert meta["report_variant"] == "tff_futures_only"
        assert meta["contract_market_name"] == "2-YEAR U.S. TREASURY NOTES"
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
        source_meta = conn.execute("SELECT metadata_json FROM sources").fetchone()[0]
        source_meta = json.loads(source_meta)
        assert source_meta["dataset_id"] == "gpe5-46if"
        assert source_meta["report_variant"] == "tff_futures_only"
        assert source_meta["options_scope"] == "futures only; excludes options positions"


def test_idempotency_duplicate_and_series_registration(tmp_path):
    raw = fixture_text()
    db = tmp_path / "cot.sqlite3"
    first = ingest_cftc(database_path=db, client=client_for(raw)[0])
    second = ingest_cftc(database_path=db, client=client_for(raw)[0])
    assert first["inserted"] == 150 and second["unchanged"] == 150
    with database(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 150
        assert conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 5


def test_revisions_keep_old_rows_and_link_new_raw_payload(tmp_path):
    old = json.loads(fixture_text())
    new = json.loads(fixture_text())
    new[0]["dealer_positions_long_all"] = "124001"
    ticks = iter([RETRIEVED, datetime(2026, 10, 12, 16, tzinfo=UTC)])
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
        before_revision = get_observations_as_of(
            conn, RETRIEVED + timedelta(hours=1), series_id=sid
        )
        after_revision = get_observations_as_of(
            conn, datetime(2026, 10, 13, tzinfo=UTC), series_id=sid
        )
        asof_key = "2026-09-29|dealer_intermediary|long"
        assert (
            next(x["value_numeric"] for x in before_revision if x["logical_key"] == asof_key)
            == 124000
        )
        assert (
            next(x["value_numeric"] for x in after_revision if x["logical_key"] == asof_key)
            == 124001
        )


def test_http_failure_is_explicit():
    client, _ = client_for([URLError("offline")])
    with pytest.raises(CftcRequestError, match="request failed"):
        client.fetch_history()


def test_participant_definitions_and_spreading_scope():
    assert CATEGORIES["dealer_intermediary"][1]["spreading"] == "dealer_positions_spread_all"
    assert "spreading" in CATEGORIES["other_reportables"][1]
    assert "spreading" not in CATEGORIES["nonreportable"][1]

