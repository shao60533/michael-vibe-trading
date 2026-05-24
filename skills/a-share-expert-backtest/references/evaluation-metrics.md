# Evaluation Metrics

## Trading Metrics

- Trade count: Number of bullish predictions evaluated as trades.
- Win rate: Share of bullish trades with net return above zero.
- Average return: Mean net return per bullish trade.
- Median return: Median net return per bullish trade.
- Payoff ratio: Average winning return divided by absolute average losing return.
- Max drawdown: Largest peak-to-trough decline of sequential trade equity.
- MFE: Maximum favorable excursion during the holding window.
- MAE: Maximum adverse excursion during the holding window.

## Judgment Metrics

- Directional accuracy: Share of all predictions whose action direction matched subsequent return.
- Coverage: Share of eligible cases where the expert emitted a valid actionable prediction.
- Confidence calibration: Accuracy and average return by confidence bucket.
- High-confidence miss rate: Share of predictions with confidence >= 0.7 that were wrong.
- Missed winner rate: `avoid`, `watch`, `sell`, or `reduce` cases where future return exceeded the bullish threshold.

## Recommended Splits

Always inspect metrics by:

- Expert and expert version.
- Action label.
- Holding horizon.
- Market regime.
- Theme or sector tag.
- Year and quarter.
- High versus low liquidity if volume/amount is available.

Do not optimize the expert on one aggregate score. Good experts should show stable performance across time, lower high-confidence miss rate, and better calibration, not only higher average return from a small number of lucky trades.
