---
name: khunter-a-share-selector
description: A股 KHunter 11 类技术形态量化选股技能,蒸馏自 KHunter 项目核心策略。覆盖底部反转(底部趋势拐点/W底/启明星)/ 趋势加速(趋势加速拐点/多金叉共振)/ 形态突破(阻力位突破/多方炮)/ 涨停态(涨停回马枪/涨停横盘)/ 洗盘(强势洗盘弱转强)/ 拉升(仙人指路)11 种。使用本 skill 的场景:用户要求按 KHunter 策略扫描 A 股、做盘前选股、按某种技术形态找候选、要看『底部趋势拐点』『仙人指路』『多方炮』『涨停横盘』等具体策略的命中股。需严格使用真实日线数据,不得编造候选。
---

# KHunter A 股选股

## 11 种策略 + 权重

权重越高强度越大,综合分排序时优先考虑高权重策略的命中。

| 权重 | 策略英文 | 策略中文 |
|---|---|---|
| 70 | LimitUpSideways | 涨停横盘 |
| 70 | ImmortalGuidance | 仙人指路 |
| 50 | BottomTrendInflection | 底部趋势拐点 |
| 50 | ResistanceBreakout | 阻力位突破 |
| 50 | WBottom | W底策略 |
| 50 | MultiGoldenCross | 多金叉共振 |
| 50 | LimitUpPullback | 涨停回马枪 |
| 50 | StrongWashWeakToStrong | 强势洗盘弱转强 |
| 30 | TrendAccelerationInflection | 趋势加速拐点 |
| 30 | MorningStar | 启明星策略 |
| 30 | MultiPartyCannon | 多方炮策略 |

## 入口

```bash
python scripts/khunter_weekly_scan.py \
  --workspace /app \
  --days 5 \
  --max-symbols 300
```

或在容器内通过 `khunter_x` 包调用:

```python
from khunter_x import run_khunter_pipeline
result = run_khunter_pipeline(days=5, max_symbols=300, top_n=10)
# → {scan, rankings, factor_scores, debates, report_markdown, outputs_dir}
```

## 数据要求

- 真实 A 股日线(`factor_analysis.data_sources.fetch_sina_daily_kline`)
- 不得编造候选 / 命中数 / 行情字段
- 若某策略真实命中 < 5 只,只允许从同一股票池补充最接近条件的弱信号候选,**必须标注「补充候选/低置信度」**

## 风险提示

11 种策略均为技术形态信号,只是研究 alerts,不构成交易指令。必须结合市场环境、流动性、限售解禁、公告催化、仓位管理综合判断。

以上为量化研究与历史回测/扫描,不构成投资建议。
