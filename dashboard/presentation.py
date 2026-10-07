"""Compact, provenance-aware view model for the dashboard and the published page.

This module is a projection over the shared read-only research report. It selects
current facts, short histories for charts, and a bounded list of recent events. It
never changes stored rows or research calculations, and every value it exposes is
either a reported FACT, a deterministic CALCULATION, or explicitly unavailable.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

# Constant-maturity yields shown on the curve, shortest to longest.
MATURITIES = (
    ("DGS2", "2-year", 2), ("DGS5", "5-year", 5), ("DGS7", "7-year", 7),
    ("DGS10", "10-year", 10), ("DGS30", "30-year", 30),
)
# CFTC TFF participant groups shown up front; the others stay in the report.
CFTC_GROUPS = (("dealer", "Dealers"), ("asset_manager", "Asset managers"),
               ("leveraged_fund", "Leveraged funds"))
# CFTC contracts by tenor, with short display names.
CFTC_CONTRACTS = (
    ("tff_futures_only_ust_2_year_note_042601", "2-year"),
    ("tff_futures_only_ust_5_year_note_044601", "5-year"),
    ("tff_futures_only_ust_10_year_note_043602", "10-year"),
    ("tff_futures_only_ust_ultra_10_year_note_043607", "Ultra 10"),
    ("tff_futures_only_ust_30_year_bond_020601", "Bond"),
)
NO_OBSERVATIONS = "UNAVAILABLE — NO OBSERVATIONS IN DATABASE"
NO_FEED = "UNAVAILABLE — NO PRODUCTION FEED CONFIGURED"
HISTORY_SESSIONS = 130   # about six months of daily yields for the chart
DEALER_WEEKS = 52
AUCTION_ROWS = 8


def build_dashboard_view(report: Mapping[str, Any], *, event_limit: int = 60) -> dict[str, Any]:
    """Make a small human-readable projection without changing research calculations."""
    yields = report.get("yield_changes", {}) or {}
    curve = _curve(yields)
    dealer = _dealer(list(report.get("dealer_positions", [])))
    cftc = _cftc(list(report.get("cftc_positions", [])))
    auctions = list(report.get("treasury_auctions", []))
    corporate = report.get("corporate_issuance", {}) or {}
    market = report.get("market_confirmation", {}) or {}
    spread = _latest(report.get("spread", []))
    all_events = list(report.get("events", []))
    events = all_events[-event_limit:] if event_limit > 0 else []
    ten = yields.get("DGS10", [])
    latest_ten = _latest(ten)
    return {
        "generated_at": report.get("generated_at"),
        "scope": dict(report.get("scope", {})),
        "data_through": max((row["date"] for row in curve["rows"] if row["date"]), default=None),
        "curve": curve,
        "curve_read": _curve_read(curve["rows"]),
        "channel": _channel(report.get("move_decomposition") or {}),
        "ten_year_history": _ten_year_history(ten, all_events),
        "spread": None if spread is None else {
            "spread_bps": spread.get("spread_bps"), "daily_change_bps": spread.get("daily_change_bps"),
            "date": spread.get("observation_date")},
        "dealer": dealer,
        "cftc": cftc,
        "auctions": [_auction(item) for item in reversed(auctions[-AUCTION_ROWS:])],
        "evidence": _evidence_status(latest_ten, spread, dealer, cftc, auctions, corporate, market),
        "rate_lock_status": _rate_lock_status(report.get("availability", {}) or {}, corporate, market),
        "corporate_status": str(corporate.get("status") or "Corporate issuance event feed not configured"),
        "market_status": str(market.get("status") or "Market confirmation unavailable"),
        "events": [_event_view(item) for item in reversed(events)],
        "event_limit": event_limit,
        "event_count": len(all_events),
        "threshold_bps": (report.get("method") or {}).get("large_move_threshold_bps"),
        "provenance": _provenance(report.get("source_provenance", []),
                                  report.get("data_freshness", {}) or {},
                                  report.get("generated_at")),
        "interpretation_limit": report.get("interpretation_limit"),
    }


def _latest(items: Any) -> Mapping[str, Any] | None:
    items = list(items or [])
    return items[-1] if items else None


def _ago(rows: list[Mapping[str, Any]], sessions: int) -> Mapping[str, Any] | None:
    return rows[-1 - sessions] if len(rows) > sessions else None


def _curve(yields: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    """Latest level and 1/5/20-session changes per maturity, plus three curve snapshots."""
    rows = []
    snapshots: dict[str, list[dict[str, Any]]] = {"latest": [], "5": [], "20": []}
    snapshot_dates: dict[str, set[str]] = {"latest": set(), "5": set(), "20": set()}
    for series, label, years in MATURITIES:
        series_rows = [r for r in yields.get(series, []) if r.get("yield_percent") is not None]
        latest = _latest(series_rows)
        rows.append({
            "series": series, "label": label, "years": years,
            "yield": None if latest is None else latest.get("yield_percent"),
            "date": None if latest is None else latest.get("observation_date"),
            "d1": None if latest is None else latest.get("daily_change_bps"),
            "d5": None if latest is None else latest.get("change_5_observations_bps"),
            "d20": None if latest is None else latest.get("change_20_observations_bps"),
        })
        for key, item in (("latest", latest), ("5", _ago(series_rows, 5)), ("20", _ago(series_rows, 20))):
            if item is not None:
                snapshots[key].append({"label": label, "years": years, "yield": item["yield_percent"]})
                snapshot_dates[key].add(item["observation_date"])
    return {
        "rows": rows,
        "snapshots": [
            {"key": key, "name": name, "points": snapshots[key],
             # A snapshot date is shown only when every maturity shares it.
             "date": next(iter(snapshot_dates[key])) if len(snapshot_dates[key]) == 1 else None}
            for key, name in (("latest", "Latest"), ("5", "5 sessions earlier"),
                              ("20", "20 sessions earlier"))
            if snapshots[key]
        ],
    }


def _curve_read(rows: list[dict[str, Any]]) -> str | None:
    """Deterministic one-sentence description of the 5-session change (a CALCULATION)."""
    moves = [(r["label"], r["d5"]) for r in rows if r["d5"] is not None]
    if len(moves) < 2:
        return None
    rose = [label for label, change in moves if change > 0.5]
    fell = [label for label, change in moves if change < -0.5]
    if len(rose) == len(moves):
        lead = "Over the last 5 sessions, yields rose at every maturity shown"
    elif len(fell) == len(moves):
        lead = "Over the last 5 sessions, yields fell at every maturity shown"
    elif not rose and not fell:
        lead = "Over the last 5 sessions, yields were little changed (within ±0.5 bp)"
    else:
        flat = [label for label, change in moves if label not in rose and label not in fell]
        parts = [f"{_join(rose)} rose" if rose else "", f"{_join(fell)} fell" if fell else "",
                 f"{_join(flat)} {'was' if len(flat) == 1 else 'were'} unchanged" if flat else ""]
        lead = "Over the last 5 sessions, yields were mixed: " + "; ".join(p for p in parts if p)
    label, change = max(moves, key=lambda item: abs(item[1]))
    text = f"{lead}. The largest move was the {label} ({_bp(change)})."
    short = next((r["d5"] for r in rows if r["series"] == "DGS2"), None)
    long_ = next((r["d5"] for r in rows if r["series"] == "DGS30"), None)
    if short is not None and long_ is not None:
        gap = long_ - short
        if abs(gap) >= 0.5:
            shape = "steepened" if gap > 0 else "flattened"
            text += f" The 2-year to 30-year spread {shape} by {abs(gap):.0f} bp."
    return text


def _channel(decomposition: Mapping[str, Any]) -> dict[str, Any]:
    """Latest 1- and 5-session component changes with a deterministic one-sentence read."""
    latest = decomposition.get("latest") or {}
    windows = latest.get("windows") or {}
    one, five = windows.get("1"), windows.get("5")
    basis = five or one
    return {"date": latest.get("date"), "one": one, "five": five,
            "term_premium": decomposition.get("term_premium"),
            "read": _channel_read(basis, 5 if basis is five and five else 1) if basis else None}


def _channel_read(window: Mapping[str, Any], sessions: int) -> str | None:
    """CALCULATION wording from signs and shares only; no cause is named."""
    nominal, real, breakeven = window.get("nominal_bps"), window.get("real_bps"), window.get("breakeven_bps")
    if nominal is None:
        return None
    span = "Over the last 5 sessions" if sessions == 5 else "On the latest session"
    verb = "rose" if nominal > 0.5 else "fell" if nominal < -0.5 else "was little changed"
    text = f"{span} the 10-year {verb}" + ("" if verb == "was little changed" else f" {abs(round(nominal))} bp")
    if real is None or breakeven is None:
        text += ". Real-yield and breakeven data for those dates are not stored yet, so the split is unknown."
    else:
        text += f": real yield {_bp(real)}, inflation breakeven {_bp(breakeven)}."
        if abs(nominal) > 0.5:
            same_real, same_be = real * nominal > 0, breakeven * nominal > 0
            if same_real and abs(real) >= abs(nominal) * 2 / 3:
                text += " Most of the move came through the real yield."
            elif same_be and abs(breakeven) >= abs(nominal) * 2 / 3:
                text += " Most of the move came through inflation breakevens."
            elif same_real and same_be:
                text += " The move was split between the real yield and breakevens."
            else:
                text += " The real yield and breakevens moved in opposite directions."
    curve = window.get("curve")
    if curve and curve["name"] != "little changed":
        led = "" if curve["led_by"] == "evenly" else f", led by the {curve['led_by']}"
        text += f" The curve moved as a {curve['name']}{led} (2-year {_bp(curve['short_change_bps'])}, 30-year {_bp(curve['long_change_bps'])})."
    return text


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _bp(value: float) -> str:
    rounded = round(value)
    sign = "+" if rounded > 0 else "−" if rounded < 0 else "±"
    return f"{sign}{abs(rounded)} bp"


def _ten_year_history(rows: list[Mapping[str, Any]], events: list[Mapping[str, Any]]) -> dict[str, Any]:
    valued = [r for r in rows if r.get("yield_percent") is not None][-HISTORY_SESSIONS:]
    points = [{"date": r["observation_date"], "yield": r["yield_percent"]} for r in valued]
    start = points[0]["date"] if points else None
    marks = []
    for item in events:
        event = item.get("event", {})
        day = event.get("event_date")
        if start and day and day >= start:
            marks.append({"date": day, "change_bps": event.get("change_bps"),
                          "yield": event.get("current_yield_percent")})
    return {"points": points, "events": marks}


def _dealer(items: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    item = _latest(items)
    if item is None:
        return None
    unit = item.get("unit")
    scale = 1e-3 if unit == "million_us_dollars" else None  # millions -> billions
    def bn(value: Any) -> float | None:
        return None if value is None or scale is None else float(value) * scale
    provenance = item.get("provenance") or {}
    history = list(item.get("history") or [])[-DEALER_WEEKS:]
    return {
        "name": item.get("series_name"), "unit": unit,
        "position_bn": bn(item.get("position")), "previous_bn": bn(item.get("previous_position")),
        "change_bn": bn(item.get("change")), "position_raw": item.get("position"),
        "date": item.get("observation_date"), "frequency": item.get("reporting_frequency"),
        "retrieval_time": provenance.get("retrieval_time"),
        "history": [{"date": h["observation_date"], "value": bn(h.get("position"))} for h in history],
    }


def _cftc(rows: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    by_key = {(r.get("series_identifier"), r.get("participant_category")): r for r in rows}
    known = {series for series, _ in CFTC_CONTRACTS}
    contracts = list(CFTC_CONTRACTS) + sorted(
        {(r.get("series_identifier"), r.get("contract") or r.get("series_identifier"))
         for r in rows if r.get("series_identifier") not in known})
    table = []
    dates = set()
    for series, label in contracts:
        groups = {}
        for key, _ in CFTC_GROUPS:
            row = by_key.get((series, key))
            groups[key] = None if row is None else {
                "net": row.get("net"), "change": row.get("net_change"),
                "long": row.get("long"), "short": row.get("short"), "spreading": row.get("spreading")}
            if row is not None:
                dates.add(row.get("positioning_date"))
        if any(groups.values()):
            table.append({"contract": label, "groups": groups})
    dates.update(r.get("positioning_date") for r in rows if r.get("positioning_date"))
    labels = dict(CFTC_CONTRACTS)
    group_names = {**dict(CFTC_GROUPS), "other_reportable": "Other reportables",
                   "nonreportable": "Nonreportable"}
    details = [{
        "contract": labels.get(r.get("series_identifier"), r.get("contract") or r.get("series_identifier")),
        "group": group_names.get(r.get("participant_category"), r.get("participant_category")),
        "long": r.get("long"), "short": r.get("short"), "spreading": r.get("spreading"),
        "net": r.get("net"), "change": r.get("net_change"), "date": r.get("positioning_date"),
    } for r in sorted(rows, key=lambda r: (
        [s for s, _ in contracts].index(r.get("series_identifier")) if r.get("series_identifier") in
        [s for s, _ in contracts] else 99, str(r.get("participant_category"))))]
    return {"date": max(dates) if dates else None, "rows": table, "details": details,
            "groups": [{"key": key, "label": label} for key, label in CFTC_GROUPS]}


def _auction(item: Mapping[str, Any]) -> dict[str, Any]:
    def dollars(value: Any, unit: Any) -> float | None:
        if value is None:
            return None
        return float(value) if unit in (None, "us_dollars") else None
    return {
        "date": item.get("auction_date"),
        "security": " ".join(x for x in (item.get("security_term"), item.get("security_type")) if x),
        "offering_usd": dollars(item.get("offering_amount"), item.get("offering_amount_unit")),
        "bid_to_cover": item.get("bid_to_cover"),
        "yield": item.get("yield_or_rate"),
        "cusip": item.get("cusip"),
    }


def _evidence_status(dgs10: Mapping[str, Any] | None, spread: Mapping[str, Any] | None,
                     dealer: Mapping[str, Any] | None, cftc: Mapping[str, Any] | None,
                     auctions: list[Any], corporate: Mapping[str, Any],
                     market: Mapping[str, Any]) -> dict[str, Any]:
    """List evidence as available only when the stored report actually contains it.

    Treasury-side items are missing because no observations were stored; corporate and
    market-confirmation items are missing because no production feed exists. The two
    reasons are kept separate so the page never implies more than the database holds.
    """
    treasury_side = [
        ("10Y yield movement", dgs10 is not None and dgs10.get("yield_percent") is not None),
        ("10Y–2Y curve movement", spread is not None and spread.get("spread_bps") is not None),
        ("Primary dealer positioning", dealer is not None),
        ("CFTC positioning", bool(cftc and cftc.get("rows"))),
        ("Treasury auction data", bool(auctions)),
    ]
    available = [label for label, present in treasury_side if present]
    missing = [{"item": label, "reason": NO_OBSERVATIONS} for label, present in treasury_side if not present]
    if corporate.get("status") != "AVAILABLE":
        missing.extend({"item": label, "reason": NO_FEED} for label in (
            "Corporate issuance event feed",
            "Corporate deal size, maturity/duration, pricing date, and settlement date"))
    if market.get("status") != "AVAILABLE":
        missing.extend({"item": label, "reason": NO_FEED} for label in (
            "Treasury futures price confirmation", "HYG", "IWM", "DXY"))
    return {"available": available, "not_available": missing,
            "treasury_side_complete": all(present for _, present in treasury_side),
            "causality_limitation": ["Co-movement, positioning, and timing do not establish causality.",
                                      "Observation-date alignment does not establish publication-time availability or causality."]}


def _rate_lock_status(availability: Mapping[str, Any], corporate: Mapping[str, Any],
                      market: Mapping[str, Any]) -> list[dict[str, str]]:
    issuance = "AVAILABLE" if corporate.get("status") == "AVAILABLE" else "UNAVAILABLE"
    return [
        {"item": "Corporate issuance event feed", "status": issuance},
        {"item": "Deal size", "status": issuance},
        {"item": "Maturity/duration", "status": issuance},
        {"item": "Pricing date", "status": issuance},
        {"item": "Settlement date", "status": issuance},
        {"item": "Treasury yield event data", "status": "AVAILABLE" if availability.get("FRED yields") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Dealer positioning", "status": "AVAILABLE" if availability.get("NYFED") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "CFTC positioning", "status": "AVAILABLE" if availability.get("CFTC") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Post-settlement Treasury response", "status": "PARTIALLY AVAILABLE" if availability.get("FRED yields") == "AVAILABLE" else "UNAVAILABLE"},
        {"item": "Cross-market confirmation", "status": "AVAILABLE" if market.get("status") == "AVAILABLE" else "UNAVAILABLE"},
    ]


def _event_view(item: Mapping[str, Any]) -> dict[str, Any]:
    event = item.get("event", {})
    study = item.get("event_study", {})
    window = (study.get("windows") or {}).get("T-5_T+5", [])
    dealer = next((value for value in (item.get("dealer_context") or {}).values()
                   if value and value.get("position") is not None), None)
    cftc = (item.get("cftc_context") or {}).get("contracts", [])
    auctions = (item.get("auction_context") or {}).get("auctions", [])
    return {
        "date": event.get("event_date"),
        "prior_date": event.get("prior_observation_date"),
        "skipped": list(event.get("skipped_no_value_dates") or []),
        "d10": event.get("change_bps"),
        "d2": event.get("dgs2_change_bps"),
        "curve": event.get("spread_change_bps"),
        "window": [{
            "offset": row.get("offset"), "date": row.get("source_observation_date"),
            "changes": {series: row.get(f"{series.lower()}_change_bps") for series, _, _ in MATURITIES},
        } for row in window],
        "summary_10y": (study.get("summary") or {}).get("DGS10"),
        "channel": ((item.get("decomposition") or {}).get("windows") or {}).get("1"),
        "dealer_date": None if dealer is None else dealer.get("observation_date"),
        "cftc_date": next((c.get("positioning_date") for c in cftc if c.get("positioning_date")), None),
        "auctions": [{"date": a.get("auction_date"),
                      "security": " ".join(x for x in (a.get("security_term"), a.get("security_type")) if x)}
                     for a in auctions],
        "corporate_status": (item.get("corporate_issuance_context") or {}).get("status"),
        "market_status": (item.get("market_confirmation") or {}).get("status"),
    }


SOURCE_NAMES = {
    "FRED": "Treasury yields (FRED)",
    "NYFED": "Primary dealer positions (NY Fed)",
    "CFTC": "Futures positioning (CFTC)",
    "U.S. Treasury Fiscal Data": "Treasury auctions (Fiscal Data)",
}


def _provenance(rows: list[Mapping[str, Any]], freshness: Mapping[str, Any],
                generated_at: Any) -> list[dict[str, Any]]:
    urls: dict[str, str | None] = {}
    for row in rows:
        source = row.get("source_identifier")
        if source and source not in urls:
            urls[source] = row.get("source_url")
    built = _parse_instant(generated_at)
    result = []
    for source in sorted(set(urls) | set(freshness), key=lambda s: list(SOURCE_NAMES).index(s)
                         if s in SOURCE_NAMES else 99):
        timing = freshness.get(source, {}) or {}
        observed = timing.get("observation_date")
        age = None
        if observed and built:
            age = (built.date() - date.fromisoformat(observed)).days
        result.append({"source": source, "name": SOURCE_NAMES.get(source, source),
                       "reference": urls.get(source), "observation_date": observed,
                       "observation_age_days": age, "retrieved": timing.get("retrieval_time"),
                       "published": timing.get("publication_time")})
    return result


def _parse_instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None
