"""Observation-order event windows and descriptive cross-market co-movement."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation

MARKET_SERIES = ("ZT", "ZF", "ZN", "TN", "UB", "ZB", "HYG", "IWM", "DXY")


def event_study(event_date: date | str, rows: Iterable[Mapping[str, Any] | Observation]) -> dict[str, Any]:
    """Build T-5/T+5, T-3/T+3 and T-1/T+1 windows on actual DGS10 dates.

    T offsets are ordered available DGS10 observations. Other series are joined
    only on the same observation date; sparse weekly sources are not repeated in
    daily rows and should be attached once through the explicit as-of context APIs.
    """
    event_day = event_date if isinstance(event_date, date) else date.fromisoformat(str(event_date)[:10])
    source_rows = [dict(row) if isinstance(row, Mapping) else _observation_mapping(row)
                   for row in rows]
    observations = [Observation.from_mapping(row) for row in source_rows]
    yield_dates = sorted({
        _day(item.observation_time) for item in observations
        if item.series_id.upper() == "DGS10"
    })
    prior = [day for day in yield_dates if day < event_day]
    following = [day for day in yield_dates if day > event_day]
    before = prior[-5:]
    after = following[:5]
    offsets = {day: index - len(before) for index, day in enumerate(before)}
    offsets.update({day: index + 1 for index, day in enumerate(after)})
    date_by_offset = {offset: day for day, offset in offsets.items()}
    date_by_offset[0] = event_day

    per_day: dict[date, dict[str, list[dict[str, Any]]]] = {}
    market_history: dict[str, dict[date, float]] = {name: {} for name in MARKET_SERIES}
    for row, item in zip(source_rows, observations, strict=True):
        observed_day = _day(item.observation_time)
        per_day.setdefault(observed_day, {}).setdefault(item.series_id.upper(), []).append(row)
        if item.series_id.upper() in market_history and item.value is not None:
            market_history[item.series_id.upper()][observed_day] = item.value

    full_windows: dict[int, list[dict[str, Any]]] = {}
    for width in (5, 3, 1):
        window = []
        for offset in range(-width, width + 1):
            day = date_by_offset.get(offset)
            by_series = per_day.get(day, {}) if day else {}
            values = {name: _latest_numeric(by_series.get(name, []))
                      for name in ("DGS2", "DGS10", *MARKET_SERIES)}
            d2, d10 = values["DGS2"], values["DGS10"]
            window.append({
                "offset": offset,
                "event_date": event_day.isoformat(),
                "source_observation_date": day.isoformat() if day else None,
                "temporal_lag_days": (event_day - day).days if day else None,
                "temporal_relationship": _relation(day, event_day) if day else None,
                "dgs2_percent": d2,
                "dgs10_percent": d10,
                "spread_percentage_points": None if d2 is None or d10 is None else d10 - d2,
                "spread_bps": None if d2 is None or d10 is None else (d10 - d2) * 100,
                "market": {
                    name: _market_value(values[name], market_history[name], day, event_day)
                    for name in MARKET_SERIES
                },
                "evidence_type": EvidenceType.OBSERVATION.value,
            })
        baseline = next((r for r in window if r["offset"] == -1), None)
        for row in window:
            for name, value_key in (("dgs2_change_bps", "dgs2_percent"),
                                    ("dgs10_change_bps", "dgs10_percent"),
                                    ("spread_change_bps", "spread_percentage_points")):
                before = None if baseline is None else baseline[value_key]
                current = row[value_key]
                row[name] = None if before is None or current is None else (current - before) * 100
        full_windows[width] = window

    full = full_windows[5]
    by_offset = {row["offset"]: row for row in full}
    summary = {}
    for series, value_key in (("DGS2", "dgs2_percent"), ("DGS10", "dgs10_percent"),
                              ("10Y-2Y", "spread_percentage_points")):
        values = {offset: row[value_key] for offset, row in by_offset.items()}
        pre = _delta(values.get(-5), values.get(-1), 100)
        event = _delta(values.get(-1), values.get(0), 100)
        post = _delta(values.get(0), values.get(5), 100)
        reversal = None
        reversal_pct = None
        if event is not None and post is not None and event != 0 and event * post < 0:
            reversal = min(abs(event), abs(post))
            reversal_pct = reversal / abs(event) * 100
        summary[series] = {
            "pre_event_move_bps": pre,
            "event_day_move_bps": event,
            "post_event_move_bps": post,
            "post_event_reversal_magnitude_bps": reversal,
            "post_event_reversal_percent": reversal_pct,
            "evidence_type": EvidenceType.CALCULATION.value,
        }
    market_available = any(row["market"][name]["return_percent"] is not None
                           for row in full for name in MARKET_SERIES)
    return {
        "event_date": event_day.isoformat(),
        "window_alignment": "T offsets count actual DGS10 observations; other market series are exact-date joins",
        "windows": {f"T-{width}_T+{width}": full_windows[width] for width in (5, 3, 1)},
        "summary": summary,
        "market_confirmation": {
            "status": "AVAILABLE" if market_available else "Market confirmation unavailable",
            "series": list(MARKET_SERIES),
            "classification": "OBSERVATION — cross-market co-movement" if market_available else None,
            "causality_established": False,
        },
    }


def _latest_numeric(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    latest = max(rows, key=lambda r: (int(r.get("revision") or 1), str(r.get("retrieval_time") or "")))
    value = latest.get("value_numeric")
    return None if value is None else float(value)


def _market_value(value: float | None, history: dict[date, float], day: date | None,
                  event_day: date) -> dict[str, Any]:
    prior_days = [observed for observed in history if day and observed < day]
    prior_day = max(prior_days) if prior_days else None
    previous = history.get(prior_day) if prior_day else None
    return {
        "value": value,
        "return_percent": None if value is None or previous in (None, 0)
        else (value / float(previous) - 1) * 100,
        "prior_observation_date": prior_day.isoformat() if prior_day else None,
        "prior_source_observation_lag_days": ((day - prior_day).days if day and prior_day else None),
        "source_observation_date": day.isoformat() if day else None,
        "event_date": event_day.isoformat(),
        "lag_days": (event_day - day).days if day else None,
        "relationship": _relation(day, event_day) if day else None,
    }


def _delta(prior: float | None, current: float | None, scale: float) -> float | None:
    return None if prior is None or current is None else (current - prior) * scale


def _day(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _relation(observed: date, event: date) -> str:
    return "BEFORE" if observed < event else "AFTER" if observed > event else "SAME_DAY"


def _observation_mapping(item: Observation) -> dict[str, Any]:
    return {"series_identifier": item.series_id, "observation_time": _day(item.observation_time).isoformat(),
            "value_numeric": item.value, "unit": item.unit, "revision": item.revision,
            "metadata": dict(item.metadata or {})}

