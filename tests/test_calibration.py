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
