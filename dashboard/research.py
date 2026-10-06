"""Read-only dashboard projection, evidence labels, and descriptive event studies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any

from treasury_flow_radar.analytics.descriptive import EvidenceType
from treasury_flow_radar.analytics.event_study import event_study
from treasury_flow_radar.analytics.evidence import evidence_from_rows
from treasury_flow_radar.analytics.report import load_observations_read_only


@dataclass(frozen=True)
class Event:
    event_id: str
    event_date: date
    event_type: str
    issuer: str | None = None
    pricing_date: date | None = None
    settlement_date: date | None = None
    size: float | None = None
    size_unit: str | None = None
    notes: str | None = None
    source: str = "USER-SUPPLIED"


def classify_freshness(
    retrieved_at: str | None,
    frequency: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Classify age against documented conservative display thresholds."""
    if not retrieved_at or not frequency:
        return "UNKNOWN"
    limits = {"daily": 5, "weekly": 14, "monthly": 45}
    days = next((limit for key, limit in limits.items() if key in frequency.lower()), None)
    if days is None:
        return "UNKNOWN"
    try:
        instant = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return "UNKNOWN"
    if instant.tzinfo is None:
        return "UNKNOWN"
    age = ((now or datetime.now(UTC)) - instant.astimezone(UTC)).total_seconds()
    return "FRESH" if age <= days * 86400 else "STALE"


def load_observations(database_path: str | Path) -> list[dict[str, Any]]:
    """Compatibility projection backed by the shared read-only analytics loader."""
    path = Path(database_path)
    if not path.is_file():
        return []
    try:
        rows = load_observations_read_only(path)
    except (OSError, ValueError):
        return []
    for row in rows:
        row["id"] = row.get("observation_id")
        row["freshness"] = classify_freshness(row.get("retrieval_time"), row.get("frequency"))
    return rows


def evidence_summary(observations: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Produce only directly attributable facts and transparent calculations."""
    rows = list(observations)
    output: list[dict[str, str]] = []
    for identifier in ("DGS2", "DGS10"):
        series = sorted(
            (r for r in rows if r["series_identifier"].upper() == identifier),
            key=lambda r: (r["observation_time"], r["revision"]),
        )
        usable = [r for r in series if r["value_numeric"] is not None]
        if usable:
            latest = usable[-1]
            fact = evidence_from_rows(EvidenceType.FACT, (
                f"{identifier} reported {latest['value_numeric']:g} {latest['unit'] or latest['default_unit'] or ''} "
                f"on {latest['observation_time'][:10]} ({latest['source_name']})."
            ).strip(), [latest])
            output.append({"type": fact.evidence_type.value, "text": fact.statement,
                           **{k: v for k, v in fact.to_dict().items() if k not in {"evidence_type", "statement"}}})
            if len(usable) > 1:
                prior = usable[-2]
                delta = (latest["value_numeric"] - prior["value_numeric"]) * 100
                direction = "rose" if delta > 0 else "fell" if delta < 0 else "was unchanged"
                calc = evidence_from_rows(EvidenceType.CALCULATION, (
                    f"{identifier} {direction} {abs(delta):g} basis points between the latest two supplied observations."
                ), [prior, latest])
                output.append({"type": calc.evidence_type.value, "text": calc.statement,
                               **{k: v for k, v in calc.to_dict().items() if k not in {"evidence_type", "statement"}}})
    for category, statement in (
        (EvidenceType.MECHANISM, "Dealer hedging can transmit temporary duration exposure into Treasury cash or futures markets; this is a possible mechanism, not a finding about a particular move."),
        (EvidenceType.HYPOTHESIS, "Corporate issuance may create temporary rate exposure and hedging pressure around pricing or settlement; event-level production data are unavailable, so this remains untested here."),
    ):
        record = evidence_from_rows(category, statement, [])
        output.append({"type": record.evidence_type.value, "text": record.statement,
                       **{k: v for k, v in record.to_dict().items() if k not in {"evidence_type", "statement"}}})
    return output


def build_event_window(event: Event, observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility view over the shared observation-order event-study engine."""
    rows = list(observations)
    study = event_study(event.event_date, rows)
    output = []
    for point in study["windows"]["T-5_T+5"]:
        day = point["source_observation_date"]
        dayrows = [r for r in rows if day and str(r["observation_time"])[:10] == day]
        output.append({
            "offset": point["offset"], "date": day,
            "yield_2y": point["dgs2_percent"], "yield_10y": point["dgs10_percent"],
            "curve_bps": point["spread_bps"],
            "yield_2y_change_bps": point["dgs2_change_bps"],
            "yield_10y_change_bps": point["dgs10_change_bps"],
            "cftc": [r for r in dayrows if "cftc" in str(r.get("source_identifier", "")).lower()],
            "nyfed": [r for r in dayrows if "nyfed" in str(r.get("source_identifier", "")).lower()
                      or "dealer" in str(r.get("source_name", "")).lower()],
            "auctions": [r for r in dayrows if "auction" in str(r.get("source_identifier", "")).lower()],
            "prices": [r for r in dayrows if str(r["series_identifier"]).upper() in {"HYG", "IWM", "DXY", "ZN", "UB", "ZB"}],
        })
    return output


def compare_events(windows: Iterable[list[dict[str, Any]]], *, minimum_sample: int = 5) -> dict[str, Any]:
    """Summarize observed event outcomes; no significance test or causal estimate."""
    outcomes = []
    post_event_changes = []
    for window in windows:
        pre = next((r["yield_10y"] for r in window if r["offset"] == -1), None)
        event_level = next((r["yield_10y"] for r in window if r["offset"] == 0), None)
        post = next((r["yield_10y"] for r in window if r["offset"] == 1), None)
        if pre is not None and post is not None:
            outcomes.append((post - pre) * 100)
        if event_level is not None and post is not None:
            post_event_changes.append((post - event_level) * 100)
    if len(outcomes) < minimum_sample:
        return {"status": "INSUFFICIENT SAMPLE", "n": len(outcomes), "mean_bps": None,
                "median_bps": None, "pct_rising": None, "post_event_n": len(post_event_changes),
                "pct_post_event_decline": None,
                "outcomes_bps": outcomes}
    return {"status": "DESCRIPTIVE SUMMARY", "n": len(outcomes),
            "mean_bps": fmean(outcomes), "median_bps": median(outcomes),
            "pct_rising": 100 * sum(v > 0 for v in outcomes) / len(outcomes),
            "post_event_n": len(post_event_changes),
            "pct_post_event_decline": (
                100 * sum(v < 0 for v in post_event_changes) / len(post_event_changes)
                if post_event_changes else None
            ),
            "outcomes_bps": outcomes}

