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


## NY Fed Primary Dealer Statistics

The second source adapter ingests one aggregate weekly positioning measure from the official [NY Fed Primary Dealer Statistics](https://www.newyorkfed.org/markets/counterparties/primary-dealers-statistics) API. The selected provider field is `PDPOSGST-TOT`: total primary dealer long positions minus short positions in U.S. Treasury securities excluding TIPS. It is stored as `dealer_net_position_nominal_treasury_ex_tips_sbn2024_pdposgst_tot` for the current structural window. This is an aggregate reported position, not a measure of intent or a trading signal. Transactions and repo series are not ingested in this initial scope.

The adapter requests the official Markets Data API historical time-series JSON for one structural series break and key ID, preserving the complete response in `raw_records`. It parses weekly as-of dates and stores numeric values in source-native millions of U.S. dollars (`million_us_dollars`); it does not multiply them into dollars. NY Fed suppression markers such as `*` remain in `raw_value` with a NULL numeric value. The selected series break is included in the internal series identity because NY Fed notes that data structures change across historical windows.

Install the package, then ingest the current structural window:

```powershell
python -m pip install -e .
python -m treasury_flow_radar.sources.nyfed
python -m treasury_flow_radar.sources.nyfed --database data/treasury_flow_radar.sqlite3
```

Use `--series-break` to retrieve a different NY Fed structural window and `--key-id` to select its corresponding field. Older windows may use different provider key IDs and definitions; confirm their mapping against that window's official data definitions before ingesting. Repeated unchanged rows are idempotent; changed values become immutable revisions linked to their predecessor.

NY Fed describes the series date as an as-of/reporting date. The site says releases are updated Thursdays at approximately 4:15 p.m. with the previous week's statistics, but the API does not provide exact historical publication instants. Therefore `publication_time` is NULL; the weekly schedule is not substituted for a publication timestamp. `observation_time` is stored as a midnight UTC date anchor with `observation_precision=calendar_date`, and `retrieval_time` records when this application fetched the response.

NY Fed says reports are dealer-submitted and not audited by the Bank, values may be revised, data are suppressed when fewer than three dealers report for a category, and totals may not add due to rounding. Consult the source definitions and their series-break windows when comparing history. The adapter reports measurements only and does not infer manipulation, causation, or directional forecasts.


## CFTC Treasury futures positioning

The third source adapter ingests the official CFTC [Traders in Financial Futures (TFF), Futures Only](https://publicreporting.cftc.gov/d/gpe5-46if) dataset through its public [Socrata JSON API](https://publicreporting.cftc.gov/resource/gpe5-46if.json). The dataset is public and does not require an API key. It includes historical weekly futures-only reports; the adapter filters to the five CFTC contract market codes below and requests source-native counts.

| Treasury futures contract | CFTC contract market code | Internal series |
| --- | --- | --- |
| 2-Year U.S. Treasury Note | `042601` | `tff_futures_only_ust_2_year_note_042601` |
| 5-Year U.S. Treasury Note | `044601` | `tff_futures_only_ust_5_year_note_044601` |
| 10-Year U.S. Treasury Note | `043602` | `tff_futures_only_ust_10_year_note_043602` |
| Ultra 10-Year U.S. Treasury Note | `043607` | `tff_futures_only_ust_ultra_10_year_note_043607` |
| U.S. Treasury Bond (30-Year) | `020601` | `tff_futures_only_ust_30_year_bond_020601` |

The contract market code is the stable provider key; CFTC's market-and-exchange description and separate `contract_market_name` are retained in observation metadata when supplied. Matching uses the exact CFTC code, never a display-name substring. The series model creates one provider-scoped series per contract. Within each series, an observation's logical key combines report date, participant category, and metric. This keeps the existing generic schema queryable without making a separate series for every category and field.

For each report, the adapter preserves open interest and long, short, and (where published) spreading positions for Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables, and Nonreportable. The CFTC TFF dataset does not publish nonreportable spreading positions; those observations are absent, not zero. The source facts remain separate: the adapter does not calculate net positions.

Install the package and run:

```powershell
python -m pip install -e .
python -m treasury_flow_radar.sources.cftc
python -m treasury_flow_radar.sources.cftc --database data/treasury_flow_radar.sqlite3
```

Values remain in source-native futures contracts (`contracts`). They are not converted to dollars or duration, and a contract count is not equivalent to Treasury notional exposure. CFTC report dates refer to the prior Tuesday's close; reports are usually released Friday at 3:30 p.m. Eastern, with holiday delays. The API records do not provide an exact publication timestamp per row, so `publication_time` stays NULL. The adapter stores the report calendar date separately from actual retrieval time.

The CFTC classifies trader positions into aggregate categories, not named firms or strategies. Stage 5 leaves weekly changes, percent-of-open-interest fields, trader counts, and concentration measures in the preserved raw page payload; it normalizes only open interest and the requested position counts. The dataset does not publish nonreportable spreading. TFF is a futures-only report and excludes options. CFTC reports cover markets meeting its reporting criteria; positions and classifications may be revised. This is positioning data, not a directional trading signal. A long or short category does not, by itself, predict Treasury yields.
