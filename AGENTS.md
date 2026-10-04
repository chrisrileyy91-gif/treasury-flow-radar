# Development rules

- Read `README.md` and `ARCHITECTURE.md` before modifying the project.
- Prefer small, testable modules.
- Keep source-specific logic inside source adapters.
- Never silently transform financial data; document transformations and units.
- Preserve raw observations whenever practical.
- Never fabricate missing observations.
- Clearly distinguish FACT, CALCULATION, OBSERVATION, MECHANISM, INFERENCE, and HYPOTHESIS.
- Never turn a correlation into a causal claim.
- Never describe dealer positioning as proof of directional intent.
- Never describe rate-lock activity as proven unless supported by evidence.
- Every important calculation must have a test.
- Prefer deterministic calculations.
- Use explicit timestamps and timezone-aware datetime handling.
- Do not introduce unnecessary dependencies.
- Do not add credentials or secrets to the repository.
- Do not commit `.env` files.
- Do not build trading entries, exits, stops, or position-sizing logic into this project.
- Do not optimize for visual appearance before the underlying data model is correct.
