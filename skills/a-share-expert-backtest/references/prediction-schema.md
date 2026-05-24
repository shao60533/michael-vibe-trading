# Prediction Schema

Use JSONL: one JSON object per case.

## Required Fields

```json
{
  "case_id": "002384_2025-08-12_5d",
  "date": "2025-08-12",
  "code": "002384",
  "action": "buy",
  "horizon_days": 5,
  "confidence": 0.72,
  "reason": "AI PCB主线仍在，回踩5日线后转强"
}
```

- `case_id`: Stable id. If omitted, the evaluator builds one from `code`, `date`, and horizon.
- `date`: Decision date, normally after that day's close.
- `code`: Six-digit A-share code.
- `action`: One of `buy`, `avoid`, `watch`, `sell`, `hold`, `reduce`.
- `horizon_days`: Trading-day horizon. If omitted, evaluator CLI default is used.
- `confidence`: Number from 0 to 1.
- `reason`: Short natural-language explanation.

## Optional Fields

- `entry`: Numeric planned entry. If omitted, evaluator uses next trading day's open.
- `stop_loss_pct`: Stop threshold as decimal, for example `0.05` for -5%.
- `take_profit_pct`: Take-profit threshold as decimal.
- `position_pct`: Suggested capital fraction.
- `expert`: Expert or skill name.
- `expert_version`: Prompt or skill version id.
- `market_regime`: Expert's perceived regime.
- `tags`: Array of themes, such as `["AI PCB", "trend"]`.

## Action Semantics

`buy` and `hold` are bullish. They are scored by realized long return after costs.

`avoid`, `sell`, and `reduce` are bearish or risk-off. They are scored directionally: correct when the future return is below the neutral threshold. No short return is assumed for A-share cash accounts unless the user explicitly requests a shortable universe.

`watch` is neutral. It is correct when the absolute future return stays inside the neutral threshold; it is a missed opportunity when the future return is strongly positive.
