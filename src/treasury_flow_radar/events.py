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
                       "name": "FOMC statement", "source_url": source["url"]})
    return events
