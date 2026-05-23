# 优化方案:飞书集成可靠性与安全加固

| 项 | 内容 |
|----|------|
| 文档状态 | Draft / 待评审 |
| 日期 | 2026-05-23 |
| 范围 | `mcp_launcher.py`(飞书入站链路、OAuth、debug 端点) |
| 影响面 | Railway 单进程部署、飞书 Bot 用户、MCP/SSE 客户端 |
| 不在范围内 | 上游 `vibe-trading-ai`(pip 包)内部逻辑;skills 内容;A 股/美股数据端点 |

---

## 1. 背景

`mcp_launcher.py` 是部署在 Railway 上的单进程服务,对外暴露三个面:MCP over SSE、OAuth 2.1、飞书事件 webhook。代码评审发现若干会在生产环境真实触发的可靠性与安全问题。本服务为 **push-即部署**,容器重启频繁,因此与"重启"相关的缺陷影响被放大。

本方案按优先级给出修复设计、代码改动点与验证方式,采用分阶段交付。

## 2. 目标 / 非目标

**目标**
- 消除重启导致的重复推送(用户被旧报告刷屏)。
- 消除入站链路对事件循环的阻塞,避免健康检查抖动 → 误重启。
- 关闭 `run_id` 路径穿越等安全缺口。
- 让"取消 run"真正生效且不误删已完成报告。

**非目标**
- 不重构整体架构(不拆服务、不引入消息队列)。
- 不改动上游 swarm 执行逻辑。
- 不追求横向扩容;当前单进程 + 低并发(聊天机器人量级)即可。

## 3. 问题清单与优先级

| 编号 | 问题 | 严重度 | 触发条件 | 阶段 |
|------|------|--------|----------|------|
| P0-1 | 容器重启后向飞书重复推送所有历史报告 | 高 | 每次部署/重启 | 阶段一 |
| P0-2 | 异步入站处理器内调用阻塞式 httpx,卡死事件循环 | 高 | 每次飞书消息 | 阶段一 |
| P1-3 | `run_id` 路径穿越 → 任意目录删除 | 中(安全) | 恶意/异常 run_id | 阶段二 |
| P1-4 | `asyncio.create_task` 未持引用,任务可能被 GC | 中 | 偶发 | 阶段一(随 P0-2 一并解决) |
| P1-5 | 取消用线程名 `swarm-{run_id}` 可能双前缀,中止逻辑失效 | 中 | 用户取消 run | 阶段二 |
| P1-6 | OAuth `redirect_uri` 未做注册绑定/校验 | 中 | 钓鱼场景 | 阶段二 |
| P2-7 | 取消会无差别删除已完成 run 的报告 | 低 | 取消已完成 run | 阶段三 |
| P2-8 | `/_debug/env` 泄露密钥前 6 位 | 低 | 持 Bearer 访问 | 阶段三 |
| P2-9 | poll loop 串行发布,慢发布拖累其他 run | 低 | 高并发 | 阶段三 |
| P2-10 | 入站处理器顶层 except 仅打印,用户零反馈 | 低 | 处理异常 | 阶段三 |

> P0-1 与 P0-2 会相互放大:P0-2 阻塞健康检查 → Railway 重启 → P0-1 重复推送。两者需在同一阶段一起修。

---

## 4. 详细设计

### P0-1 重启重复推送

**根因**
`_restore_feishu_pending_from_disk()`(`mcp_launcher.py:1104`)启动时把所有带 `feishu_meta.json` 且状态为 `completed/failed/cancelled` 的 run 重新放回 `_feishu_pending`,poll loop 随即重新 `_publish_terminal_run`。但发布成功后只 `_feishu_pending.pop(run_id)`(`:2667`),**从不在磁盘上标记"已发布"**,run 目录长期驻留。于是每次重启都把历史已完成 run 重新推一遍。

**方案**:引入磁盘级"已发布"标记,restore 时跳过已发布的终态 run;真正需要恢复的只有 `running` 状态、以及"容器宕机时已完成但还没来得及推"的终态 run(无标记)。

```python
def _feishu_published_path(run_id: str):
    import pathlib
    return (pathlib.Path(mcp_server.__file__).resolve().parent /
            ".swarm" / "runs" / run_id / "feishu_published.json")

def _mark_feishu_published(run_id: str) -> None:
    p = _feishu_published_path(run_id)
    if not p.parent.exists():
        return
    try:
        p.write_text(json.dumps({"published_at": time.time()}), encoding="utf-8")
    except Exception as e:
        print(f"[feishu] mark published {run_id} err: {e}", flush=True)

def _is_feishu_published(run_id: str) -> bool:
    return _feishu_published_path(run_id).exists()
```

`_restore_feishu_pending_from_disk` 中的状态判断改为:

```python
if run.status == RunStatus.running:
    pass  # 真正需要恢复
elif run.status in (RunStatus.completed, RunStatus.failed,
                    RunStatus.cancelled) and not _is_feishu_published(rid):
    pass  # 宕机时未及推送的终态 run,恢复一次
else:
    continue  # 已发布,跳过
```

poll loop 中,`_publish_terminal_run` 返回后(无论成功失败,best-effort 语义)立即落标记,避免无限重推:

```python
if run.status in (RunStatus.completed, RunStatus.failed, RunStatus.cancelled):
    try:
        loop.run_until_complete(_publish_terminal_run(run, info))
    except Exception as e:
        print(f"[feishu] publish err {run_id}: {e}", flush=True)
    finally:
        _mark_feishu_published(run_id)        # 新增
    with _feishu_pending_lock:
        _feishu_pending.pop(run_id, None)
```

> 标记只由 poll loop 写,不放进 `_publish_terminal_run` 内部,这样 `_feishu_handle_status` 手动重拉报告时不会污染标记。

**验证**:本地造两个完成态 run 目录(含 `feishu_meta.json`),首次 restore 应入队并发布一次、落标记;再次 restore 应跳过。

---

### P0-2 阻塞事件循环(同时解决 P1-4)

**根因**
`feishu_events`(`:2711`)用 `asyncio.create_task(_feishu_handle_message(body))` 把处理器丢到 uvicorn 事件循环。但处理器内部 `_feishu_send_text`/`_feishu_get_tenant_token`/`runtime.start_run` 都是**同步阻塞**调用(同步 `httpx.Client`,单次 10–15s)。单 worker 下整个 ASGI 服务(SSE 流、其他 webhook、`/healthz`)被冻结;`HEALTHCHECK`(30s/5s,`Dockerfile:57`)可能超时 → 误重启。同时 `create_task` 返回值未被持有,任务可能被 GC(P1-4)。

**方案(推荐):把整条入站处理移到独立 worker 线程,各自跑私有事件循环**——与现有 poller 线程同构。这样处理器内的同步调用只阻塞该 worker 线程,不影响服务循环;`await _llm_route` 等异步逻辑通过 `asyncio.run` 照常工作;`create_task` GC 问题一并消失。

```python
def _spawn_feishu_handler(body: dict) -> None:
    def _runner():
        try:
            asyncio.run(_feishu_handle_message(body))
        except Exception as e:
            print(f"[feishu] handler thread error: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=_runner, daemon=True, name="feishu-msg").start()
```

`feishu_events` 内:

```python
if event_type == "im.message.receive_v1":
    _spawn_feishu_handler(body)   # 取代 asyncio.create_task(...)
```

**权衡**:每条入站消息一个短生命周期线程。聊天机器人量级(低 QPS)+ event_id 去重已在前置,线程数可控,可接受。
**备选(更轻但改动面大)**:保留 `create_task`,把每个同步调用点逐个 `await asyncio.to_thread(...)` 包装,并用模块级 `set` 持有 task 引用。改动点分散、易漏,故不作首选。

**验证**:本地起服务,在处理器里 `time.sleep(8)` 模拟慢发送,期间并发请求 `/healthz` 应立即 200(线程方案);用 `create_task` 直跑则会被阻塞——以此对照确认修复。

---

### P1-3 `run_id` 路径穿越

**根因**
`_feishu_handle_cancel_run`(`:3061`)`swarm_dir / run_id` 后直接 `shutil.rmtree`;`run_id` 来自 LLM router / 用户消息,未校验。已验证 `Path('/app/.swarm/runs') / '../../../tmp/evil'` 落到 `/tmp/evil`。`/_debug/purge-run`(`:515`)同理。

**方案**:统一 run_id 白名单校验,所有"用 run_id 拼路径或杀线程"的入口前置拦截。

```python
_RUN_ID_RE = re.compile(r"^swarm-\d{8}-\d{6}-[0-9a-f]{4,}$")

def _valid_run_id(run_id: str) -> bool:
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return False
    return _RUN_ID_RE.match(run_id) is not None
```

应用点:`_feishu_handle_cancel_run`、`_feishu_handle_status`、`_feishu_meta_path`/`_feishu_published_path` 的调用前、`debug_purge_run`。校验失败直接回"run_id 格式非法"。

> ⚠️ 待确认:`_RUN_ID_RE` 需与上游 `SwarmStore` 的 id 生成规则核对(示例为 `swarm-20260506-171102-016a0768`)。若上游格式更宽,放宽正则但**保留** `/`、`\`、`..` 的硬拒绝作为兜底。

**验证**:单元用例覆盖 `swarm-...`(通过)、`../etc`、`a/b`、空串(拒绝)。

---

### P1-5 取消逻辑:线程名双前缀 + 未调用上游 cancel

**根因**
`run.id` 形如 `swarm-2026...`,代码却拼 `target_name = f"swarm-{run_id}"`(`:3037`)= `swarm-swarm-2026...`,大概率匹配不到真实线程 → `PyThreadState_SetAsyncExc` 空转,swarm 并未真正中止,却已 `rmtree` run 目录(线程还在往已删目录写)。`:3036` 注释提到"runtime 内置 cancel(设置 cancel_event)",但代码实际并未调用该 API。

**方案(需先调研)**
1. **首选**:核查 `SwarmRuntime` 是否提供 `cancel_run(run_id)` / `cancel_event` 之类的协作式取消 API;有则改为调用它,让上游优雅停止(协作式取消远比异步注入 `SystemExit` 安全——后者无法打断阻塞的 C 调用/网络 I/O)。
2. **兜底**:若上游无 cancel API,修正线程名匹配——按"`t.name == run_id` 或 `t.name == f"swarm-{run_id}"` 或 `run_id in t.name`"宽松匹配,并先确认上游真实命名。
3. 取消后再删 disk(见 P2-7,终态 run 不删报告)。

> ⚠️ 本环境未安装 `vibe-trading-ai`,无法直接核验线程命名与是否有 cancel API。此项实施前需 `pip download` 解包确认(README 已给命令)。

**验证**:起一个长 run,发"取消",确认线程确实停止(`/_debug/threads` 不再有该线程)且报告未被误删。

---

### P1-6 OAuth `redirect_uri` 未绑定

**根因**
`authorize_post`(`:375`)校验口令后重定向到客户端任意传入的 `redirect_uri`;DCR(`:277`)只原样回显不绑定。标准 OAuth 要求 `redirect_uri` 与注册值精确匹配。

**方案(保持无状态,正规做法)**:把注册时的 `redirect_uris` 编进 `client_id`(签名 JWT),`/authorize` 与 `/token` 解码 `client_id` 后校验 `redirect_uri` ∈ 注册集合。无需引入持久化存储。

```python
# /register: client_id 改为携带 redirect_uris 的签名 token
client_id = "mcp-" + _jwt_encode({
    "typ": "client", "redirect_uris": body.get("redirect_uris", []),
    "iat": now,  # 无 exp:客户端注册长期有效
}[...])   # 形式细化见实现

# /authorize, /token: 校验
def _client_redirect_uris(client_id: str) -> list[str] | None:
    p = _jwt_decode(client_id.removeprefix("mcp-"))   # typ=client 时不校验 exp
    return p.get("redirect_uris") if p and p.get("typ") == "client" else None
```

`authorize_get`/`authorize_post` 中:若该 client 注册了 redirect_uris,则要求请求的 `redirect_uri` 精确命中;命中失败返回 `invalid_request`(且**不**重定向,直接报错页,避免开放重定向)。
**最小化备选**:若不想改 client_id 结构,至少限制 `redirect_uri` 的 scheme(仅 `https://` 或 `http://localhost`/`127.0.0.1`)以降低开放重定向风险。

> 威胁模型说明:本服务"单一共享密钥=管理员",攻击者需诱导持密用户在显示攻击者 client_id 的页面输入口令,实际风险中等;但 redirect_uri 绑定是 OAuth 合规底线,建议补齐。

**验证**:注册 client A(redirect=`https://a/cb`),用 A 的 client_id 但请求 `redirect_uri=https://evil/cb` 应被拒。

---

### P2 杂项(阶段三)

- **P2-7 取消误删完成报告**(`:3062`):`_feishu_handle_cancel_run` 删 disk 前判断 run 终态;`completed` 的 run 拒绝删除报告,仅从 pending 移除并提示。
- **P2-8 `/_debug/env` 泄露前 6 位**(`:489`):改为只返回 `True/False`(是否设置)+ 长度,不返回明文片段。
- **P2-9 poll 串行发布**(`:2654`):慢发布会拖住其他 run。低优先;如需,可把单个 run 的 publish 提交到线程池并发执行,或限制 summarizer 重试上限。当前量级可暂不动。
- **P2-10 顶层 except 静默**(`:2898`):`_feishu_handle_message` 兜底 except 里给原 chat 回一句"处理出错,请重试",避免用户零反馈。

---

## 5. 实施顺序

| 阶段 | 包含 | 交付物 | 说明 |
|------|------|--------|------|
| 一 | P0-1, P0-2(含 P1-4) | 标记机制 + worker 线程改造 | 止血,优先上线 |
| 二 | P1-3, P1-5, P1-6 | run_id 校验 + 取消调研修复 + redirect 绑定 | 安全/正确性;P1-5 需先解包上游核验 |
| 三 | P2-7~P2-10 | 打磨 | 体验/防御纵深 |

每阶段独立成 commit(或独立 PR),便于回滚与评审。

## 6. 测试与回归策略

现状:仓库无测试,CI 仅 `py_compile` + skill/env 校验。

- **新增最小 pytest**(`tests/test_helpers.py`),覆盖纯函数:`_valid_run_id`、`_extract_target`、`_classify_preset`、`_parse_explicit_preset`、`_md_to_feishu_blocks`、`_is_feishu_published`/`_mark_feishu_published`(用 tmp_path)。
- **CI**:在 `ci.yml` 增加 `pytest -q`(若引入 pytest 依赖)。
- **本地烟测**:用飞书测试企业 sandbox 跑一遍"分析 SOXL → 推回卡片",并手动重启进程验证不重复推送、`/healthz` 在慢发送期间仍即时 200。
- 阶段一务必先验证 P0-1 的 restore/标记闭环(见 P0-1 验证步骤)。

## 7. 风险与回滚

- 各改动均为局部、低耦合;回滚 = `git revert` 对应 commit。
- worker 线程方案改变了入站执行模型——需确认线程内 `asyncio.run` 与上游同步 API 协作正常(已分析无共享可变状态冲突,`_feishu_pending` 等均有锁)。
- P1-5、P1-6 涉及上游与 OAuth 客户端兼容,实施前分别需:解包上游核验线程/cancel API、用真实 MCP 客户端回归 OAuth 流程。

## 8. 待确认事项(评审时拍板)

1. P0-2 采用 **worker 线程**(推荐)还是 **逐点 `to_thread`** 方案?
2. P1-5 是否允许我先 `pip download --no-deps vibe-trading-ai==0.1.6` 解包核验上游 cancel API / 线程命名?
3. P1-6 采用 **签名 client_id 绑定**(正规)还是 **scheme 白名单**(最小)?
4. 是否同意引入 pytest + 在 CI 跑(会新增一个 dev 依赖)?
