from __future__ import annotations

from datetime import date, timedelta

import pytest

from treasury_flow_radar.analytics.attribution import attribute_window


def sessions(start: str, n: int) -> list[date]:
    day, out = date.fromisoformat(start), []
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def record(led: str, nominal: float = 0, breakeven: float = 0) -> dict:
    return {"windows": {"1": {"nominal_bps": nominal, "breakeven_bps": breakeven,
                              "curve": {"led_by": led}}}}


def test_split_unexplained_and_ranking_follow_the_formula():
    days = sessions("2026-09-28", 6)                     # Sep 28 base, then 5 window sessions
    ten = dict(zip(days, [5.00, 5.04, 5.06, 5.00, 5.04, 5.04], strict=True))  # +4, +2, -6, +4, 0
    decomp = {days[1].isoformat(): record("long end"), days[2].isoformat(): record("long end"),
              days[3].isoformat(): record("short end"), days[4].isoformat(): record("short end")}
    deal = {"id": "d", "name": "Deal", "launch_date": days[1].isoformat(), "pricing_date": days[2].isoformat(),
            "tranches": [{"currency": "USD", "amount": 40e9, "maturity_year": 2036, "coupon_percent": 5.0}]}
    result = attribute_window(
        yields={"DGS10": ten}, decomposition_by_date=decomp, deals=[deal], auctions=[],
        releases=[{"date": days[4].isoformat(), "short": "Jobs report", "kind": "labor", "weight": 1.0}])
    by = {c["name"]: c for c in result["candidates"]}
    # Deal: +4 and +2 hedge-build sessions, long-led -> fit 1, prior 1 -> 6 bp.
    # Next session -6 short-led -> unwind fit 0.25 -> 1.5 bp; 4.5 bp unexplained.
    assert by["Deal"]["attributed_bps"] == pytest.approx(7.5)
    assert by["Jobs report"]["attributed_bps"] == pytest.approx(4.0)
    assert result["total_abs_bps"] == pytest.approx(16)
    assert result["unexplained_bps"] == pytest.approx(4.5)
    assert result["best"]["name"] == "Deal" and result["best"]["share"] == pytest.approx(7.5 / 16)
    assert result["evidence_type"] == "INFERENCE"


def test_shared_session_is_split_and_never_over_attributed():
    days = sessions("2026-10-05", 6)
    ten = dict(zip(days, [5.0, 5.0, 5.0, 5.0, 5.0, 5.10], strict=True))
    decomp = {days[-1].isoformat(): record("short end", nominal=10, breakeven=1)}
    releases = [{"date": days[-1].isoformat(), "short": "CPI", "kind": "inflation", "weight": 1.0},
                {"date": days[-1].isoformat(), "short": "Jobs report", "kind": "labor", "weight": 1.0}]
    result = attribute_window(yields={"DGS10": ten}, decomposition_by_date=decomp, deals=[],
                              auctions=[], releases=releases)
    shares = {c["name"]: c["attributed_bps"] for c in result["candidates"]}
    assert shares == {"CPI": pytest.approx(5), "Jobs report": pytest.approx(5)}
    assert result["unexplained_bps"] == pytest.approx(0)


def test_small_deal_and_wrong_direction_explain_little():
    days = sessions("2026-10-05", 6)
    ten = dict(zip(days, [5.0, 5.0, 5.0, 5.0, 5.0, 4.95], strict=True))   # falls 5 bp
    decomp = {days[-1].isoformat(): record("long end")}
    small = {"id": "s", "name": "Small deal", "launch_date": days[-1].isoformat(), "pricing_date": days[-1].isoformat(),
             "tranches": [{"currency": "USD", "amount": 1e9, "maturity_year": 2036, "coupon_percent": 5.0}]}
    result = attribute_window(yields={"DGS10": ten}, decomposition_by_date=decomp, deals=[small],
                              auctions=[], releases=[])
    assert result["best"] is None                       # a fall does not fit a hedge build
    assert result["unexplained_bps"] == pytest.approx(5)


def test_weekend_release_is_not_attributed():
    days = sessions("2026-10-05", 6)
    ten = dict(zip(days, [5.0] * 5 + [5.05], strict=True))
    result = attribute_window(yields={"DGS10": ten}, decomposition_by_date={}, deals=[], auctions=[],
                              releases=[{"date": "2026-10-10", "short": "X", "kind": "labor", "weight": 1.0}])
    assert result["candidates"] == [] and result["unexplained_bps"] == pytest.approx(5)


def test_release_with_daily_dates_is_excluded_from_calendar():
    from treasury_flow_radar.analytics.research import _release_calendar
    rows = [{"series_identifier": "FRED_RELEASE_180", "observation_time": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
             "metadata": {"release_id": 180}} for i in range(200)]
    rows += [{"series_identifier": "FRED_RELEASE_10", "observation_time": "2026-09-11", "metadata": {"release_id": 10}}]
    releases, notes = _release_calendar(rows)
    assert [r["short"] for r in releases] == ["CPI"]
    assert notes and "not a publication calendar" in notes[0]
