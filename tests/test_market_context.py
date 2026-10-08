from __future__ import annotations

from datetime import date, timedelta

from treasury_flow_radar.analytics.candidates import _spread_check
from treasury_flow_radar.analytics.market_context import market_context


def _days(n: int) -> list[date]:
    out, d = [], date(2026, 1, 5)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _wiggle(days: list[date], base: float, step: float) -> dict[date, float]:
    return {d: base + (step if i % 2 else 0) for i, d in enumerate(days)}


def test_flight_to_safety_and_rates_specific_days_are_read_from_signs():
    days = _days(60)
    window = days[-2:]
    ten = _wiggle(days, 4.50, 0.01)
    spx = _wiggle(days, 7000, 7)                     # usual day about ±0.1%
    hy = _wiggle(days, 3.00, 0.01)                   # usual day ±1 bp
    ten[window[0]], spx[window[0]], hy[window[0]] = ten[days[-3]] - 0.06, spx[days[-3]] * 0.97, hy[days[-3]] + 0.15
    ten[window[1]], spx[window[1]], hy[window[1]] = ten[window[0]] + 0.05, spx[window[0]], hy[window[0]]
    ctx = market_context(ten_year=ten, market={"SP500": spx, "BAMLH0A0HYM2": hy}, window=window)
    first, second = ctx["sessions"]
    assert first["cells"]["SP500"]["unusual"] and first["cells"]["SP500"]["change"] < -2.9
    assert first["read"].startswith("Consistent with a flight to safety")
    assert second["read"].startswith("Stocks and credit were within their usual daily range")
    assert set(ctx["missing"]) == {"BAMLC0A0CM", "DTWEXBGS"}


def test_no_market_series_is_unavailable_not_empty_evidence():
    days = _days(10)
    assert market_context(ten_year=_wiggle(days, 4.5, 0.01), market={}, window=days[-5:])["status"] == "UNAVAILABLE"


def test_ig_spread_check_compares_with_a_usual_move_over_the_same_sessions():
    days = _days(60)
    ig = _wiggle(days, 0.80, 0.01)                   # usual daily change 1 bp
    launch, start, end = days[-3], days[-4], days[-1]
    ig[end] = ig[start] + 0.06                       # +6 bp over 3 sessions; usual about ±1.7 bp
    check = _spread_check(ig, start, end, launch)
    assert check["status"] == "pass" and "+6 bp" in check["detail"]
    ig[end] = ig[start]
    assert _spread_check(ig, start, end, launch)["status"] == "fail"
    assert _spread_check({}, start, end, launch)["status"] == "n/a"
