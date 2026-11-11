"""Load the curated corporate deal ledger (events/corporate_deals.json).

The ledger is hand-maintained and is not a production issuance feed. Every factual
field a deal carries must be named in the ``supports`` list of at least one source with
a URL; a deal that fails this check is rejected rather than shown.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

EVENTS_DIR = Path(__file__).resolve().parents[2] / "events"
DEFAULT_LEDGER = EVENTS_DIR / "corporate_deals.json"
DEFAULT_FOMC = EVENTS_DIR / "fomc_meetings.json"
DEFAULT_CASH_GUIDANCE = EVENTS_DIR / "treasury_cash_guidance.json"
FACT_FIELDS = ("launch_date", "pricing_date", "settlement_date_expected", "transaction_close_date",
               "size_usd", "tranches", "loans", "credit_note", "market")
DATE_FIELDS = ("launch_date", "pricing_date", "settlement_date_expected", "transaction_close_date")


class LedgerError(ValueError):
    """The ledger is malformed or a fact lacks a supporting source."""


def validate_deal(deal: dict[str, Any]) -> dict[str, Any]:
    for field in ("id", "name", "pricing_date", "sources"):
        if not deal.get(field):
            raise LedgerError(f"deal {deal.get('id', '?')!r} is missing {field}")
    for field in DATE_FIELDS:
        if deal.get(field):
            date.fromisoformat(str(deal[field]))
    sources = deal["sources"]
    if not all(isinstance(s, dict) and str(s.get("url", "")).startswith("https://") for s in sources):
        raise LedgerError(f"deal {deal['id']!r}: every source needs an https URL")
    supported = {field for s in sources for field in s.get("supports", [])}
    unsupported = [f for f in FACT_FIELDS if deal.get(f) not in (None, "", []) and f not in supported]
    if unsupported:
        raise LedgerError(f"deal {deal['id']!r}: no source supports {', '.join(unsupported)}")
    return deal


def load_deals(path: str | Path = DEFAULT_LEDGER) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    deals = document.get("deals", []) if isinstance(document, dict) else []
    ids = [d.get("id") for d in deals]
    if len(ids) != len(set(ids)):
        raise LedgerError("deal ids must be unique")
    return [validate_deal(dict(d)) for d in deals]


CASH_TARGET_FIELDS = ("quarter_end", "cash_balance_usd_bn")
CASH_PEAK_FIELDS = ("label", "level_usd_bn", "band_usd_bn")


def _check_treasury_sourced(entry: dict[str, Any], facts: tuple[str, ...]) -> None:
    sources = entry.get("sources") or []
    if not sources or not all(isinstance(s, dict) and str(s.get("url", "")).startswith("https://home.treasury.gov/")
                              for s in sources):
        raise LedgerError(f"cash guidance {entry.get('id', '?')!r}: every source must be a home.treasury.gov URL")
    supported = {field for s in sources for field in s.get("supports", [])}
    missing = [f for f in facts if entry.get(f) is None]
    if missing:
        raise LedgerError(f"cash guidance {entry.get('id', '?')!r} is missing {', '.join(missing)}")
    unsupported = [f for f in facts if f not in supported]
    if unsupported:
        raise LedgerError(f"cash guidance {entry.get('id', '?')!r}: no source supports {', '.join(unsupported)}")


def load_cash_guidance(path: str | Path = DEFAULT_CASH_GUIDANCE) -> dict[str, Any] | None:
    """Treasury's published cash-balance plan (quarter-end assumptions and projected peaks).

    Returns None when the ledger file is absent. Every number must be supported by a
    home.treasury.gov source; a malformed or unsupported entry is rejected, not shown."""
    path = Path(path)
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    targets = [dict(t) for t in document.get("targets", [])]
    peaks = [dict(p) for p in document.get("peaks", [])]
    for target in targets:
        _check_treasury_sourced(target, CASH_TARGET_FIELDS)
        date.fromisoformat(str(target["quarter_end"]))
    for peak in peaks:
        _check_treasury_sourced(peak, CASH_PEAK_FIELDS)
        start, end = date.fromisoformat(str(peak["window_start"])), date.fromisoformat(str(peak["window_end"]))
        if end < start:
            raise LedgerError(f"cash guidance {peak.get('id', '?')!r}: window ends before it starts")
    ends = [t["quarter_end"] for t in targets]
    if len(ends) != len(set(ends)):
        raise LedgerError("cash guidance: one target per quarter end")
    return {"targets": sorted(targets, key=lambda t: t["quarter_end"]),
            "peaks": sorted(peaks, key=lambda p: p["window_start"]),
            "window_rule": document.get("window_rule"),
            "next_release": document.get("next_release"),
            "next_release_source": document.get("next_release_source")}


def load_fomc_decisions(path: str | Path = DEFAULT_FOMC) -> list[dict[str, Any]]:
    """FOMC decision dates from the Federal Reserve calendar file, as release-like events."""
    path = Path(path)
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    source = document.get("source") or {}
    if not str(source.get("url", "")).startswith("https://www.federalreserve.gov/"):
        raise LedgerError("FOMC calendar must cite the Federal Reserve calendar page")
    events = []
    for meeting in document.get("meetings", []):
        day = date.fromisoformat(meeting["decision_date"])
        events.append({"date": day.isoformat(), "kind": "policy",
                       "short": "FOMC decision" + (" (with projections)" if meeting.get("sep") else ""),
                       "name": "FOMC statement", "source_url": source["url"], "weight": 1.0})
    return events
