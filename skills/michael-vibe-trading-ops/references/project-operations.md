# Project Operations Reference

## Contents

- Project map
- Runtime surfaces
- Runtime skill model
- Change checklists
- Validation commands

## Project Map

`mcp_launcher.py` is the production entrypoint. It mounts MCP over SSE, OAuth 2.1 PKCE endpoints, Feishu event handling, Notion sync, debug endpoints, and extra MCP tools such as `start_swarm_async`.

`Dockerfile` installs `vibe-trading-ai==0.1.6`, `uvicorn[standard]`, `python-multipart`, `stockstats`, and the special `mootdx` plus `pytdx` combination used by A-share data skills. It copies local `skills/` into the upstream `SkillsLoader().skills_dir`.

`skills/` contains runtime skills shipped into the container:

- `a-stock-data`: A-share data endpoints and helper code.
- `global-stock-data`: US and Hong Kong stock data endpoints and helper code.
- `xiao-eyu`: A-share hot-money trader analysis style and references.
- `michael-vibe-trading-ops`: Project maintenance workflow for agents working on this repo.

`.env.example` is the source of truth for documented environment variables. Any env var read from `mcp_launcher.py` should appear there.

`.github/workflows/ci.yml` compiles `mcp_launcher.py`, checks every `skills/*/SKILL.md`, warns if a skill is missing from Dockerfile sanity checks, and fails when env vars used by code are missing from `.env.example`.

## Runtime Surfaces

Public paths are limited to `/`, `/healthz`, OAuth discovery and token endpoints, `/register`, `/authorize`, `/token`, and `/feishu/events`. All other HTTP paths, including debug endpoints, require bearer auth or a valid JWT.

MCP is mounted at `/` by `mcp_app`, with SSE exposed as `/sse` and messages as `/messages`.

Feishu event handling starts swarm runs in the background, stores routing metadata, polls completion, summarizes the final report, optionally adds the `xiao-eyu` view for `stock_decision` templates, and publishes to Feishu card, Feishu docx, and Notion when configured.

Notion is optional. It is enabled only when `NOTION_API_KEY` and either `NOTION_DATABASE_ID` or `NOTION_PARENT_PAGE_ID` are present.

## Runtime Skill Model

The Docker image copies every local `skills/<name>/` directory into the upstream `vibe-trading-ai` skills directory. Treat local skill directories as production inputs, not just docs.

When adding a runtime skill:

1. Use a lowercase hyphen-case directory name.
2. Put required trigger information in `SKILL.md` frontmatter `description`.
3. Keep data-provider skills self-contained in `SKILL.md` when the runtime may not load bundled references.
4. Add dependency notes to Dockerfile only when the skill imports packages unavailable in the container.
5. Add the new `SKILL.md` path to Dockerfile sanity `ls`.
6. Update README if the user-facing skill set changes.

Prefer Codex-compatible frontmatter with only `name` and `description` for new local skills. Existing imported third-party skills may contain extra metadata; do not churn them unless the task is to normalize or validate them.

## Change Checklists

For `mcp_launcher.py` changes:

- Preserve the global httpx timeout patch before imports that may construct clients.
- Keep `MCP_AUTH_TOKEN` required at startup.
- Keep debug endpoints auth-gated by leaving them out of `PUBLIC_PATHS`.
- Redact secrets in debug output and logs.
- Use try/except around Feishu, Notion, DeepSeek, OpenRouter, and OpenAI calls so integration failures do not crash the app.

For Feishu publishing changes:

- Check interactive card, docx, and Notion paths together.
- Keep long reports out of chat messages; prefer card summary plus full report links.
- Preserve persisted run metadata so restarts can recover routing.
- Keep user-facing Chinese copy concise and operational.

For environment variables:

- Add new variables to `.env.example`.
- Keep required versus optional sections accurate.
- Avoid defaulting secrets to real values.
- Re-run the CI-style env var check if `os.environ.get` or `os.getenv` calls change.

For Dockerfile changes:

- Avoid new pip dependencies unless needed.
- Keep the `mootdx` `--no-deps` install unless deliberately changing the dependency strategy.
- If `vibe-trading-ai` is upgraded, re-check imports for `mcp_server`, `src.swarm.runtime`, `src.swarm.store`, and `src.agent.skills`.

## Validation Commands

Use local `python3` when `python` is not available:

```bash
python3 -m py_compile mcp_launcher.py
python3 /Users/zhangshuai/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/michael-vibe-trading-ops
```

Run the CI env var check after changing env access:

```bash
missing=0
for var in $(grep -oE 'os\.environ\.get\("[A-Z_]+"|os\.getenv\("[A-Z_]+"' mcp_launcher.py \
  | grep -oE '"[A-Z_]+"' | sort -u | tr -d '"'); do
  if ! grep -q "^${var}=" .env.example; then
    echo "missing $var"
    missing=1
  fi
done
exit $missing
```

When Docker or dependency behavior changes and time allows:

```bash
docker build -t michael-vibe-trading:local-check .
```
