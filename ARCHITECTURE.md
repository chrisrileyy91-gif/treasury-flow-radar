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

## Data lineage

Every observation should eventually be traceable to its source, source timestamp, observation timestamp, retrieval timestamp, frequency, series/instrument/metric, raw value, and normalized value when applicable. Missing values must remain missing rather than being fabricated.

Historical reconstruction is essential to answer: **“What did the system actually know at that point in time?”** Preserve publication/retrieval timing and revisions where the source makes them available; do not substitute later knowledge for the view available at the historical point being studied.

External APIs and data formats belong behind source adapters. Provider-specific behavior must not leak into normalization, storage, analysis, or presentation.
