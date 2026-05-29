"""Load latest KHunter rankings.json from STATE_DIR/outputs/.

为盘中异动提供 leader 个股因子分 lookup:
  - 找最新 <date>-khunter-a-share-daily/rankings.json
  - 提供 lookup_score(code) -> {score, rank, strategies, confidence}
  - graceful 兜底:文件不存在 / JSON 坏 / code 不在榜 → None
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_TOP_N = 20  # 只标注 Top20,避免在「领涨股 KH 4.2」这种鸡肋分上浪费视觉空间


def _outputs_root() -> Path:
    state_dir = os.environ.get("STATE_DIR", "").strip().rstrip("/")
    if state_dir:
        return Path(state_dir) / "outputs"
    return Path("/tmp/outputs")


def _find_latest_rankings() -> Path | None:
    """returns path to latest <date>-khunter-a-share-daily/rankings.json or None."""
    root = _outputs_root()
    if not root.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if not name.endswith("-khunter-a-share-daily"):
            continue
        # 日期前缀(YYYY-MM-DD)
        date_part = name[:10]
        rj = d / "rankings.json"
        if rj.exists():
            candidates.append((date_part, rj))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


class KHunterIndex:
    """加载一次,内存里查 leader code → score。"""

    def __init__(self) -> None:
        self._scores: dict[str, dict[str, Any]] = {}
        self._analysis_date: str = ""
        self._source_path: str = ""
        self._load()

    def _load(self) -> None:
        path = _find_latest_rankings()
        if path is None:
            print("[intraday/kh-link] no KHunter rankings.json found (state_dir empty?)",
                  flush=True)
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[intraday/kh-link] failed to parse {path}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return
        self._analysis_date = str(data.get("analysis_date") or "")
        self._source_path = str(path)
        all_ranked = data.get("all_ranked") or []
        # 只索引 Top20,降低误标
        for rank, item in enumerate(all_ranked[:_TOP_N], start=1):
            code = str(item.get("code") or "")
            if not code:
                continue
            self._scores[code] = {
                "score": item.get("total_score"),
                "rank": rank,
                "strategies_cn": item.get("strategies_cn") or [],
                "confidence": item.get("confidence", "?"),
            }
        print(f"[intraday/kh-link] loaded {len(self._scores)} Top{_TOP_N} from "
              f"{path.name} (date={self._analysis_date})", flush=True)

    def lookup_score(self, code: str) -> dict[str, Any] | None:
        """返回 {score, rank, strategies_cn, confidence} 或 None。"""
        if not code:
            return None
        return self._scores.get(code)

    @property
    def analysis_date(self) -> str:
        return self._analysis_date

    @property
    def loaded_count(self) -> int:
        return len(self._scores)


def load_latest_index() -> KHunterIndex:
    """每次调用重新读盘 — 调用频率低(每天 3-5 次)无必要做缓存。"""
    return KHunterIndex()
