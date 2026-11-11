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

from collections.abc import Iterable, Mapping
from datetime import date
from statistics import median
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
        "method": {
            "bond_lights": "funding_tight, vol_elevated (basis read) and high-yield spread up "
                           f"{CREDIT_WIDEN_BPS:.0f} bp or more over {CREDIT_SESSIONS} sessions; 0 calm, 1 warning, "
                           "2+ stressed (display rule)",
            "vix_bands": "below 15 calm, 15-20 normal, 20-30 nervous, 30+ fearful (convention, not calibrated)",
            "erp": f"Damodaran monthly implied ERP ranked against all stored months; <= {ERP_THIN_PCT:.0f}th pct "
                   f"thin, >= {ERP_WIDE_PCT:.0f}th wide",
        },
    }
