from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from dashboard.overview import overview
from dashboard.renderer import render_report
from treasury_flow_radar.alert import build_message
from treasury_flow_radar.analytics.report import load_observations_read_only
from treasury_flow_radar.analytics.treasury_cash import (
    cash_guidance_from_rows,
    merge_cash_guidance,
    treasury_cash,
)
from treasury_flow_radar.events import load_cash_guidance
from treasury_flow_radar.run_health import treasury_needs_inspection
from treasury_flow_radar.sources import treasury_refunding as tr

EST_URL = "https://home.treasury.gov/news/press-releases/sb0675"
STMT_URL = "https://home.treasury.gov/news/press-releases/sb0678"
NOW = datetime(2026, 11, 11, 16, tzinfo=UTC)

# Shapes copied from the live pages (probe run 2026-11-11): unquoted attributes, en dashes,
# a non-breaking space, and the word "assuming" as a link in the statement.
RECENT = ("<div><p>Released 3:00 PM Monday, November 2, 2026</p><ul>"
          "<li><a href=/news/press-releases/sb0675>Financing Estimates: 2026 - 4th Quarter</a></li>"
          "<li><a href=/news/press-releases/sb0676>Economic Policy Statements to TBAC: 2026 - 4th Quarter</a></li>"
          "</ul><p>Released 8:30 AM Wednesday, November 4, 2026</p><ul>"
          "<li><a href=/news/press-releases/sb0678>Policy Statement: 2026 - 4th Quarter</a></li></ul></div>")
ESTIMATES = (
    '<time datetime=2026-11-02T12:00:00Z class=datetime>November 2, 2026</time><h2>Treasury Announces Marketable '
    "Borrowing Estimates</h2><p>The U.S. Department of the Treasury today announced its current estimates of "
    "privately-held net marketable borrowing for the October–December 2026 and January–March 2027 quarters.</p>"
    "<ul><li>During the October–December 2026 quarter, Treasury expects to borrow $618 billion in privately-held "
    "net marketable debt, assuming an end-of-December cash balance of $850 billion. The borrowing estimate is $10 "
    "billion lower than announced in August 2026.</li><li>During the January–March 2027 quarter, Treasury expects "
    "to borrow $800 billion in privately-held net marketable debt, assuming an end-of-March cash balance of $1.000 "
    "trillion.</li><li>During the July–September 2026 quarter, Treasury borrowed $742 billion in privately-held "
    "net marketable debt and ended the quarter with a cash balance of $984 billion. In August 2026, Treasury "
    "estimated borrowing of $739 billion and assumed an end-of-September cash balance of $950 billion.</li></ul>")
STATEMENT = (
    "<time class=datetime>November 4, 2026</time><h3 id=cash-balance>CASH BALANCE</h3><p>Treasury is "
    "<a href=/news/press-releases/sb0675>assuming</a> an $850 billion cash balance at the end of December.\xa0 "
    "However, based on current projections for the upcoming refunding quarter, Treasury estimates that the size of "
    "the Treasury General Account (TGA) could peak at $1.025 trillion (plus or minus $50 billion) in late November "
    "and late January.\xa0 These figures are consistent with Treasury’s long-standing cash balance policy and are "
    "driven by the large outflows expected to occur at those times.</p>")


class _Response:
    def __init__(self, body: str, status: int = 200):
        self.body, self.status = body.encode(), status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _opener(pages):
    return lambda request, timeout: _Response(pages[request.full_url])


def _pages(recent=RECENT, estimates=ESTIMATES, statement=STATEMENT):
    return {tr.RECENT_URL: recent, EST_URL: estimates, STMT_URL: statement}


def _plan(**kw):
    return tr.fetch_plan(opener=_opener(_pages(**kw)), clock=lambda: NOW)


def test_reads_both_quarter_targets_and_the_projected_peak():
    plan = _plan()
    assert (plan.estimates_url, plan.statement_url) == (EST_URL, STMT_URL)
    assert (plan.estimates_date, plan.statement_date) == (date(2026, 11, 2), date(2026, 11, 4))
    assert [(t.quarter_end, t.billions) for t in plan.targets] == [(date(2026, 12, 31), 850.0), (date(2027, 3, 31), 1000.0)]
    assert plan.targets[0].quarter == "October-December 2026"
    assert (plan.peak.billions, plan.peak.band_billions) == (1025.0, 50.0)
    assert [(w.label, w.start, w.end) for w in plan.peak.windows] == [
        ("late November", date(2026, 11, 16), date(2026, 11, 30)),
        ("late January", date(2027, 1, 16), date(2027, 1, 31))]   # January rolls into the next year


def test_matches_the_hand_entered_ledger():
    plan, seed = _plan(), load_cash_guidance()
    assert [(t.quarter_end.isoformat(), t.billions) for t in plan.targets] == [
        (t["quarter_end"], t["cash_balance_usd_bn"]) for t in seed["targets"]]
    assert [(w.start.isoformat(), w.end.isoformat()) for w in plan.peak.windows] == [
        (p["window_start"], p["window_end"]) for p in seed["peaks"]]


def test_a_statement_without_a_peak_is_fine():
    statement = STATEMENT.split("However,")[0] + "</p>"
    assert _plan(statement=statement).peak is None


@pytest.mark.parametrize(("change", "message"), [
    ({"recent": RECENT.replace("Financing Estimates", "Borrowing Outlook")}, "no Financing Estimates link"),
    ({"recent": RECENT.replace("href=/news/press-releases/sb0678", "href=https://example.com/x")},
     "off home.treasury.gov"),
    ({"estimates": ESTIMATES.replace("assuming an end-of-December cash balance of $850 billion",
                                     "assuming an end-of-December cash balance of about $850 billion")},
     "2 cash assumptions mentioned but 1"),
    ({"estimates": ESTIMATES.replace("assuming an end-of-", "assumes an end-of-")}, "no quarter-end cash assumption"),
    ({"estimates": ESTIMATES.replace("end-of-March cash", "end-of-February cash")}, "states an end-of-February"),
    ({"estimates": ESTIMATES.replace("class=datetime>November 2, 2026</time>", ">soon</time>")}, "no release date"),
    ({"statement": STATEMENT.replace("$850 billion cash balance", "$900 billion cash balance")},
     r"Policy Statement assumes \$900B"),
    ({"statement": STATEMENT.replace("(plus or minus $50 billion)", "(give or take)")}, "peak is mentioned"),
    ({"statement": STATEMENT.replace("in late November and late January", "in late November and around tax day")},
     "peak timing"),
    ({"statement": STATEMENT.replace("November 4, 2026", "February 3, 2027")}, "not from the same refunding"),
])
def test_fails_closed_whenever_anything_is_uncertain(change, message):
    with pytest.raises(tr.RefundingError, match=message):
        _plan(**change)


def test_a_page_that_stopped_updating_fails():
    with pytest.raises(tr.RefundingError, match="still shows the 2026-11-02 estimates"):
        tr.fetch_plan(opener=_opener(_pages()), clock=lambda: datetime(2027, 3, 15, tzinfo=UTC))


def test_http_errors_fail():
    with pytest.raises(tr.RefundingError, match="HTTP 503"):
        tr.fetch_plan(opener=lambda request, timeout: _Response("", 503), clock=lambda: NOW)


def _count(db, series=None):
    conn = sqlite3.connect(db)
    where = "" if series is None else f"join series s on s.id=o.series_id where s.identifier='{series}'"
    return conn.execute(f"select count(*) from observations o {where}").fetchone()[0]


def test_ingest_stores_once_and_records_revisions(tmp_path):
    db = tmp_path / "t.sqlite3"
    first = tr.ingest_refunding(database_path=db, plan=_plan())
    assert first == {"inserted": 5, "unchanged": 0, "missing": 0}      # 2 targets, 2 peak windows, 1 statement
    assert tr.ingest_refunding(database_path=db, plan=_plan()) == {"inserted": 0, "unchanged": 5, "missing": 0}
    raw = sqlite3.connect(db).execute("select count(*) from raw_records").fetchone()[0]
    noisy = _plan(estimates=ESTIMATES + "<!-- per-request token 8f2 -->")
    assert tr.ingest_refunding(database_path=db, plan=noisy)["inserted"] == 0
    assert sqlite3.connect(db).execute("select count(*) from raw_records").fetchone()[0] == raw   # no new copy
    revised = _plan(estimates=ESTIMATES.replace("$1.000 trillion", "$950 billion"))
    assert tr.ingest_refunding(database_path=db, plan=revised)["inserted"] == 1
    revs = sqlite3.connect(db).execute("""select o.revision, o.value_numeric from observations o join series s
        on s.id=o.series_id where s.identifier='TREASURY_CASH_ASSUMPTION' and o.logical_key='2027-03-31'
        order by o.revision""").fetchall()
    assert revs == [(1, 1000.0), (2, 950.0)]


def test_card_plan_comes_from_the_database_and_matches_the_seed(tmp_path):
    db = tmp_path / "t.sqlite3"
    tr.ingest_refunding(database_path=db, plan=_plan())
    stored = cash_guidance_from_rows(load_observations_read_only(db))
    assert stored["latest_statement"] == "2026-11-04" and stored["latest_estimates"] == "2026-11-02"
    merged = merge_cash_guidance(stored, load_cash_guidance())
    rows = [{"series_identifier": "WTREGEN", "observation_time": "2026-11-04", "value_numeric": 957_000.0}]
    from_db = treasury_cash(rows, merged)["plan"]
    from_seed = treasury_cash(rows, load_cash_guidance())["plan"]
    for key in ("phase", "outlook", "gap_to_target_bn", "planned_change_to_target_bn"):
        assert from_db[key] == from_seed[key]
    assert from_db["peak"]["rise_to_peak_bn"] == pytest.approx(68.0)
    assert from_db["target"]["sources"] == [EST_URL] and from_db["peak"]["sources"] == [STMT_URL]


def test_a_newer_statement_without_a_peak_clears_the_old_peak(tmp_path):
    db = tmp_path / "t.sqlite3"
    tr.ingest_refunding(database_path=db, plan=_plan())
    later = (STATEMENT.split("However,")[0] + "</p>").replace("November 4, 2026", "November 5, 2026")
    tr.ingest_refunding(database_path=db, plan=_plan(statement=later))
    stored = cash_guidance_from_rows(load_observations_read_only(db))
    assert stored["latest_statement"] == "2026-11-05" and stored["peaks"] == []
    assert merge_cash_guidance(stored, load_cash_guidance())["peaks"] == []


def test_newer_targets_replace_the_seed_and_next_release_drops_once_passed():
    seed = load_cash_guidance()
    stored = {"targets": [{"quarter_end": "2027-03-31", "cash_balance_usd_bn": 900.0, "sources": [{"url": EST_URL}]},
                          {"quarter_end": "2027-06-30", "cash_balance_usd_bn": 875.0, "sources": [{"url": EST_URL}]}],
              "peaks": [], "latest_statement": "2027-02-03", "latest_estimates": "2027-02-01"}
    merged = merge_cash_guidance(stored, seed)
    assert [(t["quarter_end"], t["cash_balance_usd_bn"]) for t in merged["targets"]] == [
        ("2026-12-31", 850), ("2027-03-31", 900.0), ("2027-06-30", 875.0)]
    assert merged["next_release"] is None                              # the seed's 2027-02-01 has passed
    assert merge_cash_guidance(None, seed)["next_release"] == "2027-02-01"


def test_rows_off_treasury_are_ignored():
    rows = [{"series_identifier": "TREASURY_CASH_ASSUMPTION", "logical_key": "2026-12-31", "value_numeric": 1.0,
             "metadata": {"url": "https://example.com/x"}}]
    assert cash_guidance_from_rows(rows) is None


def test_a_failed_read_shows_inspect_treasury_on_the_page_and_in_pheme():
    report = {"end_date": "2026-11-11", "sources": [
        {"source": "fred-tga", "status": "SUCCESS"},
        {"source": "treasury-refunding", "status": "FAILED", "message": "no Financing Estimates link"}]}
    assert treasury_needs_inspection(report) and not treasury_needs_inspection(None)
    message = build_message(report)
    assert "Inspect Treasury." in message and "no Financing Estimates link" in message and "FRED failed" not in message
    assert build_message({"sources": [{"source": "treasury-refunding", "status": "SUCCESS"}]}) is None
    rows = [{"series_identifier": "WTREGEN", "observation_time": "2026-11-04", "value_numeric": 957_000.0}]
    view = {"cash": treasury_cash(rows, load_cash_guidance()), "inspect_treasury": True}
    assert "this is the last one stored. Inspect Treasury." in overview(view)


def test_header_shows_each_flag(tmp_path):
    from dashboard.reporting import load_dashboard_report
    report = load_dashboard_report(tmp_path / "missing.sqlite3")
    report["run_health"] = {"fred_failed": False, "treasury_failed": True}
    page = render_report(report)
    assert "Inspect Treasury." in page and "Inspect FRED." not in page
