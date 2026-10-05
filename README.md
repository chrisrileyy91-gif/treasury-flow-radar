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

Run `python -m treasury_flow_radar.sources.treasury_auctions` to ingest the selected auction history. The public endpoint requires no API key. The adapter requests the source's record/publication calendar date (`record_date`), announcement date (the source field is spelled `announcemt_date`), auction date, issue/settlement date, maturity, CUSIP, original issue date/term, explicit `reopening` flag, offering/accepted amounts, bid-to-cover ratio, yield/rate fields, and coupon/interest rate. Source fields are not present for every security, so missing and null values remain null. `total_accepted` is the source's accepted/awarded total; no separate award value is calculated. Amounts retain Fiscal Data's source-native USD-thousands scale; raw numeric strings remain in `raw_value` and in the preserved page payload. Interest/yield/rate values remain in percent units, and bid-to-cover remains a ratio.

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

Five- and 20-observation changes/returns compare the current point with the point five or twenty **supplied observations** earlier. Daily changes compare consecutive supplied observations; holidays and weekends are not synthesized. An intervening NULL remains a gap and makes calculations requiring that observation unknown. Rolling mean uses the last 5 or 20 yield observations; rolling volatility is population standard deviation of the last 5 or 20 consecutive-observation daily changes, in basis points. Curve spreads use dates shared by both yield series only. NY Fed percentage change is omitted when its prior value is zero or unavailable.

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
python -m treasury_flow_radar.ingest --source fred-dgs2 --source fred-dgs10
```

The runner invokes the existing adapters, which preserve raw responses and normalized observations in the existing database and keep revisions immutable. Re-running an unchanged range is idempotent. Reports include inserted, unchanged, and missing normalized observations, new raw records, and source totals. Each provider is ingested in its own transaction; a failed provider is reported and does not undo successful providers. The command exits nonzero if any selected provider fails.

FRED requires `FRED_API_KEY` in the process environment. The runner never loads `.env` automatically or prints the key. When the key is absent, each selected FRED series is reported as SKIPPED and no request is made. NY Fed, CFTC Socrata, and Treasury Fiscal Data use the already-configured public endpoints and require no key. DNS, network, HTTP, parsing, and persistence errors are reported as FAILED rather than being represented as successful empty data.

FRED, CFTC, and Treasury auction requests are bounded to the requested date range (FRED observation dates, CFTC weekly report dates, and auction dates respectively). The NY Fed time-series endpoint returns its selected series-break response as a whole; the adapter retains that complete raw response and stores only normalized observations inside the requested range. Its reporting remains weekly. A date-range selection does not delete older observations already in the database.

The dashboard reads the same configured SQLite database. After a successful run, reload it to see any newly persisted DGS2/DGS10, dealer-position, CFTC, and auction observations. A missing database or unavailable source remains visibly UNKNOWN/UNAVAILABLE. Raw records contain original provider response payloads; normalized observations point to those records and retain source observation, optional publication, and retrieval times separately.
