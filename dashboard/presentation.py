"""Compact, provenance-aware views for the dashboard and offline snapshot."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any


def build_dashboard_view(report: Mapping[str, Any], *, event_limit: int = 60) -> dict[str, Any]:
    """Make a small human-readable projection without changing research calculations.

    The research report remains the audit/calculation layer. This projection keeps
    current facts and bounded event evidence for presentation, avoiding repeated
    raw observations and provenance payloads in static HTML.
    """
    yields = report.get("yield_changes", {})
    dgs2 = _latest(yields.get("DGS2", []))
    dgs10 = _latest(yields.get("DGS10", []))
    spread = _latest(report.get("spread", []))
    dealers = list(report.get("dealer_positions", []))
    dealer = _latest(dealers)
    cftc = _cftc_contracts(report.get("cftc_positions", []))
    auctions = list(report.get("treasury_auctions", []))
    latest_auction = _latest(auctions)
    availability = dict(report.get("availability", {}))
    corporate = report.get("corporate_issuance", {})
    market = report.get("market_confirmation", {})
    events = list(report.get("events", []))[-event_limit:]
    return {
        "generated_at": report.get("generated_at"),
        "scope": dict(report.get("scope", {})),
        "system_read": _system_read(dgs2, dgs10, spread, dealer, cftc, latest_auction, corporate, market),
        "happening": {
            "dgs10": _yield_fact(dgs10),
            "dgs2": _yield_fact(dgs2),
            "spread": _spread_fact(spread),
            "dealer": _dealer_fact(dealer),
            "cftc": cftc,
            "auction": _auction_fact(latest_auction),
        },
        "evidence": _evidence_status(availability, corporate, market),
        "rate_lock_status": _rate_lock_status(availability, corporate, market),
        "events": [_event_view(item) for item in events],
        "provenance": _provenance(report.get("source_provenance", []), report.get("data_freshness", {})),
        "interpretation_limit": report.get("interpretation_limit"),
        "event_limit": event_limit,
        "event_count": len(report.get("events", [])),
    }


def _latest(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return items[-1] if items else None


def _yield_fact(item: Mapping[str, Any] | None) -> dict[str, Any]:
    if item is None:
        return {"status": "UNAVAILABLE — NO OBSERVATIONS", "evidence_type": "FACT"}
    return {"status": "AVAILABLE", "yield_percent": item.get("yield_percent"),
            "observation_date": item.get("observation_date"),
            "daily_change_bps": item.get("daily_change_bps"),
            "change_5_observations_bps": item.get("change_5_observations_bps"),
            "change_10_observations_bps": item.get("change_10_observations_bps"),
            "evidence_type": item.get("evidence_type", "CALCULATION")}


def _spread_fact(item: Mapping[str, Any] | None) -> dict[str, Any]:
    if item is None:
        return {"status": "UNAVAILABLE — NO SHARED DGS2/DGS10 DATE", "evidence_type": "CALCULATION"}
    return {"status": "AVAILABLE", "spread_bps": item.get("spread_bps"),
            "daily_change_bps": item.get("daily_change_bps"),
            "observation_date": item.get("observation_date"), "evidence_type": item.get("evidence_type")}


def _dealer_fact(item: Mapping[str, Any] | None) -> dict[str, Any]:
    if item is None:
        return {"status": "UNAVAILABLE — NO OBSERVATIONS", "evidence_type": "FACT"}
    provenance = item.get("provenance") or {}
    return {"status": "AVAILABLE", "series_name": item.get("series_name"), "position": item.get("position"),
            "change": item.get("change"), "unit": item.get("unit"),
            "observation_date": item.get("observation_date"), "frequency": item.get("reporting_frequency"),
            "retrieval_time": provenance.get("retrieval_time"), "publication_time": provenance.get("publication_time"),
            "source": provenance.get("source_identifier"), "evidence_type": item.get("evidence_type")}


def _cftc_contracts(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("contract") or row.get("series_identifier"))].append(row)
    results = []
    for contract, values in sorted(grouped.items()):
        values.sort(key=lambda r: (str(r.get("positioning_date")), str(r.get("participant_category"))))
        latest_date = max(str(value.get("positioning_date") or "") for value in values)
        current = [value for value in values if str(value.get("positioning_date") or "") == latest_date]
        dealer = next((value for value in current if value.get("participant_category") == "dealer"), current[0])
        results.append({
            "contract": contract, "positioning_date": latest_date or None,
            "summary_category": dealer.get("participant_category"), "net": dealer.get("net"),
            "net_change": dealer.get("net_change"), "long": dealer.get("long"), "short": dealer.get("short"),
            "spreading": dealer.get("spreading"), "evidence_type": dealer.get("evidence_type", "FACT"),
            "details": [_cftc_row(value) for value in current],
        })
    return results


def _cftc_row(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = row.get("provenance") or {}
    return {key: row.get(key) for key in ("participant_category", "long", "short", "spreading", "net", "net_change", "positioning_date", "reporting_frequency", "evidence_type")} | {
        "source": provenance.get("source_identifier"), "retrieval_time": provenance.get("retrieval_time"),
        "publication_time": provenance.get("publication_time"),
    }


def _auction_fact(item: Mapping[str, Any] | None) -> dict[str, Any]:
    if item is None:
        return {"status": "UNAVAILABLE — NO OBSERVATIONS", "evidence_type": "FACT"}
    provenance = item.get("provenance") or {}
    return {"status": "AVAILABLE", "security_type": item.get("security_type"), "security_term": item.get("security_term"),
            "auction_date": item.get("auction_date"), "offering_amount": item.get("offering_amount"),
            "accepted_amount": item.get("accepted_amount"), "bid_to_cover": item.get("bid_to_cover"),
            "yield_or_rate": item.get("yield_or_rate"), "yield_or_rate_unit": item.get("yield_or_rate_unit"),
            "source": provenance.get("source_identifier"), "retrieval_time": provenance.get("retrieval_time"),
            "publication_time": provenance.get("publication_time"), "evidence_type": item.get("evidence_type")}


def _system_read(dgs2: Mapping[str, Any] | None, dgs10: Mapping[str, Any] | None,
                 spread: Mapping[str, Any] | None, dealer: Mapping[str, Any] | None,
                 cftc: list[dict[str, Any]], auction: Mapping[str, Any] | None,
                 corporate: Mapping[str, Any], market: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"label": "Treasury yields", "value": _movement(dgs10), "detail": _yield_fact(dgs10), "evidence_type": "CALCULATION"},
        {"label": "Curve", "value": _curve_read(spread), "detail": _spread_fact(spread), "evidence_type": "CALCULATION"},
        {"label": "Primary dealers", "value": _availability(dealer is not None), "detail": _dealer_fact(dealer), "evidence_type": "FACT"},
        {"label": "CFTC positioning", "value": f"AVAILABLE — {len(cftc)} Treasury contracts" if cftc else "UNAVAILABLE — NO OBSERVATIONS", "detail": cftc, "evidence_type": "FACT"},
        {"label": "Treasury supply", "value": _availability(auction is not None), "detail": _auction_fact(auction), "evidence_type": "FACT"},
        {"label": "Corporate issuance", "value": str(corporate.get("status") or "UNAVAILABLE — NO PRODUCTION FEED CONFIGURED"), "detail": {}, "evidence_type": "FACT"},
        {"label": "Market confirmation", "value": str(market.get("status") or "Market confirmation unavailable"), "detail": {}, "evidence_type": "OBSERVATION"},
    ]


def _movement(item: Mapping[str, Any] | None) -> str:
    if item is None or item.get("change_5_observations_bps") is None:
        return "UNAVAILABLE — INSUFFICIENT OBSERVATIONS"
    change = float(item["change_5_observations_bps"])
    # Exact deterministic wording: the sign of the five-observation calculation.
    direction = "RISING" if change > 0 else "FALLING" if change < 0 else "UNCHANGED"
    return f"{direction} over 5 observations ({change:.1f} bp)"


def _curve_read(item: Mapping[str, Any] | None) -> str:
    if item is None:
        return "UNAVAILABLE — NO SHARED DGS2/DGS10 DATE"
    value = item.get("spread_bps")
    change = item.get("daily_change_bps")
    suffix = "" if change is None else f"; latest change {float(change):.1f} bp"
    return f"10Y–2Y {float(value):.1f} bp{suffix}" if value is not None else "UNAVAILABLE"


def _availability(available: bool) -> str:
    return "AVAILABLE" if available else "UNAVAILABLE — NO OBSERVATIONS"


def _evidence_status(availability: Mapping[str, Any], corporate: Mapping[str, Any], market: Mapping[str, Any]) -> dict[str, list[str]]:
    available = ["10Y yield movement", "10Y–2Y curve movement", "dealer positioning", "CFTC positioning", "Treasury auction data"]
    missing = []
    if corporate.get("status") != "AVAILABLE":
        missing.extend(["Corporate issuance event feed", "Corporate deal size, maturity/duration, pricing date, and settlement date"])
    if market.get("status") != "AVAILABLE":
        missing.extend(["Treasury futures price confirmation", "HYG", "IWM", "DXY"])
    return {"available": available, "not_available": missing,
            "causality_limitation": ["Co-movement, positioning, and timing do not establish causality.",
                                      "Observation-date alignment does not establish publication-time availability or causality."]}


def _rate_lock_status(availability: Mapping[str, Any], corporate: Mapping[str, Any], market: Mapping[str, Any]) -> list[dict[str, str]]:
    issuance_available = corporate.get("status") == "AVAILABLE"
    return [
        {"item": "Corporate issuance event feed", "status": "AVAILABLE" if issuance_available else "UNAVAILABLE"},
        {"item": "Deal size", "status": "AVAILABLE" if issuance_available else "UNAVAILABLE"},
        {"item": "Maturity/duration", "status": "AVAILABLE" if issuance_available else "UNAVAILABLE"},
        {"item": "Pricing date", "status": "AVAILABLE" if issuance_available else "UNAVAILABLE"},
        {"item": "Settlement date", "status": "AVAILABLE" if issuance_available else "UNAVAILABLE"},
        {"item": "Treasury yield event data", "status": "AVAILABLE" if availability.get("FRED yields") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Dealer positioning", "status": "AVAILABLE" if availability.get("NYFED") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "CFTC positioning", "status": "AVAILABLE" if availability.get("CFTC") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Post-settlement Treasury response", "status": "PARTIALLY AVAILABLE" if availability.get("FRED yields") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Cross-market confirmation", "status": "AVAILABLE" if market.get("status") == "AVAILABLE" else "UNAVAILABLE"},
    ]


def _event_view(item: Mapping[str, Any]) -> dict[str, Any]:
    event = item.get("event", {})
    dealer = item.get("dealer_context", {})
    cftc = item.get("cftc_context", {}).get("contracts", [])
    auctions = item.get("auction_context", {}).get("auctions", [])
    corporate = item.get("corporate_issuance_context", {})
    market = item.get("market_confirmation", {})
    study = item.get("event_study", {})
    return {
        "summary": {"date": event.get("event_date"), "dgs10_change_bps": event.get("change_bps"),
                    "dgs2_change_bps": event.get("dgs2_change_bps"), "curve_change_bps": event.get("spread_change_bps"),
                    "dealer_available": any(value and value.get("position") is not None for value in dealer.values()),
                    "cftc_available": bool(cftc), "auction_available": bool(auctions),
                    "corporate_available": corporate.get("status") == "AVAILABLE",
                    "market_confirmation_available": market.get("status") == "AVAILABLE"},
        "market_move": {key: event.get(key) for key in ("event_date", "prior_observation_date", "prior_yield_percent", "current_yield_percent", "change_bps", "dgs2_change_bps", "spread_10y_minus_2y_bps", "spread_change_bps", "evidence_type")},
        "dealer_positioning": {name: _compact_dealer(value) for name, value in dealer.items()},
        "cftc_positioning": [_compact_cftc_contract(value) for value in cftc],
        "treasury_supply": [_compact_auction(value) for value in auctions],
        "corporate_issuance": {"status": corporate.get("status"), "events": corporate.get("events", [])},
        "market_confirmation": {"status": market.get("status"), "classification": market.get("classification")},
        "event_study": {"alignment": study.get("window_alignment"), "summary": study.get("summary"), "windows": study.get("windows")},
        "limitations": ["Weekly dealer and CFTC context is not forward-filled.",
                        "Observation-date alignment does not establish publication-time availability or causality."],
    }


def _compact_dealer(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {key: value.get(key) for key in ("position", "unit", "observation_date", "lag_days", "temporal_relationship", "change_from_prior_observation", "evidence_type", "availability_note")}


def _compact_cftc_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"contract": value.get("contract"), "positioning_date": value.get("positioning_date"),
            "days_before_event": value.get("days_before_event"), "temporal_relationship": value.get("temporal_relationship"),
            "participant_categories": [{key: item.get(key) for key in ("category", "long", "short", "spreading", "net_position", "evidence_type")} for item in value.get("participant_categories", [])]}


def _compact_auction(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in ("security_type", "security_term", "auction_date", "offering_amount", "accepted_amount", "bid_to_cover", "yield_or_rate", "yield_or_rate_unit", "evidence_type", "temporal_alignment")}


def _provenance(rows: list[Mapping[str, Any]], freshness: Mapping[str, Any]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for row in rows:
        source = row.get("source_identifier")
        if not source or source in seen:
            continue
        seen.add(source)
        timing = freshness.get(source, {})
        result.append({"source": source, "reference": row.get("source_url"), "series": row.get("series_identifier"),
                       "observation_date": timing.get("observation_date"), "retrieved": timing.get("retrieval_time"),
                       "published": timing.get("publication_time"), "evidence_type": "FACT"})
    return result

