# Changelog

本项目所有用户/接入方可感知的变更都记录在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## 如何维护

每次 PR 如果有以下情况之一，都要在 `[Unreleased]` 段加一行：

- 新增功能 → `### Added`
- 行为变更 / 兼容性影响 → `### Changed`
- 修复用户感知的 bug → `### Fixed`
- 删除/下线 → `### Removed`
- 安全相关 → `### Security`
- 仅文档 / 内部重构（不影响用户） → 不必登记

cut 版本时把 `[Unreleased]` 整体移到一个带日期的版本号下，再开新的 `[Unreleased]`。

---

## [Unreleased]

### Added

- **KHunter A 股日报完整 pipeline**(11 策略 + 横截面个股因子评分 + 多专家辩论 + 多文件交付):
  - 新 skill `khunter-a-share-selector/`(SKILL.md + scripts/khunter_weekly_scan.py)— 11 种 KHunter 策略蒸馏脚本
  - 新 Python 包 `khunter_x/`:
    - `scanner.py` 包装 weekly_scan.py
    - `stock_factors.py` 横截面个股因子(9 因子:20/60 动量 + 波动率 + 换手代理 + 量价相关 + 资金流代理 + MA20 相对 + 当日活跃 + KH 策略权重)+ 数据质量评级 + 风险惩罚
    - `debate.py` Top10 多专家辩论(单次 LLM 调用 / 7 类专家 + 游资视角 / 并行,可配 max_concurrent)
    - `delivery.py` 生成 outputs/<日期>-khunter-a-share-daily/{report.md, candidates.json/csv, rankings.json/csv, generation.log, feishu_status.json}
    - `service.py` 编排 + sync/async 两种入口
  - 新 MCP tool `run_khunter_daily_pipeline(days, max_symbols, top_n, enable_debate)` 给 Claude Desktop/Code 用
  - 飞书自然语言入口:`KHunter` / `K-Hunter` / 任意 11 策略中文名(`底部趋势拐点` / `仙人指路` / `涨停回马枪` 等)触发
  - 走标准 `_publish_terminal_run` 管道:卡片(精简)+ 飞书 docx(完整 report.md)+ Notion + outputs/ 多文件落 STATE_DIR
  - **工作日 cron**:`_daily_khunter_scheduler` 进程内 asyncio task,默认北京时间 08:00 推送,跳过周末(`DAILY_KHUNTER_WEEKDAYS_ONLY=true`)
  - 11 策略 + 权重:涨停横盘/仙人指路(70)、底部趋势拐点/阻力位突破/W底/多金叉共振/涨停回马枪/强势洗盘弱转强(50)、趋势加速拐点/启明星/多方炮(30)
  - 因子总分映射 0-10 分,base = 5 + z·1.5,再扣风险惩罚(涨幅>50% / 高波动 / 流动性差)
  - 数据质量等级 A/B/C(< 30 K线 = C 不评分, < 60 = B, 否则 A)
  - 兜底:scanner 失败 KHunterScanError;debate 失败不影响其他 stock;publish 失败仍保留 outputs/ + 错误写 feishu_status.json
  - 新 env vars(全部可选):`KHUNTER_HARD_TIMEOUT=600`、`KHUNTER_SCRIPTS_DIR`、`KHUNTER_WORKSPACE`、`DAILY_KHUNTER_CHAT_ID`、`DAILY_KHUNTER_HOURS=8`、`DAILY_KHUNTER_WEEKDAYS_ONLY=true`

- **每日定时热点事件推送**:
  - 新增 `_daily_hot_event_scheduler` 进程内 asyncio 后台任务,启动时 `asyncio.create_task` 拉起
  - 默认北京时间 **10:00 + 15:00** 各推一条:bot 从近期新闻流挑当日最值得做产业链拆解的事件,跑标准 hot_event_research 分析,推送到指定群(卡片 + 飞书 docx + Notion)
  - 配置 env vars:`DAILY_HOT_EVENT_CHAT_ID`(必填,空则禁用)、`DAILY_HOT_EVENT_HOURS=10,15`、`DAILY_HOT_EVENT_TZ_OFFSET=8`
  - `pick_daily_event_name()` 让 LLM 按优先级(政策 > 技术突破 > 龙头动作 > 板块异动)从新闻流挑题,失败 fallback 到「今日 A 股热点」
  - 单次 push 失败不影响下次调度;容器重启时间窗 ±1min 卡在调度点会漏该次,不补推

- **`hot_event_research` 包 + MCP tool + 飞书自然语言入口**(auto-researcher 风格 A 股热点事件深度分析):
  - 新 Python 包 `hot_event_research/`:`run_hot_event_analysis(event_name)` → markdown 报告
    - `service.py` 编排:LLM 路由(抽 entity/keywords)→ 抓东财全球资讯 + 关键词过滤 → LLM 主分析按结构化 schema 输出
    - `data_sources.py` 单独 HTTP 抓取(零新依赖,只用 httpx)
  - 新 MCP tool `run_hot_event_research(event_name)` — 暴露给 Claude Desktop/Code
  - 飞书自然语言触发:`热点分析:XXX` / `auto-researcher XXX` / `题材拆解 XXX` / `事件分析:XXX` / `催化分析` / `产业链分析`
  - 输出 schema:事件概况 / 核心题材逻辑(催化 + 炒作路径)/ 产业链表 / 重点个股 / 预期差 / 风险提示 / 数据证据 / 免责
  - 走标准 `_publish_terminal_run` 管道:卡片(简洁)+ 飞书 docx(完整,落投研文件夹)+ Notion,preset=event_driven_task_force → macro_theme 模板
  - **兜底**:路由失败 fallback、新闻抓取失败降级到「无数据上下文」纯框架分析、主分析失败抛 HotEventError、整体 180s 硬超时(`HOT_EVENT_TIMEOUT` env 可调)、数据稀薄时 LLM 在「数据证据」段明确告知
  - 个股代码强制 6 位 + .SH/.SZ 格式,prompt 明确禁止 hallucinate
  - run_id 前缀 `hotevent-{ts}-{hex}` 便于日志检索

- **`sequoia_x` 包 + MCP tool + 飞书自然语言入口**(A 股 Sequoia-X 6 策略扫描):
  - skill `sequoia-x-a-share-selector/` 入仓(SKILL.md + references/strategy-map.md + agents/openai.yaml + scripts/sequoia_x_weekly_scan.py + scripts/sequoia_x_monthly_backtest.py)
  - 新 Python 包 `sequoia_x/` 包装 scripts 为可 import 的 `run_weekly_scan(days, max_symbols, ...)`,SkillsLoader 动态解析 scripts 位置
  - 新 MCP tool `run_sequoia_x_scan` — 暴露给 Claude Desktop / Code
  - 飞书自然语言触发:消息含 `sequoia` / `红杉` / `海龟突破` / `RPS 突破` / `涨停洗盘` / `高位窄幅旗形` 等关键词自动识别 → 跑扫描 → 走 `_publish_terminal_run` 推回卡片 + 飞书文档 + Notion
  - 6 策略:MaVolume / TurtleTrade / HighTightFlag / LimitUpShakeout / UptrendLimitDown / RpsBreakout
  - **极端情况兜底**:scripts 内部已有 3 次 retry + 东财→新浪 fallback + 全 per-stock try/except。wrapper 额外加:`SequoiaScanError` 翻译三种硬失败(无股池 / 全空 panel / 无最近交易日)、整体 `asyncio.wait_for=300s` 硬超时(`SEQUOIA_HARD_TIMEOUT` env 可调)、per-chat in-flight 去重防止重复触发、partial coverage(error_symbols)透传到卡片
  - Dockerfile 加 `COPY sequoia_x/ /app/sequoia_x/`,零新 pip 依赖(pandas + urllib 已有)

- **`factor_analysis` 包 + 2 个 MCP tool**(A 股行业因子量化研究):
  - `run_a_share_industry_factor_research` MCP tool — 东财行业板块行情 + QuantsPlaybook 风格量价/择时因子 + LightGBM 预测 + 近期回测 + 研报热度,输出 markdown 报告或 JSON
  - `validate_a_share_february_factor_model` MCP tool — 2 月训练 / 3-5 月验证(no-lookahead)
  - 飞书自然语言触发:消息匹配「行业/板块 × 因子/量化/lightgbm/回测/轮动/预测」正则自动跑因子分析,结果直接推回(走 `_feishu_handle_factor_research` 文本长消息分块发送,不走 swarm 路径)
  - Dockerfile 加 `libgomp1` 系统包 + `pandas/numpy/scikit-learn/lightgbm` pip 依赖,`COPY factor_analysis/ /app/`
- **3 个新 skill**:
  - `a-share-expert-backtest` — A 股专家历史盲测 / 胜率收益评估(含 references / agents / scripts 子目录)
  - `michael-vibe-trading-ops` — 维护和扩展本仓库的项目操作技能(meta skill)
  - `pdf-loader` — PDF 加载与文本抽取(用于研报 / 年报 / SEC filing)

### Fixed

- **LLM JSON 调用全面健壮化**:新环境变量,指向 Railway Volume 挂载路径(如 `/app/data`)。设置后,swarm runs(`.swarm/runs/{id}/`,包含报告 + feishu_meta + events)+ OAuth DCR registry(`oauth_clients.json`)落到 Volume,deploy 之间持久保留。不设时退化到 ephemeral 老行为。启动时打 `[boot] STATE_DIR active: ...` 日志。

### Changed

- **可观测性大幅增强** — Feishu 消息处理链路的每个分支决策点都打 log,不再 silent return:
  - `feishu_events` webhook 入口:`[feishu/webhook] event_type=X event_id=Y src=IP`,以及 url_verification / 非 im.message.receive_v1 / 未知 body shape 等都明确 log
  - `_feishu_handle_message`:接收日志(chat/sender/msg_type)+ 每个 silent return(missing chat_id / msg_type 非 text / content JSON 解析失败 / text 为空)都 log 原因
  - `_llm_route`:input log + output log + reject 原因(unknown action / unknown preset / no target / no run_id)分别 log
  - dispatcher:`[feishu/dispatch] action=X preset=Y target=Z gurus=...`,regex fallback 也 log
  - 之前用户发指令但没结果时无法定位是哪一环挂了 → 现在每一环都有迹可循

### Security (重要 — 行为有 breaking 变化)

- **OAuth 加固**:
  - `/register` 之前只回显 `redirect_uris` 不做服务端保存,任何人都能构造任意 client_id + 任意 redirect_uri 走授权流。现在 client_id 由服务端发号 + `redirect_uris` 写入 `/tmp/oauth_clients.json` allowlist。
  - `/authorize`(GET+POST)校验 `client_id` 已注册,`redirect_uri` 在该 client 的 allowlist 中(精确匹配,无 prefix / 模式)。
  - `redirect_uri` 只允许 `https://` 或 `http://localhost`/`127.0.0.1`(本地 client)。其他 scheme 全部拒绝。
  - 授权码从「自签 JWT(可重放)」改为「服务端一次性 opaque code」,`/token` 兑换时 pop+delete,内存里同时清扫过期 code。
  - Refresh token 仍是 JWT,但兑换时:校验 `typ==refresh`、client_id 仍在 registry、若请求带 client_id 必须与 token 内的一致。

- **飞书 webhook 强制鉴权**:
  - 启用了 `LARK_APP_ID`/`LARK_APP_SECRET` 但**没有**配 `FEISHU_VERIFICATION_TOKEN` 或 `FEISHU_ENCRYPT_KEY` 时,`/feishu/events` 路由不注册 + 启动 stderr 警告(防裸跑)。
  - `/feishu/events` 每个 POST 都强制校验 token(`hmac.compare_digest`),没匹配返 403。
  - 新增 `FEISHU_WEBHOOK_MAX_BYTES`(默认 64KB)body 上限,`FEISHU_WEBHOOK_RATE_LIMIT`(默认 30 req/IP/60s)防刷。

- **飞书 run 权限隔离**:
  - `list_runs` / `status` / `cancel_run` / `查一下 latest` 都按 `feishu_meta.json` 的 `receive_id`(chat_id)+ `sender_open_id` 做 authz。
  - 群 A 不能查 / 取消 / 看群 B 的 run;私聊看不到他人 run。
  - 通过 MCP 工具直接发起(无 feishu_meta)的 run 对所有 Feishu chat 不可见,只能从 `/_debug/republish` 走管理员通道补发。

- **`/_debug/*` 收紧**:
  - 新增 `ENABLE_DEBUG_ENDPOINTS`(默认 `false`)总开关 + `ADMIN_AUTH_TOKEN`(独立于 `MCP_AUTH_TOKEN`)凭据。两者都设才注册路由,生产默认安全。
  - `AuthMiddleware` 拿到 `/_debug/*` 路径时强制要求 `ADMIN_AUTH_TOKEN`,不接受 `MCP_AUTH_TOKEN` 或 OAuth access token(防 MCP token 泄露顺带打开运维通道)。
  - 副作用端点 (`purge-run` / `republish` / `fix-historic-doc-share`) 强制 `methods=["POST"]`。
  - `fix-historic-doc-share` 的 `entity` 参数白名单校验(`tenant_readable` / `tenant_editable` / `anyone_readable` / `anyone_editable` / `closed`),拒绝任意值。

### Fixed

- **httpx timeout monkey patch 彻底移除**:之前全局 cap `httpx.Client.read=60`,但 `_deepseek_json_call` 需要 90s 给 DeepSeek-v4-pro reasoning model 长输出留时间,被悄悄改成 60s 偶发 ReadTimeout。移除全局 patch + 每个 `httpx.Client(...)` 调用点显式声明 timeout(已审计 7 处全合规)+ `_lifespan` 启动断言一个 `read=90` AsyncClient 实际拿到的就是 90.0(非任何 import 副作用改的)。

### Fixed

- **LLM JSON 调用全面健壮化**:抽出共享 helper `_deepseek_json_call`,把 summarizer / guru route / guru voice 三个 site 统一收口。修复:
  - `max_tokens` 全部上调留够 reasoning + output (summarizer 4000→6000,route 1500→3000,voice 1500→3000)
  - 显式检测 `finish_reason==length` 短路 parse 重试(之前会浪费 3 次重试解析必然失败的截断 JSON)
  - 不再 fallback 到 `reasoning_content`(那是 CoT 思维链不是 JSON,导致老 router 偶发 "no JSON in response" 假阳性)
  - read timeout 60→90s(reasoning model 长输出经常 60s+)
  - 错误日志带 `content_len` / `finish_reason` / content snippet 上下文,便于诊断
- **silent partial degradation 显式化**:游资视图为空时 `_publish_terminal_run` 现在显式 log `[publish] guru views EMPTY ... 卡片将不带 游资速看`,避免静默漏环
- **CONTRIBUTING PR checklist 加硬规则**:LLM prompt / JSON schema 改动必须重算 max_tokens + 本地 smoke-test (`/_debug/republish skip_feishu_card=true`),不准直接 push

### Added

- **F 方案:docx 写入用户云盘文件夹**:新增 `FEISHU_DRIVE_FOLDER_TOKEN` 环境变量。若设置,bot 创建 docx 时带 `folder_token` 参数,文档落到用户云盘指定文件夹下,自动继承文件夹的「分享/可见」权限。绕开 `drive:drive` 权限难题。前提:文件夹所有者要在飞书 UI 把该文件夹分享给 bot 并给「可编辑」权限。
- **飞书 docx 自动开链接共享**：bot 新创建的每个 docx 默认设为「组织内可阅读」(`tenant_readable`),群成员可直接打开链接而无需申请权限。由 `FEISHU_DOC_SHARE_ENTITY` 环境变量控制(可改 `tenant_editable` / `anyone_readable` / `closed`)。需要 bot app 启用 `docs:doc` 或 `drive:drive` 权限并发新版本激活。
- **`/_debug/list-feishu-chats`**：列出 bot 所在的所有群,返回 chat_id,用于运维操作
- **`/_debug/republish`** (POST):用 final_report 文本 + chat_id 构造合成 Run 触发完整 publish,不依赖磁盘 Run 状态,用于补发被 deploy 擦盘丢失的报告

---

## [0.2.0] — 2026-05-23

### Added

- **multi-guru 游资观点 addendum**：A 股 `stock_decision` 类报告下方会附加 1-2 位游资视角锐评，10 位 voice 池由 `_route_gurus` LLM 路由自动选互补派别
- **飞书指令支持指定游资**：自然语言里说"用陈小群看 茅台"、"控回撤派分析 002594"等，LLM router 抽 `gurus` 字段透传到 publish，绕过自动路由
- **9 个新游资 skill**：北京炒家 / 陈小群 / 92 科比 / 涅盘重升 / 一瞬流光 / 采莲路 / 小睿睿 / 华东大导弹 / 归因（合 xiao-eyu 共 10 位，覆盖模式派/龙头/情绪/资金流/接力/控回撤/进攻/低频/资讯/理解力 10 个派别）
- **SIGTERM 优雅退出**：Railway 部署 / 重启时通知所有 in-flight 飞书 chat 「服务部署重启，本次分析被中断，请重新发送原指令」，不再 silent failure
- **新环境变量**：`GURU_VIEW_MODE`（auto/fixed/off）、`GURU_VIEW_MAX`（1-3）

### Changed

- **Dockerfile** skill 安装从硬列改为 `for d in /tmp/extra_skills/*/` 循环 + `SKILL.md` 存在性校验，以后加 skill 不用改 Dockerfile
- **CI workflow** 同步改为「每个 skill 有 SKILL.md」单点检查（之前要求每个名字在 Dockerfile 显式出现）
- **xiao-eyu skill** 升级到 v2，强调「主线每天变 + 前瞻分析必须先取实时数据」的硬约束

### Removed

- 单 voice 的 `_generate_youzi_view` / `_XIAO_EYU_SKILL_TEXT` / `summary["youzi_view"]`（被 multi-guru 替代）

---

## [0.1.0] — 2026-05-23

### Added

- **初始迁移**：从本地 `~/vibe-trading-mcp/deploy/` 迁到共享 GitHub repo `shao60533/michael-vibe-trading`，main 分支接 Railway 自动部署
- **MCP over SSE**：`/sse` 端点，静态 Bearer + OAuth 2.1 PKCE，供 Claude Desktop / Code / 移动端 Custom Connector 使用
- **飞书 Bot**：`/feishu/events` webhook，LLM router 解析自然语言指令（28 个 swarm preset 自动匹配），结果以飞书互动卡片 + 飞书 docx + Notion 三处同步推送
- **xiao-eyu addendum**：A 股 `stock_decision` 报告下方附加小鳄鱼一位游资视角
- **a-stock-data skill**（A 股 28 端点：mootdx TCP 行情 + 腾讯 + 东财 + 同花顺 + 巨潮 + ...）
- **global-stock-data skill**（美港股 18 端点：新浪 + 腾讯 + 东财 + Yahoo + SEC）
- **工程规范**：README / LICENSE (MIT) / CONTRIBUTING / .env.example / GitHub Actions CI（4 项检查）

### Fixed

- **mootdx 依赖冲突**：`httpx[socks]<0.26` vs vibe-trading-ai 新 httpx 死循环回溯撞 build daemon 超时；改 `pip install --no-deps mootdx pytdx` 跳过冲突依赖
- **飞书 SSE 双挂载 307 redirect**：FastMCP 挂在 root 而非 `/sse`，避免双层 mount
- **httpx LLM stream 卡死**：monkey-patch `httpx.Client.__init__` 把 read timeout 封顶 60s
- **DeepSeek-v4-pro 摘要偶发空内容**：3 次重试 + 升温
- **Feishu docx 共享报错 99991672**：要求 `drive:drive` 权限（启用后需在飞书开放平台「版本管理与发布」发新版激活）
- **Notion `parent_database_id` 模式 vs `parent_page_id` 模式**：代码分两路兼容
- **Feishu 事件 retry 导致 swarm 重发**：`event_id` 级 dedup + `(chat_id, target)` 级 in-flight 拦截
- **容器重启丢 in-memory `_feishu_pending`**：每 run 落 `feishu_meta.json`，lifespan startup 时 `_restore_feishu_pending_from_disk()`

[Unreleased]: https://github.com/shao60533/michael-vibe-trading/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/shao60533/michael-vibe-trading/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shao60533/michael-vibe-trading/releases/tag/v0.1.0
