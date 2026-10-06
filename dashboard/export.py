"""Export a read-only research snapshot as self-contained HTML."""
from __future__ import annotations

import argparse
from pathlib import Path

from dashboard.renderer import render_report
from dashboard.reporting import load_dashboard_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a self-contained Treasury Flow Radar HTML snapshot.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = load_dashboard_report(args.database)
    html = render_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8", newline="")
    print(f"Wrote offline snapshot: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

