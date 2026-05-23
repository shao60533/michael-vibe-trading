# 协作规范

## 分支模型

- `main` — 受保护分支，**每次 push 触发 Railway 自动部署**。禁止直接 push，只接受 PR merge
- 功能分支命名:
  - `feat/<scope>` 新功能(如 `feat/add-options-skill`)
  - `fix/<scope>` 修 bug(如 `fix/feishu-dedup-race`)
  - `chore/<scope>` 杂活(依赖升级、文档、CI)
  - `refactor/<scope>` 重构
  - `perf/<scope>` 性能

## 提交流程

```bash
git checkout -b feat/your-feature
# ... 改代码 ...
python -m py_compile mcp_launcher.py   # 本地先过 syntax check
git add -p                              # 注意:不要 git add -A,避免误提交 .env / 大文件
git commit -m "feat: 简明说明"
git push -u origin feat/your-feature
gh pr create --base main --fill         # 或在 GitHub 网页发 PR
```

## 提交信息格式

[Conventional Commits](https://www.conventionalcommits.org/zh-hans/):

```
<type>: <简明描述>

<可选正文,说明 why 而不是 what>
```

类型: `feat` `fix` `refactor` `docs` `test` `chore` `perf` `ci`

## PR 检查清单

提 PR 前自查:

- [ ] `python -m py_compile mcp_launcher.py` 通过(CI 会再跑一次)
- [ ] 没把密钥/token 写进代码或测试文件
- [ ] 没把 `.env` 文件 commit 进去
- [ ] 新增环境变量同步更新到 `.env.example`(CI 会自动校验)
- [ ] 新增 skill 只要扔进 `skills/<name>/SKILL.md` 即可,Dockerfile 已是 for-loop 自动装(CI 校验每个 skill 有 SKILL.md)
- [ ] 改了 publish 路径(card / doc / notion)的话,三个地方都要同步改
- [ ] 改了 `_publish_terminal_run` 流程 / 新增飞书指令,在 README 「飞书使用指北」表格里加一行
- [ ] 用户可感知的变更(新功能 / 行为变更 / bug 修复)在 `CHANGELOG.md` 的 `[Unreleased]` 段加一条
- [ ] 注释说明 *why* 而不是 *what*(代码本身能讲清楚 what)

## 代码风格

- 单文件不超过 ~2500 行(`mcp_launcher.py` 已经在此边界,新功能尽量拆模块)
- 函数 < 50 行,超出拆
- 中文注释 OK,但变量/函数名英文
- 错误处理: 外部调用(Feishu / Notion / DeepSeek)用 try/except + print 日志,不抛
- 不引入新的 pip 依赖除非真的必要 — 每加一个就要担心 mootdx 那种依赖冲突死循环

## 部署 / 回滚

正常情况下 `git push origin main` → Railway 自动 deploy。

回滚最快的方式:

```bash
git revert <bad-commit>
git push origin main   # 又一次自动 deploy
```

紧急情况也可以 Railway dashboard 直接 redeploy 到上一个 SUCCESS 的 deploy id。

## 密钥管理

- **不要** 把 `MCP_AUTH_TOKEN` / `DEEPSEEK_API_KEY` / `LARK_APP_SECRET` / `NOTION_API_KEY` 写进代码
- **不要** commit `.env` 文件(`.gitignore` 已排除)
- 所有密钥统一在 Railway service 的 **Variables** 配置
- 如果不小心泄露过,立即去对应平台 rotate
