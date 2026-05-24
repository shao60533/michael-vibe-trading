---
name: sequoia-x-a-share-selector
description: A股 Sequoia-X 量化选股技能。用于按 sngyai/Sequoia-X 的日线策略扫描或测试 A 股候选，包括海龟突破、均线放量、高窄旗形、涨停洗盘、上升趋势跌停、RPS 相对强度突破；当用户要求“Sequoia-X 选股”“红杉/Sequoia 策略”“最近一周选股测试”“用现有 A 股数据接口跑策略”“把 GitHub 选股项目蒸馏成 skill”时使用。输出必须说明数据来源、样本范围、候选股、触发策略、失败/缺数情况，并明确不构成投资建议。
---

# Sequoia-X A股选股

## Core Workflow

1. Confirm the task is A-share daily-bar screening or validation, not intraday execution.
2. Use real market data before giving specific candidates. Prefer existing local data adapters:
   - Workspace module: `factor_analysis.data_sources.fetch_sina_daily_kline`.
   - Active universe: Eastmoney when available, falling back to Sina market center sorted by amount.
   - Existing `a-stock-data` skill endpoints when deeper stock context is needed.
3. For quick recent testing, run:

```bash
/Users/zhangshuai/.codex/venvs/a-share-skills/bin/python \
  /Users/zhangshuai/.codex/skills/sequoia-x-a-share-selector/scripts/sequoia_x_weekly_scan.py \
  --workspace /Users/zhangshuai/michael-vibe-trading \
  --days 5 \
  --max-symbols 300
```

4. If the user requests the exact strategy definitions or tuning details, read `references/strategy-map.md`.
5. Report candidates by date and strategy. Include universe source, universe size, fetched symbols, missing symbols, latest trading date, and whether amount is a proxy.

## Strategy Guardrails

- Treat Sequoia-X signals as research alerts, not buy instructions.
- Do not invent current leaders or signals without live/recent data.
- If only a subset universe is scanned, state that clearly.
- If the Sina adapter is used, note that `amount` is approximated as `close * volume`, matching the existing workspace adapter.
- RPS requires cross-sectional ranking and at least 120 bars; do not compute it from a single stock in isolation.
- Sequoia-X originally uses baostock 后复权 daily bars and SQLite. This skill distills the strategy logic and can run against the local workspace's public-data adapters.

## Outputs

Return a compact Chinese report with:

- 数据源和样本：接口、日期、股票池大小、成功/失败数量。
- 最近一周结果：按日期列出策略命中数和前若干候选。
- 风险说明：缺数、停牌、新股历史不足、免费接口可能延迟或字段口径差异。
- 结尾固定提醒：`以上为量化研究与历史回测/扫描，不构成投资建议。`
