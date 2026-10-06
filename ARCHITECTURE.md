# Architecture

The intended data flow is:

```text
LIVE DATA
    ↓
SOURCE ADAPTERS
    ↓
RAW DATA / PROVENANCE
    ↓
NORMALIZATION
    ↓
HISTORICAL DATABASE
    ↓
RESEARCH ANALYTICS (DESCRIPTIVE)
    ↓
DASHBOARD
```

## Layers

- **Live data**: authoritative published data and source metadata, obtained only through approved acquisition methods.
- **Source adapters**: isolate provider-specific requests, formats, identifiers, and publication conventions. Changes to one provider must not contaminate other layers.
- **Raw data / provenance**: retain source payloads where practical, alongside retrieval metadata, so inputs can be audited and reprocessed.
- **Normalization**: convert source representations into consistent internal forms. Transformations must be explicit, documented, and reproducible.
- **Historical database**: persist raw and normalized observations and their lineage. SQLite is the initial database; it uses Python's standard library.
- **Research analytics**: calculate research measures from normalized history. It must distinguish measurements from interpretation.
- **Dashboard**: present evidence and uncertainty, labeling claims by evidence category.

## SQLite database

The database layer is under `src/treasury_flow_radar/database/`. It uses SQLite through Python's built-in `sqlite3`, enables foreign-key enforcement on every connection, and initializes the schema explicitly with `initialize_database(path)`. The default file is `data/treasury_flow_radar.sqlite3`; callers may supply another path. A connection context manager commits successful work, rolls back errors, and closes the connection.

### Table responsibilities

- **sources** registers a provider using a stable application identifier, display name, source type, optional URL, and provider metadata.
- **series** identifies a source-specific series, instrument, or metric. Its identifier is provider-scoped; the schema does not give special treatment to any example instrument.
- **raw_records** retains the provider's raw record/response text, content type, provider record identifier where available, SHA-256 payload digest, retrieval time, and metadata. A source/record-id/payload-digest uniqueness key avoids storing identical identified payloads repeatedly.
- **observations** stores an observation's logical key and revision, source and series references, timestamps, optional numeric and text values, unit, source-provided raw value, optional raw-record reference, and metadata.

A provider may emit non-numeric observations: `value_text` is available independently of `value_numeric`. Structured provider payloads belong in `raw_records`, not in provider-specific observation columns. Composite foreign keys ensure the series and raw record belong to the observation's source.

### Timestamp semantics

Times are accepted as timezone-aware datetimes or ISO-8601 strings with an explicit offset and stored as UTC ISO-8601 strings with fixed microsecond precision.

- **observation_time** is the time the underlying market or economic measure refers to.
- **publication_time** is when the provider made that information public. It may be unknown (`NULL`).
- **retrieval_time** is when this system obtained the record.

These fields represent distinct events. For example, a Tuesday observation published and retrieved Friday retains Tuesday as its observation time and Friday as its publication and retrieval times. Naive datetimes are rejected.

### Revisions and duplicate handling

Each logical observation is keyed by `(source_id, series_id, logical_key)` and has an integer revision starting at 1. Revisions are immutable rows; a later version points to its immediate predecessor through `revision_of_id`. Revisions therefore preserve the original and the provider's later replacement without requiring event-sourcing machinery.

The uniqueness constraint on `(source_id, series_id, logical_key, revision)` prevents the same revision from being recorded twice. Re-inserting an identical version returns the existing ID; submitting different contents under that same revision raises an error instead of overwriting history. A provider correction must be inserted as the next revision. Raw records are deduplicated by source, provider record identifier, and payload digest when an identifier is supplied.

### Provenance and as-of queries

Observation rows explicitly reference both their provider and provider-scoped series, and can link to the raw record that produced the normalized value. This supports tracing an observation to its source, raw provider representation, values, units, and all three times.

`get_observations_as_of(connection, as_of)` returns the highest revision per logical observation for which retrieval and any known publication time are no later than the requested instant. A record with unknown publication time can be considered knowable after retrieval. This is a deterministic information-availability rule, not a claim that a record was valid or correct. The query answers the historical question: **“What did the system actually know at that point in time?”** Later revisions are excluded before they became available, preventing look-ahead from silently entering event studies.

The current model records observation versions, not every repeated transport-level fetch. Raw record deduplication avoids redundant payload storage; retrieval of an unchanged duplicate does not create a new observation version. If exact auditability of every polling attempt becomes necessary, that can be added as a separate retrieval log without changing observation identity.

## FRED source adapter

The first source adapter is `src/treasury_flow_radar/sources/fred.py`. It owns FRED-specific HTTP requests, JSON validation, missing-value handling, and mapping of only DGS10 and DGS2 into the provider-neutral database API. The command `python -m treasury_flow_radar.sources.fred [DGS10] [DGS2]` ingests an explicitly selected series set (or both by default). No FRED-specific request or parsing logic lives in the database package.

The data path is:

1. The adapter requests the FRED `/fred/series/observations` endpoint with the selected series ID, JSON response format, and ascending observation dates.
2. It captures the response body as received, selected HTTP response metadata, retrieval time, FRED real-time metadata, and each source value.
3. It registers the FRED source and provider-scoped series definitions, then stores the complete response in `raw_records`.
4. Each source calendar date becomes an observation with a numeric rate in percent, the exact FRED value string in `raw_value`, and a foreign-key link to that response. FRED's `.` missing marker yields a NULL numeric value, not zero.
5. An unchanged date/value is skipped as an already-known observation. A changed value is stored as the next immutable revision, linked to the prior row. The database's duplicate semantics are unchanged.

The observations API requires `FRED_API_KEY`; `TFR_USER_AGENT` is optional, and `TREASURY_FLOW_RADAR_DB` or the CLI's `--database` selects the database path. API keys are read from environment variables and are not written into request provenance. The adapter uses the official FRED Web Services API, not an unauthenticated CSV path.

### FRED timestamp and vintage limitations

FRED's `date` field describes the observation's calendar date. The adapter stores that date as midnight UTC solely as a date anchor and marks `observation_precision=calendar_date`; it is not an assertion that the value was observed or published at midnight. FRED's `realtime_start` and `realtime_end` values describe date-based real-time periods and are kept in observation metadata and in the raw response. They do not establish an exact release instant. Therefore `publication_time` remains NULL for these records. `retrieval_time` records when this application received the response.

If a later FRED response changes a historical value, the adapter can identify the new value and preserve it as a revision, but it does not backfill the exact date/time when FRED first published that change. The as-of query will consequently gate those versions by retrieval time. This is intentionally incomplete rather than inventing publication precision.

## Data lineage

Every observation should be traceable to its source, source timestamp, observation timestamp, retrieval timestamp, frequency, series/instrument/metric, raw value, and normalized value when applicable. Missing values must remain missing rather than being fabricated.

Historical reconstruction is essential to answer: **“What did the system actually know at that point in time?”** Preserve publication/retrieval timing and revisions where the source makes them available; do not substitute later knowledge for the view available at the historical point being studied.

External APIs and data formats belong behind source adapters. Provider-specific behavior must not leak into normalization, storage, analysis, or presentation.


## NY Fed Primary Dealer Statistics adapter

`src/treasury_flow_radar/sources/nyfed.py` owns requests and parsing for the Federal Reserve Bank of New York Markets Data API. In this stage it selects one aggregate series: provider key ID `PDPOSGST-TOT`, described by the NY Fed catalog as total nominal U.S. Treasury dealer position (long minus short), excluding TIPS. The internal series identity includes the provider structural window (for example, `SBN2024`) so definition changes are not silently folded into one series. Repo, fails, and transaction/activity fields are intentionally outside this first implementation.

The adapter requests `/api/pd/get/{seriesbreak}/timeseries/{keyid}.json`, parses the matching `pd.timeseries` records, and retains the full JSON response in `raw_records`. The source-native numeric values are stored in `million_us_dollars` (millions of U.S. dollars), without a scale conversion. A source marker for suppressed/unavailable values is retained in `raw_value` and represented by a NULL numeric value. Every observation links to the raw payload and records its provider key ID and structural window in metadata.

Run `python -m treasury_flow_radar.sources.nyfed` for the current window, or provide `--series-break` and `--key-id` for another historical window. The NY Fed source page describes data coverage beginning January 28, 1998 and notes that it is divided into time windows because reporting structures changed. Prior windows may have distinct key IDs and definitions; their mappings must be checked against that window's official data definitions. Each window is registered as a separate internal series. Weekly source dates are calendar dates; the adapter anchors them at midnight UTC and marks their precision rather than claiming a time of day.

The source page reports Thursday updates at about 4:15 p.m. for the preceding week, but this schedule is not an exact historical release instant. The adapter therefore stores `publication_time=NULL`; retrieval time is the actual request time. It also documents that data are dealer-submitted and not audited by the NY Fed, may be revised, may be suppressed when fewer than three dealers report, and may not add due to rounding. Revisions are stored using the existing immutable observation model. No NY Fed-specific database tables or interpretation logic are added.


## CFTC TFF Treasury futures adapter

`src/treasury_flow_radar/sources/cftc.py` uses the official [CFTC Traders in Financial Futures, Futures Only dataset](https://publicreportinghub.cftc.gov/d/gpe5-46if) and its public [Socrata JSON resource](https://publicreportinghub.cftc.gov/resource/gpe5-46if.json). It filters on CFTC contract market code for the 2-Year note (`042601`), 5-Year note (`044601`), 10-Year note (`043602`), Ultra 10-Year note (`043607`), and Treasury Bond/30-Year (`020601`). The CFTC data portal provides these identifiers and descriptions; the code, API response market name, and exchange are retained as metadata.

This is the TFF report because its five published classifications match the requested participant categories: Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables, and Nonreportable. The similar Disaggregated report describes a different set of categories and is not used. The current Data Hub metadata identifies `report_date_as_yyyy_mm_dd`, `contract_market_name`, `cftc_contract_market_code`, and the source position fields as the active columns. It does not expose `as_of_date_in_form_yy_mm_dd`; the adapter no longer selects or normalizes that removed field. No alternate date field is substituted. Each report row is normalized into long-form observations: one series per contract, while the logical key is `report-date|participant-category|metric`. Category and metric are explicit observation metadata. Open interest uses the `all_participants` category. This yields queryable observations without adding CFTC-specific columns/tables or separate series for every metric.

The adapter preserves open interest and each available category's long, short, and spreading values. The four reportable categories include spreading fields; nonreportable positions have long/short values but no spreading field in this dataset. The selected raw payload includes current-schema source changes in open interest and dealer long, short, and spreading positions; these source-reported weekly changes are not substituted for changes calculated from successive normalized observations. Percentages of open interest, trader counts, concentration fields, and other unselected fields are outside this stage's scope. Missing source values remain NULL with their raw marker kept; zero remains numeric zero. Counts are stored in the source unit `contracts`, never scaled into dollars or duration. Net position is not calculated in this stage.

The Socrata API is public and uses no credential. The adapter requests only the selected contract codes and relevant source fields, ordered by report date/code with stable pagination. Each page response is preserved in `raw_records`; normalized observation rows point to their page's raw record. The report date is the Tuesday as-of date, stored as a UTC calendar-date anchor. The CFTC usually releases on Friday at 3:30 p.m. Eastern and can delay releases for holidays, but the row dataset does not supply each record's exact public-release timestamp. Publication time therefore remains NULL; retrieval time is captured from the actual request. If a source value changes, the generic immutable revision model records a successor linked to its prior observation. Identical reingestion is idempotent. Identical rows repeated across pages are deduplicated, while conflicting repeats fail the fetch before database writes. Each normalized metric also records its exact CFTC source field. The report variant is recorded as TFF Futures Only (`tff_futures_only`), distinct from TFF Futures and Options Combined and Legacy COT datasets.

TFF is aggregate positioning by participant class and futures only; it excludes options and does not identify individual traders or explain their strategies. CFTC reports are subject to its reporting thresholds and may later be revised. Category long/short labels do not themselves imply a yield direction, intent, or trading recommendation.

## Treasury Fiscal Data auction adapter (Stage 6)

`src/treasury_flow_radar/sources/treasury_auctions.py` isolates requests, parsing, page validation, and normalization for the U.S. Treasury Fiscal Data [Treasury Securities Auctions Data](https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/) dataset. Its exact resource is `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query`. Requests use the API's `fields`, `filter`, `sort`, `page[number]`, and `page[size]` parameters. The selected source `security_type` / `security_term` pairs are Notes and Bonds at 2-, 5-, 7-, 10-, 20-, and 30-Year terms. TIPS and floating-rate rows are excluded by their source flags; Bills and other terms are outside this stage. The official type and term are preserved verbatim. No Treasury-specific database tables or dependencies are introduced.

### Source field mapping and units

The adapter retains raw page JSON and the selected native fields: `record_date`, `cusip`, `security_type`, `security_term`, `announcemt_date` (Treasury's spelling), `auction_date`, `issue_date`, `maturity_date`, `original_issue_date`, `original_security_term`, `reopening`, `inflation_index_security`, `floating_rate`, `offering_amt`, `total_accepted`, `comp_accepted`, `noncomp_accepted`, `bid_to_cover_ratio`, `high_yield`, `high_investment_rate`, `high_discnt_rate`, and `int_rate`. Fiscal Data's accepted amount is represented by `total_accepted`; the adapter does not derive a separate award field. `int_rate` is retained as the coupon/interest rate where provided. Amounts remain in source-native USD thousands, rates in percentage points, and bid-to-cover in ratio units. Exact numeric source strings are stored in `raw_value`; parsed values are numeric only where parsing succeeds. NULL stays NULL.

Each numeric field is a provider-neutral observation with `source_field` metadata and a logical identity composed from CUSIP, auction date, issue date, and source field. This separates reopening auctions for the same CUSIP. A source row without an explicit identifier is identified by that documented composite; no fictitious source-native ID is added. A source revision to one normalized field creates an immutable observation revision linked to its predecessor. Reingesting equal raw field values is idempotent. Page parsing and duplicate checks complete before database initialization/writes, so conflicting same-identity rows cannot partially persist.

### Date and availability semantics

The source documents `record_date` as the date data was published, not an exact release timestamp. We preserve it as `record_date` in observation metadata and leave database `publication_time` NULL. The exact source announcement field is `announcemt_date`; `auction_date`, `issue_date` (issue/settlement date), `maturity_date`, and `original_issue_date` remain separate source calendar dates. `observation_time` uses the auction calendar date as a UTC date anchor and metadata marks its precision as `calendar_date`. Date parsing never converts an offset through UTC, so it cannot move a Treasury date to an adjacent day. Retrieval time is captured from the injected aware clock and gates the generic as-of query whenever publication time is unknown.

The source's `reopening` Yes/No flag is preserved exactly. A separate metadata classification maps only explicit Yes to `reopening` and No to `new_issue`; absent or unrecognized values remain NULL. TreasuryDirect explains that a reopening sells additional amounts of the existing CUSIP and has a different issue date ([official guidance](https://www.treasurydirect.gov/auctions/when-auctions-happen/schedule-auction-reopenings/)). The adapter does not infer this classification when the source flag is absent.

### Completeness, revisions, and limitations

Fiscal Data pagination is handled with 1,000-row pages and API totals. All pages are fetched before storage; changing pagination totals, missing rows, malformed envelopes, and conflicting duplicates fail closed. Identical rows within or across pages are stored once, while each original response page remains provenance. Observation metadata carries the dataset, source field, CUSIP, official security type/term, dates, raw source fields, and reopening flag; each observation references both its source and raw page.

The endpoint's `record_date` does not provide an exact release time, and retrieval time only establishes when this system obtained the response. Treasury data may be revised without exposing the exact first publication instant of a correction. The normalized scope excludes Bills, TIPS, FRNs, and non-target terms. The API's broader columns remain accessible in saved response pages only when they were requested; this adapter requests the documented field subset above. Fixtures are realistic synthetic Fiscal Data-shaped samples and cover pagination overlap, a reopening, missing values, revisions, and as-of selection. The official endpoint live smoke test was attempted but could not be completed: the browser tool said the URL was inaccessible, and local shell networking previously failed DNS resolution. Offline tests/compile/Ruff are validated; live connectivity is not.


## Corporate issuance normalization (Stage 7)

### Source decision

The source review distinguishes four data types:

- **Event-level primary issuance:** the desired deal/tranche-level records. No reliable, freely accessible structured feed was verified for this stage. The SEC-approved FINRA Corporate Bond New Issue Reference Data Service describes relevant issue terms, but its approval says implementation was to follow a separate fee filing; current endpoint and access availability were not verified ([SEC approval order](https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking/sr-finra-2019-008)). No network adapter is enabled pending a supported provider contract and access terms.
- **Secondary-market transactions:** FINRA TRACE data describe executed trades and their execution time, quantity, price, and yield. They are not issuance events and are not ingested as issuance ([FINRA description](https://www.finra.org/finra-data/fixed-income/about-trade-activity)). Historical enhanced TRACE access requires an agreement and fees ([FINRA historical data access](https://www.finra.org/filing-reporting/trace/historic-academic-data)).
- **Aggregate issuance statistics:** SEC Corporate Bond Offering statistics are updated quarterly, report counts/proceeds based on pricing date, and derive from LSEG SDC Platinum. They are suitable for context or aggregate validation only, not event-level records ([SEC methodology](https://www.sec.gov/data-research/statistics-data-visualizations/corporate-bond-offerings)).
- **Individual filings:** EDGAR's public submissions API provides filing metadata, and XBRL APIs aggregate reported company facts ([SEC API guide](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)). Offering documents can carry terms but are unstructured filing documents, not a consistent issuance feed. Stage 7 does not scrape them.

The selected implementation source is therefore **none for production ingestion**. The offline fixture implements a provider-neutral page contract only; all example issuers, terms, and records are synthetic. A future licensed/provider adapter should map its event-level response to this contract while retaining the exact original payload and documenting its own coverage, access, semantics, fields, and timestamp precision. This does not infer that no commercial or gated feed exists; access was not established in this environment.

### Adapter and normalized tranche facts

`sources/corporate_issuance.py` receives JSON pages shaped as `data[]` plus stable `meta.total-count` and `meta.total-pages`. A provider-specific HTTP client can be added at this boundary without changing SQLite. One row equals one tranche; sibling rows retain the source deal ID and each independent tranche/security ID. The source record ID is mandatory and provider supplied. Missing deal/security/tranche IDs remain NULL; no synthetic identifiers are created. Rows must include pricing date or settlement date for an event-time anchor.

The long-form provider-scoped `corporate_issuance_tranche` series stores each field as a generic observation, without new database tables. Numeric principal, coupon, yield, and spread are parsed from strings while exact source strings remain `raw_value`; a source currency stays in the principal observation's unit. No currency conversion is performed. Dates accept ISO calendar dates, ISO timestamps, US month/day/year, and year/month/day forms, and their written calendar date is kept. Unknown fields are kept in the raw page and `source_native_fields`. Normalized metadata groups observations by tranche and preserves issuer/deal/tranche identifiers and fields needed to reconstruct the record.

Page shapes, row types, page totals, required source record IDs, normalized fields, and duplicate identities are fully checked before database initialization/writes. Identical repeated rows deduplicate; conflicting rows with a repeated provider ID raise an error. Unchanged observations are idempotent. If normalized data or source-native fields change, new immutable revisions link to their predecessors. The generic sources, series, raw_records, observations, publication-time, retrieval-time, and as-of behavior remains unchanged.

### Duration and limits

When a maturity date is on or after pricing date (or settlement date if pricing date is missing), the adapter records `duration_years_estimate` as elapsed days divided by 365.25. Metadata labels it `estimate` and names the method `maturity_years_proxy_act_365_25`. It is a rough remaining-tenor proxy, not source-supplied duration, Macaulay duration, modified duration, or a price sensitivity calculation. No estimate is written when inputs are absent or inconsistent.

Corporate issuance is a research input, not proof of dealer hedging. “Large corporate issuance may create temporary duration/rate exposure for underwriting dealers, potentially generating Treasury/futures hedging pressure before settlement” remains a hypothesis to test, not a causal conclusion. Fixture tests are offline. A live smoke test against a selected event-level source was not performed because no public structured production source was selected. No Stage 8 analytics or Stage 9 dashboard work is included.


## Stage 8 — Descriptive market analytics

The pure, provider-neutral `src/treasury_flow_radar/analytics/descriptive.py` module consumes an observation projection of the existing generic database rows. `Observation.from_mapping` accepts normalized row fields, parses metadata JSON, uses a provider `series_identifier` when supplied (or FRED's `metadata.series_id`), and selects the greatest revision for each source series/logical identity. Callers doing historical reconstruction should provide rows filtered to the desired as-of timestamp using the database layer's existing as-of query; the analytics does not override provenance or publication/retrieval cutoffs.

### Yield and curve units

The FRED adapter stores DGS2 and DGS10 as percentage points, not decimal fractions: e.g. 4.25 means 4.25%. Yield changes are reported both in percentage points and basis points; `basis_points_change = (current_percent - prior_percent) × 100`. Five- and 20-observation changes compare with exactly that many input observations earlier (the prior point is one observation before current). Rolling means use the latest 5/20 yield levels. Rolling volatility is population standard deviation of the latest 5/20 consecutive-observation changes, converted to basis points. No calendar series is generated: consecutive observations may span weekends or market holidays, and NULL values break the relevant lag or rolling window.

The curve function aligns DGS2 and DGS10 by exact observation date, then calculates `10Y − 2Y` in basis points and changes over the same observation lags. It does not carry one series forward to match the other. Curve outputs are CALCULATION records and remain descriptive, with no trading classification.

### Position and price measures

CFTC observations are paired by contract series, report date, participant category, and source metric. Dealer/intermediary, leveraged-fund, and asset-manager net positions are `long − short` in the source's futures contracts. Spreading remains a separate field and is never added to net. Changes are calculated only between supplied report observations; their dates and weekly cadence are retained, with no daily upsampling. If either long or short is missing, net and its change remain NULL.

NY Fed positioning changes use the series' source-native units (currently million U.S. dollars). Percent change is `(current / prior − 1) × 100` only when both values exist and prior is nonzero. Missing observations remain missing and break adjacent changes.

Generic price-series returns use the last supplied price and the supplied price 5 or 20 observations earlier. Daily return uses consecutive supplied prices. The function has no provider dependencies and can consume normalized series identified as HYG, IWM, DXY, Treasury futures prices, or other symbols when those inputs exist. Stage 8 adds no source adapter: the implemented project sources are FRED yields, CFTC weekly futures positions, NY Fed dealer positions, and Treasury auctions. None supplies HYG/IWM/DXY prices or Treasury futures market prices. These identifiers are interface placeholders, and missing price inputs yield no metric.

### Confirmation and evidence

`build_confirmation_vector` classifies each supplied change against explicit configurable absolute thresholds. Defaults are ±0.5 basis points for yield changes and ±0.1% for price returns; a move exactly at the threshold is FLAT. Missing values produce UNKNOWN. Rising yields are worded as “yield rose / Treasury price pressure”; falling yields as “yield fell / Treasury price support.” HYG, IWM, and DXY use neutral rising/falling descriptions. The vector is tagged OBSERVATION because it summarizes simultaneous conditions. Individual computed numeric measures are tagged CALCULATION. The upstream source observations remain FACTs. This layer does not emit MECHANISM, INFERENCE, or HYPOTHESIS claims and cannot convert arithmetic into causal language.

All calculation results are deterministic for a given normalized observation set. Invalid numeric values and conflicting equal revisions are rejected. Divide-by-zero percentage calculations remain unavailable. No forward fill, interpolation, weekend fabrication, market-price source, causal analysis, event study, signal, dashboard, or alerting is added in Stage 8. A Windows production request reached the official CFTC Data Hub and returned HTTP 400 because the old adapter selected the nonexistent `as_of_date_in_form_yy_mm_dd` column. The current-schema repair removes it, and a Codex-runtime live rerun remains unverified because DNS resolution for the CFTC host fails here. Offline validation uses synthetic fixtures; live-source calculations remain unverified until Windows ingestion succeeds.


## Stage 9 — Local dashboard, evidence, and descriptive event study

The optional standard-library dashboard is started from the repository root with `python -m pip install -e .` followed by `python -m dashboard.app --database data/treasury_flow_radar.sqlite3`. It binds to loopback by default (127.0.0.1:8765) and performs no network data requests. `dashboard/research.py` opens an existing database using SQLite read-only mode; a missing/uninitialized database produces an empty snapshot and is never initialized or written.

The dashboard projects existing `sources`, `series`, `observations`, and `raw_records` provenance fields. It selects the highest immutable revision for each source/series/logical key. The page covers 2Y/10Y yields and exact-date 10Y−2Y spread, aggregate NY Fed dealer series, CFTC report-date long/short/spreading/net and prior-report net change, Treasury auction metadata and source-native field values, market confirmations only when normalized price records exist, and provenance/freshness. Corporate issuance production events are unavailable; test fixtures are not used as dashboard inputs.

Freshness labels use retrieval age only: daily ≤5 days, weekly ≤14 days, monthly ≤45 days. Missing or unsupported frequency/timestamp is UNKNOWN. These deterministic UI thresholds are not changes to the stored data or claims about a provider's formal publication schedule. Observation time, optional publication time, retrieval time, reporting frequency, unit, and raw record reference stay separately visible.

Evidence labels remain semantically distinct: FACT for a reported value, CALCULATION for arithmetic, OBSERVATION for measured co-movement, MECHANISM for a possible transmission process, INFERENCE for a qualified interpretation, and HYPOTHESIS for a proposed explanation to test. The dashboard exposes a static mechanism/hypothesis description separately from source-backed facts. Its current status is INSUFFICIENT EVIDENCE because no production issuance event source is available; it does not derive confidence percentages, causal claims, predictions, or trading signals from the observations.

The event interface accepts a user-supplied event object with identifier/date/type/issuer/pricing date/settlement date/size/unit/notes and marks it USER-SUPPLIED — UNVERIFIED. It is a view parameter only and is not ingested into the production database. T−5…T+5 are offsets over the nearest five actual yield-observation dates on each side, with T0 aligned to the exact event calendar date. A missing offset or missing exact-date T0 is represented with NULL/UNKNOWN; no calendar rows are fabricated. 2Y/10Y yield changes are relative to the observed T−1 level. Curve spread is calculated only when both yields exist on the same date. NY Fed, CFTC, Treasury supply, and normalized HYG/IWM/DXY/Treasury-futures observations are exposed only when their stored date matches the window date; weekly CFTC and NY Fed data remain sparse.

The pure `compare_events` helper reports mean/median 10Y change from T−1 to T+1, fraction rising, fraction declining, and the raw outcome list when at least five complete events are supplied. Below that explicit threshold it returns INSUFFICIENT SAMPLE and no aggregate values. It does not calculate p-values or significance. The event output is descriptive and cannot establish that an event caused market behavior.


Event comparison denominator details: T−1-to-T+1 10Y changes determine the mean, median, and percentage rising. Post-event decline is a separate T0-to-T+1 comparison with its own reported sample count; it is NULL when no event has both values. Neither metric is a causal estimate.


## Stage 10 — Controlled historical ingestion

Run `python -m treasury_flow_radar.ingest` after installing the project editable. The runner calls the existing FRED, NY Fed, CFTC TFF, and Treasury Fiscal Data ingestion functions; it does not implement alternate provider parsing. The default inclusive date window is today minus 730 days through today. `--start-date`, `--end-date`, repeatable `--source`, and `--database` narrow the operation. Source names are `fred-dgs2`, `fred-dgs10`, `nyfed`, `cftc`, and `treasury-auctions`.

FRED receives `observation_start`/`observation_end` in its official API request. CFTC report dates and Treasury auction dates are constrained in their respective public API query filters before pagination. NY Fed currently returns a full selected `seriesbreak`/key response; that raw response is preserved and normalized rows are limited to the requested date window. No rows are daily-expanded, and the stored NY Fed/CFTC frequencies remain weekly. Older rows already present in SQLite are not deleted when a narrower window is ingested.

Only FRED requires credentials. `FRED_API_KEY` is read from the process environment, never printed, and is not loaded from `.env`; a missing key marks each selected FRED series SKIPPED before any request. NY Fed, CFTC, and Treasury use existing public endpoints. The runner catches provider failures per selected adapter, prints an explicit FAILED result and sanitized error, then continues to other selected providers. Existing adapters fetch/validate before their own SQLite transaction, so a later provider failure does not roll back a completed provider transaction.

The JSON run report distinguishes SUCCESS, SKIPPED, and FAILED; reports inserted/unchanged/missing normalized counts, newly added raw payload count, and cumulative per-source normalized/raw totals. Re-ingestion remains idempotent through the existing adapter revision and raw-payload deduplication contracts. Raw response bodies stay in `raw_records`; normalized facts stay in `observations` and reference a `raw_record_id`. Source observation date, publication timestamp (NULL when the provider omits it), and actual retrieval timestamp remain distinct. No credentials or database contents are committed by this code change.


## Research analytics and report output

The provider-neutral calculations live in `src/treasury_flow_radar/analytics/descriptive.py`; yield-event alignment/report assembly lives in `research.py`; the JSON CLI and read-only SQLite projection live in `report.py`. Dashboard code can call these APIs without embedding SQL. The CLI queries latest immutable observation revisions with their source, series, raw-record, publication, and retrieval provenance, using SQLite read-only mode.

Yield levels are stored as percentage points; one percentage point is 100 basis points. Daily changes and 5-/10-observation changes use actual ordered observations, not calendar reindexing. Rolling volatility is emitted only when a complete window of observed changes exists. Curve spread is calculated only on exact shared DGS2/DGS10 dates and is returned in both percentage points and basis points.

A large-move event is an OBSERVATION selected when the absolute change between adjacent non-missing DGS10 observations meets the configurable threshold (5 bp by default). Dealer context selects the nearest reported NY Fed position on or before the event and computes its change against the preceding reported dealer observation; no daily forward-fill is performed. CFTC context selects the latest report date on or before the event, computes long minus short only when both inputs exist, retains spreading separately, and reports the calendar-day lag. Auction matching compares the event date with actual auction dates inside a configurable calendar-day window; it does not relabel an auction as occurring on the yield date.

Output item labels distinguish FACT (source-reported values), CALCULATION (deterministic arithmetic), and OBSERVATION (a measured event/pattern). The taxonomy also names MECHANISM, INFERENCE, and HYPOTHESIS so later editorial research can preserve those distinctions. The report does not assert a causal relation, intent, or manipulation. This is a descriptive research and measurement system, not a trading system. Synthetic values are confined to offline tests; the production report reads only the database.


## Complete research-system architecture

### Shared analytic/report layer

`analytics.temporal.align_observation` selects an actual prior, following, or nearest observation and returns source date, event date, lag, relationship, source/series identity, value/unit, observation ID, and available publication/retrieval timestamps. Weekly inputs are never expanded to daily rows.

`analytics.research.build_research_report` combines stored FRED DGS2/DGS10, NY Fed dealer positions, CFTC TFF positions, Treasury auctions, any configured event-level corporate issuance records, and normalized market price series. It emits only values supported by the data, with source provenance and explicit unavailability. The SQLite projection selects latest immutable revisions and opens the database read-only.

`analytics.event_study.event_study` creates T−5/T+5, T−3/T+3, and T−1/T+1 windows over actual DGS10 observation dates. It calculates yield and exact-date curve changes, supplied market series returns, pre/event/post moves, and descriptive post-event reversal metrics when observations permit. Sparse market series are not substituted across dates. Context is associated by actual source dates, and co-movement is explicitly non-causal.

### Issuance analytics boundary

The provider-neutral tranche adapter stores optional announcement/pricing/settlement/maturity dates, deal/issuer identifiers, terms, benchmark values, classification, source URL, publication/retrieval timestamps, and raw provenance. It refuses to identify itself as a configured production feed without a provider-specific identifier and source URL. No live provider has been selected; corporate event status remains unavailable. SEC aggregate issue counts and TRACE trades are excluded as event substitutes.

The issuance analytics measures milestone Treasury-yield differences and the first available post-settlement yield observation on or after 1/2/3/5 calendar-day targets. The maturity-years estimate is explicitly a proxy. Principal multiplied by estimated years is currency-years, not DV01 or observed hedging. None of these measurements establishes causality.

### Evidence and interpretation

An `EvidenceRecord` identifies one of FACT, CALCULATION, OBSERVATION, MECHANISM, INFERENCE, or HYPOTHESIS, along with a statement, supporting rows, source, observation dates, and retrieval dates. Confidence requires an explicitly defined statistical basis. No arbitrary evidence count or confidence score is used. Mechanisms remain separate from observed facts, and no manipulation conclusion is inferred from timing or correlation.

### Dashboard and portable snapshot

The live HTTP app and offline exporter both consume `dashboard.reporting.load_dashboard_report`, which calls the shared read-only loader and report builder. The renderer embeds the report data, CSS, and JavaScript in one HTML file. Static export reads the SQLite database without mutation, states “Generated at” with the snapshot timestamp, and displays unavailable-source notices. Viewing the export does not need Python, SQLite, credentials, or network access.

Windows PowerShell commands from the repository root:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -e .
$env:FRED_API_KEY = "<your FRED API key>"
python -m treasury_flow_radar.ingest --database data/treasury_flow_radar.sqlite3
python -m dashboard.app --database data/treasury_flow_radar.sqlite3
python -m treasury_flow_radar.analytics.report --database data/treasury_flow_radar.sqlite3 --start-date 2025-01-01 --end-date 2025-12-31
python -m dashboard.export --database data/treasury_flow_radar.sqlite3 --output data/treasury_flow_radar_report.html
```

### Current provider availability

FRED DGS2/DGS10, NY Fed positioning, CFTC Treasury futures positions, and Treasury auction ingestion are implemented. Market confirmation supports Treasury futures, HYG, IWM, and DXY identifiers but no production price feed is configured. No production corporate event-level issuance provider is enabled. These missing feeds remain unavailable in reports and are never filled by test fixtures.


## Human-readable dashboard projection

The dashboard presentation module is a projection over the shared read-only research report. It selects current facts, compact per-contract CFTC summaries, source timing, evidence availability, a rate-lock research checklist, provenance, and a bounded list of recent event details. It does not alter database rows, ingestion, evidence types, or calculations.

The renderer uses that projection for both modes. The live app requests up to 60 recent expandable events. The exporter uses the default 20-event static snapshot limit so the portable report stays small while preserving a usable audit trail. Every retained event is organized as market move, dealer positioning, CFTC positioning, Treasury supply, corporate issuance, market confirmation, event study, provenance, limitations, and collapsed raw structured evidence. The renderer never embeds the full report object, raw database, environment values, or credentials.

The first screen uses SYSTEM READ, WHAT IS HAPPENING?, an evidence-availability panel, and a corporate issuance/rate-lock research-status checklist. Deterministic yield language is limited to the sign of the actual five-observation DGS10 change; it is not a market score, signal, or prediction. CFTC compact summaries show one current participant category per contract and preserve all current categories in collapsed details. Observation date, retrieval time, and publication time stay distinct throughout the projection.
