"""Small, deterministic market description functions over generic observations.

Yields use percentage points (for example 4.25 means 4.25%). CFTC positions
retain source-native contracts; NY Fed levels retain source-native units.
Functions never interpolate, fill calendar dates, or infer causation.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from statistics import fmean, pstdev
from typing import Any


class EvidenceType(StrEnum):
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    OBSERVATION = "OBSERVATION"
    MECHANISM = "MECHANISM"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"


class Direction(StrEnum):
    RISING = "RISING"
    FALLING = "FALLING"
    FLAT = "FLAT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Observation:
    """Provider-neutral projection of one normalized database observation."""

    series_id: str
    observation_time: date | datetime | str
    value: float | None
    unit: str | None = None
    logical_key: str | None = None
    revision: int = 1
    metadata: Mapping[str, Any] | None = None
    frequency: str | None = None
    evidence_type: EvidenceType = EvidenceType.FACT

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Observation:
        metadata = row.get("metadata", row.get("metadata_json", {}))
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            raise TypeError("observation metadata must be a JSON object")
        value = row.get("value_numeric")
        if value is not None:
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("observation values must be finite or null")
        revision = int(row.get("revision", 1))
        if revision < 1:
            raise ValueError("observation revision must be positive")
        series_id = row.get("series_identifier")
        if series_id is None and isinstance(metadata.get("series_id"), str):
            series_id = metadata["series_id"]
        if series_id is None:
            series_id = row.get("series_id", "")
        return cls(
            series_id=str(series_id),
            observation_time=row.get("observation_time"),
            value=value,
            unit=row.get("unit"),
            logical_key=row.get("logical_key"),
            revision=revision,
            metadata=metadata,
            frequency=row.get("series_frequency", row.get("frequency")),
        )


@dataclass(frozen=True)
class YieldMetrics:
    series_id: str
    observation_time: date
    yield_percent: float | None
    daily_change_percentage_points: float | None
    daily_change_bps: float | None
    change_5_observations_bps: float | None
    change_10_observations_bps: float | None
    change_20_observations_bps: float | None
    rolling_mean_5_percent: float | None
    rolling_mean_20_percent: float | None
    rolling_volatility_5_bps: float | None
    rolling_volatility_20_bps: float | None
    evidence_type: EvidenceType = EvidenceType.CALCULATION


@dataclass(frozen=True)
class CurveMetrics:
    observation_time: date
    spread_10y_minus_2y_percentage_points: float | None
    spread_10y_minus_2y_bps: float | None
    daily_change_bps: float | None
    change_5_observations_bps: float | None
    change_20_observations_bps: float | None
    evidence_type: EvidenceType = EvidenceType.CALCULATION


@dataclass(frozen=True)
class PositionMetrics:
    series_id: str
    observation_time: date
    participant: str
    long_contracts: float | None
    short_contracts: float | None
    net_position_contracts: float | None
    net_change_contracts: float | None
    spreading_contracts: float | None
    reporting_frequency: str | None
    evidence_type: EvidenceType = EvidenceType.CALCULATION


@dataclass(frozen=True)
class LevelChange:
    series_id: str
    observation_time: date
    value: float | None
    unit: str | None
    change_from_prior: float | None
    percent_change_from_prior: float | None
    frequency: str | None
    evidence_type: EvidenceType = EvidenceType.CALCULATION


@dataclass(frozen=True)
class PriceReturns:
    series_id: str
    observation_time: date
    price: float | None
    daily_return_percent: float | None
    return_5_observations_percent: float | None
    return_20_observations_percent: float | None
    evidence_type: EvidenceType = EvidenceType.CALCULATION


@dataclass(frozen=True)
class ConfirmationThresholds:
    """Absolute move at or below each threshold is classified FLAT."""

    yield_bps: float = 0.5
    price_return_percent: float = 0.1

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.yield_bps)
            or not math.isfinite(self.price_return_percent)
            or self.yield_bps < 0
            or self.price_return_percent < 0
        ):
            raise ValueError("confirmation thresholds must be finite and nonnegative")


@dataclass(frozen=True)
class ConfirmationVector:
    treasury_yields: Direction
    hyg: Direction
    iwm: Direction
    dxy: Direction
    treasury_yield_interpretation: str | None
    evidence_type: EvidenceType = EvidenceType.OBSERVATION


def _obs(item: Observation | Mapping[str, Any]) -> Observation:
    if isinstance(item, Observation):
        return item
    return Observation.from_mapping(item)


def _day(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid observation date: {value!r}") from exc


def _latest_per_date(items: Iterable[Observation | Mapping[str, Any]]) -> list[Observation]:
    latest: dict[tuple[str, date, str | None], Observation] = {}
    for raw in items:
        item = _obs(raw)
        if not item.series_id:
            raise ValueError("series identifier is required")
        day = _day(item.observation_time)
        key = (item.series_id, day, item.logical_key)
        old = latest.get(key)
        if old is None or item.revision > old.revision:
            latest[key] = item
        elif item.revision == old.revision and item != old:
            raise ValueError(f"conflicting observation revision for {item.series_id} on {day}")
    return sorted(
        latest.values(), key=lambda x: (x.series_id, _day(x.observation_time), x.logical_key or "")
    )


def _series_values(
    items: Iterable[Observation | Mapping[str, Any]], series_id: str
) -> list[tuple[date, float | None, Observation]]:
    by_day: dict[date, Observation] = {}
    for item in _latest_per_date(items):
        if item.series_id != series_id:
            continue
        day = _day(item.observation_time)
        if day in by_day and by_day[day].value != item.value:
            raise ValueError(f"multiple values for series {series_id!r} on {day}")
        by_day[day] = item
    return [(d, by_day[d].value, by_day[d]) for d in sorted(by_day)]


def _window_mean(values: list[float | None], end: int, size: int) -> float | None:
    window = values[max(0, end - size + 1) : end + 1]
    return fmean(window) if len(window) == size and all(v is not None for v in window) else None  # type: ignore[arg-type]


def _window_vol(changes_bps: list[float | None], end: int, size: int) -> float | None:
    window = changes_bps[max(0, end - size + 1) : end + 1]
    return pstdev(window) if len(window) == size and all(v is not None for v in window) else None  # type: ignore[arg-type]


def _lag_change(
    values: list[float | None], end: int, lag: int, *, scale: float = 1.0
) -> float | None:
    prior = end - lag
    if prior < 0 or values[end] is None or values[prior] is None:
        return None
    return (values[end] - values[prior]) * scale


def yield_metrics(
    observations: Iterable[Observation | Mapping[str, Any]],
    series_id: str,
    *,
    expected_unit: str = "Percent",
) -> list[YieldMetrics]:
    """Calculate yield deltas; changes use prior available observations, never filled dates."""
    rows = _series_values(observations, series_id)
    for _, _, item in rows:
        if item.unit is not None and item.unit.casefold() not in {
            expected_unit.casefold(),
            "percent",
        }:
            raise ValueError(
                f"yield {series_id!r} requires percentage-point values, got unit {item.unit!r}"
            )
    vals = [value for _, value, _ in rows]
    daily_pp = [_lag_change(vals, i, 1) for i in range(len(vals))]
    daily_bps = [None if value is None else value * 100 for value in daily_pp]
    return [
        YieldMetrics(
            series_id,
            day,
            value,
            daily_pp[i],
            daily_bps[i],
            _lag_change(vals, i, 5, scale=100),
            _lag_change(vals, i, 10, scale=100),
            _lag_change(vals, i, 20, scale=100),
            _window_mean(vals, i, 5),
            _window_mean(vals, i, 20),
            _window_vol(daily_bps, i, 5),
            _window_vol(daily_bps, i, 20),
        )
        for i, (day, value, _) in enumerate(rows)
    ]


def curve_metrics(
    observations: Iterable[Observation | Mapping[str, Any]],
    *,
    two_year_series: str = "DGS2",
    ten_year_series: str = "DGS10",
) -> list[CurveMetrics]:
    """Calculate exact-date 10Y minus 2Y spreads in basis points on shared dates only."""
    obs = _latest_per_date(observations)
    two = {day: val for day, val, _ in _series_values(obs, two_year_series)}
    ten = {day: val for day, val, _ in _series_values(obs, ten_year_series)}
    dates = sorted(set(two) & set(ten))
    spreads = [None if two[d] is None or ten[d] is None else (ten[d] - two[d]) * 100 for d in dates]
    spreads_pp = [None if two[d] is None or ten[d] is None else ten[d] - two[d] for d in dates]
    return [
        CurveMetrics(
            d,
            spreads_pp[i],
            spread,
            _lag_change(spreads, i, 1),
            _lag_change(spreads, i, 5),
            _lag_change(spreads, i, 20),
        )
        for i, (d, spread) in enumerate(zip(dates, spreads, strict=True))
    ]


_POSITION_NAMES = {
    "dealer": "dealer",
    "dealer_intermediary": "dealer",
    "dealer/intermediary": "dealer",
    "leveraged_funds": "leveraged_fund",
    "leveraged fund": "leveraged_fund",
    "lev_money": "leveraged_fund",
    "asset_manager": "asset_manager",
    "asset_manager_institutional": "asset_manager",
    "asset manager": "asset_manager",
    "asset_mgr": "asset_manager",
    "other_reportables": "other_reportable",
    "other reportables": "other_reportable",
    "other_reportable": "other_reportable",
    "nonreportable": "nonreportable",
    "non_reportable": "nonreportable",
    "non-reportable": "nonreportable",
}


def positioning_metrics(
    observations: Iterable[Observation | Mapping[str, Any]],
) -> list[PositionMetrics]:
    """Calculate CFTC outright net positions by report, keeping spreading independent."""
    grouped: dict[tuple[str, date, str], dict[str, float | None]] = defaultdict(dict)
    frequencies: dict[tuple[str, date, str], str | None] = {}
    for item in _latest_per_date(observations):
        meta = item.metadata or {}
        participant = _POSITION_NAMES.get(str(meta.get("participant_category", "")).casefold())
        metric = str(meta.get("metric", "")).casefold()
        if participant is None or metric not in {"long", "short", "spreading"}:
            continue
        if item.unit is not None and item.unit != "contracts":
            raise ValueError(
                f"CFTC outright positioning must retain contract units, got {item.unit!r}"
            )
        key = (item.series_id, _day(item.observation_time), participant)
        if metric in grouped[key] and grouped[key][metric] != item.value:
            raise ValueError(f"duplicate CFTC {metric} measure for {key}")
        grouped[key][metric] = item.value
        frequencies[key] = item.frequency or meta.get("reporting_frequency") or "weekly"
    output: list[PositionMetrics] = []
    last_net: dict[tuple[str, str], float | None] = {}
    for key in sorted(grouped):
        series, day, participant = key
        measures = grouped[key]
        long = measures.get("long")
        short = measures.get("short")
        net = None if long is None or short is None else long - short
        prior = last_net.get((series, participant))
        delta = None if net is None or prior is None else net - prior
        last_net[(series, participant)] = net
        output.append(
            PositionMetrics(
                series,
                day,
                participant,
                long,
                short,
                net,
                delta,
                measures.get("spreading"),
                frequencies.get(key),
            )
        )
    return output


def level_changes(
    observations: Iterable[Observation | Mapping[str, Any]], series_id: str
) -> list[LevelChange]:
    """Compute NY Fed-style level changes and safe percentage changes in native units."""
    rows = _series_values(observations, series_id)
    out: list[LevelChange] = []
    prior: float | None = None
    frequency: str | None = None
    unit: str | None = None
    for day, value, item in rows:
        unit = item.unit or unit
        frequency = item.frequency or (item.metadata or {}).get("reporting_frequency") or frequency
        change = None if value is None or prior is None else value - prior
        pct = None if value is None or prior in (None, 0) else (value / prior - 1) * 100
        out.append(LevelChange(series_id, day, value, unit, change, pct, frequency))
        prior = value
    return out


def market_price_returns(
    observations: Iterable[Observation | Mapping[str, Any]], series_id: str
) -> list[PriceReturns]:
    """Calculate returns only across successive supplied observations of a price series."""
    rows = _series_values(observations, series_id)
    values = [v for _, v, _ in rows]

    def ret(i: int, lag: int) -> float | None:
        prior = i - lag
        if prior < 0 or values[i] is None or values[prior] in (None, 0):
            return None
        return (values[i] / values[prior] - 1) * 100  # type: ignore[operator]

    return [
        PriceReturns(series_id, day, price, ret(i, 1), ret(i, 5), ret(i, 20))
        for i, (day, price, _) in enumerate(rows)
    ]


def _direction(change: float | None, threshold: float) -> Direction:
    if change is None or not math.isfinite(change):
        return Direction.UNKNOWN
    if change > threshold:
        return Direction.RISING
    if change < -threshold:
        return Direction.FALLING
    return Direction.FLAT


def build_confirmation_vector(
    *,
    treasury_yield_change_bps: float | None,
    hyg_return_percent: float | None = None,
    iwm_return_percent: float | None = None,
    dxy_return_percent: float | None = None,
    thresholds: ConfirmationThresholds | None = None,
) -> ConfirmationVector:
    """Summarize observed simultaneous directions without a score or causal label."""
    thresholds = thresholds or ConfirmationThresholds()
    y = _direction(treasury_yield_change_bps, thresholds.yield_bps)
    interpretation = {
        Direction.RISING: "yield rose / Treasury price pressure",
        Direction.FALLING: "yield fell / Treasury price support",
        Direction.FLAT: "yield move within configured flat threshold",
        Direction.UNKNOWN: None,
    }[y]
    return ConfirmationVector(
        y,
        _direction(hyg_return_percent, thresholds.price_return_percent),
        _direction(iwm_return_percent, thresholds.price_return_percent),
        _direction(dxy_return_percent, thresholds.price_return_percent),
        interpretation,
    )

