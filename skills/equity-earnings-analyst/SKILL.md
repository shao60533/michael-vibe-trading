---
name: equity-earnings-analyst
description: >-
  「个股财报点评」方法论 skill——对已覆盖/关注的 A 股个股，在定期报告（一季报/中报/三季报/年报、业绩预告 forecast、业绩快报
  express）发布后 1-2 天内，产出机构级（中金/中信/高盛口径）的 8-12 页财报点评：先做超预期/低预期（beat/miss）拆解、关键
  指标与分部/产品/区域拆分、毛利率与指引（管理层展望）分析，再更新盈利预测（新旧对比 + 变动原因）与投资逻辑，最后给评级与
  目标价，配 1-3 张汇总表 + 8-12 张图，所有数据强制标注一手来源（巨潮 cninfo 公告 / 交易所 / 东财·同花顺研报 / Tushare）。
  当用户要：点评/分析某只 A 股的财报或季报年报（"点评一下 XX 的三季报/年报"）、做"业绩超预期/低预期"判断、更新某股盈利
  预测与目标价、写业绩预告/快报快评、或做 post-earnings/盘后财报报告时，都应调用本 skill。它是 value-chain-teardown
  （产业链全栈拆解）的"个股财报"子模块，常被其在 Step3/Step4 调用以验证单个环节龙头的盈利质量。本 skill 强制要求：在给出
  任何"超预期与否/贵不贵/目标价"判断前，先通过 a-stock-data skill / Tushare MCP / Vibetrading-Michael MCP / IBKR MCP /
  Web 拉取最新定期报告、一致预期与实时行情估值（第零步），对一切推断性数字标注"方向性/示意"，并使用人民币（元）计价。
---

# 个股财报点评 (Equity Earnings Analyst · A 股版)

对**已覆盖/已关注的 A 股个股**，在定期报告发布后产出机构级 **财报点评报告**（中金 / 中信 / 高盛口径）。

> 本 skill 由 Anthropic `financial-services` 的 `earnings-analysis` skill 适配而来，方法论模板完整保留在 `references/`，
> 但所有数据源、披露口径、计价单位已替换为 A 股语境。阅读 `references/*.md` 时，请按下方「美股→A 股映射」把美股口径换成 A 股口径再执行。

**核心特征：**
- **篇幅**：8-12 页 / 3,000-5,000 字（点评，不是 30+ 页的首次覆盖深度报告）
- **表格**：1-3 张汇总表（关键指标，**不放完整三大报表**，假设读者已看过深度报告）
- **图表**：8-12 张（季度营收/归母净利/毛利率趋势、分部、超预期幅度、预测调整、估值）
- **时效**：定期报告/预告/快报发布后 1-2 天内
- **焦点**：**只讲"新增信息"**——超预期与否、盈利预测调整、投资逻辑变化，不复述公司背景
- **计价**：人民币（元/亿元），不要用美元

## 何时使用

- "点评一下 XX 的三季报 / 年报 / 中报"
- "XX 业绩超预期吗 / beat or miss"
- "更新一下 XX 的盈利预测和目标价"
- "XX 发了业绩预告/快报，快评一下"
- value-chain-teardown 在 Step3/Step4 需要验证某环节龙头的盈利质量时调用本 skill

**不要用于：** 首次覆盖/深度报告（→ 用更重的覆盖 skill）；纯产业链拆解（→ 用 [[value-chain-teardown]]）；只要一句话快评（→ 直接回答即可）。

## 第零步（强制）：拉取最新一手数据，禁止用训练数据里的旧财报

A 股定期报告有法定披露窗口，训练数据必然滞后。给出任何 beat/miss、预测、目标价之前，**必须**先取最新数据：

1. **最新定期报告 / 预告 / 快报原文** → `a-stock-data` skill（巨潮 cninfo 公告检索 + 三表）；或 Tushare MCP：`income` / `balancesheet` / `cashflow` / `fina_indicator`（财务指标）、`forecast`（业绩预告）、`express`（业绩快报）、`disclosure_date`（披露日历，确认是否真的已披露）。
2. **一致预期（市场 consensus）** → `a-stock-data` 的同花顺一致预期 EPS / 东财研报三年 EPS；或 Tushare `report_rc`（券商研报盈利预测）。用于算 beat/miss 的分母，**必须带日期**。
3. **实时行情 / 估值（PE/PB/总市值/换手）** → Vibetrading-Michael MCP；或 Tushare `daily_basic`；或 `a-stock-data` 腾讯/东财接口；（若个股有港股/美股对应或用户用 IBKR 持仓）IBKR MCP `search_contracts`→`get_price_snapshot`。
4. **确认日期**：写下今天日期与报告披露日期，确认报告在最近一个披露季内；预告/快报与正式报告口径不要混用。

> 取不到一手数据时，明确说明缺口、标注数字为"方向性/示意"，不要编造精确值。

## 美股 → A 股口径映射（读 references 时按此替换）

| references 里的美股口径 | 用 A 股对应 |
|---|---|
| 10-Q / 10-K filing、SEC、EDGAR | 一季报/中报/三季报/年报、业绩预告(forecast)/快报(express)；**巨潮资讯网 cninfo** 公告原文；交易所(上交所/深交所/北交所) |
| EDGAR viewer 超链接 | 巨潮 cninfo 公告页 / 交易所公告页 超链接 |
| Bloomberg / FactSet consensus | Wind / 东财 / 同花顺一致预期 / Tushare `report_rc` 券商预测（带日期） |
| Earnings call transcript / 8-K | 业绩说明会纪要 / 投资者关系活动记录表（巨潮）/ 电话会纪要 |
| Investor presentation | 公司业绩交流 PPT / 路演材料（如有） |
| USD / $ | 人民币 元 / 亿元 |
| GAAP/Non-GAAP | 会计准则口径；区分扣非 vs 归母净利润（**重点关注扣非净利润**）；同比/环比 |

**A 股特有、点评必查：** 归母 vs 扣非净利润、经营性现金流、季度环比（QoQ）与同比（YoY）、应收/存货变化、分红预案、限售解禁、是否 ST/问询函、商誉减值。

## 引用与来源（强制）

每张图表、每个关键数字都要带**具体来源 + 可点击超链接**：
- ✅ 定期报告/预告/快报（披露日期 + 巨潮 cninfo 链接）
- ✅ 一致预期来源（Wind/东财/同花顺/券商研报，带日期）
- ✅ 业绩说明会/投关纪要（如有，带日期）
- ✅ 上一期指引/预测（用于新旧对比）

报告末尾附"来源与参考"清单，所有链接为可点击超链接（指向巨潮/交易所/券商研报，**不要**用 SEC/EDGAR）。

## 五阶段工作流

1. **数据采集**（先做第零步）— 详见 `references/workflow.md`（按上表把搜索目标换成巨潮/Tushare/东财）。
2. **分析** — 各关键指标 beat/miss、分部/产品/区域拆分、毛利率与指引、更新盈利预测。详见 `references/workflow.md`。
3. **出图（8-12 张）** — 季度营收/归母净利/扣非/毛利率趋势、分部、超预期幅度、预测调整、估值。本服务端用 **Pillow**（已装 fonts-noto-cjk，**红涨绿跌**）或 matplotlib。详见 `references/workflow.md`。
4. **成稿（8-12 页）** — 页面结构见 `references/report-structure.md`：P1 摘要(评级+目标价+要点) / P2-3 业绩拆解 / P4-5 关键指标与指引 / P6-7 投资逻辑更新 / P8-10 估值与预测 / P11-12 附录。
5. **质检与交付** — 见 `references/best-practices.md`（核对数据、来源、时效）。

## 输出规范

- **主交付**：8-12 页报告。命名：`<公司>_<报告期>_财报点评.pdf`（例：`贵州茅台_2025三季报_财报点评.pdf`）。
- **格式**：服务端无 Word 环境，输出 **PDF（WeasyPrint / playwright print）或可滚动 HTML**（与 value-chain-teardown 的至行·Zenith 视觉系统一致），不要默认 DOCX/Times New Roman；正文用中文字体（Noto/思源/Hiragino）。
- 含 8-12 张内嵌图、1-3 张汇总表、完整可点击来源清单。

## 资源

- `references/workflow.md` — 数据采集/分析/出图/成稿的分阶段细则（美股口径，按上表替换）。
- `references/report-structure.md` — 逐页模板与表格/格式要求。
- `references/best-practices.md` — 好/坏标题示例、技巧、常见错误、质检清单。

## 依赖

- Python（matplotlib/pandas/seaborn 或服务端 Pillow）出图。
- PDF/HTML 出稿（WeasyPrint/playwright），**非** DOCX。
- 数据：`a-stock-data` skill、Tushare MCP、Vibetrading-Michael MCP、（可选）IBKR MCP。

## 组合使用

- [[value-chain-teardown]]：产业链全栈拆解的统御层；本 skill 是其"个股财报"子模块。
