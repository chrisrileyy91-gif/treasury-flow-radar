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

These are expected authoritative sources; this initialization does not mean any of them has been implemented:

- Federal Reserve Bank of New York Primary Dealer Statistics
- CFTC Commitments of Traders
- CFTC Bank Participation Report
- U.S. Treasury
- Federal Reserve / FRED
- Corporate bond issuance data

## Project status

This repository is at its initialization stage. Data ingestion, normalization rules, calculations, database schema, and dashboard behavior will be developed and reviewed in later stages. See [ARCHITECTURE.md](ARCHITECTURE.md) for the intended system layers and lineage requirements.
