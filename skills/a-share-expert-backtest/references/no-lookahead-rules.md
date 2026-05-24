# No-Lookahead Rules

Historical expert tests are only useful when the expert is blind to the future.

## Hard Rules

- Do not include prices, returns, headlines, announcements, filings, analyst reports, or sector moves after the case date.
- Do not include labels such as "future winner", "next 5-day return", "later became leader", or "eventually broke down" in the expert prompt.
- Do not use indicators calculated with future bars. Rolling indicators must end on or before the case date.
- Do not select only famous winners or famous failures. Use deterministic sampling or document the sampling rule.
- Do not tune the expert on the test set and then report the same set as final performance.

## Case Construction

For each case date:

1. Build the information packet from data available through that date's close.
2. Ask the expert for a prediction and store the raw response.
3. Only after storing the prediction, load future OHLCV for evaluation.

## Split Discipline

Use at least two sets:

- Development set: Used to inspect failures and revise prompts or skills.
- Holdout set: Used only after revisions are done.

For serious comparisons, use walk-forward batches: revise on older cases, validate on later unseen cases.
