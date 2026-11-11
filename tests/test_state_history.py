from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from dashboard.overview import overview, state_history_line
from treasury_flow_radar.analytics.descriptive import _latest_per_date
from treasury_flow_radar.analytics.stock_risk import (
    _point_in_time_lights,
    state_history,
    stock_risk,
)

BANNED = re.compile(r"\b(buy|sell now|should sell|should buy|reduce|trim|exit|hedge your|price target)\b",
                    re.IGNORECASE)
START = date(2025, 1, 1)
N = 320
FUNDING_ON = range(180, 200)      # SOFR 5 bp over IORB
VIX_UP = range(240, 250)          # VIX 25


def _day(i):
    return START + timedelta(days=i)


def _rows(n=N):
    rows = []

    def add(series, i, value):
        rows.append({"series_identifier": series, "observation_time": _day(i).isoformat(), "value_numeric": value})
    level = 4.0
    for i in range(n):
        add("IORB", i, 4.0)
        add("SOFR", i, 4.05 if i in FUNDING_ON else 3.99)
        # Daily 10-year moves shrink over time, so realized volatility keeps falling: rate-vol light stays off.
        step = (12.0 - i * 0.03) / 100 * (1 if i % 2 else -1)
        level += step
        add("DGS10", i, level)
        add("BAMLH0A0HYM2", i, 3.0)
        add("VIXCLS", i, 25.0 if i in VIX_UP else 15.0)
        add("SP500", i, 1000.0 * 1.001 ** i)   # +0.1% a day: every forward 20-session return is +2.02%
    return rows


def test_history_classifies_each_day_and_measures_what_followed():
    h = state_history(_latest_per_date(_rows()))
    states = h["states"]
    assert states["early_warning"]["days"] > 0 and states["early_warning"]["episodes"] == 1
    assert states["fear_elsewhere"]["days"] == len(VIX_UP) and states["fear_elsewhere"]["episodes"] == 1
    assert states["unpriced_stress"]["days"] == 0 and states["unpriced_stress"]["mean_forward_pct"] is None
    assert states["together"]["days"] == 0
    assert states["quiet"]["mean_forward_pct"] == pytest.approx((1.001 ** 20 - 1) * 100)
    assert states["quiet"]["share_up_pct"] == 100
    assert states["fear_elsewhere"]["mean_vix_change"] == pytest.approx(-10.0)
    assert sum(s["days"] for s in states.values()) == h["classified_days"]
    assert h["end"] == _day(N - 1 - 20).isoformat()          # the last day with a full forward window
    assert h["evidence_type"] == "CALCULATION"
    # First 10-year change is day 1, first 20-session window ends day 20, 120th window ends day 139.
    assert h["start"] == _day(139).isoformat()


def test_lights_use_only_data_available_on_each_day():
    full = _point_in_time_lights(_latest_per_date(_rows()))
    cut = _point_in_time_lights(_latest_per_date(_rows(210)))
    for key in ("funding", "rate_vol", "credit"):
        days, table = cut[key]
        assert days and all(full[key][1][d] == table[d] for d in days), key


def test_history_needs_prices_and_vix():
    rows = [r for r in _rows() if r["series_identifier"] != "SP500"]
    assert state_history(_latest_per_date(rows)) is None


def test_card_shows_how_the_current_state_played_out_and_the_full_table():
    risk = stock_risk(_rows(), {"state": {"funding_tight": False, "vol_elevated": False}})
    assert risk["state"] == "quiet"
    line = state_history_line(risk)
    assert "since May 20, 2025, quiet days" in line and "+2.02% on average" in line and "up 100%" in line
    html = overview({"stock_risk": risk})
    assert "How each combination has played out since May 20, 2025" in html
    assert "Quiet on both sides <b>(now)</b>" in html and ">never</td>" in html and "up 100%" in html
    assert "Descriptive, not a forecast." in html


def test_line_for_a_state_that_never_happened_and_for_few_stretches():
    risk = stock_risk(_rows(), {"state": {"funding_tight": False, "vol_elevated": False}})
    risk["state"] = "unpriced_stress"
    assert "hasn't occurred since May 20, 2025" in state_history_line(risk)
    risk["state"] = "fear_elsewhere"
    line = state_history_line(risk)
    assert "10 trading days across 1 separate stretches" in line
    assert "Only 1 separate stretches, so this is history, not a pattern." in line
    assert "against +2.02% after quiet days" in line
    assert not BANNED.search(re.sub(r"<[^>]+>", "", line))
    risk["state"] = "partial"
    assert state_history_line(risk) is None
