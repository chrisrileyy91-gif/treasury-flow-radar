"""Synthetic offline coverage for Stage 8 descriptive analytics."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from treasury_flow_radar.analytics import (
    ConfirmationThresholds,
    Direction,
    EvidenceType,
    Observation,
    build_confirmation_vector,
    curve_metrics,
    level_changes,
    market_price_returns,
    positioning_metrics,
    yield_metrics,
)


def obs(
    series: str, day: date, value: float | None, *, unit: str | None = None, **kwargs
) -> Observation:
    return Observation(
        series, day, value, unit, logical_key=kwargs.pop("logical_key", day.isoformat()), **kwargs
    )


def weekdays(start: date, n: int) -> list[date]:
    found: list[date] = []
    day = start
    while len(found) < n:
        if day.weekday() < 5:
            found.append(day)
        day += timedelta(days=1)
    return found


def test_yield_daily_change_and_basis_points():
    rows = [
        obs("DGS10", date(2025, 1, 2), 4.00, unit="Percent"),
        obs("DGS10", date(2025, 1, 3), 4.08, unit="Percent"),
    ]
    result = yield_metrics(rows, "DGS10")[-1]
    assert result.daily_change_percentage_points == pytest.approx(0.08)
    assert result.daily_change_bps == pytest.approx(8.0)
    assert result.evidence_type == EvidenceType.CALCULATION


def test_yield_5_observation_change():
    days = weekdays(date(2025, 1, 6), 6)
    result = yield_metrics(
        [obs("DGS2", d, i / 10, unit="Percent") for i, d in enumerate(days)], "DGS2"
    )
    assert result[4].change_5_observations_bps is None
    assert result[5].change_5_observations_bps == pytest.approx(50)


def test_yield_20_observation_change():
    days = weekdays(date(2025, 1, 6), 21)
    result = yield_metrics(
        [obs("DGS10", d, i / 100, unit="Percent") for i, d in enumerate(days)], "DGS10"
    )
    assert result[-2].change_20_observations_bps is None
    assert result[-1].change_20_observations_bps == pytest.approx(20)


def test_yield_rolling_mean_and_volatility_are_defined():
    days = weekdays(date(2025, 2, 3), 6)
    increments = [0, 1, 3, 6, 10, 15]
    result = yield_metrics(
        [
            obs("DGS10", d, 4 + i / 100, unit="Percent")
            for d, i in zip(days, increments, strict=True)
        ],
        "DGS10",
    )[-1]
    assert result.rolling_mean_5_percent == pytest.approx(4.07)
    assert result.rolling_volatility_5_bps == pytest.approx(1.41421356237)


def test_missing_yield_values_break_changes_and_rolling_windows():
    days = weekdays(date(2025, 1, 6), 7)
    rows = [
        obs("DGS2", day, None if i == 3 else 4 + i / 100, unit="Percent")
        for i, day in enumerate(days)
    ]
    result = yield_metrics(rows, "DGS2")
    assert result[3].yield_percent is None
    assert result[3].daily_change_bps is None
    assert result[4].daily_change_bps is None
    assert result[6].rolling_mean_5_percent is None


def test_yield_revisions_use_latest_version_and_deterministic_sort():
    d1, d2 = date(2025, 1, 2), date(2025, 1, 3)
    rows = [
        obs("DGS10", d2, 4.2),
        obs("DGS10", d1, 4.0, revision=1),
        obs("DGS10", d1, 4.1, revision=2),
    ]
    result = yield_metrics(rows, "DGS10")
    assert [row.observation_time for row in result] == [d1, d2]
    assert result[0].yield_percent == 4.1
    assert result[1].daily_change_bps == pytest.approx(10)
    assert yield_metrics(list(reversed(rows)), "DGS10") == result


def test_yield_units_are_explicit_and_invalid_units_rejected():
    with pytest.raises(ValueError, match="percentage-point"):
        yield_metrics([obs("DGS10", date(2025, 1, 2), 4.0, unit="basis_points")], "DGS10")


def test_curve_is_10y_minus_2y_in_basis_points():
    day = date(2025, 1, 2)
    result = curve_metrics([obs("DGS2", day, 4.0), obs("DGS10", day, 4.4)])
    assert result[0].spread_10y_minus_2y_bps == pytest.approx(40)


def test_curve_change_uses_previous_shared_observation_only():
    days = weekdays(date(2025, 3, 3), 7)
    rows = []
    for i, day in enumerate(days):
        rows.extend([obs("DGS2", day, 4), obs("DGS10", day, 4.4 + i / 100)])
    result = curve_metrics(rows)
    assert result[1].daily_change_bps == pytest.approx(1)
    assert result[5].change_5_observations_bps == pytest.approx(5)
    assert result[6].change_5_observations_bps == pytest.approx(5)


def test_curve_supports_20_observation_change():
    days = weekdays(date(2025, 3, 3), 21)
    rows = []
    for i, day in enumerate(days):
        rows.extend([obs("DGS2", day, 4), obs("DGS10", day, 4 + i / 100)])
    result = curve_metrics(rows)
    assert result[-1].change_20_observations_bps == pytest.approx(20)


def test_curve_does_not_fabricate_dates_or_fill_a_missing_leg():
    d1, d2, d3 = weekdays(date(2025, 4, 7), 3)
    rows = [
        obs("DGS2", d1, 4),
        obs("DGS10", d1, 4.4),
        obs("DGS2", d2, 4),
        obs("DGS10", d3, 4.6),
        obs("DGS2", d3, 4),
    ]
    result = curve_metrics(rows)
    assert [r.observation_time for r in result] == [d1, d3]
    assert [r.spread_10y_minus_2y_bps for r in result] == pytest.approx([40, 60])
    assert result[1].daily_change_bps == pytest.approx(20)


def cftc(
    series: str, day: date, participant: str, metric: str, value: float, logical: str
) -> Observation:
    return Observation(
        series,
        day,
        value,
        "contracts",
        logical_key=logical,
        metadata={"participant_category": participant, "metric": metric},
        frequency="weekly",
    )


def test_cftc_net_position_and_change_by_reporting_observation():
    d1, d2 = weekdays(date(2025, 5, 5), 2)
    rows = [
        cftc("tff_10y", d1, "dealer_intermediary", "long", 100, f"{d1}|dealer|long"),
        cftc("tff_10y", d1, "dealer_intermediary", "short", 70, f"{d1}|dealer|short"),
        cftc("tff_10y", d2, "dealer_intermediary", "long", 120, f"{d2}|dealer|long"),
        cftc("tff_10y", d2, "dealer_intermediary", "short", 80, f"{d2}|dealer|short"),
    ]
    result = positioning_metrics(rows)
    assert result[0].net_position_contracts == 30
    assert result[1].net_position_contracts == 40
    assert result[1].net_change_contracts == 10
    assert result[1].reporting_frequency == "weekly"


def test_cftc_spreading_is_retained_separately_from_net():
    day = date(2025, 6, 3)
    rows = [
        cftc("tff_2y", day, "leveraged_funds", m, v, f"{day}|lev|{m}")
        for m, v in [("long", 50), ("short", 45), ("spreading", 700)]
    ]
    result = positioning_metrics(rows)[0]
    assert result.participant == "leveraged_fund"
    assert result.net_position_contracts == 5
    assert result.spreading_contracts == 700
    assert (
        result.net_position_contracts
        != result.long_contracts - result.short_contracts + result.spreading_contracts
    )


def test_cftc_asset_manager_net_and_no_daily_upsampling():
    days = weekdays(date(2025, 7, 1), 2)
    rows = []
    for i, day in enumerate(days):
        rows += [
            cftc("tff_bond", day, "asset_manager_institutional", "long", 10 + i, f"{day}|am|l"),
            cftc("tff_bond", day, "asset_manager_institutional", "short", 4, f"{day}|am|s"),
        ]
    result = positioning_metrics(rows)
    assert len(result) == 2
    assert result[-1].net_position_contracts == 7
    assert result[-1].net_change_contracts == 1
    assert result[-1].reporting_frequency == "weekly"


def test_missing_cftc_side_does_not_infer_net_or_bridge_change():
    d1, d2 = weekdays(date(2025, 8, 4), 2)
    rows = [
        cftc("x", d1, "dealer_intermediary", "long", 10, "1"),
        cftc("x", d1, "dealer_intermediary", "short", 3, "2"),
        cftc("x", d2, "dealer_intermediary", "long", 11, "3"),
    ]
    result = positioning_metrics(rows)
    assert result[-1].net_position_contracts is None
    assert result[-1].net_change_contracts is None


def test_cftc_non_contract_unit_is_rejected():
    day = date(2025, 8, 11)
    row = Observation(
        "cftc-series",
        day,
        10,
        "million_us_dollars",
        logical_key="dealer-long",
        metadata={"participant_category": "dealer_intermediary", "metric": "long"},
    )
    with pytest.raises(ValueError, match="contract units"):
        positioning_metrics([row])


def test_nyfed_native_unit_change_and_meaningful_percentage():
    d1, d2 = weekdays(date(2025, 9, 1), 2)
    result = level_changes(
        [
            obs("pdpos", d1, 1_000, unit="million_us_dollars", frequency="weekly"),
            obs("pdpos", d2, 1_100, unit="million_us_dollars", frequency="weekly"),
        ],
        "pdpos",
    )
    assert result[-1].change_from_prior == 100
    assert result[-1].percent_change_from_prior == pytest.approx(10)
    assert result[-1].unit == "million_us_dollars"
    assert result[-1].frequency == "weekly"


def test_nyfed_zero_prior_has_no_percentage_change():
    days = weekdays(date(2025, 9, 8), 2)
    result = level_changes([obs("pd", days[0], 0), obs("pd", days[1], 50)], "pd")
    assert result[-1].change_from_prior == 50
    assert result[-1].percent_change_from_prior is None


def test_nyfed_missing_observation_stays_missing_and_breaks_delta():
    days = weekdays(date(2025, 9, 15), 3)
    result = level_changes(
        [obs("pd", days[0], 100), obs("pd", days[1], None), obs("pd", days[2], 120)], "pd"
    )
    assert result[1].value is None
    assert result[2].change_from_prior is None


def test_market_price_returns_and_zero_price_guard():
    days = weekdays(date(2025, 10, 6), 21)
    values = list(range(100, 121))
    result = market_price_returns(
        [obs("HYG", d, v, unit="USD") for d, v in zip(days, values, strict=True)], "HYG"
    )
    assert result[1].daily_return_percent == pytest.approx(1)
    assert result[5].return_5_observations_percent == pytest.approx(5)
    assert result[20].return_20_observations_percent == pytest.approx(20)
    zero = market_price_returns([obs("IWM", days[0], 0), obs("IWM", days[1], 2)], "IWM")
    assert zero[-1].daily_return_percent is None


def test_market_price_missing_points_are_not_interpolated():
    days = weekdays(date(2025, 10, 6), 4)
    result = market_price_returns(
        [obs("DXY", days[0], 100), obs("DXY", days[2], None), obs("DXY", days[3], 102)], "DXY"
    )
    assert len(result) == 3
    assert result[1].price is None
    assert result[2].daily_return_percent is None
    assert days[1] not in {r.observation_time for r in result}


@pytest.mark.parametrize(
    "delta,expected",
    [
        (0.6, Direction.RISING),
        (-0.6, Direction.FALLING),
        (0.5, Direction.FLAT),
        (None, Direction.UNKNOWN),
    ],
)
def test_direction_threshold_boundaries(delta, expected):
    vector = build_confirmation_vector(treasury_yield_change_bps=delta)
    assert vector.treasury_yields == expected


def test_market_confirmation_missing_series_and_yield_wording():
    vector = build_confirmation_vector(
        treasury_yield_change_bps=8,
        hyg_return_percent=-1,
        iwm_return_percent=None,
        dxy_return_percent=0.05,
    )
    assert vector.treasury_yields == Direction.RISING
    assert vector.treasury_yield_interpretation == "yield rose / Treasury price pressure"
    assert vector.hyg == Direction.FALLING
    assert vector.iwm == Direction.UNKNOWN
    assert vector.dxy == Direction.FLAT
    assert vector.evidence_type == EvidenceType.OBSERVATION


def test_confirmation_thresholds_are_configurable_and_nonnegative():
    thresholds = ConfirmationThresholds(yield_bps=3, price_return_percent=0.5)
    result = build_confirmation_vector(
        treasury_yield_change_bps=2, hyg_return_percent=-0.4, thresholds=thresholds
    )
    assert result.treasury_yields == Direction.FLAT
    assert result.hyg == Direction.FLAT
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ConfirmationThresholds(yield_bps=-1)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ConfirmationThresholds(price_return_percent=float("nan"))


def test_mapping_projection_and_evidence_tiers():
    item = Observation.from_mapping(
        {
            "series_id": "DGS10",
            "observation_time": "2025-01-02T00:00:00Z",
            "value_numeric": "4.25",
            "unit": "Percent",
            "metadata_json": '{"provider":"FRED"}',
        }
    )
    assert item.value == 4.25
    assert item.metadata["provider"] == "FRED"
    assert item.evidence_type == EvidenceType.FACT
    assert EvidenceType.FACT.value == "FACT"
    assert {m.evidence_type for m in yield_metrics([item], "DGS10")} == {EvidenceType.CALCULATION}


def test_nonfinite_values_and_same_revision_conflicts_fail_clearly():
    with pytest.raises(ValueError, match="finite"):
        Observation.from_mapping(
            {"series_id": "DGS10", "observation_time": "2025-01-02", "value_numeric": float("nan")}
        )
    day = date(2025, 1, 2)
    with pytest.raises(ValueError, match="conflicting observation revision"):
        yield_metrics([obs("DGS10", day, 4, revision=1), obs("DGS10", day, 5, revision=1)], "DGS10")

