---
name: michael-vibe-trading-ops
description: 维护和扩展 michael-vibe-trading 仓库的项目操作技能。用于处理本项目的 MCP over SSE 服务、Feishu 飞书 Bot、Notion 同步、OAuth PKCE、Railway 部署、Dockerfile、环境变量、CI、mcp_launcher.py、vibe-trading-ai 版本升级，以及新增或修改 skills 目录里的 A 股、美港股、游资视角等运行时 skill。触发场景包括修 bug、加功能、排查部署或飞书消息、更新数据 skill、调整发布流程、检查安全和提交前验证。
---

# Michael Vibe Trading Ops

## Overview

Use this skill to modify, debug, deploy, or extend the michael-vibe-trading repository without re-discovering its deployment wrapper, custom runtime skills, and Feishu/Notion publishing path.

This skill is for project operations. For market analysis, use the runtime skills `a-stock-data`, `global-stock-data`, and `xiao-eyu`.

## First Pass

Start by reading `README.md`, `CONTRIBUTING.md`, `Dockerfile`, `.env.example`, and the relevant `mcp_launcher.py` sections. Check `git status --short --branch` before editing.

Load `references/project-operations.md` when the task touches deployment, auth, Feishu, Notion, runtime skill loading, Dockerfile dependencies, environment variables, CI, or the `vibe-trading-ai` package boundary.

## Project Rules

- Treat `mcp_launcher.py` as the production entrypoint. Keep changes narrow and avoid broad rewrites of the monolith unless the user asks for refactoring.
- Keep Railway behavior in mind: merging to `main` triggers deployment. Follow the PR flow in `CONTRIBUTING.md`; do not push directly to `main`.
- Never commit secrets. Add any new environment variable to `.env.example`, and keep debug output redacted.
- Prefer standard library and existing dependencies. Add Dockerfile dependencies only when the feature truly needs them.
- When adding or renaming anything under `skills/<name>/`, include `SKILL.md`, update the Dockerfile sanity `ls` list, and update README project layout if the visible skill set changes.
- Keep runtime data skills self-contained in their `SKILL.md` unless the target runtime is known to load bundled references.
- If changing Feishu card, Feishu docx, or Notion output, check all three publishing surfaces for compatible content and failure behavior.

## Common Workflows

For a runtime skill update, inspect the existing skill style, keep the trigger description specific, validate frontmatter if the skill should also be Codex-compatible, and check whether Dockerfile dependencies need to change.

For an MCP or auth change, verify `PUBLIC_PATHS`, OAuth metadata, SSE routes, and bearer/JWT behavior. Debug endpoints must stay auth-gated.

For a Feishu or Notion change, preserve fail-soft behavior: external API failures should log and degrade without crashing the service.

For an upstream `vibe-trading-ai` upgrade, update the pinned Dockerfile version only after checking import paths used by `mcp_launcher.py`, especially `mcp_server`, `src.swarm.*`, and `src.agent.skills.SkillsLoader`.

## Validation

Run targeted checks before finishing:

```bash
python3 -m py_compile mcp_launcher.py
python3 /Users/zhangshuai/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/michael-vibe-trading-ops
```

When Dockerfile dependencies or runtime skill installation changes, run a Docker build if time and network allow. When only docs or skill text changed, at least run the skill validator and inspect the final diff.
