# michael-vibe-trading

A 股 / 美港股 / 加密 多市场 AI 投研服务。基于 [vibe-trading-ai](https://pypi.org/project/vibe-trading-ai/) swarm 的 28 个分析师 preset，部署在 Railway 上对外提供：

- **MCP over SSE** — 供 Claude Desktop / Code / 移动端 Custom Connector 使用，支持静态 Bearer + OAuth 2.1 PKCE
- **飞书 Bot Webhook** — 自然语言对话触发分析，结果以飞书互动卡片 + 飞书云文档 + Notion 三处同步推送
- **数据 Skill** — `a-stock-data`（A 股 28 端点）、`global-stock-data`（美港股 18 端点）
- **游资观点 Skill** — 10 位 A 股新生代游资 voice，分析 A 股个股时 LLM 自动选 1-2 位互补派别给观点；也可在飞书指令里指定（"用陈小群看 茅台"）

## 架构

```
┌────────────────┐         ┌──────────────────────────────────┐
│ Claude Desktop │ ─SSE──► │                                  │
│  / Code / 移动 │         │  vibe-trading-mcp (Railway)      │
└────────────────┘         │                                  │
                           │  ┌──────────────────────────┐    │
┌────────────────┐         │  │ FastMCP /sse + OAuth     │    │
│ 飞书群 / 私聊  │ ─event─►│  │ Feishu /feishu/events    │    │
└────────────────┘         │  │ Notion sync              │    │
                           │  │ SwarmRuntime + Skills    │    │
                           │  └──────────────────────────┘    │
                           └──────────────────────────────────┘
                                       │           │
                                       ▼           ▼
                                ┌────────────┐ ┌──────────┐
                                │ DeepSeek   │ │ 飞书 docx│
                                │ v4-pro     │ │ + Notion │
                                └────────────┘ └──────────┘
```

## 部署

GitHub main 分支已对接 Railway，**push 即部署**。

### 一键发布

```bash
git push origin main
```

Railway 会自动 build Dockerfile → healthcheck `/healthz` → 切流量。约 3-5 分钟生效。

### 手动 CLI 部署（可选 fallback）

```bash
railway login
railway link --project fb06b159-f913-4227-8cb9-fbd689e5d1b4 --environment production --service vibe-trading-mcp
railway up
```

### 查看部署状态 / 日志

```bash
railway deployment list --service vibe-trading-mcp | head -5
railway logs --service vibe-trading-mcp           # 运行时日志
railway logs --service vibe-trading-mcp --build   # build 日志
```

## 环境变量

完整列表见 [`.env.example`](.env.example)。**Railway 上配置**，不要 commit 真实值。

| 必填 | 变量 | 用途 |
|------|------|------|
| ✅ | `MCP_AUTH_TOKEN` | MCP Bearer 共享密钥 + OAuth /authorize 口令(**勿与 ADMIN 共用**) |
| ✅ | `DEEPSEEK_API_KEY` | swarm LLM 调用 + 摘要 + 游资视角 |
| △ | `LARK_APP_ID` / `LARK_APP_SECRET` | 飞书 bot 凭据 |
| △ | `FEISHU_VERIFICATION_TOKEN` | **配了 LARK_APP_* 就必须配此项**(或 `FEISHU_ENCRYPT_KEY`),否则 `/feishu/events` 不注册 |
| △ | `FEISHU_WEBHOOK_MAX_BYTES` / `FEISHU_WEBHOOK_RATE_LIMIT` | webhook body 上限 / per-IP 速率(默认 64KB / 30 req/60s) |
| △ | `NOTION_API_KEY` + (`NOTION_DATABASE_ID` 或 `NOTION_PARENT_PAGE_ID`) | Notion 同步（不填则跳过） |
| △ | `GURU_VIEW_MODE` | `auto`（默认，LLM 路由选游资）/ `fixed:n1,n2`（固定）/ `off`（关闭） |
| △ | `GURU_VIEW_MAX` | 每次最多几位游资观点（1-3，默认 2） |
| 🔒 | `ADMIN_AUTH_TOKEN` | `/_debug/*` 运维端点专用凭据,**与 MCP_AUTH_TOKEN 独立**;不设则所有 debug 路由不注册 |
| 🔒 | `ENABLE_DEBUG_ENDPOINTS` | `true` 才注册 `/_debug/*`(默认 false,生产安全) |
| 💾 | `STATE_DIR` | Railway Volume 挂载点(如 `/app/data`)。设了 + 挂 Volume → swarm runs + OAuth registry 跨 deploy 持久化 |

## 状态持久化(Railway Volume)

默认 Railway 容器 ephemeral,每次 deploy 重建文件系统 → swarm 已完成的报告 / OAuth 注册的 client / feishu_meta 全部清零。要长期保留:

1. Railway dashboard → service `vibe-trading-mcp` → **Volumes** → **Add Volume**,挂载路径填 `/app/data`(或任意路径)
2. 同一 service 的 **Variables** 加 `STATE_DIR=/app/data`(和 Volume 路径一致)
3. 下次 deploy 后生效:启动日志会出现 `[boot] STATE_DIR active: /app/data (swarm runs → /app/data/.swarm/runs)`

不设 `STATE_DIR` 时退化到老行为(写 site-packages ephemeral 目录),每次 deploy 清零。

## 安全模型

- **MCP / OAuth**:`/sse` 路径要求 Bearer = `MCP_AUTH_TOKEN`(静态)或经过 OAuth 流的 access token。DCR 动态注册客户端时,服务端保存 `client_id` 和 `redirect_uris` allowlist;`/authorize` 严格校验 `redirect_uri` 在客户端 allowlist 中,且 scheme 必须是 https(`localhost`/`127.0.0.1` 例外)。授权码是服务端一次性 opaque code(非 JWT),`/token` 兑换后立即失效。Refresh token 绑定 `client_id`,刷新时校验 client 仍在 registry。
- **飞书 webhook**:`/feishu/events` 收到的每个 POST 必须带匹配的 `FEISHU_VERIFICATION_TOKEN`(或 encrypt key);否则 403。启用了 LARK_APP_* 但没设 token 时,路由根本不注册。body 有 size 上限 + per-IP 速率。
- **飞书 run 权限隔离**:`list_runs` / `查一下 latest` / `取消 latest` / 显式 run_id 查询都按 `feishu_meta.json` 里的 `receive_id` + `sender_open_id` 做授权 — 群 A 看不到群 B 的 run,私聊看不到他人的 run,通过 MCP 直接发起(无 feishu_meta)的 run 对所有 Feishu chat 不可见(只能从 `/_debug/republish` 走管理员通道)。
- **`/_debug/*`**:默认禁用。生效需要 `ENABLE_DEBUG_ENDPOINTS=true` + `ADMIN_AUTH_TOKEN` 都设。mutating 端点(`purge-run` / `republish` / `fix-historic-doc-share`)强制 POST。`fix-historic-doc-share` 的 `entity` 参数有白名单。

## 飞书使用指北

bot 用 LLM router 解析自然语言指令，常用形式：

| 指令 | 行为 |
|------|------|
| `分析 茅台` / `看下 NVDA` | 默认 `investment_committee` preset |
| `茅台技术面` / `英伟达 K 线` | 自动切 `technical_analysis_panel` |
| `茅台财报` / `分析下苹果季报` | 切 `earnings_research_desk` |
| `半导体板块` / `光模块怎么样` | 切 `sector_rotation_team` |
| `BTC 链上活跃度` | 切 `crypto_research_lab` |
| `用陈小群看 茅台` | A 股分析 + 强制只用陈小群一位游资 |
| `分析 002594，用北京炒家和小鳄鱼` | 强制用这两位游资 |
| `控回撤派看 隆基` | 派别名 LLM 自动映射成 xiang-cheng-cai-lian-lu |
| `最近跑过哪些分析` / `失败的 run` | 列历史 |
| `查一下 latest` / `查一下 swarm-xxx` | 拉报告 |
| `取消 swarm-xxx` / `把当前在跑的干掉` | 终止 |
| `怎么用` / `有哪些 preset` | help |

完整支持的 28 个 preset 见 [`mcp_launcher.py`](mcp_launcher.py) 顶部 `KNOWN_PRESETS` 集合，或在飞书发 `presets`。

## 游资观点 (10 voice multi-guru)

A 股 `stock_decision` 类 preset 的报告下方会附加「🐊 游资观点」段落，由 1-2 位互补派别的游资从他们的视角给 3-5 句锐评：

| skill 名 | 中文 / 别名 | 派别 |
|---|---|---|
| `xiao-eyu` | 小鳄鱼 | 理解力派 |
| `bei-jing-chao-jia` | 北京炒家 | 模式派 |
| `chen-xiao-qun` | 陈小群、群神 | 龙头信仰派 |
| `jiu-er-ke-bi` | 92 科比 | 情绪周期派 |
| `nie-pan-chong-sheng` | 涅盘重升、升大 | 资金流派 |
| `yi-shun-liu-guang` | 一瞬流光、光神 | 高位接力派 |
| `xiang-cheng-cai-lian-lu` | 采莲路、川哥 | 控回撤派 |
| `xiao-rui-rui` | 小睿睿、睿神 | 进攻派 |
| `hua-dong-da-dao-dan` | 华东大导弹 | 低频狙击派 |
| `gui-yin` | 归因 | 资讯派 |

**路由逻辑**（mcp_launcher.py 的 `_route_gurus` + `_generate_single_guru_view`）：

1. 用户没指定 → LLM 看 10 位画像 + 报告片段，选 1-2 位互补派别返回 JSON
2. 用户指定 → 白名单校验 + cap `GURU_VIEW_MAX`，跳过路由
3. 非 A 股短线场景（美股/港股/加密/宏观）→ 路由返回空，不附加
4. 渲染：飞书卡 / 飞书 docx / Notion 三处都用同一份 voice，每位单独子段

## 部署可靠性

- **健康检查**：`/healthz` 返回 200 即视为存活
- **优雅重启**：容器收 SIGTERM（Railway 部署 / 重启）时，`_lifespan` 的 finally 阶段会扫所有 in-flight `_feishu_pending`，给每个原 chat 发「⚠️ 服务部署重启，本次分析被中断，请重新发送原指令」。20s 超时保护，30s 内必须 exit
- **磁盘易失**：Railway 不挂 Volume 时容器文件系统是 ephemeral，`/usr/local/.../mcp_server/.swarm/runs/` 每次 deploy 都会重建。**部署时进行中的 swarm 分析会丢失**（线程死 + 状态盘擦），用户需重发指令。下一步要加 Railway Volume 保留 run 历史
- **重启恢复**：`_restore_feishu_pending_from_disk()` 在 lifespan startup 阶段扫盘上 `feishu_meta.json`，把进度恢复到 in-memory dict。如果上次 SIGTERM 前 run 已 completed 但还没 publish，重启后会补推

## 文件布局

```
.
├── Dockerfile               # Python 3.11 + vibe-trading-ai + mootdx (--no-deps) + pytdx
├── mcp_launcher.py          # 主入口：SSE + OAuth + Feishu webhook + Notion + multi-guru
├── railway.json             # Railway build config (DOCKERFILE, healthcheck /healthz)
├── skills/
│   ├── a-stock-data/        # A 股 28 端点（mootdx + 腾讯 + 东财 + 同花顺 + 巨潮 + ...）
│   ├── global-stock-data/   # 美港股 18 端点（新浪 + 腾讯 + 东财 + Yahoo + SEC）
│   ├── xiao-eyu/            # 小鳄鱼（理解力派，通用）
│   ├── bei-jing-chao-jia/   # 北京炒家（模式派）
│   ├── chen-xiao-qun/       # 陈小群（龙头信仰派）
│   ├── jiu-er-ke-bi/        # 92 科比（情绪周期派）
│   ├── nie-pan-chong-sheng/ # 涅盘重升（资金流派）
│   ├── yi-shun-liu-guang/   # 一瞬流光（高位接力派）
│   ├── xiang-cheng-cai-lian-lu/ # 采莲路（控回撤派）
│   ├── xiao-rui-rui/        # 小睿睿（进攻派）
│   ├── hua-dong-da-dao-dan/ # 华东大导弹（低频狙击派）
│   └── gui-yin/             # 归因（资讯派）
├── .env.example             # 环境变量模板（必填 / 选填均列出）
├── CHANGELOG.md             # 变更历史（按版本/日期）
└── .github/workflows/ci.yml # PR + push 时跑 4 项检查（syntax / skill / env / 文档同步）
```

## 上游引擎 (vibe-trading-ai)

**真正干活的 swarm / agent / 回测代码不在本 repo 里**，作为 pip 包 `vibe-trading-ai==0.1.6` 装进容器(在 Dockerfile 里锁版本)。本 repo 只是部署 wrapper(Dockerfile + 入口 launcher + 自定义 skill + 飞书 / Notion 集成)。

容器内上游引擎的位置 `/usr/local/lib/python3.11/site-packages/`：

| 路径 | 内容 |
|------|------|
| `mcp_server.py` | FastMCP server，定义 `start_swarm_async` / `list_skills` 等 MCP tool |
| `src/agent/loop.py` | agent 推理循环 |
| `src/swarm/` | SwarmRuntime / SwarmStore / RunStatus 状态机 |
| `src/core/runner.py` | 任务执行 |
| `src/providers/llm.py` | LLM provider 抽象 (DeepSeek / OpenAI / OpenRouter) |
| `src/skills/` | 内置 skill (yfinance / akshare / ...) — 我们的 `a-stock-data` / `global-stock-data` / `xiao-eyu` 也被装进同一目录 |
| `backtest/` | 回测引擎 (options / 期货 / 标准回测) |

**想看源码**：

```bash
# 本地拉 wheel 看代码
pip download --no-deps vibe-trading-ai==0.1.6 -d /tmp/vibe && \
  unzip /tmp/vibe/vibe_trading_ai-0.1.6-py3-none-any.whl -d /tmp/vibe/src
```

**想升级版本**：改 `Dockerfile` 里 `vibe-trading-ai==X.Y.Z`，push 即生效。需要先在本地装新版跑过一次再推。

**想魔改 swarm 内部**：当前不在 repo 范围；真要改时切换到 vendor 模式 (把 wheel 解压进 repo + Dockerfile 改 `pip install -e .`)，看 [CONTRIBUTING.md](CONTRIBUTING.md) 或讨论。

## 本地开发

```bash
# 跑 syntax check（CI 也会跑同一个）
python -m py_compile mcp_launcher.py

# 本地起服务（需要先 export 上面所有环境变量）
pip install vibe-trading-ai==0.1.6 uvicorn[standard] python-multipart stockstats
pip install --no-deps mootdx pytdx
python mcp_launcher.py
```

健康检查：`curl http://localhost:8000/healthz`

## 分支与协作

- `main` — 受保护，每次 push 触发 Railway 部署。**不要直接 push**，走 PR
- 功能分支 — `feat/xxx` / `fix/xxx` / `chore/xxx`
- 每次有用户感知的变更，更新 [`CHANGELOG.md`](CHANGELOG.md) 的 `[Unreleased]` 段
- 详细规范见 [CONTRIBUTING.md](CONTRIBUTING.md)

## License

[MIT](LICENSE)
