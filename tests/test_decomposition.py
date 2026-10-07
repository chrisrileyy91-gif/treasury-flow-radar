from __future__ import annotations

import pytest

from treasury_flow_radar.analytics.decomposition import curve_shape, decompose_moves


def obs(series: str, day: str, value: float | None) -> dict:
    return {"series_identifier": series, "observation_time": day, "value_numeric": value,
            "unit": "percent", "source_identifier": "FRED", "revision": 1}


@pytest.mark.parametrize(("short", "long", "name", "led"), [
    (2, 10, "bear steepener", "long end"),
    (10, 2, "bear flattener", "short end"),
    (-10, -3, "bull steepener", "short end"),
    (-2, -9, "bull flattener", "long end"),
    (5, 5, "parallel rise", "evenly"),
    (-5, 4, "steepening twist", "short end"),
    (3, -6, "flattening twist", "long end"),
    (0.2, -0.3, "little changed", "evenly"),
    (0, 6, "bear steepener", "long end"),
])
def test_curve_shape_names(short, long, name, led):
    shape = curve_shape(short, long)
    assert shape["name"] == name and shape["led_by"] == led
    assert shape["slope_change_bps"] == pytest.approx(long - short)


def test_curve_shape_unknown_without_both_ends():
    assert curve_shape(None, 3) is None and curve_shape(3, None) is None


def test_components_gap_and_term_premium_lag():
    days = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
    rows = []
    for i, d in enumerate(days):
        rows += [obs("DGS10", d, 5.00 + i / 100), obs("DFII10", d, 2.80 + i / 200),
                 obs("T10YIE", d, 2.20 + i / 200), obs("DGS2", d, 4.80), obs("DGS30", d, 5.40 + i / 50)]
    # Term premium is published with a lag: nothing on the last two sessions.
    rows += [obs("THREEFYTP10", d, 1.00 + i / 100) for i, d in enumerate(days[:4])]
    result = decompose_moves(rows)
    latest = result["latest"]
    assert latest["date"] == "2026-10-01"
    five = latest["windows"]["5"]
    assert five["start_date"] == "2026-09-24"
    assert five["nominal_bps"] == pytest.approx(5)
    assert five["real_bps"] == pytest.approx(2.5) and five["breakeven_bps"] == pytest.approx(2.5)
    assert five["gap_bps"] == pytest.approx(0)
    assert five["term_premium_bps"] is None          # not published for the end date: unknown
    assert five["curve"]["name"] == "bear steepener"
    one = latest["windows"]["1"]
    assert one["nominal_bps"] == pytest.approx(1) and one["start_date"] == "2026-09-30"
    tp = result["term_premium"]
    assert tp["date"] == "2026-09-29" and tp["percent"] == pytest.approx(1.03)
    assert tp["change_bps"] is None                  # only 4 sessions published: fewer than 5


def test_market_closure_is_skipped_not_filled():
    rows = [obs("DGS10", "2026-07-02", 4.00), obs("DGS10", "2026-07-03", None),
            obs("DGS10", "2026-07-06", 4.10), obs("DFII10", "2026-07-02", 2.0),
            obs("DFII10", "2026-07-06", 2.06), obs("T10YIE", "2026-07-02", 2.0),
            obs("T10YIE", "2026-07-06", 2.05)]
    one = decompose_moves(rows)["by_date"]["2026-07-06"]["windows"]["1"]
    assert one["start_date"] == "2026-07-02"
    assert one["nominal_bps"] == pytest.approx(10)
    assert one["real_bps"] == pytest.approx(6) and one["breakeven_bps"] == pytest.approx(5)
    assert one["gap_bps"] == pytest.approx(-1)      # reported, never forced to zero
    assert "2026-07-03" not in decompose_moves(rows)["by_date"]


def test_missing_component_series_is_unknown_not_zero():
    rows = [obs("DGS10", "2026-07-01", 4.0), obs("DGS10", "2026-07-02", 4.1)]
    one = decompose_moves(rows)["latest"]["windows"]["1"]
    assert one["nominal_bps"] == pytest.approx(10)
    assert one["real_bps"] is None and one["breakeven_bps"] is None and one["gap_bps"] is None
    assert one["curve"] is None and decompose_moves(rows)["term_premium"] is None
