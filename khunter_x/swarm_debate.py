"""Real swarm investment_committee debate per Top candidate.

跟 debate.py(单 LLM 调用模拟辩论)互斥的另一种实现:
- 每只股启动一个真实 swarm investment_committee preset
- 串行跑(同时只一个),避免 LLM 限速 + 容器内存压力
- 单只 30 分钟超时,Top10 总 5 小时上限
- 失败一只不影响其他,标记 timeout/error/failed,部分结果仍写 docx

输出 schema (per code):
{
  "code": "600519",
  "name": "贵州茅台",
  "status": "completed" | "failed" | "cancelled" | "timeout" | "error",
  "run_id": "swarm-...",
  "final_report": "<完整 markdown>",   # status=completed 时
  "error": "<error msg>",              # 非 completed 时
  "elapsed": 360.5,                    # 秒
}
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


# 推断 6 位代码 → ticker.SH / .SZ
def _infer_ticker(code: str) -> str:
    code = (code or "").strip().upper()
    if "." in code:
        return code
    if not code:
        return code
    if code.startswith(("6", "9")):
        return code + ".SH"
    return code + ".SZ"


async def run_swarm_debates_for_top(
    top_candidates: list[dict[str, Any]],
    timeout_per_run: int = 1800,
    poll_interval: float = 15.0,
    progress_callback: Callable[[int, int, str, str, str, float], Awaitable[None]] | None = None,
) -> dict[str, dict[str, Any]]:
    """串行跑每个 Top 候选的 swarm investment_committee。

    Args:
        top_candidates: [{code, name, strategies, ...}, ...]
        timeout_per_run: 单只股 swarm 上限(默认 30 分钟)
        poll_interval: 每多少秒查一次 run 状态
        progress_callback: async fn(idx, total, code, name, status, elapsed)
                            完成每只股后调用,handler 可用来发飞书进度

    Returns:
        {code: {status, run_id, final_report, error, elapsed}}
    """
    import mcp_server
    from src.swarm.runtime import SwarmRuntime
    from src.swarm.store import SwarmStore
    from src.swarm.models import RunStatus

    swarm_dir = mcp_server.AGENT_DIR / ".swarm" / "runs"
    store = SwarmStore(base_dir=swarm_dir)
    runtime = SwarmRuntime(store=store)

    results: dict[str, dict[str, Any]] = {}
    total = len(top_candidates)
    print(f"[khunter/swarm-debate] start {total} sequential swarm runs, "
          f"timeout {timeout_per_run}s each", flush=True)

    for idx, cand in enumerate(top_candidates, 1):
        code = cand.get("code") or ""
        name = cand.get("name") or code
        if not code:
            continue
        ticker = _infer_ticker(code)
        started = time.time()
        result: dict[str, Any] = {
            "code": code, "name": name,
            "status": "pending", "run_id": "",
            "final_report": "", "error": "", "elapsed": 0.0,
        }
        print(f"[khunter/swarm-debate] {idx}/{total} start {ticker} {name}",
              flush=True)
        try:
            variables = {
                "target": ticker, "market": "CN",
                "goal": (f"投委会深度评估 {ticker} ({name}) — "
                         f"由 KHunter Top10 触发,需覆盖技术面/资金面/情绪/"
                         f"行业景气/公告/估值/事件/风险共 8 个维度,"
                         f"输出多方/空方/分歧/共识/次日验证"),
            }
            run = runtime.start_run("investment_committee", variables)
            result["run_id"] = run.id

            # 异步 poll 直到 terminal 或超时
            while True:
                elapsed = time.time() - started
                if elapsed > timeout_per_run:
                    result["status"] = "timeout"
                    result["error"] = f"exceeded {timeout_per_run}s,run 可能仍在跑"
                    print(f"[khunter/swarm-debate] {idx}/{total} TIMEOUT "
                          f"{ticker} run_id={run.id}", flush=True)
                    break
                await asyncio.sleep(poll_interval)
                try:
                    refreshed = store.load_run(run.id)
                except Exception as exc:
                    print(f"[khunter/swarm-debate] {idx}/{total} load_run err: "
                          f"{type(exc).__name__}: {exc}", flush=True)
                    continue
                if refreshed is None:
                    continue
                if refreshed.status in (RunStatus.completed, RunStatus.failed,
                                         RunStatus.cancelled):
                    result["status"] = refreshed.status.value
                    fr = (refreshed.final_report or "").strip()
                    result["final_report"] = fr
                    if refreshed.status != RunStatus.completed:
                        result["error"] = f"swarm 终态: {refreshed.status.value}"
                    elif not fr:
                        result["error"] = "swarm completed 但 final_report 为空"
                    break
        except Exception as exc:
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[khunter/swarm-debate] {idx}/{total} exception {ticker}: "
                  f"{result['error']}", flush=True)

        result["elapsed"] = round(time.time() - started, 1)
        results[code] = result
        print(f"[khunter/swarm-debate] {idx}/{total} done {code} "
              f"status={result['status']} elapsed={result['elapsed']}s "
              f"report_len={len(result['final_report'])}", flush=True)
        if progress_callback:
            try:
                await progress_callback(
                    idx, total, code, name,
                    result["status"], result["elapsed"],
                )
            except Exception as cb_exc:
                print(f"[khunter/swarm-debate] progress_callback err: "
                      f"{cb_exc}", flush=True)

    return results
