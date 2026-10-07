"""Split 10-year yield moves into components and describe the curve's shape.

Every output here is a CALCULATION on stored FRED series; none of it is a causal claim.

* Real yield (DFII10) and inflation breakeven (T10YIE) are source-reported market
  measures. Their changes are shown next to the nominal 10-year (DGS10) change, and
  the gap ``nominal - real - breakeven`` is reported rather than assumed to be zero.
* The term premium (THREEFYTP10) is a model estimate from the Board of Governors for a
  zero-coupon bond, published with a lag. It is reported separately with its own date
  and is never mixed into the nominal/real/breakeven identity.
* Curve shape compares the 2-year and 30-year changes over the same sessions using the
  standard descriptive names (bear/bull steepener/flattener).

Windows count DGS10 sessions that carry a value; market-closed dates are skipped, and a
component is UNKNOWN unless it has a value on both window dates. Nothing is filled.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation, yield_metrics

NOMINAL, REAL, BREAKEVEN, TERM_PREMIUM = "DGS10", "DFII10", "T10YIE", "THREEFYTP10"
SHORT, LONG = "DGS2", "DGS30"
SERIES = (NOMINAL, REAL, BREAKEVEN, TERM_PREMIUM, SHORT, LONG)
TOLERANCE_BPS = 0.5


def curve_shape(short_bps: float | None, long_bps: float | None,
                tolerance: float = TOLERANCE_BPS) -> dict[str, Any] | None:
    """Name the curve move from 2-year and 30-year changes (basis points).

    Direction: "bear" when neither end fell and at least one rose by more than the
    tolerance, "bull" for the mirror case, "twist" when the ends moved in opposite
    directions. Slope: steepener when long minus short exceeds the tolerance,
    flattener when it is below minus the tolerance, otherwise parallel.
    """
    if short_bps is None or long_bps is None:
        return None
    slope = long_bps - short_bps
    if abs(short_bps) <= tolerance and abs(long_bps) <= tolerance:
        name = "little changed"
        direction = "flat"
    else:
        if short_bps >= -tolerance and long_bps >= -tolerance:
            direction = "bear"
        elif short_bps <= tolerance and long_bps <= tolerance:
            direction = "bull"
        else:
            direction = "twist"
        steep = slope > tolerance
        flat = slope < -tolerance
        names = {
            ("bear", True, False): "bear steepener", ("bear", False, True): "bear flattener",
            ("bear", False, False): "parallel rise",
            ("bull", True, False): "bull steepener", ("bull", False, True): "bull flattener",
            ("bull", False, False): "parallel fall",
            ("twist", True, False): "steepening twist", ("twist", False, True): "flattening twist",
            ("twist", False, False): "twist",
        }
        name = names[(direction, steep, flat)]
    if abs(abs(long_bps) - abs(short_bps)) <= tolerance:
        led_by = "evenly"
    else:
        led_by = "long end" if abs(long_bps) > abs(short_bps) else "short end"
    return {"name": name, "direction": direction, "slope_change_bps": slope,
            "short_change_bps": short_bps, "long_change_bps": long_bps, "led_by": led_by,
            "evidence_type": EvidenceType.CALCULATION.value}


def _valued(observations: list[Observation], series_id: str) -> dict[date, float]:
    present = any(item.series_id == series_id for item in observations)
    if not present:
        return {}
    return {m.observation_time: m.yield_percent for m in yield_metrics(observations, series_id)
            if m.yield_percent is not None}


def decompose_moves(observations: Iterable[Observation | Mapping[str, Any]],
                    *, windows: tuple[int, ...] = (1, 5)) -> dict[str, Any]:
    """Return per-session decompositions keyed by ISO date, plus latest term premium."""
    items = [o if isinstance(o, Observation) else Observation.from_mapping(o) for o in observations]
    relevant = [o for o in items if o.series_id in SERIES]
    values = {sid: _valued(relevant, sid) for sid in SERIES}
    sessions = sorted(values[NOMINAL])
    by_date: dict[str, dict[str, Any]] = {}
    for index, day in enumerate(sessions):
        record: dict[str, Any] = {"date": day.isoformat(), "windows": {}}
        for width in windows:
            if index - width < 0:
                record["windows"][str(width)] = None
                continue
            start = sessions[index - width]

            def change(series_id: str, start: date = start, day: date = day) -> float | None:
                before, after = values[series_id].get(start), values[series_id].get(day)
                return None if before is None or after is None else (after - before) * 100

            nominal, real, breakeven = change(NOMINAL), change(REAL), change(BREAKEVEN)
            gap = (None if None in (nominal, real, breakeven)
                   else nominal - real - breakeven)  # type: ignore[operator]
            record["windows"][str(width)] = {
                "start_date": start.isoformat(), "end_date": day.isoformat(),
                "nominal_bps": nominal, "real_bps": real, "breakeven_bps": breakeven,
                "gap_bps": gap, "term_premium_bps": change(TERM_PREMIUM),
                "curve": curve_shape(change(SHORT), change(LONG)),
                "evidence_type": EvidenceType.CALCULATION.value,
            }
        by_date[day.isoformat()] = record
    return {"by_date": by_date, "latest": by_date[sessions[-1].isoformat()] if sessions else None,
            "term_premium": _term_premium_latest(values[TERM_PREMIUM])}


def _term_premium_latest(series: dict[date, float], width: int = 5) -> dict[str, Any] | None:
    """Latest published term premium and its change over its own last `width` sessions."""
    days = sorted(series)
    if not days:
        return None
    latest = days[-1]
    start = days[-1 - width] if len(days) > width else None
    return {"date": latest.isoformat(), "percent": series[latest],
            "change_bps": None if start is None else (series[latest] - series[start]) * 100,
            "start_date": None if start is None else start.isoformat(), "sessions": width,
            "evidence_type": EvidenceType.CALCULATION.value,
            "note": "Board of Governors model estimate for a 10-year zero-coupon bond; not a market quote."}
