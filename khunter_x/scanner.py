"""Wrapper around `khunter_weekly_scan.py` scripts. Same pattern as sequoia_x.

Hardening:
- scripts 内部已有 3 次 retry + 东财→新浪 fallback + per-stock try/except
- 本 wrapper 加: 路径动态解析、workspace 固定 /app、错误翻译
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class KHunterScanError(Exception):
    """Hard failure: 无股池 / 全空 panel / 无最近交易日。"""


def _resolve_scripts_dir() -> Path:
    env_path = os.environ.get("KHUNTER_SCRIPTS_DIR", "").strip()
    if env_path:
        return Path(env_path)
    try:
        from src.agent.skills import SkillsLoader
        skills_dir = Path(SkillsLoader().skills_dir)
        candidate = skills_dir / "khunter-a-share-selector" / "scripts"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return (Path(__file__).resolve().parent.parent
            / "skills" / "khunter-a-share-selector" / "scripts")


def _ensure_imports() -> None:
    scripts_dir = _resolve_scripts_dir()
    if not scripts_dir.exists():
        raise KHunterScanError(
            f"KHunter scripts dir not found: {scripts_dir} — check Dockerfile COPY")
    p = str(scripts_dir)
    if p not in sys.path:
        sys.path.insert(0, p)


def run_weekly_scan(
    days: int = 5,
    max_symbols: int = 300,
    datalen: int = 260,
    pause_seconds: float = 0.03,
    top_per_strategy: int = 8,
    top_union: int = 12,
    end_date: str | None = None,
    include_st: bool = False,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Run KHunter 11-strategy scan. Returns result dict matching script's main() shape.

    Raises:
        KHunterScanError: hard failure (no universe / no panel / no eval dates).
    """
    _ensure_imports()
    from khunter_weekly_scan import (  # type: ignore
        StockMeta, to_sina_symbol, fetch_active_universe,
        collect_panel, last_trading_dates, scan_dates, format_markdown,
        STRATEGY_WEIGHTS, STRATEGY_NAMES_CN,
    )

    workspace = os.environ.get("KHUNTER_WORKSPACE", "/app")
    started = time.time()

    # 1. Universe
    if symbols:
        universe = [
            StockMeta(code=c.strip(), name=c.strip(), sina_symbol=to_sina_symbol(c.strip()))
            for c in symbols if c.strip()
        ]
    else:
        try:
            universe = fetch_active_universe(max_symbols, include_st=include_st)
        except Exception as exc:
            raise KHunterScanError(
                f"获取活跃股池失败 — 东财/新浪 fallback 都挂: "
                f"{type(exc).__name__}: {exc}") from exc
    if not universe:
        raise KHunterScanError("活跃股池为空")

    # 2. Panel
    panel, errors = collect_panel(
        workspace=workspace, universe=universe,
        datalen=datalen, pause_seconds=pause_seconds,
    )
    if not panel:
        raise KHunterScanError(
            f"全部 {len(universe)} 只股 K 线都拿不到 — "
            f"workspace={workspace}. errors_sample[0:3]={errors[:3]}")

    # 3. Evaluation dates
    eval_dates = last_trading_dates(panel, days=days, end_date=end_date)
    if not eval_dates:
        raise KHunterScanError("没解析出最近交易日")

    # 4. Scan strategies
    daily_results = scan_dates(
        panel=panel, eval_dates=eval_dates,
        top_per_strategy=top_per_strategy, top_union=top_union,
    )

    elapsed = round(time.time() - started, 1)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": ("workspace factor_analysis.data_sources.fetch_sina_daily_kline "
                         "+ Eastmoney/Sina active universe"),
        "parameters": {
            "workspace": workspace, "days": days,
            "max_symbols": len(universe), "datalen": datalen,
            "top_per_strategy": top_per_strategy, "top_union": top_union,
            "end_date": end_date or "",
        },
        "coverage": {
            "requested_symbols": len(universe),
            "fetched_symbols": len(panel),
            "error_symbols": len(errors),
            "elapsed_seconds": elapsed,
        },
        "dates": [str(d.date()) for d in eval_dates],
        "analysis_date": str(eval_dates[-1].date()),
        "daily_results": daily_results,
        "strategy_weights": dict(STRATEGY_WEIGHTS),
        "strategy_names_cn": dict(STRATEGY_NAMES_CN),
        "errors_sample": errors[:20],
        # 保留 panel 给 factor_scoring 用 (不进 JSON serialization,内存传递)
        "_panel": panel,
    }
    result["report_markdown_scan"] = format_markdown(result)
    return result
