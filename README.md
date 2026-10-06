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
- Federal Reserve / FRED — DGS2, DGS5, DGS7, DGS10, and DGS30 ingestion is implemented
- Corporate bond issuance data

## FRED Treasury yields

The FRED adapter supports the DGS2, DGS5, DGS7, DGS10, and DGS30 Treasury constant maturity rates (2-, 5-, 7-, 10-, and 30-year). It requests the official FRED observations API, preserves the complete JSON response in `raw_records`, and links each normalized row to the response that produced it. Values are stored as numeric rates in percent. FRED's `.` missing marker is retained as the raw value and stored with a NULL numeric value, never as zero.

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

The source adapters and analytics/dashboard stages described below are implemented. FRED DGS2/DGS10 requires a key; NY Fed, CFTC, and Treasury use their existing public endpoints. The research report and dashboard use observations already stored in SQLite.

Corporate issuance retains a provider-neutral event/tranche model, but no production event-level source is configured. Treasury futures market prices, HYG, IWM, and DXY also have normalized-series support but no production feed configured. These remain explicitly unavailable; no fixture data is treated as production. See the current research-system section and [ARCHITECTURE.md](ARCHITECTURE.md) for shared analytics, event-study, dashboard, export, and availability details.

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

The third source adapter ingests the official CFTC [Traders in Financial Futures (TFF), Futures Only](https://publicreportinghub.cftc.gov/d/gpe5-46if) dataset through its public [Socrata JSON API](https://publicreportinghub.cftc.gov/resource/gpe5-46if.json). The dataset is public and does not require an API key. It includes historical weekly futures-only reports; the adapter filters to the five CFTC contract market codes below and requests source-native counts.

| Treasury futures contract | CFTC contract market code | Internal series |
| --- | --- | --- |
| 2-Year U.S. Treasury Note | `042601` | `tff_futures_only_ust_2_year_note_042601` |
| 5-Year U.S. Treasury Note | `044601` | `tff_futures_only_ust_5_year_note_044601` |
| 10-Year U.S. Treasury Note | `043602` | `tff_futures_only_ust_10_year_note_043602` |
| Ultra 10-Year U.S. Treasury Note | `043607` | `tff_futures_only_ust_ultra_10_year_note_043607` |
| U.S. Treasury Bond (30-Year) | `020601` | `tff_futures_only_ust_30_year_bond_020601` |

The contract market code is the stable provider key; CFTC's market-and-exchange description and separate `contract_market_name` are retained in observation metadata when supplied. Matching uses the exact CFTC code, never a display-name substring. The series model creates one provider-scoped series per contract. Within each series, an observation's logical key combines report date, participant category, and metric. This keeps the existing generic schema queryable without making a separate series for every category and field.

The current CFTC Data Hub schema identifies `report_date_as_yyyy_mm_dd` as the report-date field and does not expose `as_of_date_in_form_yy_mm_dd`. The adapter now selects and parses the published report date only; it does not substitute another date field. `report_date` is preserved as the source calendar date and used as the UTC midnight observation anchor, while exact publication time remains unknown and retrieval time is captured separately.

For each report, the adapter preserves open interest and long, short, and (where published) spreading positions for Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables, and Nonreportable. The CFTC TFF dataset does not publish nonreportable spreading positions; those observations are absent, not zero. The source facts remain separate: the adapter does not calculate net positions.

Install the package and run:

```powershell
python -m pip install -e .
python -m treasury_flow_radar.sources.cftc
python -m treasury_flow_radar.sources.cftc --database data/treasury_flow_radar.sqlite3
```

Values remain in source-native futures contracts (`contracts`). They are not converted to dollars or duration, and a contract count is not equivalent to Treasury notional exposure. CFTC report dates refer to the prior Tuesday's close; reports are usually released Friday at 3:30 p.m. Eastern, with holiday delays. The API records do not provide an exact publication timestamp per row, so `publication_time` stays NULL. The adapter stores the report calendar date separately from actual retrieval time.

The CFTC classifies trader positions into aggregate categories, not named firms or strategies. The selected raw page payload also retains the current-schema fields `change_in_open_interest_all`, `change_in_dealer_long_all`, `change_in_dealer_short_all`, and `change_in_dealer_spread_all`; these source-reported weekly changes are distinct from changes calculated across successive observations. Percent-of-open-interest fields, trader counts, and concentration measures are outside the selected request and normalized scope. The adapter normalizes open interest and the requested position counts. The dataset does not publish nonreportable spreading. TFF is a futures-only report and excludes options. CFTC reports cover markets meeting its reporting criteria; positions and classifications may be revised. This is positioning data, not a directional trading signal. A long or short category does not, by itself, predict Treasury yields.

## Treasury auction and supply ingestion (Stage 6)

Stage 6 uses the official [Treasury Securities Auctions Data](https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/) dataset and its structured [Fiscal Data API endpoint](https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query). It contains announced and auctioned marketable Treasury securities. The adapter currently selects source-labeled Notes and Bonds with 2-Year, 5-Year, 7-Year, 10-Year, 20-Year, and 30-Year terms. Bills, TIPS, and floating-rate notes are outside this initial scope. A 20-Year row is kept with the source's own security type (typically `Bond`); the adapter does not replace official `security_type` labels with an inferred category.

Run `python -m treasury_flow_radar.sources.treasury_auctions` to ingest the selected auction history. The public endpoint requires no API key. The adapter requests the source's record/publication calendar date (`record_date`), announcement date (the source field is spelled `announcemt_date`), auction date, issue/settlement date, maturity, CUSIP, original issue date/term, explicit `reopening` flag, offering/accepted amounts, bid-to-cover ratio, yield/rate fields, and coupon/interest rate. Source fields are not present for every security, so missing and null values remain null. `total_accepted` is the source's accepted/awarded total; no separate award value is calculated. Amounts are stored in Fiscal Data's source-native whole U.S. dollars (unit `us_dollars`; the API types these fields as `CURRENCY0`, e.g. a $44 billion offering is `44000000000`). Earlier ingestions mislabelled them `thousand_us_dollars`; re-ingesting records a corrected revision linked to each earlier row; raw numeric strings remain in `raw_value` and in the preserved page payload. Interest/yield/rate values remain in percent units, and bid-to-cover remains a ratio.

Treasury's `record_date` is a publication **date**, not a publication timestamp. It is stored as a date in observation metadata; `publication_time` stays NULL. Announcement, auction, issue/settlement, maturity, and original issue dates are kept separately as calendar dates. The auction date anchors `observation_time`; retrieval time is captured separately for the database's historical as-of behavior. Date parsing keeps the date written by Treasury and does not convert it through a timezone.

The source's explicit `reopening` value is retained as supplied and also mapped to `new_issue` or `reopening` when it is exactly `No` or `Yes`; an unknown value remains unknown. Treasury describes a reopening as an additional offering of an existing security with the same CUSIP and maturity but a different issue date ([TreasuryDirect reopening guidance](https://www.treasurydirect.gov/auctions/when-auctions-happen/schedule-auction-reopenings/)). The adapter does not infer reopening status from CUSIP, term, or maturity.

The Fiscal Data API is paginated. The adapter requests 1,000 rows per page, verifies stable `total-count` and `total-pages`, and checks that all expected unique selected rows were received. Exact duplicate rows are deduplicated; conflicting rows sharing the same CUSIP/auction/issue identity fail the fetch before the database is opened for writes. Each response page is preserved in `raw_records`; every normalized numeric observation links to its page, identifies its source field, and keeps CUSIP, source security labels, dates, reopening data, and source fields in metadata. Values corrected by a later retrieval become immutable revisions linked to their predecessor; identical reingestion is idempotent.

The offline fixtures mimic the Fiscal Data JSON envelope, string-valued numeric fields, nullable fields, and pagination, including overlapping pages, a reopening, and a revised result. Fixture rows are synthetic contract examples, not claims about actual historical auctions. The full offline suite passes (66 tests), and Python compile validation passes. Ruff passes for the Stage 6 adapter and its tests. A live endpoint smoke test could not be completed: the browser tool reported that the official API URL was not accessible, and shell access previously failed DNS resolution. Live source access therefore remains unverified in this environment.

Limitations: the source supplies publication date but no exact publication time, so the system must not claim intraday publication-time precision. Only the listed nominal Notes and Bonds are normalized; other marketable security families are intentionally excluded. The adapter preserves selected API fields and the complete response pages it receives, not every column in the 100-plus-field dataset. No supply totals, market interpretation, or trading signals are calculated.


## Corporate issuance normalization (Stage 7)

No public, reliable event-level primary issuance feed was selected for this stage. The SEC's Corporate Bond Offerings data is quarterly/annual aggregate counts and proceeds, uses LSEG SDC Platinum as its source, and excludes several offering categories; it is useful for aggregate context and later validation, not tranche ingestion ([SEC dataset and methodology](https://www.sec.gov/data-research/statistics-data-visualizations/corporate-bond-offerings)). SEC EDGAR's public submissions API exposes filing metadata, while its XBRL APIs expose reported company facts; neither is a structured new-issue/tranche feed ([SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)). Individual offering filings may contain terms but require document-specific extraction, which this project intentionally does not scrape.

FINRA TRACE reports executed secondary-market transactions, including execution time, quantity, price, and yield. It does not identify primary issuance events ([FINRA TRACE data description](https://www.finra.org/finra-data/fixed-income/about-trade-activity)). FINRA's approved Corporate Bond New Issue Reference Data Service is the closest regulatory design for event-level terms, but the SEC approval states implementation would follow a separate fees filing; this review did not verify a publicly available structured feed or current access terms ([SEC approval order](https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking/sr-finra-2019-008)). Therefore no live source adapter is enabled. The structured fixture is explicitly synthetic and is not historical market data.

The adapter contract in `treasury_flow_radar.sources.corporate_issuance` accepts paginated JSON pages with one provider-identified tranche per row. It normalizes issuer/security/deal/tranche IDs, amount, source currency, coupon/yield/spread, maturity, pricing and settlement dates, benchmark, rating, sector, callable status and transaction type. Optional source terms remain nullable. Extra source-specific fields remain in `source_native_fields`, and each complete source page is linked as raw provenance. Tranches stay separate under their parent deal identifiers; the adapter never fabricates a deal or security identifier. Currency is retained as supplied; no FX conversion is performed.

Observation time uses pricing date, or settlement date when pricing date is absent. Exact source publication timestamps and system retrieval timestamps remain separate; an unavailable publication time is NULL. Reingestion is idempotent; provider corrections create linked immutable revisions; conflicting duplicate IDs and incomplete pagination fail before the database is initialized or written.

A maturity-based years estimate is stored only when pricing/settlement date and maturity are available and maturity is not before the event date. It is labeled `estimate` with method `maturity_years_proxy_act_365_25`; it is remaining tenor, not modified duration or a bond-pricing calculation. Corporate issuance is a RESEARCH INPUT, not proof of dealer hedging. The hypothesis remains: “Large corporate issuance may create temporary duration/rate exposure for underwriting dealers, potentially generating Treasury/futures hedging pressure before settlement.” Access to a structured event-level source, vendor-specific identifier mapping, missing publication times, and source coverage remain limitations.


## Descriptive market analytics (Stage 8)

The provider-neutral `treasury_flow_radar.analytics` package accepts normalized observations from the existing source adapters. It calculates yield changes and rolling summaries, same-date 10Y–2Y curve spreads, CFTC outright net positions with spreading retained separately, NY Fed position changes, and price returns for any supplied instrument series. FRED yields are in percentage points (for example, 4.25 means 4.25%); a 0.01 percentage-point move is 1 basis point. Position outputs retain contracts or the NY Fed source-native unit.

Five- and 20-observation changes/returns compare the current point with the point five or twenty **supplied observations** earlier. Daily changes compare consecutive supplied observations; holidays and weekends are not synthesized. For daily yield series, a date whose source row has no value (FRED's `.` marker, typically a bond-market closure) is treated as a non-session: it is skipped, never filled, and the next session is compared with the last valued session. Results list the skipped dates (`skipped_no_value_dates`). For other series, an intervening NULL remains a gap and makes calculations requiring that observation unknown. Rolling mean uses the last 5 or 20 yield observations; rolling volatility is population standard deviation of the last 5 or 20 consecutive-observation daily changes, in basis points. Curve spreads use dates shared by both yield series only. NY Fed percentage change is omitted when its prior value is zero or unavailable.

The existing source adapters provide FRED DGS2/DGS10 yields, CFTC Treasury futures positions, NY Fed aggregate Treasury dealer positioning, and Treasury auction terms. They do not provide Treasury futures prices or HYG, IWM, or DXY price histories. The analytics accepts those as future normalized series (identifiers `HYG`, `IWM`, and `DXY`) and returns no fabricated values when none are supplied. No additional market-data source or scraper is introduced.

A market-confirmation vector labels supplied concurrent moves RISING, FALLING, FLAT, or UNKNOWN. Its configurable defaults classify yield moves within ±0.5 bp and price returns within ±0.1% as FLAT. Rising yield is described as yield rose / Treasury price pressure; falling yield as yield fell / Treasury price support. The vector is an OBSERVATION of co-movement, not a signal or causal conclusion. Numeric analytics are CALCULATION outputs. A calculation cannot establish a MECHANISM, INFERENCE, or HYPOTHESIS by itself. Corporate issuance remains a research input, not proof of dealer hedging.

A Windows production request reached the official CFTC Data Hub but failed with HTTP 400 because the adapter selected the removed `as_of_date_in_form_yy_mm_dd` field. The current-schema adapter removes that field; live verification from this Codex runtime remains pending because its DNS cannot resolve the CFTC host. Offline tests use fixtures only. See [ARCHITECTURE.md](ARCHITECTURE.md) for calculation and missing-data details.


## Local research dashboard and event studies (Stage 9)

Install the package and start the local, dependency-free dashboard:

```powershell
python -m pip install -e .
python -m dashboard.app --database data/treasury_flow_radar.sqlite3
```

Open http://127.0.0.1:8765. Pass `--host` or `--port` to change the local listener. The dashboard reads the existing SQLite database in read-only mode. If the file or schema is absent, it starts with UNKNOWN/UNAVAILABLE states and does not create or modify a database.

The page contains Treasury Market, Dealer Positioning, Futures Positioning, Treasury Supply, Corporate Issuance, Market Confirmation, Evidence / System Read, Data Freshness / Provenance, and Event Study panels. Corporate issuance is explicitly unavailable as a production event-level feed. Offline synthetic fixtures are used by tests only; they are never read as dashboard production records. HYG, IWM, DXY, and Treasury-futures market confirmation appears only when normalized values already exist.

Every observation retains source, series, observation date, publication time, retrieval time, frequency, freshness, unit, and raw-record reference. Freshness is a dashboard display rule based on retrieval age: daily up to 5 days, weekly up to 14 days, monthly up to 45 days; unknown frequency/timestamp yields UNKNOWN. These thresholds do not alter source data.

The evidence taxonomy distinguishes FACT (reported source value), CALCULATION (reproducible arithmetic), OBSERVATION (co-movement description), MECHANISM (possible process), INFERENCE (interpretation beyond measurement), and HYPOTHESIS (proposed explanation requiring further testing). The current System Read is INSUFFICIENT EVIDENCE; no confidence scores, trading directions, causal claims, or forecasts are produced.

The event view accepts a user-supplied event identifier, date, type, issuer, pricing/settlement dates, size/unit, and notes. Such an event is marked USER-SUPPLIED — UNVERIFIED and is not stored as a production observation. T−5 to T+5 offsets refer to the preceding/following available Treasury yield observation dates; they do not invent calendar-day or market observations. T0 is the exact supplied event date; absent source values remain UNKNOWN. Changes are shown relative to the actual T−1 observation when available. NY Fed, CFTC, auction, and normalized market-price inputs are attached only on their exact observed dates, so weekly positioning is never upsampled. Multiple-event descriptive summaries require at least five events with both T−1 and T+1 10Y yields; smaller samples return INSUFFICIENT SAMPLE. No significance test or causal estimate is implemented.


Event comparison definitions: the mean, median, and percentage rising summarize 10Y change from T−1 to T+1. The percentage post-event decline separately compares T+1 against T0 and reports its own complete-event count; if no T0/T+1 pairs exist, that percentage is NULL.


## Populate historical observations

Run the existing official-source adapters through the controlled orchestrator:

```powershell
python -m pip install -e .
python -m treasury_flow_radar.ingest
```

The default range is the latest 730 calendar days through today. Choose a database path or narrower date range as needed:

```powershell
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3 --start-date 2024-10-05 --end-date 2026-10-05
python -m treasury_flow_radar.ingest --source nyfed --source cftc
python -m treasury_flow_radar.ingest --source treasury-auctions
python -m treasury_flow_radar.ingest --source fred-dgs2 --source fred-dgs5 --source fred-dgs7 --source fred-dgs10 --source fred-dgs30
```

The runner invokes the existing adapters, which preserve raw responses and normalized observations in the existing database and keep revisions immutable. Re-running an unchanged range is idempotent. Reports include inserted, unchanged, and missing normalized observations, new raw records, and source totals. Each provider is ingested in its own transaction; a failed provider is reported and does not undo successful providers. The command exits nonzero if any selected provider fails.

FRED requires `FRED_API_KEY` in the process environment. The runner never loads `.env` automatically or prints the key. When the key is absent, each selected FRED series is reported as SKIPPED and no request is made. NY Fed, CFTC Socrata, and Treasury Fiscal Data use the already-configured public endpoints and require no key. DNS, network, HTTP, parsing, and persistence errors are reported as FAILED rather than being represented as successful empty data.

FRED, CFTC, and Treasury auction requests are bounded to the requested date range (FRED observation dates, CFTC weekly report dates, and auction dates respectively). The NY Fed time-series endpoint returns its selected series-break response as a whole; the adapter retains that complete raw response and stores only normalized observations inside the requested range. Its reporting remains weekly. A date-range selection does not delete older observations already in the database.

The dashboard reads the same configured SQLite database. After a successful run, reload it to see any newly persisted DGS2/DGS10, dealer-position, CFTC, and auction observations. A missing database or unavailable source remains visibly UNKNOWN/UNAVAILABLE. Raw records contain original provider response payloads; normalized observations point to those records and retain source observation, optional publication, and retrieval times separately.


## Descriptive research analytics

The reusable analytics API in `treasury_flow_radar.analytics` calculates DGS2/DGS10 changes in percentage points and basis points, 5- and 10-observation changes, supported rolling volatility, exact-date 10Y-minus-2Y spreads, and configurable large DGS10 move observations (default absolute threshold: 5 bp). A change is calculated only across actual supplied observations; missing dates are not synthesized. Spreads require both yield series on the same observation date.

The report joins a detected yield event to the latest NY Fed dealer observation and CFTC report dated on or before the event. It includes each source's actual observation/report date, dealer change from the previous reported observation, CFTC long/short/spreading and net positions, and the CFTC report lag. Weekly NY Fed and CFTC observations remain weekly; the report never forward-fills them as daily data. Treasury auction rows are matched within a configurable calendar-day window (default three days), and auction dates stay distinct from yield-event dates. Source URLs, observation IDs, raw-record IDs, and retrieval/publication timestamps are included where available. NY Fed/CFTC exact historical publication times are not supplied; observation-date alignment does not assert that a weekly value had been published by the event date.

Every report measurement is labeled FACT, CALCULATION, or OBSERVATION as appropriate. MECHANISM, INFERENCE, and HYPOTHESIS remain distinct evidence categories and are not generated as conclusions from co-occurrence. Timing and alignment do not establish causation, intent, or manipulation. The analytics are a research/measurement tool, not a trading system.

Generate machine-readable JSON with:

```powershell
python -m treasury_flow_radar.analytics.report --database data/treasury_flow_radar.sqlite3 --start-date 2025-01-01 --end-date 2025-12-31
```

The report opens the configured SQLite file in read-only mode and does not modify source observations. Use `--threshold-bps` and `--auction-window-days` to configure event and auction matching windows.


## Complete research system — current architecture

### Availability

| Area | Status |
| --- | --- |
| FRED DGS2/DGS10, NY Fed dealer positioning, CFTC TFF Treasury contracts, Treasury auctions | Ingestion adapters implemented; data availability depends on successful ingestion |
| Descriptive analytics, temporal alignment, event studies, evidence records | Implemented over actual stored observations |
| Treasury futures prices, HYG, IWM, DXY | Normalized series supported; no production price source configured |
| Corporate issuance event model and rate-lock measurements | Provider-neutral architecture implemented; production issuance feed unavailable |
| Live dashboard and self-contained HTML report | Implemented; both consume the same read-only report layer |

The system does not produce buy/sell recommendations, entries, exits, sizing, or forecasts. Co-movement and temporal association do not establish causality. No production data are fabricated. Corporate issuance is a hypothesis to test, not an assumption.

### Installation and operating commands (Windows PowerShell)

From the repository root:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Set FRED_API_KEY in the process environment if ingesting FRED; the project does not load .env automatically:

```powershell
$env:FRED_API_KEY = "<your FRED API key>"
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3 --start-date 2024-10-05 --end-date 2026-10-05
```

Run selected public-source adapters without FRED:

```powershell
python -m treasury_flow_radar.ingest --source nyfed --source cftc --source treasury-auctions
```

Build a read-only JSON report and start the live dashboard:

```powershell
python -m treasury_flow_radar.analytics.report --database data/treasury_flow_radar.sqlite3 --start-date 2025-01-01 --end-date 2025-12-31
python -m dashboard.app --database data/treasury_flow_radar.sqlite3
```

Open `http://127.0.0.1:8765`. Export the same analytics/report output to a standalone offline HTML snapshot:

```powershell
python -m dashboard.export --database data/treasury_flow_radar.sqlite3 --output data/treasury_flow_radar_report.html
```

The snapshot includes its styling, JavaScript, and displayed data, shows its generation timestamp, and opens without Python, SQLite, a network connection, or API keys. It is read-only with respect to the source database.

### Temporal alignment and event study

Reusable joins report the source observation date, event date, signed lag, and BEFORE/SAME_DAY/AFTER relation. NY Fed and CFTC weekly records stay sparse: align to an actual prior report when needed, never forward-fill. Unknown historical publication times remain unknown and date alignment is not treated as public-availability proof.

Event-study T−5…T+5, T−3…T+3, and T−1…T+1 offsets count actual DGS10 observations. The calculations include DGS2, DGS10, exact-date 10Y–2Y spread, and available exact-date normalized market-series returns, plus pre/event/post changes and reversal measures where there are sufficient observations. Dealer, CFTC, auction, and corporate contexts retain their source dates. No missing sessions or prices are synthesized.

### Corporate event fields and status

The adapter preserves provider-issued issuer/deal identifiers, announcement/pricing/settlement/maturity dates, principal/currency, coupon, yield/spread, Treasury benchmark maturity/yield, credit spread/classification, source reference, publication/retrieval times, and raw source provenance when supplied. No production provider is selected. SEC quarterly issuance aggregates and TRACE secondary transactions are not substitutes for primary issue events.

When only maturity is available, estimated duration is explicitly a maturity-years proxy, not cash-flow duration or DV01. Principal times that estimate is currency-years, not measured dealer hedge activity. The report and dashboard display “Corporate issuance event feed not configured” until a legitimate production event-level source is enabled. Any future issuance relationship is described as observed alongside or temporally associated with market moves; it does not establish causality.

### Evidence and interpretation

Evidence records distinguish FACT, CALCULATION, OBSERVATION, MECHANISM, INFERENCE, and HYPOTHESIS and carry statements, support, sources, observation dates, and retrieval dates. Confidence is omitted unless a statistical basis is explicitly defined. A calculation or co-movement observation does not by itself establish a mechanism, inference, hypothesis, causality, or manipulation.


## The published page (phone URL)

The page is published at **https://chrisrileyy91-gif.github.io/treasury-flow-radar/** by the `Publish Treasury Flow Radar` GitHub Actions workflow (`.github/workflows/publish.yml`). It runs every weekday evening (01:17 UTC Tuesday–Saturday, about 9:17 pm Eastern) and can also be started by hand from the repository's Actions tab ("Run workflow").

Each run:

1. Downloads the database from the `database` release of this repository (the file `treasury_flow_radar.sqlite3.gz`). The database is never committed to git.
2. Ingests all official sources (FRED needs the `FRED_API_KEY` repository secret; the others are public).
3. Checks the database (`python -m treasury_flow_radar.dbcheck`): it must pass SQLite's integrity check and hold at least as many observations as before. Observations are immutable revisions, so a correct run only adds rows; otherwise the run stops and the stored copy is kept.
4. Uploads the database plus a dated backup (`treasury_flow_radar-YYYY-MM-DD.sqlite3.gz`), keeping the 14 most recent backups.
5. Builds the page with `python -m dashboard.export` and deploys it to GitHub Pages.

**The cloud copy is the main database.** To work with it on your own computer, download it first (PowerShell, from the repository folder):

```powershell
gh release download database --pattern treasury_flow_radar.sqlite3.gz --dir data --clobber
python -c "import gzip,shutil;shutil.copyfileobj(gzip.open('data/treasury_flow_radar.sqlite3.gz'),open('data/treasury_flow_radar.sqlite3','wb'))"
```

Running ingestion locally afterwards only changes your local copy; the next scheduled run continues from the cloud copy.

### Page layout

The page is one self-contained HTML file (about 90 KB, no external requests) designed for a phone first. Sections answer, in order: *What is happening?* (a deterministic sentence about the last five sessions, the 2/5/7/10/30-year curve now versus 5 and 20 sessions earlier, and the 10-year over about six months), *Dealer positioning* (NY Fed aggregate, weekly), *Futures positioning* (CFTC net contracts for dealers, asset managers and leveraged funds, with every group's long/short/spreading in a collapsed table), *Treasury supply* (recent coupon auctions), *What evidence is missing?* (hatched panels for data that is not stored or has no production feed, and the rate-lock checklist), *Large 10-year moves* (each with a T−5…T+5 table across the curve), *Sources and freshness*, and *How to read this page* (the evidence taxonomy).

Charts are drawn in the browser from a small embedded data block and every chart has a plain table, so the content is readable without JavaScript. No value is estimated or filled; anything absent reads "Unknown". Labels distinguish Fact, Calculation, Observation, and Hypothesis. The published page shows the 20 most recent large moves; the local live dashboard (`python -m dashboard.app`) shows 60 and adds a "Look at a date" form for user-supplied, unverified event dates.
