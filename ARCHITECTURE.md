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
