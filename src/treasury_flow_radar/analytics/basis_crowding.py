"""Basis-trade setup read: leveraged-fund crowding, overnight funding, and realized volatility.

The question this answers is not "where are yields going" but "how much stress would it take
to force an unwind of the cash-futures basis trade". Three inputs, each a CALCULATION over
stored official observations:

1. **Crowding** — CFTC TFF leveraged-fund net futures positions, per contract and summed across
   contracts in 10-year-note equivalents (DV01-weighted), ranked against their own stored history.
2. **Funding** — SOFR (and its 99th percentile) minus the Fed's interest rate on reserve
   balances (IORB), same calendar date, in basis points.
3. **Volatility** — realized volatility of daily 10-year yield changes. This is *not* the ICE
   BofA MOVE index (implied volatility), which is a licensed ICE product with no public feed here.

The combined state is a display rule over those calculations (thresholds below are documented
constants, not calibrated probabilities). It describes a setup; it does not time or predict an
unwind, and a short leveraged-fund futures position is consistent with a basis trade but does
not prove one: the cash and repo legs are not observed in CFTC data.

DV01 method (ESTIMATE, ``ctd_proxy_par_bond_over_cf``)
------------------------------------------------------
Futures DV01 ~= DV01 of the cheapest-to-deliver (CTD) note per $100 / its conversion factor,
scaled to the contract's face value. The CTD and its coupon are not ingested, so:

* CTD remaining maturity is proxied by the *shortest* maturity in the CME deliverable basket.
  With yields below the 6% conversion-factor coupon, lower-duration issues tend to be cheapest
  to deliver; this is a standard approximation, not an observation.
* The CTD is treated as a par bond: coupon = yield interpolated from the stored constant-maturity
  curve (DGS2/5/7/10/30) at the proxy maturity on or before the report date.
* The conversion factor uses the 6% semiannual convention with maturity rounded down to whole
  months (CME rounds to quarters for 10-year and longer contracts; the difference is small).

Expect errors of roughly ten to twenty percent per contract versus vendor futures DV01. The
per-contract crowding ranks use raw contracts and do not depend on this estimate; only the
cross-contract aggregate does.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from statistics import median, pstdev
from typing import Any

from treasury_flow_radar.analytics.descriptive import (
    EvidenceType,
    Observation,
    _latest_per_date,
    positioning_metrics,
    yield_metrics,
)


@dataclass(frozen=True)
class ContractSpec:
    label: str
    face_value: float          # U.S. dollars of face per contract (CME contract unit)
    ctd_proxy_years: float     # shortest remaining maturity in the CME deliverable basket
    deliverable: str


# CME Group contract specifications (contract unit and deliverable grade).
CONTRACTS: dict[str, ContractSpec] = {
    "tff_futures_only_ust_2_year_note_042601": ContractSpec(
        "2-year", 200_000, 1.75, "remaining maturity 1y9m to 2y; original term at most 5y3m"),
    "tff_futures_only_ust_5_year_note_044601": ContractSpec(
        "5-year", 100_000, 4 + 2 / 12, "remaining maturity at least 4y2m; original term at most 5y3m"),
    "tff_futures_only_ust_10_year_note_043602": ContractSpec(
        "10-year", 100_000, 6.5, "remaining maturity 6.5y to 10y"),
    "tff_futures_only_ust_ultra_10_year_note_043607": ContractSpec(
        "Ultra 10", 100_000, 9 + 5 / 12, "original-issue 10y notes with 9y5m to 10y remaining"),
    "tff_futures_only_ust_30_year_bond_020601": ContractSpec(
        "Bond", 100_000, 15.0, "remaining maturity 15y to under 25y"),
}
TEN_YEAR = "tff_futures_only_ust_10_year_note_043602"
CURVE_TENORS = {"DGS2": 2.0, "DGS5": 5.0, "DGS7": 7.0, "DGS10": 10.0, "DGS30": 30.0}
CONTRACT_SPEC_SOURCE = ("https://www.cmegroup.com/education/courses/introduction-to-treasuries/"
                        "understand-treasuries-contract-specifications.html")

# Display rules. Documented constants, not calibrated thresholds.
CROWDED_PERCENTILE = 80.0
HEAVY_PERCENTILE = 60.0         # label bands below the crowded line; shared with the synthesis
SMALL_PERCENTILE = 33.0       # leveraged-fund short in the top fifth of its stored history
VOL_ELEVATED_PERCENTILE = 80.0
MIN_HISTORY_WEEKS = 52
MIN_HISTORY_SESSIONS = 120
FUNDING_MEDIAN_SESSIONS = 10    # median damps single-day month- and quarter-end spikes
REALIZED_VOL_SESSIONS = 20
CF_COUPON_PERCENT = 6.0
DV01_METHOD = "ctd_proxy_par_bond_over_cf"


# ------------------------------------------------------------------ pure calculations

def percentile_rank(history: Iterable[float], value: float) -> float | None:
    """Share of ``history`` (which should include ``value``) at or below ``value``, in percent."""
    values = [v for v in history if v is not None]
    if not values:
        return None
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def value_at_percentile(history: Iterable[float], percentile: float) -> float | None:
    """Smallest stored value whose ``percentile_rank`` reaches ``percentile``."""
    values = sorted(v for v in history if v is not None)
    if not values:
        return None
    index = max(0, math.ceil(percentile / 100.0 * len(values)) - 1)
    return values[index]


def interpolate_yield(curve: Mapping[float, float], years: float) -> float | None:
    """Linear interpolation on the supplied tenor curve; flat beyond its ends. None if empty."""
    points = sorted((t, y) for t, y in curve.items() if y is not None)
    if not points:
        return None
    if years <= points[0][0]:
        return points[0][1]
    if years >= points[-1][0]:
        return points[-1][1]
    for (t0, y0), (t1, y1) in pairwise(points):
        if t0 <= years <= t1:
            return y0 + (y1 - y0) * (years - t0) / (t1 - t0)
    return None  # unreachable for a sorted curve


def bond_price(coupon_percent: float, yield_percent: float, years: float) -> float:
    """Price per 100 face of a semiannual bond on a coupon date (closed form; periods may be fractional)."""
    periods = 2.0 * years
    c = coupon_percent / 2.0
    y = yield_percent / 200.0
    if abs(y) < 1e-12:
        return c * periods + 100.0
    v = (1.0 + y) ** -periods
    return c * (1.0 - v) / y + 100.0 * v


def par_bond_dv01_per_100(yield_percent: float, years: float) -> float:
    """Price change per 100 face for a 1 bp yield move of a par bond (central difference)."""
    bump = 0.01  # one basis point, in percent
    return (bond_price(yield_percent, yield_percent - bump, years)
            - bond_price(yield_percent, yield_percent + bump, years)) / 2.0


def conversion_factor(coupon_percent: float, years: float) -> float:
    """6% semiannual conversion factor; maturity rounded down to whole months."""
    months = math.floor(years * 12 + 1e-9)
    return bond_price(coupon_percent, CF_COUPON_PERCENT, months / 12.0) / 100.0


def contract_dv01(spec: ContractSpec, curve: Mapping[float, float]) -> float | None:
    """Estimated dollars per contract per 1 bp; None if the curve has no usable point."""
    ctd_yield = interpolate_yield(curve, spec.ctd_proxy_years)
    if ctd_yield is None:
        return None
    cf = conversion_factor(ctd_yield, spec.ctd_proxy_years)
    return spec.face_value / 100.0 * par_bond_dv01_per_100(ctd_yield, spec.ctd_proxy_years) / cf


def realized_vol(changes_bps: list[float]) -> float | None:
    return pstdev(changes_bps) if len(changes_bps) >= 2 else None


# ------------------------------------------------------------------ assembly

def _day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _daily_levels(observations: list[Observation], series_id: str) -> dict[date, float]:
    return {_day(o.observation_time): o.value for o in observations
            if o.series_id == series_id and o.value is not None}


def _curve_on_or_before(levels: Mapping[str, Mapping[date, float]], day: date
                        ) -> tuple[dict[float, float], date | None]:
    """Each tenor's latest stored value on or before ``day`` (no later data is used)."""
    curve: dict[float, float] = {}
    used: list[date] = []
    for series, tenor in CURVE_TENORS.items():
        prior = [d for d in levels.get(series, {}) if d <= day]
        if prior:
            latest = max(prior)
            curve[tenor] = levels[series][latest]
            used.append(latest)
    return curve, (min(used) if used else None)


def _crowding(observations: list[Observation], curve_levels: Mapping[str, Mapping[date, float]]
              ) -> dict[str, Any]:
    metrics = [m for m in positioning_metrics(observations) if m.participant == "leveraged_fund"]
    open_interest: dict[tuple[str, date], float] = {}
    for o in observations:
        meta = o.metadata or {}
        if (o.series_id in CONTRACTS and meta.get("metric") == "open_interest"
                and o.value is not None):
            open_interest[(o.series_id, _day(o.observation_time))] = o.value
    net: dict[str, dict[date, float]] = defaultdict(dict)
    for m in metrics:
        if m.series_id in CONTRACTS and m.net_position_contracts is not None:
            net[m.series_id][m.observation_time] = m.net_position_contracts

    per_contract = []
    for series, spec in CONTRACTS.items():
        history = net.get(series, {})
        if not history:
            continue
        latest_day = max(history)
        days = sorted(history)
        latest = history[latest_day]
        prior = history[days[-2]] if len(days) > 1 else None
        shorts = [-v for v in history.values()]
        oi = open_interest.get((series, latest_day))
        share_hist = [-history[d] / open_interest[(series, d)] * 100 for d in days
                      if open_interest.get((series, d))]
        share = -latest / oi * 100 if oi else None
        enough = len(days) >= MIN_HISTORY_WEEKS
        per_contract.append({
            "series_identifier": series,
            "contract": spec.label,
            "report_date": latest_day.isoformat(),
            "net_contracts": latest,
            "weekly_change_contracts": None if prior is None else latest - prior,
            "open_interest": oi,
            "net_short_share_of_open_interest_percent": share,
            "short_percentile": percentile_rank(shorts, -latest) if enough else None,
            "share_percentile": (percentile_rank(share_hist, share)
                                 if enough and share is not None and len(share_hist) >= MIN_HISTORY_WEEKS
                                 else None),
            "record_short": -latest >= max(shorts),
            "history_weeks": len(days),
            "history_start": days[0].isoformat(),
            "evidence_type": EvidenceType.CALCULATION.value,
        })

    # Aggregate in 10-year-note equivalents, only on report dates where every contract reported.
    all_days = sorted(set.intersection(*(set(net[s]) for s in CONTRACTS)) if all(net.get(s) for s in CONTRACTS)
                      else set())
    aggregate_rows = []
    for day in all_days:
        curve, curve_day = _curve_on_or_before(curve_levels, day)
        dv01 = {s: contract_dv01(spec, curve) for s, spec in CONTRACTS.items()}
        if any(v is None for v in dv01.values()) or not dv01[TEN_YEAR]:
            continue
        base = dv01[TEN_YEAR]
        total = sum(net[s][day] * dv01[s] / base for s in CONTRACTS)
        oi_total = (sum(open_interest[(s, day)] * dv01[s] / base for s in CONTRACTS)
                    if all((s, day) in open_interest for s in CONTRACTS) else None)
        aggregate_rows.append({
            "report_date": day.isoformat(),
            "net_10y_equivalents": total,
            "net_dv01_usd_per_bp": sum(net[s][day] * dv01[s] for s in CONTRACTS),
            "share_of_open_interest_percent": None if not oi_total else -total / oi_total * 100,
            "curve_date": curve_day.isoformat() if curve_day else None,
            "contract_dv01_usd_per_bp": {CONTRACTS[s].label: round(v, 2) for s, v in dv01.items()},
        })
    aggregate: dict[str, Any] | None = None
    if aggregate_rows:
        latest = aggregate_rows[-1]
        prior = aggregate_rows[-2] if len(aggregate_rows) > 1 else None
        shorts = [-r["net_10y_equivalents"] for r in aggregate_rows]
        shares = [r["share_of_open_interest_percent"] for r in aggregate_rows
                  if r["share_of_open_interest_percent"] is not None]
        enough = len(aggregate_rows) >= MIN_HISTORY_WEEKS
        aggregate = {
            **latest,
            "weekly_change_10y_equivalents": (None if prior is None
                                              else latest["net_10y_equivalents"] - prior["net_10y_equivalents"]),
            "short_percentile": percentile_rank(shorts, -latest["net_10y_equivalents"]) if enough else None,
            "share_percentile": (percentile_rank(shares, latest["share_of_open_interest_percent"])
                                 if enough and latest["share_of_open_interest_percent"] is not None
                                 and len(shares) >= MIN_HISTORY_WEEKS else None),
            "record_short": -latest["net_10y_equivalents"] >= max(shorts),
            "peak_short_10y_equivalents": max(shorts),
            "crowded_threshold_10y_equivalents": (value_at_percentile(shorts, CROWDED_PERCENTILE)
                                                  if enough else None),
            "history_weeks": len(aggregate_rows),
            "history_start": aggregate_rows[0]["report_date"],
            "history": [{"date": r["report_date"], "value": r["net_10y_equivalents"]} for r in aggregate_rows],
            "dv01_method": DV01_METHOD,
            "evidence_type": "ESTIMATE",
        }
    return {"contracts": per_contract, "aggregate": aggregate}


def _funding(observations: list[Observation]) -> dict[str, Any] | None:
    sofr = _daily_levels(observations, "SOFR")
    tail = _daily_levels(observations, "SOFR99")
    iorb = _daily_levels(observations, "IORB")
    days = sorted(d for d in sofr if d in iorb)
    if not days:
        return None
    spread = [(sofr[d] - iorb[d]) * 100 for d in days]
    medians = [median(spread[max(0, i - FUNDING_MEDIAN_SESSIONS + 1): i + 1])
               for i in range(len(spread)) if i >= FUNDING_MEDIAN_SESSIONS - 1]
    latest_day = days[-1]
    current_median = medians[-1] if medians else None
    tail_days = [d for d in days if d in tail]
    tail_latest = (tail[tail_days[-1]] - iorb[tail_days[-1]]) * 100 if tail_days else None
    enough = len(medians) >= MIN_HISTORY_SESSIONS
    streak = 0
    for value in reversed(spread):
        if value <= 0:
            break
        streak += 1
    return {
        "date": latest_day.isoformat(),
        "sofr_percent": sofr[latest_day],
        "iorb_percent": iorb[latest_day],
        "sofr_minus_iorb_bps": spread[-1],
        "median_sessions": FUNDING_MEDIAN_SESSIONS,
        "sofr_minus_iorb_median_bps": current_median,
        "sofr_minus_iorb_median_percentile": (percentile_rank(medians, current_median)
                                              if enough and current_median is not None else None),
        "sofr99_minus_iorb_bps": tail_latest,
        "sofr99_date": tail_days[-1].isoformat() if tail_days else None,
        "sofr_above_iorb": None if current_median is None else current_median > 0,
        "sessions_above_iorb_streak": streak,
        "history_sessions": len(days),
        "history_start": days[0].isoformat(),
        "history": [{"date": d.isoformat(), "value": s} for d, s in zip(days, spread, strict=True)][-130:],
        "evidence_type": EvidenceType.CALCULATION.value,
    }


def _volatility(observations: list[Observation]) -> dict[str, Any] | None:
    metrics = [m for m in yield_metrics(observations, "DGS10") if m.daily_change_bps is not None]
    if len(metrics) < REALIZED_VOL_SESSIONS:
        return None
    changes = [m.daily_change_bps for m in metrics]
    series = [realized_vol(changes[i - REALIZED_VOL_SESSIONS + 1: i + 1])
              for i in range(REALIZED_VOL_SESSIONS - 1, len(changes))]
    current = series[-1]
    enough = len(series) >= MIN_HISTORY_SESSIONS
    return {
        "date": metrics[-1].observation_time.isoformat(),
        "sessions": REALIZED_VOL_SESSIONS,
        "realized_vol_bps_per_day": current,
        "realized_vol_percentile": percentile_rank(series, current) if enough else None,
        "elevated_threshold_bps_per_day": (value_at_percentile(series, VOL_ELEVATED_PERCENTILE)
                                           if enough else None),
        "history_sessions": len(series),
        "measure": "population standard deviation of daily DGS10 changes; realized, not implied (not MOVE)",
        "evidence_type": EvidenceType.CALCULATION.value,
    }


def _state(aggregate: Mapping[str, Any] | None, funding: Mapping[str, Any] | None,
           vol: Mapping[str, Any] | None) -> dict[str, Any]:
    pct = None if aggregate is None else aggregate.get("short_percentile")
    crowded = None if pct is None else pct >= CROWDED_PERCENTILE
    tight = None if funding is None else funding.get("sofr_above_iorb")
    vol_pct = None if vol is None else vol.get("realized_vol_percentile")
    elevated = None if vol_pct is None else vol_pct >= VOL_ELEVATED_PERCENTILE
    if crowded is None:
        code, label = "UNKNOWN", "Not enough positioning history to rank crowding"
    elif crowded and tight:
        code, label = "CROWDED_FUNDING_TIGHT", "Crowded basis trade with funding tightening"
    elif crowded and tight is None:
        code, label = "CROWDED_FUNDING_UNKNOWN", "Crowded basis trade; funding not measured"
    elif crowded:
        code, label = "CROWDED_FUNDING_CALM", "Crowded basis trade, funding calm"
    else:
        code = "NOT_CROWDED"
        if pct >= HEAVY_PERCENTILE:
            label = "Large leveraged-fund short, below the crowded line"
        elif pct < SMALL_PERCENTILE:
            label = "Leveraged-fund short smaller than in most weeks"
        else:
            label = "Leveraged-fund short in its typical range"
    return {
        "code": code,
        "label": label,
        "crowded": crowded,
        "funding_tight": tight,
        "vol_elevated": elevated,
        "flag": code == "CROWDED_FUNDING_TIGHT",
        "rules": {
            "crowded": f"10y-equivalent leveraged-fund net short at or above the {CROWDED_PERCENTILE:.0f}th "
                       f"percentile of at least {MIN_HISTORY_WEEKS} stored weekly reports",
            "funding_tight": f"{FUNDING_MEDIAN_SESSIONS}-session median of SOFR minus IORB above zero",
            "vol_elevated": f"{REALIZED_VOL_SESSIONS}-session realized 10-year volatility at or above the "
                            f"{VOL_ELEVATED_PERCENTILE:.0f}th percentile of at least {MIN_HISTORY_SESSIONS} sessions",
        },
        "evidence_type": EvidenceType.OBSERVATION.value,
    }


def basis_setup(rows: Iterable[Mapping[str, Any] | Observation]) -> dict[str, Any]:
    """Build the crowding / funding / volatility read from normalized observation rows."""
    observations = _latest_per_date(rows)
    curve_levels = {s: _daily_levels(observations, s) for s in CURVE_TENORS}
    crowding = _crowding(observations, curve_levels)
    funding = _funding(observations)
    vol = _volatility(observations)
    return {
        "crowding": crowding,
        "funding": funding,
        "volatility": vol,
        "state": _state(crowding["aggregate"], funding, vol),
        "method": {
            "dv01": DV01_METHOD,
            "contract_specs_source": CONTRACT_SPEC_SOURCE,
            "ctd_proxy_years": {spec.label: round(spec.ctd_proxy_years, 4) for spec in CONTRACTS.values()},
            "percentile": "share of stored history at or below the current value, current value included",
            "alignment": "Positioning is dated to the Tuesday CFTC report date; funding and volatility are their own "
                         "latest dates; nothing is forward-filled",
            "limits": "Futures leg only: cash Treasury and repo legs of any basis trade are not observed. "
                      "Percentiles rank against stored history only, which grows with ingestion. "
                      "Implied rate volatility (MOVE) is licensed by ICE and not ingested.",
        },
    }
