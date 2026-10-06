"""Dependency-free live dashboard using the shared analytics report layer."""
from __future__ import annotations

import argparse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dashboard.renderer import render_report
from dashboard.reporting import load_dashboard_report
from dashboard.research import Event, build_event_window, load_observations


def render_dashboard(database_path: str | Path,
                     query: dict[str, list[str]] | None = None) -> str:
    """Render live SQLite data through the same report builder as HTML export."""
    report = load_dashboard_report(database_path)
    user_event: dict[str, Any] | None = None
    query = query or {}
    raw_date = (query.get("event_date") or [""])[0]
    if raw_date:
        try:
            day = date.fromisoformat(raw_date)
            event = Event(
                event_id=(query.get("event_id") or ["user-event"])[0],
                event_date=day,
                event_type=(query.get("event_type") or ["Research event"])[0],
                issuer=(query.get("issuer") or [None])[0],
                pricing_date=(date.fromisoformat(query["pricing_date"][0])
                              if query.get("pricing_date", [""])[0] else None),
                settlement_date=(date.fromisoformat(query["settlement_date"][0])
                                 if query.get("settlement_date", [""])[0] else None),
                size=(float(query["size"][0]) if query.get("size", [""])[0] else None),
                size_unit=(query.get("size_unit") or [None])[0],
                notes=(query.get("notes") or [None])[0],
                source="USER-SUPPLIED — UNVERIFIED",
            )
            user_event = {
                "event_id": event.event_id,
                "event_date": event.event_date.isoformat(),
                "event_type": event.event_type,
                "issuer": event.issuer,
                "source": event.source,
                "window": build_event_window(event, load_observations(database_path)),
                "evidence_type": "OBSERVATION",
            }
        except (ValueError, TypeError):
            user_event = None
    return render_report(report, user_event=user_event, allow_event_input=True, event_limit=60)


def make_server(database_path: str | Path, host: str = "127.0.0.1",
                port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            body = render_dashboard(database_path, query).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Treasury Flow Radar dashboard")
    parser.add_argument("--database", default="data/treasury_flow_radar.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.database, args.host, args.port)
    print(f"Treasury Flow Radar: http://{args.host}:{server.server_port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

