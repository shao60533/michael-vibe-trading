"""Production wrapper around the Sequoia-X scan scripts.

Hardening on top of what the scripts already do:
  - scripts internal: `_get_json` retries 3x w/ backoff (already)
  - scripts internal: `fetch_active_universe` Eastmoney → Sina fallback (already)
  - scripts internal: `collect_panel` per-stock try/except,partial coverage 透传
  - this wrapper: workspace pinned to /app, scripts dir resolved via SkillsLoader,
    custom exception types, no argparse dependency, importable from anywhere.

Caller layer (mcp_launcher / Feishu handler) is responsible for:
  - asyncio.wait_for (整体硬超时)
  - per-chat in-flight dedup
  - publish 流水线 (card + docx + Notion)
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class SequoiaScanError(Exception):
    """Raised when Sequoia-X scan can't proceed:
    - 活跃股池接口都挂(东财 + 新浪 fallback 失败)
    - panel 全空(所有股 K 线都拿不到)
    - 解析不出最近交易日
    """


def _resolve_scripts_dir() -> Path:
    """Find scripts/ at runtime.

    Priority:
    1. SEQUOIA_SCRIPTS_DIR env var
    2. SkillsLoader.skills_dir / sequoia-x-a-share-selector / scripts
       (container install path,被 Dockerfile cp 进 site-packages 时用)
    3. Local dev fallback (relative to this file)
    """
    env_path = os.environ.get("SEQUOIA_SCRIPTS_DIR", "").strip()
    if env_path:
        return Path(env_path)
    try:
        from src.agent.skills import SkillsLoader
        skills_dir = Path(SkillsLoader().skills_dir)
        candidate = skills_dir / "sequoia-x-a-share-selector" / "scripts"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return (Path(__file__).resolve().parent.parent
            / "skills" / "sequoia-x-a-share-selector" / "scripts")


def _ensure_imports() -> None:
    """Make weekly_scan module importable. Done lazily for clear errors."""
    scripts_dir = _resolve_scripts_dir()
    if not scripts_dir.exists():
        raise SequoiaScanError(
            f"Scripts dir not found: {scripts_dir} — "
            f"check skill install in container")
    p = str(scripts_dir)
    if p not in sys.path:
        sys.path.insert(0, p)


def run_weekly_scan(
    days: int = 5,
    max_symbols: int = 300,
    datalen: int = 180,
    min_amount: float = 100_000_000,
    rps_period: int = 120,
    rps_threshold: float = 90.0,
    top_per_strategy: int = 10,
    pause_seconds: float = 0.03,
    end_date: str | None = None,
    include_st: bool = False,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Run Sequoia-X A-share strategy scan over recent trading days.

    Returns the same dict shape as the script's `main()` (with `report_markdown`).

    Raises:
        SequoiaScanError: hard failure (no universe / no panel / no dates).
        Exception: unexpected error in scripts (passed through for caller log).
    """
    _ensure_imports()
    # Lazy import after sys.path setup
    from sequoia_x_weekly_scan import (  # type: ignore
        StockMeta, to_sina_symbol, fetch_active_universe,
        collect_panel, last_trading_dates, scan_dates, format_markdown,
    )

    workspace = os.environ.get("SEQUOIA_WORKSPACE", "/app")
    started = time.time()

    # 1. Build universe
    if symbols:
        universe = [
            StockMeta(code=c.strip(), name=c.strip(), sina_symbol=to_sina_symbol(c.strip()))
            for c in symbols if c.strip()
        ]
    else:
        try:
            universe = fetch_active_universe(max_symbols, include_st=include_st)
        except Exception as exc:
            # 东财 + 新浪 fallback 都挂了
            raise SequoiaScanError(
                f"获取活跃股池失败 — 东财/新浪 fallback 都挂: "
                f"{type(exc).__name__}: {exc}") from exc
    if not universe:
        raise SequoiaScanError("活跃股池为空 — 接口返回 0 只")

    # 2. Fetch K-line panel (per-stock try/except inside collect_panel)
    panel, errors = collect_panel(
        workspace=workspace, universe=universe,
        datalen=datalen, pause_seconds=pause_seconds,
    )
    if not panel:
        raise SequoiaScanError(
            f"全部 {len(universe)} 只股 K 线都拿不到 — "
            f"workspace={workspace} 数据源出问题。"
            f"errors_sample[0:3]={errors[:3]}")

    # 3. Recent trading dates
    eval_dates = last_trading_dates(panel, days=days, end_date=end_date)
    if not eval_dates:
        raise SequoiaScanError("没解析出最近交易日 — 数据时间戳异常")

    # 4. Run 6 strategies
    daily_results = scan_dates(
        panel=panel, eval_dates=eval_dates,
        min_amount=min_amount, rps_period=rps_period,
        rps_threshold=rps_threshold, top_per_strategy=top_per_strategy,
    )

    elapsed = round(time.time() - started, 1)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": ("workspace factor_analysis.data_sources.fetch_sina_daily_kline "
                         "+ Eastmoney/Sina active universe"),
        "parameters": {
            "workspace": workspace, "days": days,
            "max_symbols": len(universe), "datalen": datalen,
            "min_amount": min_amount, "rps_period": rps_period,
            "rps_threshold": rps_threshold, "end_date": end_date or "",
        },
        "coverage": {
            "requested_symbols": len(universe),
            "fetched_symbols": len(panel),
            "error_symbols": len(errors),
            "elapsed_seconds": elapsed,
        },
        "dates": [str(d.date()) for d in eval_dates],
        "daily_results": daily_results,
        "errors_sample": errors[:20],
    }
    result["report_markdown"] = format_markdown(result)
    return result
