"""Explicit date alignment for sparse and differently sampled observations."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Literal

from treasury_flow_radar.analytics.descriptive import EvidenceType, Observation


@dataclass(frozen=True)
class TemporalMatch:
    source_observation_date: date
    event_date: date
    lag_days: int
    relationship: Literal["BEFORE", "SAME_DAY", "AFTER"]
    value: float | None
    unit: str | None
    source_identifier: str | None
    series_identifier: str
    observation_id: int | None
    retrieval_time: str | None
    publication_time: str | None
    evidence_type: EvidenceType = EvidenceType.FACT

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_observation_date"] = self.source_observation_date.isoformat()
        result["event_date"] = self.event_date.isoformat()
        result["evidence_type"] = self.evidence_type.value
        return result


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def align_observation(
    observations: Iterable[Observation | Mapping[str, Any]],
    event_date: date | str,
    *,
    direction: Literal["prior", "following", "nearest"] = "prior",
    max_lag_days: int | None = None,
) -> TemporalMatch | None:
    """Select one supplied observation and preserve the signed temporal relation.

    ``prior`` means the latest observation on/before the event. ``following``
    means the earliest observation on/after it. ``nearest`` chooses the smallest
    absolute calendar-day distance and breaks ties toward the prior observation.
    No interpolation or daily expansion occurs.
    """
    event = _date(event_date)
    if max_lag_days is not None and max_lag_days < 0:
        raise ValueError("max_lag_days must be nonnegative")
    candidates: list[tuple[date, Observation, Mapping[str, Any]]] = []
    for raw in observations:
        item = raw if isinstance(raw, Observation) else Observation.from_mapping(raw)
        raw_mapping: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        observed = _date(item.observation_time)
        if direction == "prior" and observed > event:
            continue
        if direction == "following" and observed < event:
            continue
        candidates.append((observed, item, raw_mapping))
    if not candidates:
        return None
    if direction == "prior":
        chosen = max(candidates, key=lambda x: x[0])
    elif direction == "following":
        chosen = min(candidates, key=lambda x: x[0])
    elif direction == "nearest":
        chosen = min(candidates, key=lambda x: (abs((x[0] - event).days), x[0] > event))
    else:
        raise ValueError(f"unsupported temporal alignment direction: {direction}")
    observed, item, row = chosen
    lag = (event - observed).days
    if max_lag_days is not None and abs(lag) > max_lag_days:
        return None
    return TemporalMatch(
        source_observation_date=observed,
        event_date=event,
        lag_days=lag,
        relationship="BEFORE" if lag > 0 else "AFTER" if lag < 0 else "SAME_DAY",
        value=item.value,
        unit=item.unit,
        source_identifier=row.get("source_identifier"),
        series_identifier=item.series_id,
        observation_id=(int(row["observation_id"]) if row.get("observation_id") is not None
                        else int(row["id"]) if row.get("id") is not None else None),
        retrieval_time=row.get("retrieval_time"),
        publication_time=row.get("publication_time"),
    )

