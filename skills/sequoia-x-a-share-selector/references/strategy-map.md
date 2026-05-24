# Sequoia-X Strategy Map

Source distilled from `sngyai/Sequoia-X` at commit `444c0db69ff36b46ef2b22ab265051d60c16029d` (2026-05-09).

## Daily Bar Fields

Required columns: `date`, `open`, `high`, `low`, `close`, `volume`, `amount`.

The original project names amount `turnover` in SQLite but stores baostock `amount`. In this skill, use `amount` for成交额. When using the workspace Sina adapter, `amount = close * volume` is a proxy because that adapter does not return true成交额.

## Strategies

### MaVolume

- Need at least 20 bars.
- Compute `ma5`, `ma20`, and `vol_ma20`.
- Trigger when yesterday `ma5 < ma20`, today `ma5 > ma20`, and today volume is greater than `1.5 * vol_ma20`.

### TurtleTrade

- Need at least 21 bars.
- Trigger when today close is above the prior 20 trading days' highest high.
- Require today's amount above 100,000,000.
- Require today's candle to be positive: close above open and close above yesterday close.
- Original project sorts candidates by circulating market cap using baostock. This skill sorts by amount proxy unless extra capitalization data is fetched.

### HighTightFlag

- Need at least 40 bars.
- Past 40-day high divided by low must be above 1.6.
- Recent 10-day high divided by low must be below 1.15.
- Recent 10-day low must stay above 80% of the 40-day high.
- Today's volume must be below `0.6 * average volume of the prior 20 days`.

### LimitUpShakeout

- Need at least 3 bars.
- Yesterday close must be at least `1.095 *` the previous close.
- Today must close below open.
- Today volume must be greater than `2.0 *` yesterday volume.
- Today low must stay at or above yesterday close.

### UptrendLimitDown

- Need at least 60 bars.
- Yesterday `ma20 > ma60`.
- Today close must be at or below `0.905 *` yesterday close.
- Today volume must be greater than `2.0 * vol_ma20`.

### RpsBreakout

- Need at least 120 bars and a cross-sectional universe.
- Compute 120-day percentage change for every stock on the evaluation date.
- Rank percentage changes cross-sectionally and keep RPS >= 90.
- Trigger when close is at least 90% of the rolling 120-day high.

## Interpretation

- These are high-beta technical alerts. Always combine with market regime, liquidity, limit-up/limit-down constraints, upcoming unlocks, announcements, and position sizing.
- Empty output is meaningful: Sequoia-X filters are intentionally sparse.
