from __future__ import annotations

from datetime import date, timedelta

import pytest

from treasury_flow_radar.analytics.basis_crowding import (
    CONTRACTS,
    TEN_YEAR,
    basis_setup,
    bond_price,
    contract_dv01,
    conversion_factor,
    interpolate_yield,
    par_bond_dv01_per_100,
    percentile_rank,
    realized_vol,
)

CURVE = {2.0: 3.6, 5.0: 3.75, 7.0: 3.95, 10.0: 4.1, 30.0: 4.7}


def _cftc_rows(series: str, day: date, lev_long: float, lev_short: float, oi: float) -> list[dict]:
    base = {"source_identifier": "CFTC", "series_identifier": series, "observation_time": day.isoformat(),
            "unit": "contracts", "frequency": "weekly", "revision": 1}
    rows = []
    for metric, value in (("long", lev_long), ("short", lev_short), ("spreading", 1000.0)):
        rows.append({**base, "logical_key": f"{day}|leveraged_funds|{metric}", "value_numeric": value,
                     "metadata": {"participant_category": "leveraged_funds", "metric": metric}})
    rows.append({**base, "logical_key": f"{day}|all_participants|open_interest", "value_numeric": oi,
                 "metadata": {"participant_category": "all_participants", "metric": "open_interest"}})
    return rows


def _daily(series: str, day: date, value: float) -> dict:
    return {"source_identifier": "FRED", "series_identifier": series, "observation_time": day.isoformat(),
            "value_numeric": value, "unit": "percent", "frequency": "daily", "revision": 1,
            "logical_key": day.isoformat(), "metadata": {}}


def _history(weeks: int, final_short: float) -> list[dict]:
    """Every contract: shorts growing 1K/week, then ``final_short`` in the last week."""
    start = date(2025, 1, 7)
    rows: list[dict] = []
    for i in range(weeks):
        day = start + timedelta(weeks=i)
        short = final_short if i == weeks - 1 else 100_000.0 + 1_000.0 * i
        for series in CONTRACTS:
            rows += _cftc_rows(series, day, 50_000.0, short, 1_000_000.0)
        for sid, tenor in (("DGS2", 2.0), ("DGS5", 5.0), ("DGS7", 7.0), ("DGS10", 10.0), ("DGS30", 30.0)):
            rows.append(_daily(sid, day, CURVE[tenor]))
    return rows


# ------------------------------------------------------------------ pure calculations

def test_percentile_rank_counts_at_or_below_inclusive():
    assert percentile_rank([1, 2, 3, 4], 4) == 100.0
    assert percentile_rank([1, 2, 3, 4], 2) == 50.0
    assert percentile_rank([], 1) is None


def test_interpolation_is_linear_inside_and_flat_outside():
    assert interpolate_yield(CURVE, 6.0) == pytest.approx(3.85)
    assert interpolate_yield(CURVE, 1.75) == 3.6
    assert interpolate_yield(CURVE, 40) == 4.7
    assert interpolate_yield(CURVE, 15.0) == pytest.approx(4.1 + 0.6 * 5 / 20)
    assert interpolate_yield({}, 5) is None


def test_par_bond_prices_at_par_and_six_percent_coupon_has_unit_conversion_factor():
    assert bond_price(4.0, 4.0, 10) == pytest.approx(100.0)
    assert conversion_factor(6.0, 9.5) == pytest.approx(1.0)
    assert conversion_factor(4.0, 10) < 1.0


def test_par_dv01_matches_closed_form_modified_duration():
    y, n = 4.0, 10.0
    # Modified duration of a semiannual par bond: (1 - (1+y/2)^-2n) / y
    modified_duration = (1 - (1 + y / 200) ** (-2 * n)) / (y / 100)
    expected = modified_duration * 100 * 0.0001  # price per 100 face for one basis point
    assert par_bond_dv01_per_100(y, n) == pytest.approx(expected, rel=1e-4)


def test_contract_dv01_estimates_are_ordered_by_duration_and_plausible():
    dv01 = {spec.label: contract_dv01(spec, CURVE) for spec in CONTRACTS.values()}
    assert dv01["2-year"] < dv01["5-year"] < dv01["10-year"] < dv01["Ultra 10"] < dv01["Bond"]
    assert 50 < dv01["10-year"] < 80          # dollars per contract per bp
    assert contract_dv01(CONTRACTS[TEN_YEAR], {}) is None


def test_realized_vol_is_population_standard_deviation():
    assert realized_vol([1.0, -1.0, 1.0, -1.0]) == pytest.approx(1.0)
    assert realized_vol([2.0]) is None


# ------------------------------------------------------------------ assembly

def test_record_short_ranks_at_100th_percentile_and_aggregate_is_dv01_weighted():
    setup = basis_setup(_history(60, 500_000.0))
    agg = setup["crowding"]["aggregate"]
    assert agg["history_weeks"] == 60 and agg["record_short"]
    assert agg["short_percentile"] == 100.0
    weights = sum(contract_dv01(spec, CURVE) for spec in CONTRACTS.values()) / contract_dv01(CONTRACTS[TEN_YEAR], CURVE)
    assert agg["net_10y_equivalents"] == pytest.approx((50_000 - 500_000) * weights)
    ten = next(c for c in setup["crowding"]["contracts"] if c["contract"] == "10-year")
    assert ten["net_contracts"] == -450_000 and ten["net_short_share_of_open_interest_percent"] == pytest.approx(45.0)
    assert ten["short_percentile"] == 100.0


def test_short_history_is_not_ranked_and_state_is_unknown():
    setup = basis_setup(_history(10, 500_000.0))
    assert setup["crowding"]["aggregate"]["short_percentile"] is None
    assert setup["state"]["code"] == "UNKNOWN" and not setup["state"]["flag"]


def test_missing_contract_week_is_excluded_from_aggregate_not_partially_summed():
    rows = _history(60, 500_000.0)
    last = max(r["observation_time"] for r in rows if r["source_identifier"] == "CFTC")
    rows = [r for r in rows if not (r["series_identifier"] == TEN_YEAR and r["observation_time"] == last)]
    agg = basis_setup(rows)["crowding"]["aggregate"]
    assert agg["report_date"] != last and agg["history_weeks"] == 59


def test_funding_spread_and_state_flag_when_sofr_above_iorb():
    rows = _history(60, 500_000.0)
    start = date(2026, 1, 2)
    for i in range(150):
        day = start + timedelta(days=i)
        rows += [_daily("SOFR", day, 4.42 if i >= 140 else 4.38), _daily("IORB", day, 4.40),
                 _daily("SOFR99", day, 4.55)]
    setup = basis_setup(rows)
    funding = setup["funding"]
    assert funding["sofr_minus_iorb_bps"] == pytest.approx(2.0)
    assert funding["sofr_minus_iorb_median_bps"] == pytest.approx(2.0)
    assert funding["sofr99_minus_iorb_bps"] == pytest.approx(15.0)
    assert funding["sofr_above_iorb"] is True
    assert setup["state"]["code"] == "CROWDED_FUNDING_TIGHT" and setup["state"]["flag"]


def test_funding_calm_and_absent_funding_are_distinct_states():
    rows = _history(60, 500_000.0)
    assert basis_setup(rows)["state"]["code"] == "CROWDED_FUNDING_UNKNOWN"
    day = date(2026, 1, 2)
    rows += [_daily("SOFR", day + timedelta(days=i), 4.33) for i in range(12)]
    rows += [_daily("IORB", day + timedelta(days=i), 4.40) for i in range(12)]
    assert basis_setup(rows)["state"]["code"] == "CROWDED_FUNDING_CALM"


def test_report_and_page_include_the_section():
    from dashboard.renderer import render_report
    from treasury_flow_radar.analytics.research import build_research_report
    rows = _history(60, 500_000.0)
    report = build_research_report(rows, start_date=date(2025, 1, 1), end_date=date(2026, 12, 31))
    assert report["basis_setup"]["crowding"]["aggregate"]["short_percentile"] == 100.0
    page = render_report(report)
    assert 'id="basis"' in page and "Basis-trade setup" in page and "10-year-note equivalents" in page
    assert "not the MOVE index" in page or "MOVE" in page


# ------------------------------------------------------------------ synthesis

def _with_funding(rows: list[dict], sofr: float, days: int = 150) -> list[dict]:
    start = date(2026, 1, 2)
    for i in range(days):
        day = start + timedelta(days=i)
        rows += [_daily("SOFR", day, sofr), _daily("IORB", day, 4.40), _daily("SOFR99", day, 4.50)]
    return rows


def _with_vol(rows: list[dict], sessions: int = 200) -> list[dict]:
    start = date(2025, 6, 2)
    rows = [r for r in rows if r["series_identifier"] != "DGS10"]
    # Wide daily swings early, narrow ones in the last 30 sessions: current vol ranks low.
    return rows + [_daily("DGS10", start + timedelta(days=i),
                          4.0 + ((0.10 if i < sessions - 30 else 0.01) if i % 2 else 0.0))
                   for i in range(sessions)]


def test_value_at_percentile_inverts_percentile_rank():
    from treasury_flow_radar.analytics.basis_crowding import value_at_percentile
    history = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    threshold = value_at_percentile(history, 80)
    assert threshold == 8 and percentile_rank(history, threshold) == 80.0
    assert value_at_percentile([], 80) is None


def test_quiet_synthesis_when_small_calm_and_ordinary():
    from treasury_flow_radar.analytics.plumbing_synthesis import synthesize
    rows = _with_vol(_with_funding(_history(60, 90_000.0), 4.38))
    syn = synthesize(basis_setup(rows))
    assert syn["headline"] == "Treasury plumbing is quiet."
    assert "smaller than in most of the stored history" in syn["lines"][0]["text"]
    assert "calm" in syn["lines"][1]["text"]
    assert syn["fed"]["text"].startswith("There is no sign of the funding pressure")
    assert syn["equities"]["evidence_type"] == "MECHANISM"
    assert any("SOFR moving above IORB" in w for w in syn["watch"])
    assert any("growing past" in w for w in syn["watch"])


def test_crowded_and_tight_synthesis_names_streak_and_mechanism():
    from treasury_flow_radar.analytics.plumbing_synthesis import synthesize
    rows = _with_funding(_history(60, 500_000.0), 4.43)
    syn = synthesize(basis_setup(rows))
    assert syn["headline"] in {"The setup for a forced unwind is building.", "Treasury plumbing is under strain."}
    assert "above it for the last 150 sessions" in syn["lines"][1]["text"]
    assert syn["fed"]["evidence_type"] == "MECHANISM" and "not QE in the 2020 sense" in syn["fed"]["text"]
    assert not any("growing past" in w for w in syn["watch"])


def test_missing_funding_does_not_claim_calm():
    from treasury_flow_radar.analytics.plumbing_synthesis import synthesize
    syn = synthesize(basis_setup(_history(60, 90_000.0)))
    assert syn["tone"] == "partial"
    assert "not measured yet" in syn["fed"]["text"]
    assert not any("forced selling." == line["text"][-15:] for line in syn["lines"][:-1])


def test_synthesis_renders_near_end_of_page():
    from dashboard.renderer import render_report
    from treasury_flow_radar.analytics.research import build_research_report
    rows = _with_funding(_history(60, 90_000.0), 4.38)
    page = render_report(build_research_report(rows, start_date=date(2025, 1, 1), end_date=date(2026, 12, 31)))
    assert page.index('id="synthesis"') > page.index('id="basis"')
    assert "What it means for stocks" in page and "Does this point to Fed buying?" in page


def test_dashboard_report_ranks_basis_on_full_history_but_windows_other_sections(tmp_path):
    from dashboard import reporting
    rows = _history(60, 500_000.0)   # 2025-01-07 onward
    old = date(2015, 1, 6)
    for i in range(200):             # ~4 years of much larger shorts, long before the window
        day = old + timedelta(weeks=i)
        for series in CONTRACTS:
            rows += _cftc_rows(series, day, 0.0, 2_000_000.0, 4_000_000.0)
        for sid, tenor in (("DGS2", 2.0), ("DGS5", 5.0), ("DGS7", 7.0), ("DGS10", 10.0), ("DGS30", 30.0)):
            rows.append(_daily(sid, day, CURVE[tenor]))
    original = reporting.load_observations_read_only
    reporting.load_observations_read_only = lambda _path: [dict(r) for r in rows]
    try:
        report = reporting.load_dashboard_report(tmp_path / "unused.sqlite3")
    finally:
        reporting.load_observations_read_only = original
    agg = report["basis_setup"]["crowding"]["aggregate"]
    assert agg["history_weeks"] == 260 and agg["history_start"] == "2015-01-06"
    assert agg["short_percentile"] < 50           # today's short is small next to the old ones
    assert report["scope"]["start_date"] >= "2024-01-01"   # other sections keep the recent window
