"""Stocks versus plumbing: are bond-side stress and stock-market nerves moving together or apart?

The question: the plumbing gauges (funding, Treasury volatility) and credit measure conditions
that can push stress into stocks; the VIX measures how nervous stock investors already are; the
implied equity risk premium measures how much extra return stocks are priced to deliver. Reading
them side by side separates two situations that look alike on a stock chart:

* **Stress in the pipes, calm stocks** — the bond side is flashing and the VIX is not. The
  stress has not reached stock prices; either it fades in the plumbing, or it is not priced yet.
* **Nervous stocks, calm pipes** — the VIX is up while funding, rate volatility and credit are
  quiet, so the fear is coming from somewhere other than market plumbing (earnings, growth,
  policy, geopolitics, a single sector).

Inputs (all stored observations; nothing is fetched here)
---------------------------------------------------------
* Bond-side lights, each a display rule (not calibrated):
  - **funding**: the basis read's ``funding_tight`` (10-session median of SOFR minus IORB above 0);
  - **rate volatility**: the basis read's ``vol_elevated`` (10-year realized volatility at or above
    its 80th percentile);
  - **credit**: ICE BofA high-yield spread (FRED BAMLH0A0HYM2) up ``CREDIT_WIDEN_BPS`` or more over
    ``CREDIT_SESSIONS`` sessions.
  0 lights = calm, 1 = warning, 2 or 3 = stressed. Crowding is not a light: it measures how much a
  stress would force out, so it is reported as an amplifier.
* **VIX** (FRED VIXCLS, index points). Conventional bands, a display rule: below 15 calm, 15 to
  under 20 normal, 20 to under 30 nervous, 30 and above fearful. "Nervous stocks" means 20 or more.
* **Implied ERP** (Damodaran, monthly, decimal fraction; shown in percent = value x 100), ranked
  against every stored month. At or below the 20th percentile is a "thin" premium, at or above the
  80th a "wide" one.

Every label is OBSERVATION (a classification of measured conditions). Nothing here forecasts
prices or says one market caused the other, and none of it is a buy or sell signal.
"""
from __future__ import annotations

import math
from bisect import bisect_right, insort
from collections.abc import Iterable, Mapping
from datetime import date
from statistics import mean, median
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation, _latest_per_date

VIX_SERIES = "VIXCLS"
HY_SERIES = "BAMLH0A0HYM2"
ERP_SERIES = "DAMODARAN_ERP"
ERP_FALLBACK_HISTORY = "DAMODARAN_ERP_T12M"   # shown separately; never substituted into the headline
TBOND_SERIES = "DAMODARAN_TBOND"
VIX_BANDS = ((15.0, "calm"), (20.0, "normal"), (30.0, "nervous"))   # upper bounds; 30+ = fearful
VIX_NERVOUS = 20.0
VIX_SESSIONS = 5
CREDIT_SESSIONS = 20
CREDIT_WIDEN_BPS = 50.0
ERP_THIN_PCT, ERP_WIDE_PCT = 20.0, 80.0
MIN_ERP_MONTHS = 36
# State history (descriptive): how each combination has played out in the stored data.
HISTORY_HORIZON = 20          # forward S&P 500 sessions
EPISODE_GAP = 5               # more than 5 trading days between state days starts a new stretch
FUNDING_MEDIAN_SESSIONS = 10  # same rule as the basis read's funding light
VOL_SESSIONS = 20             # same rule as the basis read's rate-volatility light
VOL_ELEVATED_PCT = 80.0
VOL_MIN_HISTORY = 120
STATES = ("quiet", "early_warning", "unpriced_stress", "fear_elsewhere", "together")


def _day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _levels(observations: list[Observation], series: str) -> list[tuple[date, float]]:
    return sorted((_day(o.observation_time), o.value) for o in observations
                  if o.series_id == series and o.value is not None)


def _rank(values: list[float], value: float) -> float:
    return 100.0 * sum(v <= value for v in values) / len(values)


def vix_band(level: float) -> str:
    for upper, name in VIX_BANDS:
        if level < upper:
            return name
    return "fearful"


def _vix(points: list[tuple[date, float]]) -> dict[str, Any] | None:
    if not points:
        return None
    day, level = points[-1]
    prior = points[-1 - VIX_SESSIONS][1] if len(points) > VIX_SESSIONS else None
    values = [v for _, v in points]
    return {"date": day.isoformat(), "level": level, "band": vix_band(level),
            "nervous": level >= VIX_NERVOUS,
            "change_sessions": VIX_SESSIONS, "change": None if prior is None else level - prior,
            "percentile": _rank(values, level) if len(values) >= 250 else None,
            "history_sessions": len(values), "history_start": points[0][0].isoformat(),
            "history": [{"date": d.isoformat(), "value": v} for d, v in points[-130:]],
            "evidence_type": EvidenceType.FACT.value}


def _credit(points: list[tuple[date, float]]) -> dict[str, Any] | None:
    if not points:
        return None
    day, level = points[-1]
    prior = points[-1 - CREDIT_SESSIONS][1] if len(points) > CREDIT_SESSIONS else None
    change = None if prior is None else (level - prior) * 100
    values = [v for _, v in points]
    return {"date": day.isoformat(), "hy_spread_percent": level, "change_sessions": CREDIT_SESSIONS,
            "change_bps": change, "widening": None if change is None else change >= CREDIT_WIDEN_BPS,
            "percentile": _rank(values, level) if len(values) >= 250 else None,
            "evidence_type": EvidenceType.CALCULATION.value}


def _erp(observations: list[Observation]) -> dict[str, Any] | None:
    points = _levels(observations, ERP_SERIES)
    if not points:
        return None
    month, value = points[-1]
    prior = points[-2] if len(points) >= 2 else None
    values = [v for _, v in points]
    enough = len(values) >= MIN_ERP_MONTHS
    pct = _rank(values, value) if enough else None
    tbond = dict(_levels(observations, TBOND_SERIES)).get(month)
    return {
        "month": month.isoformat(), "erp_percent": value * 100,
        "change_bps": None if prior is None else (value - prior[1]) * 10000,
        "prior_month": None if prior is None else prior[0].isoformat(),
        "percentile": pct, "median_percent": median(values) * 100 if enough else None,
        "band": None if pct is None else "thin" if pct <= ERP_THIN_PCT else "wide" if pct >= ERP_WIDE_PCT else "middle",
        "tbond_percent": None if tbond is None else tbond * 100,
        "implied_return_percent": None if tbond is None else (value + tbond) * 100,
        "history_months": len(values), "history_start": points[0][0].isoformat(),
        "history": [{"date": d.isoformat(), "value": v * 100} for d, v in points],
        "source": "Aswath Damodaran, NYU Stern, ERPbymonth.xlsx (ERP, T12m with sustainable payout)",
        "evidence_type": EvidenceType.CALCULATION.value,
    }


def _bond_side(basis: Mapping[str, Any] | None, credit: dict[str, Any] | None) -> dict[str, Any]:
    state = (basis or {}).get("state") or {}
    lights = [{"key": "funding", "name": "Funding", "on": state.get("funding_tight")},
              {"key": "rate_vol", "name": "Rate volatility", "on": state.get("vol_elevated")},
              {"key": "credit", "name": "Credit", "on": None if credit is None else credit["widening"]}]
    known = [lt["on"] for lt in lights if lt["on"] is not None]
    on = sum(bool(v) for v in known)
    level = (None if len(known) < len(lights) else
             "calm" if on == 0 else "warning" if on == 1 else "stressed")
    return {"level": level, "lights_on": on, "lights": lights,
            "crowded": state.get("crowded"), "evidence_type": EvidenceType.OBSERVATION.value}


def _state(bond: str | None, vix: dict[str, Any] | None) -> str:
    if bond is None or vix is None:
        return "partial"
    nervous = vix["nervous"]
    if bond == "calm":
        return "fear_elsewhere" if nervous else "quiet"
    if nervous:
        return "together"
    return "unpriced_stress" if bond == "stressed" else "early_warning"


def _on_or_before(table: Mapping[date, Any], days: list[date], day: date) -> Any:
    """Latest value dated on or before ``day`` within 7 calendar days, else None (no carrying stale data)."""
    i = bisect_right(days, day) - 1
    if i < 0 or (day - days[i]).days > 7:
        return None
    return table[days[i]]


def _point_in_time_lights(observations: list[Observation]) -> dict[str, tuple[list[date], dict[date, bool]]]:
    """Each bond-side light for every day it can be computed, using only data up to that day."""
    sofr, iorb = dict(_levels(observations, "SOFR")), dict(_levels(observations, "IORB"))
    days = sorted(d for d in sofr if d in iorb)
    spread = [(sofr[d] - iorb[d]) * 100 for d in days]
    funding = {days[i]: median(spread[i - FUNDING_MEDIAN_SESSIONS + 1: i + 1]) > 0
               for i in range(FUNDING_MEDIAN_SESSIONS - 1, len(days))}

    yields = _levels(observations, "DGS10")
    changes = [(yields[i][0], (yields[i][1] - yields[i - 1][1]) * 100) for i in range(1, len(yields))]
    seen: list[float] = []
    vol: dict[date, bool] = {}
    for i in range(VOL_SESSIONS - 1, len(changes)):
        window = [c for _, c in changes[i - VOL_SESSIONS + 1: i + 1]]
        avg = sum(window) / VOL_SESSIONS
        value = math.sqrt(sum((c - avg) ** 2 for c in window) / VOL_SESSIONS)
        insort(seen, value)
        if len(seen) >= VOL_MIN_HISTORY:   # expanding percentile: no look-ahead
            vol[changes[i][0]] = 100.0 * bisect_right(seen, value) / len(seen) >= VOL_ELEVATED_PCT

    hy = _levels(observations, HY_SERIES)
    credit = {hy[i][0]: (hy[i][1] - hy[i - CREDIT_SESSIONS][1]) * 100 >= CREDIT_WIDEN_BPS
              for i in range(CREDIT_SESSIONS, len(hy))}
    return {key: (sorted(table), table) for key, table in
            (("funding", funding), ("rate_vol", vol), ("credit", credit))}


def _episodes(positions: list[int]) -> int:
    return sum(1 for a, b in zip([None, *positions], positions) if a is None or b - a > EPISODE_GAP)


def state_history(observations: list[Observation]) -> dict[str, Any] | None:
    """How each stocks-vs-plumbing state has played out: forward S&P 500 behavior after each day.

    Every day is classified with the card's rules using only data available on that day
    (funding and credit as on the card; rate volatility ranked against its history up to that
    day). Descriptive CALCULATION over overlapping windows: the number of separate stretches
    (``episodes``) is the real sample size. Past behavior in one period, not a forecast."""
    spx = _levels(observations, "SP500")
    vix_points = _levels(observations, VIX_SERIES)
    if len(spx) <= HISTORY_HORIZON or not vix_points:
        return None
    vix_days, vix_map = [d for d, _ in vix_points], dict(vix_points)
    lights = _point_in_time_lights(observations)
    spx_days, spx_vals = [d for d, _ in spx], [v for _, v in spx]
    classified: list[tuple[int, str, float, float, float | None]] = []
    for i in range(len(spx) - HISTORY_HORIZON):
        day = spx_days[i]
        on = [_on_or_before(table, keys, day) for keys, table in lights.values()]
        level = _on_or_before(vix_map, vix_days, day)
        if level is None or any(v is None for v in on):
            continue
        count = sum(bool(v) for v in on)
        bond = "calm" if count == 0 else "warning" if count == 1 else "stressed"
        state = _state(bond, {"nervous": level >= VIX_NERVOUS})
        forward = (spx_vals[i + HISTORY_HORIZON] / spx_vals[i] - 1) * 100
        drawdown = (min(spx_vals[i + 1: i + HISTORY_HORIZON + 1]) / spx_vals[i] - 1) * 100
        later = _on_or_before(vix_map, vix_days, spx_days[i + HISTORY_HORIZON])
        classified.append((i, state, forward, drawdown, None if later is None else later - level))
    if not classified:
        return None
    states = {}
    for name in STATES:
        rows = [r for r in classified if r[1] == name]
        vix_moves = [r[4] for r in rows if r[4] is not None]
        states[name] = {
            "days": len(rows),
            "episodes": _episodes([r[0] for r in rows]),
            "mean_forward_pct": mean(r[2] for r in rows) if rows else None,
            "median_forward_pct": median(r[2] for r in rows) if rows else None,
            "share_up_pct": 100.0 * sum(r[2] > 0 for r in rows) / len(rows) if rows else None,
            "mean_max_drawdown_pct": mean(r[3] for r in rows) if rows else None,
            "mean_vix_change": mean(vix_moves) if vix_moves else None,
        }
    return {
        "start": spx_days[classified[0][0]].isoformat(),
        "end": spx_days[classified[-1][0]].isoformat(),
        "classified_days": len(classified),
        "horizon_sessions": HISTORY_HORIZON,
        "states": states,
        "limited_by": "credit (FRED serves about three years of ICE BofA spreads; the window grows as runs store more)",
        "method": ("each S&P 500 trading day classified with the card's rules using only data available that day; "
                   "rate volatility ranked against its own history up to that day; forward return = S&P 500 close "
                   f"{HISTORY_HORIZON} sessions later; a gap of more than {EPISODE_GAP} trading days starts a new "
                   "stretch. Overlapping windows: the stretch count is the real sample size."),
        "evidence_type": EvidenceType.CALCULATION.value,
    }


def stock_risk(rows: Iterable[Mapping[str, Any] | Observation],
               basis: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The stocks-vs-plumbing read, or None when neither VIX nor ERP observations are stored."""
    observations = _latest_per_date(rows)
    vix = _vix(_levels(observations, VIX_SERIES))
    erp = _erp(observations)
    if vix is None and erp is None:
        return None
    credit = _credit(_levels(observations, HY_SERIES))
    bond = _bond_side(basis, credit)
    return {
        "state": _state(bond["level"], vix),
        "state_evidence_type": EvidenceType.OBSERVATION.value,
        "bond": bond,
        "credit": credit,
        "vix": vix,
        "erp": erp,
        "history": state_history(observations),
        "method": {
            "bond_lights": "funding_tight, vol_elevated (basis read) and high-yield spread up "
                           f"{CREDIT_WIDEN_BPS:.0f} bp or more over {CREDIT_SESSIONS} sessions; 0 calm, 1 warning, "
                           "2+ stressed (display rule)",
            "vix_bands": "below 15 calm, 15-20 normal, 20-30 nervous, 30+ fearful (convention, not calibrated)",
            "erp": f"Damodaran monthly implied ERP ranked against all stored months; <= {ERP_THIN_PCT:.0f}th pct "
                   f"thin, >= {ERP_WIDE_PCT:.0f}th wide",
        },
    }
