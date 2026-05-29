"""共用的可视化字符串生成器 — KHunter docx + intraday docx 都用。

设计原则:
- 只用 Unicode 字符 + emoji,不依赖图片上传(零运行时依赖)
- 飞书 docx 文本块原生支持 → 进 docx 无需额外渲染
- 同时保留在 .md 文件里也漂亮
"""
from __future__ import annotations

import math


# Unicode 实心 / 空心方块 — 大多数 CJK 字体都有
_FILL = "█"
_EMPTY = "░"


def score_bar(value: float | None, max_val: float = 10.0,
              width: int = 10, with_text: bool = True) -> str:
    """0-10 分数渲染成 ▓▓▓▓▓▓▓▓░░ + 文字。

    width: 总格子数(默认 10)
    with_text: 末尾追加 ` 8.2/10`
    """
    if value is None or (isinstance(value, float) and value != value):
        empty = _EMPTY * width
        return f"{empty} —" if with_text else empty
    v = max(0.0, min(float(value), float(max_val)))
    filled = int(round(v / max_val * width))
    bar = _FILL * filled + _EMPTY * (width - filled)
    if with_text:
        return f"{bar} {v:.1f}/{max_val:.0f}"
    return bar


def heat_emoji(score: float | None) -> str:
    """根据综合分给个温度 emoji,让 Top10 表格一眼看出热度。"""
    if score is None or (isinstance(score, float) and score != score):
        return "❄️"
    if score >= 8:
        return "🔥"
    if score >= 6.5:
        return "🌡️"
    if score >= 5:
        return "💧"
    return "❄️"


def confidence_emoji(c: str | None) -> str:
    """置信度 → emoji 化"""
    if not c:
        return "❓"
    c = c.upper()
    return {"A": "✅", "B": "⚠️", "C": "❌"}.get(c, "❓")


def pct_bar(pct: float, max_abs: float = 10.0, width: int = 8) -> str:
    """涨跌幅 → 单向 bar (只支持 ≥0 用例)。pct % 单位。

    用法:盘中板块涨幅 / 个股涨幅,max_abs=10 一般覆盖
    """
    if pct is None or (isinstance(pct, float) and pct != pct):
        return _EMPTY * width
    v = max(0.0, min(pct, max_abs))
    filled = int(round(v / max_abs * width))
    return _FILL * filled + _EMPTY * (width - filled)


def inflow_bar(yi: float, max_yi: float = 20.0, width: int = 8) -> str:
    """主力净流入(亿)→ 单向 bar 用 max_yi 归一化。"""
    if yi is None or (isinstance(yi, float) and yi != yi):
        return _EMPTY * width
    v = max(0.0, min(abs(yi), max_yi))
    filled = int(round(v / max_yi * width))
    sign = "+" if yi >= 0 else "−"
    return f"{sign}{_FILL * filled + _EMPTY * (width - filled)}"


def histogram(buckets: list[tuple[str, int]], max_bar_width: int = 30) -> list[str]:
    """文字直方图。

    Args:
        buckets: [(label, count), ...] 顺序保持
        max_bar_width: 最长那条的格数
    Returns:
        每行一个 str,可直接 join
    """
    if not buckets:
        return []
    max_n = max((n for _, n in buckets), default=0) or 1
    max_label_len = max((len(lbl) for lbl, _ in buckets), default=0)
    rows: list[str] = []
    for lbl, n in buckets:
        bar = _FILL * max(0, int(round(n / max_n * max_bar_width)))
        rows.append(f"`{lbl.ljust(max_label_len)}` {bar} **{n}**")
    return rows


def risk_badge(penalty: float) -> str:
    """风险扣分 → 警示 emoji 数(0=无 / 1=轻 / 2=中 / 3=重)"""
    if penalty is None or penalty <= 0.5:
        return "—"
    if penalty <= 1.0:
        return "⚠️"
    if penalty <= 2.0:
        return "⚠️⚠️"
    return "🚨🚨🚨"
