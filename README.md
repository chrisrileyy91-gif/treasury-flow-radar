# Treasury Flow Radar

Treasury Flow Radar is a research and measurement system for studying Treasury market plumbing, positioning, dealer activity, corporate issuance pressure, rate-lock hedging conditions, and subsequent hedge-unwind behavior.

The central research question is:

> Is there evidence that a Treasury market move is being mechanically amplified by corporate issuance, dealer hedging, positioning, or related Treasury-market plumbing?

The system is intended to distinguish observation from interpretation. It is a research and measurement system, not a trading signal generator. It must never claim that a dealer, corporation, or institution intentionally manipulated Treasury prices without direct evidence. Corporate issuance and rate-lock activity are hypotheses to be tested, not assumed causal explanations.

## Evidence taxonomy

Dashboard claims should identify the category they belong to:

- **FACT** — a directly reported value or statement from an identified source.
- **CALCULATION** — a reproducible result derived from stated inputs and a documented method.
- **OBSERVATION** — a description of a measured pattern, without asserting its cause.
- **MECHANISM** — a documented explanation of how a market process could operate.
- **INFERENCE** — an interpretation supported by evidence but not directly established by it.
- **HYPOTHESIS** — a proposed explanation or relationship that remains to be tested.

A category label does not replace source attribution, uncertainty, or method details.

## Expected initial data sources

These are expected authoritative sources, and implementation status varies:

- Federal Reserve Bank of New York Primary Dealer Statistics
- CFTC Commitments of Traders
- CFTC Bank Participation Report
- U.S. Treasury
- Federal Reserve / FRED — initial DGS10 and DGS2 ingestion is implemented
- Corporate bond issuance data

## FRED Treasury yields

The FRED adapter currently supports only DGS10 (10-Year Treasury Constant Maturity Rate) and DGS2 (2-Year Treasury Constant Maturity Rate). It requests the official FRED observations API, preserves the complete JSON response in `raw_records`, and links each normalized row to the response that produced it. Values are stored as numeric rates in percent. FRED's `.` missing marker is retained as the raw value and stored with a NULL numeric value, never as zero.

The FRED Web Services API requires a registered API key. Set it in the process environment; the project does not load `.env` files automatically. In PowerShell:

```powershell
$env:FRED_API_KEY = "<your FRED API key>"
$env:TREASURY_FLOW_RADAR_DB = "data/treasury_flow_radar.sqlite3"  # optional
```

Install the package into the active Python environment, then run either one series or both:

```powershell
python -m pip install -e .
python -m treasury_flow_radar.sources.fred DGS10
python -m treasury_flow_radar.sources.fred DGS2
python -m treasury_flow_radar.sources.fred DGS10 DGS2
# With no series argument, both supported series are ingested.
python -m treasury_flow_radar.sources.fred
```

The database path can also be supplied with `--database PATH`. `FRED_API_KEY` is required; `TFR_USER_AGENT` optionally overrides the request user agent. The checked-in `.env.example` contains variable names only and is not automatically read by the CLI. The official FRED Web Services API requires a key; this adapter does not use an alternate public CSV endpoint.

FRED observation dates are calendar dates, not release timestamps. The adapter stores each date using a midnight UTC anchor to fit the database timestamp field and labels its precision as `calendar_date`; this anchor does not mean the yield was published at midnight. The API's returned real-time date fields are retained as metadata, but `publication_time` remains NULL because the endpoint does not provide an exact historical publication timestamp. `retrieval_time` is captured when the response is received.

Repeated ingestion of unchanged observations is idempotent. If FRED changes a value for an observation date, the adapter records a new immutable database revision linked to its predecessor. No FRED API key or other credential belongs in the repository.

## Project status

FRED DGS10/DGS2 ingestion is the first implemented source path. The other listed providers, further series, calculations, dashboard behavior, and trading signals are not implemented. See [ARCHITECTURE.md](ARCHITECTURE.md) for the schema, provenance flow, and timestamp limitations.
