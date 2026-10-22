"""Export a read-only research snapshot as self-contained HTML."""
from __future__ import annotations

import argparse
from pathlib import Path

from dashboard.renderer import render_report
from dashboard.reporting import load_dashboard_report
from treasury_flow_radar.run_health import fred_needs_inspection, load_ingest_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a self-contained Treasury Flow Radar HTML snapshot.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ingest-report", type=Path, default=None,
                        help="This run's ingest report; when its FRED pull failed the page says 'Inspect FRED.'")
    args = parser.parse_args(argv)
    report = load_dashboard_report(args.database)
    if args.ingest_report is not None:
        report["run_health"] = {"fred_failed": fred_needs_inspection(load_ingest_report(args.ingest_report))}
    html = render_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8", newline="")
    print(f"Wrote offline snapshot: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
