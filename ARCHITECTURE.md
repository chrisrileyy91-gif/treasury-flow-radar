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
SIGNAL ENGINE
    ↓
DASHBOARD
```

## Layers

- **Live data**: authoritative published data and source metadata, obtained only through approved acquisition methods.
- **Source adapters**: isolate provider-specific requests, formats, identifiers, and publication conventions. Changes to one provider must not contaminate other layers.
- **Raw data / provenance**: retain source payloads where practical, alongside retrieval metadata, so inputs can be audited and reprocessed.
- **Normalization**: convert source representations into consistent internal forms. Transformations must be explicit, documented, and reproducible.
- **Historical database**: persist raw and normalized observations and their lineage. SQLite is the initial database; it uses Python's standard library.
- **Signal engine**: calculate research measures from normalized history. It must distinguish measurements from interpretation.
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

`src/treasury_flow_radar/sources/cftc.py` uses the official [CFTC Traders in Financial Futures, Futures Only dataset](https://publicreporting.cftc.gov/d/gpe5-46if) and its public [Socrata JSON resource](https://publicreporting.cftc.gov/resource/gpe5-46if.json). It filters on CFTC contract market code for the 2-Year note (`042601`), 5-Year note (`044601`), 10-Year note (`043602`), Ultra 10-Year note (`043607`), and Treasury Bond/30-Year (`020601`). The CFTC data portal provides these identifiers and descriptions; the code, API response market name, and exchange are retained as metadata.

This is the TFF report because its five published classifications match the requested participant categories: Dealer/Intermediary, Asset Manager/Institutional, Leveraged Funds, Other Reportables, and Nonreportable. The similar Disaggregated report describes a different set of categories and is not used. Each report row is normalized into long-form observations: one series per contract, while the logical key is `report-date|participant-category|metric`. Category and metric are explicit observation metadata. Open interest uses the `all_participants` category. This yields queryable observations without adding CFTC-specific columns/tables or separate series for every metric.

The adapter preserves open interest and each available category's long, short, and spreading values. The four reportable categories include spreading fields; nonreportable positions have long/short values but no spreading field in this dataset. Missing source values remain NULL with their raw marker kept; zero remains numeric zero. Counts are stored in the source unit `contracts`, never scaled into dollars or duration. Net position is not calculated in this stage.

The Socrata API is public and uses no credential. The adapter requests only the selected contract codes and relevant source fields, ordered by report date/code with stable pagination. Each page response is preserved in `raw_records`; normalized observation rows point to their page's raw record. The report date is the Tuesday as-of date, stored as a UTC calendar-date anchor. The CFTC usually releases on Friday at 3:30 p.m. Eastern and can delay releases for holidays, but the row dataset does not supply each record's exact public-release timestamp. Publication time therefore remains NULL; retrieval time is captured from the actual request. If a source value changes, the generic immutable revision model records a successor linked to its prior observation. Identical reingestion is idempotent.

TFF is aggregate positioning by participant class and futures only; it excludes options and does not identify individual traders or explain their strategies. CFTC reports are subject to its reporting thresholds and may later be revised. Category long/short labels do not themselves imply a yield direction, intent, or trading recommendation.
