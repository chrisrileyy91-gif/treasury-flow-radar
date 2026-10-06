# Treasury Flow Radar

Treasury Flow Radar is a research and measurement system for Treasury yields, dealer positioning, CFTC futures positioning, auction supply, and cross-market context. It distinguishes reported facts, calculations, observations, mechanisms, inferences, and hypotheses. It is not a trading system: it never generates buy/sell recommendations, entries, exits, position sizing, or forecasts. Co-movement and timing do not establish causality or manipulation.

## What is available

| Area | Status |
| --- | --- |
| FRED DGS2/DGS10 yields | Live ingestion; an API key is required |
| NY Fed primary dealer Treasury positioning | Live public-source ingestion; weekly aggregate series |
| CFTC TFF Treasury futures positioning | Live public-source ingestion for 2Y, 5Y, 10Y, Ultra 10Y, and 30Y; weekly participant categories |
| U.S. Treasury auctions | Live public-source ingestion for 2Y, 5Y, 7Y, 10Y, 20Y, and 30Y Notes/Bonds |
| Research analytics and event studies | Implemented over observations present in SQLite |
| Treasury futures prices, HYG, IWM, DXY | Normalized series support; no production provider is configured |
| Corporate new-issue events | Provider-neutral model and analytics; no production event-level feed is configured |
| Live dashboard and offline HTML snapshot | Implemented and use the same analytics report layer |

Production observations are never fabricated. Missing feeds appear as unavailable. Corporate issuance is a hypothesis input and no causal claim is made. SEC aggregate issuance is not substituted for deal events, and TRACE secondary trades are not treated as primary issuance.

## Install and ingest (Windows PowerShell)

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

FRED requires a user-supplied API key in the current PowerShell process. The project does not load `.env` automatically and does not print or store the key:

```powershell
$env:FRED_API_KEY = "<your FRED API key>"
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3
```

The default ingestion window is the latest 730 calendar days. To select a range and sources:

```powershell
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3 --start-date 2024-10-05 --end-date 2026-10-05
python -m treasury_flow_radar.ingest --source nyfed --source cftc --source treasury-auctions
python -m treasury_flow_radar.ingest --source fred-dgs2 --source fred-dgs10
```

Successful re-ingestion is idempotent; changed source facts create immutable revisions. Each adapter retains source payload provenance. A missing FRED key is reported as SKIPPED; network, HTTP, parsing, and persistence failures are reported as FAILED.

## Research report and dashboard

Create a read-only JSON research report and start the live Python/SQLite dashboard:

```powershell
python -m treasury_flow_radar.analytics.report --database data/treasury_flow_radar.sqlite3 --start-date 2025-01-01 --end-date 2025-12-31
python -m dashboard.app --database data/treasury_flow_radar.sqlite3
```

Open `http://127.0.0.1:8765`. The dashboard reads SQLite in read-only mode. Research events are large observed DGS10 changes; their details include available yield, dealer, CFTC, auction, corporate issuance, market confirmation, and evidence context. NY Fed and CFTC weekly observations are aligned to the prior report date with explicit lag and are never forward-filled. Exact publication time remains unknown when the source does not provide it.

## Offline HTML snapshot

Export the same report view as a standalone HTML file:

```powershell
python -m dashboard.export --database data/treasury_flow_radar.sqlite3 --output data/treasury_flow_radar_report.html
```

The file contains its CSS, JavaScript, and displayed data; it needs no network, Python, SQLite, or API key when opened in Chrome or Edge. It is a snapshot and displays its generation timestamp and unavailable-data warnings.

## Corporate issuance limits

The adapter accepts provider-mapped event/tranche records and stores issuer/deal identity, announcement/pricing/settlement/maturity dates, principal and currency, coupon/yield/spread, benchmark terms, credit classification, duration estimate, raw payload, source URL, publication time, and retrieval time when supplied. No production provider is enabled. The available duration estimate is remaining maturity in years (`ACT/365.25`), not cash-flow or modified duration and not DV01. Treasury-equivalent duration pressure is displayed in currency-years and is not a causal market-pressure measure. The analytics report corporate issuance as unavailable until a legitimate production event feed is configured.

## Research interpretation

Evidence records carry a taxonomy, statement, supporting observations, source, dates, and retrieval metadata. Confidence is omitted unless a statistical basis is defined. Event-study T offsets use actual observed DGS10 dates, and missing values stay missing. The analytics describe measurements and co-movement; they do not prove a mechanism, establish causality, or produce trading signals. See [ARCHITECTURE.md](ARCHITECTURE.md) for data flow and methods.

