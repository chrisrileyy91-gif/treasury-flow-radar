from __future__ import annotations

from datetime import date, timedelta

import pytest

from treasury_flow_radar.analytics.attribution import attribute_window
from treasury_flow_radar.analytics.calibration import calibrate_priors, event_type


def _days(n: int) -> list[date]:
    start = date(2025, 1, 1)
    return [start + timedelta(days=i) for i in range(n)]


def test_prior_is_the_variance_share_ordinary_days_lack():
    days = _days(60)
    events = {d: {"Jobs report"} for d in days[:10]} | {d: {"Claims"} for d in days[10:13]}
    changes = {d: (4.0 if i % 2 else -4.0) if d in events and "Jobs report" in events[d]
               else (2.0 if i % 2 else -2.0) for i, d in enumerate(days)}
    out = calibrate_priors(changes, events, before=days[-1] + timedelta(days=1),
                           fallback={"Jobs report": 1.0, "Claims": 0.4})
    jobs = out["types"]["Jobs report"]
    assert jobs["prior"] == pytest.approx(1 - 4 / 16)          # variance 4 ordinary vs 16 on jobs days
    assert jobs["measured"] and jobs["distinguishable"] and jobs["basis"].startswith("days with no other")
    claims = out["types"]["Claims"]                              # only 3 days: keep the stated fallback
    assert not claims["measured"] and claims["prior"] == 0.4


def test_calmer_event_days_get_zero_and_the_window_is_excluded():
    days = _days(60)
    events = {d: {"GDP"} for d in days[:10]}
    changes = {d: (1.0 if d in events else 3.0) * (1 if i % 2 else -1) for i, d in enumerate(days)}
    changes[days[-1]] = 500.0                                    # in the window: must not be used
    out = calibrate_priors(changes, events, before=days[-1], fallback={})
    assert out["types"]["GDP"]["prior"] == 0.0 and out["types"]["GDP"]["estimate"] < 0
    assert out["end"] == days[-2].isoformat()


def test_event_type_merges_fomc_variants():
    assert event_type({"short": "FOMC decision (with projections)"}) == "FOMC decision"


def test_assumed_deal_prior_is_capped_at_the_strongest_measured_prior():
    days = [d for d in _days(140) if d.weekday() < 5]
    jobs_days = set(days[:60:6])                                 # 10 jobs days before the window
    level, ten = 5.0, {}
    for i, d in enumerate(days):
        level += (0.04 if d in jobs_days else 0.02) * (1 if i % 2 else -1)
        ten[d] = round(level, 4)
    window = days[-5:]
    deal = {"id": "d", "name": "Deal", "launch_date": window[0].isoformat(), "pricing_date": window[1].isoformat(),
            "tranches": [{"currency": "USD", "amount": 80e9, "maturity_year": 2036, "coupon_percent": 5.0}]}
    result = attribute_window(
        yields={"DGS10": ten}, decomposition_by_date={}, deals=[deal], auctions=[],
        releases=[{"date": d.isoformat(), "short": "Jobs report", "kind": "labor", "weight": 1.0} for d in jobs_days])
    measured = result["calibration"]["types"]["Jobs report"]["prior"]
    assert measured == pytest.approx(0.75, abs=0.01)
    assert result["calibration"]["deal_prior_cap"] == pytest.approx(measured)
    deal = next(c for c in result["candidates"] if c["type"] == "corporate_deal")
    assert deal["prior"] == pytest.approx(measured)             # 80B deal would be 1.0 uncapped


def test_calendar_coverage_starts_where_the_shortest_calendar_starts():
    from treasury_flow_radar.analytics.attribution import calendar_coverage

    releases = [{"date": "2006-01-31", "short": "FOMC decision"}, {"date": "2010-02-12", "short": "CPI"},
                {"date": "2010-03-12", "short": "CPI"}]
    auctions = [{"auction_date": "2024-10-28"}]
    assert calendar_coverage(releases, auctions) == (date(2024, 10, 28), "Treasury auction")
    assert calendar_coverage(releases, []) == (date(2010, 2, 12), "CPI")
    assert calendar_coverage([], []) == (None, None)


def test_long_history_measures_priors_without_changing_the_window():
    days = [d for d in (date(2020, 1, 1) + timedelta(days=i) for i in range(900)) if d.weekday() < 5]
    jobs = set(days[5:-10:21])
    level, ten = 4.0, {}
    for i, d in enumerate(days):
        level += (0.04 if d in jobs else 0.02) * (1 if i % 2 else -1)
        ten[d] = round(level, 4)
    releases = [{"date": d.isoformat(), "short": "Jobs report", "kind": "labor", "weight": 1.0} for d in sorted(jobs)]
    recent = {d: v for d, v in ten.items() if d >= days[-60]}
    short = attribute_window(yields={"DGS10": recent}, decomposition_by_date={}, deals=[], auctions=[],
                             releases=releases)
    long = attribute_window(yields={"DGS10": recent}, decomposition_by_date={}, deals=[], auctions=[],
                            releases=releases, calibration_history={"ten": ten, "releases": releases})
    assert long["calibration"]["types"]["Jobs report"]["days"] > 3 * short["calibration"]["types"]["Jobs report"]["days"]
    assert long["calibration"]["coverage_start"] == min(jobs).isoformat()
    assert long["calibration"]["coverage_limited_by"] == "Jobs report"
    assert [s["date"] for s in long["sessions"]] == [s["date"] for s in short["sessions"]]
    assert long["calibration"]["types"]["Jobs report"]["prior"] == pytest.approx(0.75, abs=0.01)


def _regime_series(years: int = 8):
    """Weekdays over `years`; CPI days are calm early and violent in the last two years."""
    days = [d for d in (date(2018, 1, 1) + timedelta(days=i) for i in range(365 * years)) if d.weekday() < 5]
    cpi = set(days[3::21])
    rare = set(days[100:100 + 21 * 10:21])           # ten days, all long before the recent window
    late = days[-1] - timedelta(days=730)
    level, ten = 4.0, {}
    for i, d in enumerate(days):
        size = 0.02
        if d in cpi:
            size = 0.08 if d >= late else 0.02
        if d in rare:
            size = 0.05
        level += size * (1 if i % 2 else -1)
        ten[d] = round(level, 4)
    releases = ([{"date": d.isoformat(), "short": "CPI", "kind": "inflation", "weight": 1.0} for d in sorted(cpi)]
                + [{"date": d.isoformat(), "short": "Rare", "kind": "growth", "weight": 0.3} for d in sorted(rare)])
    return days, ten, releases


def test_recent_priors_track_the_regime_and_carry_the_long_run_value():
    _days, ten, releases = _regime_series()
    result = attribute_window(yields={"DGS10": ten}, decomposition_by_date={}, deals=[], auctions=[],
                              releases=releases)
    cal = result["calibration"]
    window_start = date.fromisoformat(result["sessions"][0]["date"])
    assert cal["recent_years"] == 3
    assert cal["start"] >= window_start.replace(year=window_start.year - 3).isoformat()
    assert cal["end"] < window_start.isoformat()
    cpi = cal["types"]["CPI"]
    assert cpi["source"] == "recent"
    # The long run averages six calm years with two violent ones; the recent window sees only the violent ones.
    assert cpi["prior"] > cpi["long_run"]["prior"] + 0.1
    assert cal["long_run"]["start"] < cal["start"]


def test_type_with_too_few_recent_days_uses_its_long_run_value():
    _, ten, releases = _regime_series()
    cal = attribute_window(yields={"DGS10": ten}, decomposition_by_date={}, deals=[], auctions=[],
                           releases=releases)["calibration"]
    rare = cal["types"]["Rare"]
    assert rare["source"] == "long run" and rare["measured"]
    assert rare["prior"] == pytest.approx(rare["long_run"]["prior"])
    assert cal["types"]["Treasury auction"]["source"] == "fallback"   # no auctions at all


def test_long_run_sentence_names_the_regime_direction():
    from dashboard.presentation import _long_run_sentence

    cal = {"long_run": {"start": "2010-08-12", "end": "2026-10-09"}}
    louder = {"source": "recent", "estimate": 0.59, "long_run": {"measured": True, "estimate": -0.1, "days": 83}}
    assert "moved the 10-year more in recent years" in _long_run_sentence(louder, cal)
    assert "2010 to 2026 (83 days) it was 0%" in _long_run_sentence(louder, cal)
    same = dict(louder, estimate=0.05)
    assert "about the same" in _long_run_sentence(same, cal)
    assert _long_run_sentence(dict(louder, source="long run"), cal) == ""
