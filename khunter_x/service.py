"""KHunter daily pipeline 编排:scan → factor score → debate → write outputs.

Caller 拿到 result 后:
  - result['outputs_dir']    本地绝对路径,用于飞书消息附路径
  - result['report_markdown']  完整报告(给 publish 走 docx/Notion)
  - result['rankings']       综合排名 dict
  - result['scan']           原始 scan 结果
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from . import delivery, scanner, stock_factors


def run_khunter_pipeline_sync(
    days: int = 5,
    max_symbols: int = 300,
    top_n: int = 10,
    enable_debate: bool = True,
    debate_max_concurrent: int = 4,
) -> dict[str, Any]:
    """同步入口 — scan + factor only (no debate, no I/O for outputs)。

    用于 MCP tool / debug 场景。完整 pipeline 走 async 版本。
    """
    scan = scanner.run_weekly_scan(days=days, max_symbols=max_symbols)
    panel = scan.pop("_panel", {})
    rows = delivery._flatten_candidates_to_rows(scan)
    grouped = delivery._union_candidates(rows)
    candidate_codes = list(grouped.keys())
    if not candidate_codes:
        return {"scan": scan, "rankings": {"top_overall": [], "all_ranked": []},
                "factor_scores": {}, "outputs_dir": None,
                "report_markdown": "(no candidates)"}
    kh_weights = {code: info["max_weight"] for code, info in grouped.items()}
    factor_scores = stock_factors.compute_cross_sectional_scores(
        panel=panel, candidate_codes=candidate_codes,
        kh_strategy_weights=kh_weights,
    )
    rankings = delivery.build_rankings(scan, factor_scores, debates=None)
    report_md = delivery.build_report_markdown(scan, rankings, debates=None)
    return {
        "scan": scan,
        "factor_scores": factor_scores,
        "rankings": rankings,
        "report_markdown": report_md,
        "outputs_dir": None,
    }


async def run_khunter_pipeline_async(
    days: int = 5,
    max_symbols: int = 300,
    top_n: int = 10,
    enable_debate: bool = True,
    debate_max_concurrent: int = 4,
    write_files: bool = True,
) -> dict[str, Any]:
    """完整异步 pipeline:scan + factor + debate(并行) + write files。

    Returns:
        {
          "scan": dict,
          "factor_scores": dict,
          "debates": dict,
          "rankings": dict,
          "outputs_dir": str or None,
          "files": {report_md, candidates_json, candidates_csv, rankings_json, rankings_csv},
          "report_markdown": str,
          "elapsed_sec": float,
        }
    """
    from . import debate as debate_mod

    started = time.time()

    # 1. Scan (同步,内部已 retry)
    print("[pipeline] step 1: scan", flush=True)
    scan = await asyncio.to_thread(
        scanner.run_weekly_scan, days, max_symbols)
    panel = scan.pop("_panel", {})

    rows = delivery._flatten_candidates_to_rows(scan)
    grouped = delivery._union_candidates(rows)
    candidate_codes = list(grouped.keys())
    print(f"[pipeline] step 1 done: {len(candidate_codes)} unique candidates", flush=True)
    if not candidate_codes:
        return {"scan": scan, "factor_scores": {}, "debates": {},
                "rankings": {"top_overall": [], "all_ranked": []},
                "outputs_dir": None, "files": {},
                "report_markdown": "(no candidates)",
                "elapsed_sec": round(time.time() - started, 1)}

    # 2. Factor scoring
    print("[pipeline] step 2: factor scoring", flush=True)
    kh_weights = {code: info["max_weight"] for code, info in grouped.items()}
    factor_scores = await asyncio.to_thread(
        stock_factors.compute_cross_sectional_scores,
        panel, candidate_codes, kh_weights,
    )
    print(f"[pipeline] step 2 done: {len(factor_scores)} scored", flush=True)

    # 3. Debate (并行,只跑 Top N)
    debates: dict[str, dict[str, Any]] = {}
    if enable_debate:
        print("[pipeline] step 3: debate (parallel)", flush=True)
        ranks_for_debate = delivery.build_rankings(scan, factor_scores, debates=None)
        top_for_debate = ranks_for_debate.get("top_overall", [])[:top_n]
        # 把 top 转成 debate 需要的 schema
        cand_for_debate = [
            {"code": it["code"], "name": it["name"],
             "strategies": it["strategies"]}
            for it in top_for_debate
        ]
        debates = await debate_mod.run_debates_for_top(
            cand_for_debate, factor_scores,
            max_concurrent=debate_max_concurrent,
        )
        print(f"[pipeline] step 3 done: {len(debates)} debates", flush=True)
    else:
        print("[pipeline] step 3 skipped (enable_debate=False)", flush=True)

    # 4. Build final rankings (with debate)
    rankings = delivery.build_rankings(scan, factor_scores, debates=debates)

    # 5. Build markdown report
    report_md = delivery.build_report_markdown(scan, rankings, debates=debates)

    # 6. Write outputs files
    files: dict[str, str] = {}
    outputs_dir = None
    if write_files:
        try:
            analysis_date = scan.get("analysis_date") or "unknown"
            outputs_dir = delivery.make_outputs_dir(analysis_date)
            delivery.append_generation_log(outputs_dir,
                f"pipeline start, candidates={len(candidate_codes)}, "
                f"top_n={top_n}, debate={enable_debate}")
            cj, cc = delivery.write_candidates_files(outputs_dir, scan)
            rj, rc = delivery.write_rankings_files(outputs_dir, rankings)
            rmd = delivery.write_report_markdown(outputs_dir, report_md)
            files = {
                "outputs_dir": str(outputs_dir),
                "report_md": str(rmd),
                "candidates_json": str(cj),
                "candidates_csv": str(cc),
                "rankings_json": str(rj),
                "rankings_csv": str(rc),
            }
            delivery.append_generation_log(outputs_dir,
                f"files written ok: {list(files.keys())}")
        except Exception as exc:
            print(f"[pipeline] write_files err: {type(exc).__name__}: {exc}",
                  flush=True)

    elapsed = round(time.time() - started, 1)
    print(f"[pipeline] done in {elapsed}s", flush=True)
    return {
        "scan": scan,
        "factor_scores": factor_scores,
        "debates": debates,
        "rankings": rankings,
        "outputs_dir": str(outputs_dir) if outputs_dir else None,
        "files": files,
        "report_markdown": report_md,
        "elapsed_sec": elapsed,
    }
