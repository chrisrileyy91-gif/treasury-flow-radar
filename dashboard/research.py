"""Read-only dashboard projection, evidence labels, and descriptive event studies."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any


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
    """Read latest immutable revisions from an existing project DB; never creates it."""
    path = Path(database_path)
    if not path.is_file():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    connection.row_factory = sqlite3.Row
    try:
        required = {"sources", "series", "observations"}
        existing = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required.issubset(existing):
            return []
        query = """
        SELECT o.id, o.source_id, o.series_id, s.identifier AS source_identifier,
               s.name AS source_name, s.url AS source_url, se.identifier AS series_identifier,
               se.name AS series_name, se.frequency, se.default_unit,
               o.logical_key, o.revision, o.observation_time, o.publication_time,
               o.retrieval_time, o.value_numeric, o.value_text, o.raw_value, o.unit,
               o.raw_record_id, o.metadata_json
        FROM observations o
        JOIN sources s ON s.id=o.source_id
        JOIN series se ON se.id=o.series_id
        WHERE o.revision=(SELECT MAX(latest.revision) FROM observations latest
          WHERE latest.source_id=o.source_id AND latest.series_id=o.series_id
            AND latest.logical_key=o.logical_key)
        ORDER BY o.observation_time, o.id
        """
        rows = []
        import json

        for row in connection.execute(query):
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            item["freshness"] = classify_freshness(item["retrieval_time"], item["frequency"])
            rows.append(item)
        return rows
    except sqlite3.Error:
        return []
    finally:
        connection.close()


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
            output.append({"type": "FACT", "text": (
                f"{identifier} reported {latest['value_numeric']:g} {latest['unit'] or latest['default_unit'] or ''} "
                f"on {latest['observation_time'][:10]} ({latest['source_name']})."
            ).strip()})
            if len(usable) > 1:
                delta = (latest["value_numeric"] - usable[-2]["value_numeric"]) * 100
                direction = "rose" if delta > 0 else "fell" if delta < 0 else "was unchanged"
                output.append({"type": "CALCULATION", "text": (
                    f"{identifier} {direction} {abs(delta):g} basis points between the latest two supplied observations."
                )})
    output.append({"type": "MECHANISM", "text": (
        "Dealer hedging can transmit temporary duration exposure into Treasury cash or futures markets; this is a possible mechanism, not a finding about a particular move."
    )})
    output.append({"type": "HYPOTHESIS", "text": (
        "Corporate issuance may create temporary rate exposure and hedging pressure around pricing or settlement; event-level production data are unavailable, so this remains untested here."
    )})
    return output


def build_event_window(event: Event, observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align to actual Treasury yield observation dates, retaining missing T0 and sparse sources."""
    rows = list(observations)
    yield_dates = sorted({
        date.fromisoformat(r["observation_time"][:10])
        for r in rows
        if r["series_identifier"].upper() in {"DGS2", "DGS10"}
    })
    before = [d for d in yield_dates if d < event.event_date][-5:]
    after = [d for d in yield_dates if d > event.event_date][:5]
    aligned = {offset: day for offset, day in zip(range(-len(before), 0), before, strict=True)}
    aligned[0] = event.event_date
    aligned.update({offset: day for offset, day in zip(range(1, len(after) + 1), after, strict=True)})
    output = []
    for offset in range(-5, 6):
        day = aligned.get(offset)
        if day is None:
            output.append({"offset": offset, "date": None, "yield_2y": None, "yield_10y": None,
                           "curve_bps": None, "yield_2y_change_bps": None,
                           "yield_10y_change_bps": None, "cftc": [], "nyfed": [],
                           "auctions": [], "prices": []})
            continue
        dayrows = [r for r in rows if r["observation_time"][:10] == day.isoformat()]
        values: dict[str, Any] = {}
        for r in dayrows:
            key = r["series_identifier"].upper()
            if key in {"DGS2", "DGS10"} and r["value_numeric"] is not None:
                values[key] = r["value_numeric"]
        dgs2, dgs10 = values.get("DGS2"), values.get("DGS10")
        cftc = [r for r in dayrows if "cftc" in r["source_identifier"].lower()]
        nyfed = [r for r in dayrows if "nyfed" in r["source_identifier"].lower() or "dealer" in r["source_name"].lower()]
        auctions = [r for r in dayrows if "auction" in r["source_identifier"].lower()]
        prices = [r for r in dayrows if r["series_identifier"].upper() in {"HYG", "IWM", "DXY", "ZN", "UB", "ZB"}]
        output.append({
            "offset": offset, "date": day.isoformat(), "yield_2y": dgs2, "yield_10y": dgs10,
            "curve_bps": None if dgs2 is None or dgs10 is None else (dgs10 - dgs2) * 100,
            "yield_2y_change_bps": None, "yield_10y_change_bps": None,
            "cftc": cftc, "nyfed": nyfed, "auctions": auctions, "prices": prices,
        })
    baseline = next((row for row in output if row["offset"] == -1), None)
    if baseline:
        for row in output:
            for maturity in ("2y", "10y"):
                current, prior = row[f"yield_{maturity}"], baseline[f"yield_{maturity}"]
                row[f"yield_{maturity}_change_bps"] = (
                    None if current is None or prior is None else (current - prior) * 100
                )
    # Window dates are observation-order offsets. Missing dates are not fabricated.
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

