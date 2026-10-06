# Architecture

```text
Official provider APIs
        ↓
Source adapters (FRED, NY Fed, CFTC, Treasury; corporate adapter boundary)
        ↓
Raw payloads + source / publication / retrieval provenance
        ↓
Normalized observations and immutable revisions (SQLite)
        ↓
Read-only report loader → shared research analytics
                         ↙                    ↘
               Live dashboard           Static HTML renderer
```

## Source and storage layers

Adapters isolate provider requests, schema quirks, identifiers, and date conventions. Implemented feeds are FRED DGS2/DGS10 (key required), NY Fed aggregate Primary Dealer Treasury positioning, CFTC Traders in Financial Futures (2Y, 5Y, 10Y, Ultra 10Y, 30Y), and Treasury Notes/Bonds auctions (2Y, 5Y, 7Y, 10Y, 20Y, 30Y). Provider payloads are retained in `raw_records`; normalized rows refer to source, series, raw record, and source-specific metadata.

SQLite schema and persistence helpers live in `src/treasury_flow_radar/database/`. Observations preserve `observation_time`, optional `publication_time`, and `retrieval_time` independently. Calendar-only source dates use a midnight UTC storage anchor with precision metadata; that anchor is not a publication-time assertion. Idempotent inputs remain unchanged, while provider corrections create linked immutable revisions. Ingestion writes are handled by source adapters; analytics and reports use SQLite read-only mode.

FRED values are percent rates; 0.01 percentage point equals one basis point. NY Fed dealer positions retain source-native millions of dollars and weekly frequency. CFTC positions are source-native futures contracts, with long, short, spreading, open interest, and report date kept distinct; spreading is not included in outright net (long minus short). Treasury auction offering and accepted amounts retain source-native USD-thousands, ratios, and yield/rate units. Missing source fields remain NULL or absent, never zero-filled.

## Temporal alignment and research analytics

`analytics.temporal.align_observation` selects an actual prior, following, or nearest observation. Each result includes source observation date, event date, signed lag, temporal relation (BEFORE/SAME_DAY/AFTER), source/series identifiers, observation ID, value, unit, and timestamps. Weekly sources are not forward-filled or expanded into daily rows. A date match is not a claim that an observation was publicly available by the event date when historical publication time is unknown.

`analytics.descriptive` supplies yield changes, 10Y–2Y curve spreads, positioning metrics, and returns. Changes are calculated across supplied observations; calendar dates and market sessions are not synthesized. `analytics.research` builds the shared report: observed large DGS10 moves, yield/curve calculations, dealer context, CFTC participant context, Treasury auction context, market confirmation availability, corporate event context, provenance, and evidence records. Auction matching uses a configurable calendar-day window. Context records preserve source and event dates and lag.

`analytics.event_study` uses actual ordered DGS10 observation dates for T−5/T+5, T−3/T+3, and T−1/T+1 windows. It computes DGS2, DGS10, and 10Y–2Y changes, pre/event/post moves, reversal magnitude and percentage when calculable, and exact-date returns for normalized Treasury futures (`ZT`, `ZN`, `UB`, `ZB`), HYG, IWM, and DXY series. Other available market series are not substituted for missing dates. It reports market confirmation unavailable when no production observations support it. These are descriptive calculations, not forecasts or causal estimates.

## Corporate issuance adapter boundary

`sources.corporate_issuance` normalizes provider-identified tranche records: issuer and optional issuer identifier, deal/tranche identifiers, announcement/pricing/settlement/maturity dates, amount, currency, coupon, yield/spread, Treasury benchmark/yield, credit spread/classification, source reference, publication/retrieval time, and raw provider fields. A source must be explicitly selected with a provider-specific identifier and URL before rows can be marked production-configured. No provider is currently wired; fixtures are synthetic test inputs only. SEC aggregate issuance and TRACE secondary transactions are not treated as primary issuance events.

`analytics.issuance` calculates milestone-aligned Treasury yield moves and calendar-day post-settlement horizons using actual observations on or after each target date. If only maturity is known, remaining maturity in years is labeled an estimate (`maturity_years_proxy_act_365_25`). Principal × this estimate is currency-years, not DV01, Treasury notional, or measured dealer hedge activity. Corporate issuance remains unavailable in the production report until a legitimate event-level feed is configured. Any observed timing is described as temporal association; it does not prove rate-lock hedging or causality.

## Evidence model

`analytics.evidence` centralizes FACT, CALCULATION, OBSERVATION, MECHANISM, INFERENCE, and HYPOTHESIS records. A record has a statement, supporting observations, source, observation dates, and retrieval dates. A confidence value requires an explicit statistical basis; no arbitrary confidence score is generated. Mechanisms are not substituted for measured facts. No conclusion alleges manipulation without direct evidence.

## Live dashboard and static export

`dashboard.reporting` calls the read-only SQLite loader and shared `build_research_report`. `dashboard.app` presents the resulting view live and labels manually entered event inputs as unverified. `dashboard.export` uses the same report builder, then `dashboard.renderer` embeds report data, CSS, and JavaScript into one HTML file. Export reads SQLite without modifying it. The snapshot requires no Python, database, API key, or network when viewed; it records “Generated at” and explicit unavailable-source states.

Commands (PowerShell from the repository root):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
$env:FRED_API_KEY = "<your FRED API key>"
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3
python -m dashboard.app --database data/treasury_flow_radar.sqlite3
python -m treasury_flow_radar.analytics.report --database data/treasury_flow_radar.sqlite3 --start-date 2025-01-01 --end-date 2025-12-31
python -m dashboard.export --database data/treasury_flow_radar.sqlite3 --output data/treasury_flow_radar_report.html
```

## Known limits

- No production market-price adapter is configured for Treasury futures, HYG, IWM, or DXY; normalized series support does not imply data availability.
- No production corporate event-level issuance feed is configured. Dashboard/report status is explicitly unavailable.
- Some sources provide observation or publication calendar dates but no exact historical release timestamp; unknown timestamps stay NULL.
- Historical reports describe available stored data and may have gaps, revisions, and varying source coverage.
- Co-movement, timing, duration proxies, and event windows do not establish causal effect, motive, or manipulation. The system is research and measurement software, not a trading signal generator.

