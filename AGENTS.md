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

# Reader rule: "What this means for you"

- Every reading shown on the page, including readings added later and calm or good-news states, carries a plain-English "What this means for you" line for the reader's sectors: energy, AI, chips/semis and software.
- Write it for a non-specialist: what the reading is in plain words, then how that condition usually reaches those sectors. Keep it short enough to read on a phone.
- It is MECHANISM or INFERENCE only: never a forecast, a buy/sell/trim instruction, or a claim that a reading moved a stock. Close the overall read with "not a buy or sell signal."
- Overview texts live in `dashboard/for_you.py`; reuse it for new readings, and extend the test in `tests/test_overview.py` that keeps trade instructions out.
