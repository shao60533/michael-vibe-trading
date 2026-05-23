# michael-vibe-trading

A 股 / 美港股 / 加密 多市场 AI 投研服务。基于 [vibe-trading-ai](https://pypi.org/project/vibe-trading-ai/) swarm 的 28 个分析师 preset，部署在 Railway 上对外提供：

- **MCP over SSE** — 供 Claude Desktop / Code / 移动端 Custom Connector 使用，支持静态 Bearer + OAuth 2.1 PKCE
- **飞书 Bot Webhook** — 自然语言对话触发分析，结果以飞书互动卡片 + 飞书云文档 + Notion 三处同步推送
- **数据 Skill** — `a-stock-data`（A 股 28 个端点）、`global-stock-data`（美港股 18 个端点）、`xiao-eyu`（A 股游资视角锐评）

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
| ✅ | `MCP_AUTH_TOKEN` | MCP Bearer 共享密钥 + OAuth 登录口令 |
| ✅ | `DEEPSEEK_API_KEY` | swarm LLM 调用 + 摘要 + 游资视角 |
| △ | `LARK_APP_ID` / `LARK_APP_SECRET` | 飞书 bot（不填则 webhook 不启用） |
| △ | `NOTION_API_KEY` + (`NOTION_DATABASE_ID` 或 `NOTION_PARENT_PAGE_ID`) | Notion 同步（不填则跳过） |
| △ | `FEISHU_VERIFICATION_TOKEN` | 飞书事件校验 token（强烈建议） |

## 文件布局

```
.
├── Dockerfile              # Python 3.11 + vibe-trading-ai + mootdx (--no-deps) + pytdx
├── mcp_launcher.py         # 主入口：SSE + OAuth + Feishu webhook + Notion + xiao-eyu
├── railway.json            # Railway build config (DOCKERFILE, healthcheck /healthz)
├── skills/
│   ├── a-stock-data/       # A 股 28 端点（mootdx + 腾讯 + 东财 + 同花顺 + 巨潮 + ...）
│   ├── global-stock-data/  # 美港股 18 端点（新浪 + 腾讯 + 东财 + Yahoo + SEC）
│   └── xiao-eyu/           # A 股游资『小鳄鱼』视角，stock_decision preset 自动附加
├── .env.example            # 环境变量模板（必填 / 选填均列出）
└── .github/workflows/ci.yml # PR + push 时跑 Python 语法检查
```

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
- 详细规范见 [CONTRIBUTING.md](CONTRIBUTING.md)

## License

[MIT](LICENSE)
