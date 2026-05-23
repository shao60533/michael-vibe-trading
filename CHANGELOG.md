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
