---
name: a-share-expert-backtest
description: A股专家判断历史盲测与回测评估。用于验证项目里的投研专家、游资视角、趋势判断或荐股建议在历史样本上的胜率、收益、最大回撤、盈亏比、置信度校准和失败模式。触发场景包括：用户要求回测专家建议、验证某个 skill 的判断准确率、抽取过去两年多个时间点做无未来函数盲测、比较不同专家或提示词版本、根据历史复盘结果迭代 A 股分析 skill、评估买入/观望/卖出/回避判断的后续表现。
---

# A-Share Expert Backtest

## Overview

Use this skill when the user wants to test whether A-share expert recommendations would have worked historically. The goal is not a normal indicator backtest; it is a no-lookahead evaluation harness for expert judgments made by project skills such as `xiao-eyu`, other trader-perspective skills, trend analysts, or stock decision presets.

## Core Workflow

1. Define the expert and hypothesis: which skill, prompt, model, or preset is being evaluated; which action labels it can emit; and which holding horizon matters.
2. Build blind historical cases from past dates. Prefer the last two years, with samples across rising, falling, and sideways regimes. Use only data available at or before each case date.
3. Ask the expert to produce structured predictions before showing future data. Require JSONL using `references/prediction-schema.md`.
4. Evaluate predictions against future OHLCV with `scripts/expert_backtest.py`. Use A-share assumptions: next-trading-day entry, no same-day sell after buy, transaction costs, slippage, and limit-up/limit-down awareness when data is available.
5. Report metrics and failure patterns: win rate, average return, median return, payoff ratio, max drawdown, hit rate by action, confidence calibration, market-regime splits, and high-confidence misses.
6. Iterate the expert skill only after reviewing raw failed cases. Avoid tuning on a tiny cherry-picked set.

## No-Lookahead Rules

Read `references/no-lookahead-rules.md` before building or running an evaluation. Any test is invalid if the expert sees post-date prices, later news, future fundamentals, later analyst reports, future index composition, or labels derived from the evaluation window.

When creating prompts for expert blind tests, include a hard cutoff:

```text
只允许使用截至 {case_date} 收盘已知的信息。禁止使用 {case_date} 之后的价格、新闻、公告、研报、财报或板块表现。
```

## Data Inputs

Prefer project data skills for retrieval:

- Use `a-stock-data` for A-share historical K lines, quotes, concepts, hot reasons, market breadth, and sector context.
- Use `xiao-eyu` or other trader skills only for generating or reviewing short-term expert judgments, not for fetching data.

If data has already been exported, `scripts/expert_backtest.py` accepts CSV files with at least:

```text
date,code,open,high,low,close
```

Optional columns: `volume`, `amount`, `benchmark_close`, `industry`, `market_regime`, `limit_up`, `limit_down`.

## Script Usage

Generate deterministic blind cases from OHLCV CSV files:

```bash
python3 skills/a-share-expert-backtest/scripts/expert_backtest.py sample \
  --prices-dir data/prices \
  --start 2024-01-01 \
  --end 2026-05-22 \
  --samples-per-month 2 \
  --horizon-days 5 \
  --output runs/expert_cases.jsonl
```

Evaluate expert predictions:

```bash
python3 skills/a-share-expert-backtest/scripts/expert_backtest.py evaluate \
  --prices-dir data/prices \
  --predictions runs/expert_predictions.jsonl \
  --horizon-days 5 \
  --cost-bps 15 \
  --slippage-bps 5 \
  --details-out runs/expert_backtest_details.csv \
  --summary-out runs/expert_backtest_summary.json
```

## Output Expectations

Return a compact report with:

- Test design: date range, sample count, horizons, costs, entry/exit assumptions, excluded cases.
- Aggregate metrics: directional accuracy, trade win rate, average/median return, payoff ratio, max drawdown.
- Calibration: confidence buckets and whether higher confidence actually wins more.
- Failure taxonomy: high-confidence losses, missed big winners, late entries, stop too tight, stop too loose, mainline misread, market-regime mismatch.
- Iteration suggestions: precise changes to the expert skill or prompt, backed by failed case examples.

End financial reports with: `以上为历史回测与模型评估，不构成投资建议。`
