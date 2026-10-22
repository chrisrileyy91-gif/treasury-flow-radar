"""Post a Pheme line to Discord when a publish run failed to pull FRED.

    python -m treasury_flow_radar.alert ingest-report.json

Silent when every FRED source succeeded. Posts when any did not (the page then shows
older yields and says "Inspect FRED."), or when the run left no readable ingest report.
Same conventions as meridian-clock's pheme.py and regime-engine's alert: discord.com only,
a Mozilla User-Agent (urllib's default gets HTTP 403), never log the webhook URL.
Unset DISCORD_PHEME_WEBHOOK: prints "Pheme silent: no webhook" and exits 0.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from treasury_flow_radar.run_health import fred_failures, load_ingest_report

PAGE_URL = "https://chrisrileyy91-gif.github.io/treasury-flow-radar/"
MAX_REASONS = 3


def build_message(report: Mapping[str, Any] | None) -> str | None:
    """The Pheme text for this run, or None when FRED came through."""
    if report is None:
        return (f"Treasury Flow Radar: the run left no readable ingest report, so FRED can't be checked. "
                f"Inspect FRED.\n{PAGE_URL}")
    failed = fred_failures(report)
    if not failed:
        return None
    total = sum(1 for s in report.get("sources") or [] if str(s.get("source", "")).startswith("fred"))
    run_day = report.get("end_date") or "this run"
    lines = [(f"Treasury Flow Radar ({run_day}): FRED failed for {len(failed)} of {total} FRED sources. "
              f"Yields on the page are stale until the next good run. Inspect FRED.")]
    reasons = Counter(str(s.get("message") or "no message") for s in failed)
    for reason, count in reasons.most_common(MAX_REASONS):
        lines.append(f"- {reason} (x{count})" if count > 1 else f"- {reason}")
    if len(reasons) > MAX_REASONS:
        lines.append(f"- and {len(reasons) - MAX_REASONS} other errors")
    lines.append(PAGE_URL)
    return "\n".join(lines)


def post(webhook: str, content: str) -> None:
    if not webhook.startswith("https://discord.com/"):
        raise ValueError("webhook must be a https://discord.com/ URL")
    body = json.dumps({"username": "Pheme", "content": content[:1900]}).encode()
    request = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (treasury-flow-radar)"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # https://discord.com/ only, checked above
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else Path("ingest-report.json")
    message = build_message(load_ingest_report(path))
    if message is None:
        print("Pheme: FRED came through, nothing to post")
        return 0
    print(message)
    webhook = os.environ.get("DISCORD_PHEME_WEBHOOK", "")
    if not webhook:
        print("Pheme silent: no webhook")
        return 0
    try:
        post(webhook, message)
    except Exception as exc:  # noqa: BLE001  # a failed alert must not fail the publish
        print(f"Pheme post failed: {type(exc).__name__}", file=sys.stderr)  # never echo the URL
        return 1
    print("Pheme: posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
