"""Cross-sectional individual-stock factors.

设计原则:
- 只用 K 线就够,不引入新数据源(腾讯财经 PE/PB 等是后续 V4+)
- 可解释:每个因子有 raw / z-score / 权重 / 解释
- 数据质量等级 + 风险惩罚
- 横截面 z-score 在「scan 命中的候选股 + 同股票池所有股」范围内做,样本越多 z 越稳

输出 schema (per code):
{
  "raw": {factor_name: float},
  "z": {factor_name: float},   # cross-sectional z-score
  "weighted": {factor_name: float},
  "total_score": float,        # 0-10 区间,加权 + 风险惩罚
  "confidence": "A" | "B" | "C",
  "data_quality_notes": [str, ...],
  "risk_penalty": float,       # 已应用的扣分
}
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


# 因子权重 — 合计 = 100,后续可做 env 配置
FACTOR_WEIGHTS = {
    "momentum_20d": 18,       # 20 日动量
    "momentum_60d": 12,       # 60 日动量
    "volatility_20d": -8,     # 波动率(负权,越低越好)
    "turnover_20d": 8,        # 换手率
    "price_volume_corr": 6,   # 量价结构
    "money_flow_proxy": 18,   # 资金流代理(净流入 / 总额)
    "ma20_relative": 8,       # 距 20MA 相对位置
    "today_amount_ratio": 12, # 当日活跃度
    "kh_strategy_weight": 18, # KHunter 策略权重加成
}


def _safe_pct_change(series: pd.Series, periods: int) -> float:
    """Compute pct change with NaN safety. Returns NaN if not enough data."""
    if len(series) <= periods:
        return float("nan")
    a = series.iloc[-1]
    b = series.iloc[-1 - periods]
    if not pd.notna(a) or not pd.notna(b) or b == 0:
        return float("nan")
    return float(a / b - 1)


def _safe_volatility(series: pd.Series, window: int = 20) -> float:
    """Annualized-ish volatility of daily returns."""
    if len(series) < window + 1:
        return float("nan")
    rets = series.pct_change().tail(window)
    if rets.isna().all() or rets.std() == 0:
        return float("nan")
    return float(rets.std())


def _money_flow_proxy(df: pd.DataFrame, window: int = 20) -> float:
    """资金流代理 = sum(amount on up-days - amount on down-days) / sum(total amount)
    近似净流入比例,无真实资金流数据时的代理。"""
    if len(df) < window:
        return float("nan")
    sub = df.tail(window).copy()
    if "amount" not in sub.columns or "close" not in sub.columns or "open" not in sub.columns:
        return float("nan")
    sub["signed"] = sub["amount"] * np.sign(sub["close"] - sub["open"])
    tot = float(sub["amount"].sum())
    if tot <= 0:
        return float("nan")
    return float(sub["signed"].sum() / tot)


def _ma20_relative(series: pd.Series) -> float:
    """距 20 日均线的相对位置: (close - ma20) / ma20"""
    if len(series) < 20:
        return float("nan")
    ma20 = series.tail(20).mean()
    if not ma20 or ma20 == 0:
        return float("nan")
    return float(series.iloc[-1] / ma20 - 1)


def _today_amount_ratio(df: pd.DataFrame, window: int = 20) -> float:
    """当日成交活跃度 = today_amount / 20d avg amount"""
    if len(df) < window + 1 or "amount" not in df.columns:
        return float("nan")
    today = float(df["amount"].iloc[-1])
    avg = float(df["amount"].tail(window + 1).iloc[:-1].mean())
    if avg <= 0:
        return float("nan")
    return float(today / avg)


def _turnover_proxy(df: pd.DataFrame, window: int = 20) -> float:
    """简化:用 volume / 总量级别 近似换手。无流通股本数据时,用 z-scored volume 近似。
    返回原始 avg(volume) — 上层做 cross-sectional 比较即可。"""
    if len(df) < window or "volume" not in df.columns:
        return float("nan")
    return float(df["volume"].tail(window).mean())


def _price_volume_corr(df: pd.DataFrame, window: int = 20) -> float:
    """20 日 close 与 volume 的 Pearson 相关系数 — 量价配合度。"""
    if len(df) < window or "volume" not in df.columns or "close" not in df.columns:
        return float("nan")
    sub = df.tail(window)
    try:
        c = sub["close"].astype(float).corr(sub["volume"].astype(float))
        return float(c) if pd.notna(c) else float("nan")
    except Exception:
        return float("nan")


def _data_quality(df: pd.DataFrame) -> tuple[str, list[str]]:
    """评数据质量等级 + 笔记。"""
    notes: list[str] = []
    if df is None or df.empty:
        return "C", ["panel 为空"]
    n = len(df)
    if n < 30:
        notes.append(f"历史 K 线 < 30 ({n}),新股或停牌")
        return "C", notes
    if n < 60:
        notes.append(f"历史 K 线 < 60 ({n}),回看窗口受限")
        return "B", notes
    nan_ratio = df["close"].isna().sum() / n if "close" in df.columns else 1
    if nan_ratio > 0.05:
        notes.append(f"close NaN 比例 {nan_ratio:.1%}")
        return "B", notes
    return "A", notes


def _risk_penalty(df: pd.DataFrame) -> tuple[float, list[str]]:
    """风险惩罚分(0-3),记录到 notes。

    场景:涨幅过大 / 波动过大 / 流动性极低 → 扣分。"""
    penalty = 0.0
    notes: list[str] = []
    if df is None or df.empty:
        return 0.0, []
    # 涨幅过大
    if len(df) >= 21:
        recent_chg = _safe_pct_change(df["close"], 20)
        if recent_chg > 0.5:
            penalty += 1.5
            notes.append(f"20日涨 {recent_chg:.1%} (>50% 高风险)")
        elif recent_chg > 0.3:
            penalty += 0.6
            notes.append(f"20日涨 {recent_chg:.1%} (>30% 注意)")
    # 波动过大
    vol = _safe_volatility(df["close"], 20)
    if pd.notna(vol) and vol > 0.06:
        penalty += 0.5
        notes.append(f"日波动 σ={vol:.3f} (>6% 高波动)")
    # 流动性极低
    if "amount" in df.columns and len(df) >= 20:
        avg_amt = float(df["amount"].tail(20).mean())
        if avg_amt < 3e7:  # 日均不足 3000 万
            penalty += 0.8
            notes.append(f"日均成交额 {avg_amt/1e7:.1f}千万 (流动性低)")
    return penalty, notes


def compute_raw_factors_for_code(
    code: str,
    panel: dict[str, pd.DataFrame],
    kh_strategy_weight: float = 0.0,
) -> dict[str, Any]:
    """对单只股算 raw 因子 + 数据质量 + 风险惩罚。"""
    df = panel.get(code)
    quality, q_notes = _data_quality(df)
    if df is None or df.empty or quality == "C":
        return {
            "raw": {k: float("nan") for k in FACTOR_WEIGHTS},
            "quality": quality, "quality_notes": q_notes,
            "risk_penalty": 0.0, "risk_notes": ["无足够数据,不评分"],
        }
    raw = {
        "momentum_20d": _safe_pct_change(df["close"], 20),
        "momentum_60d": _safe_pct_change(df["close"], 60),
        "volatility_20d": _safe_volatility(df["close"], 20),
        "turnover_20d": _turnover_proxy(df, 20),
        "price_volume_corr": _price_volume_corr(df, 20),
        "money_flow_proxy": _money_flow_proxy(df, 20),
        "ma20_relative": _ma20_relative(df["close"]),
        "today_amount_ratio": _today_amount_ratio(df, 20),
        "kh_strategy_weight": float(kh_strategy_weight),
    }
    penalty, r_notes = _risk_penalty(df)
    return {
        "raw": raw,
        "quality": quality, "quality_notes": q_notes,
        "risk_penalty": penalty, "risk_notes": r_notes,
    }


def cross_sectional_zscore(values: list[float]) -> list[float]:
    """对一组 raw values 做 z-score。NaN 保持 NaN。"""
    arr = np.array([v if pd.notna(v) else float("nan") for v in values], dtype=float)
    valid = arr[~np.isnan(arr)]
    if len(valid) < 3:
        return [float("nan")] * len(values)
    mu, sigma = valid.mean(), valid.std()
    if sigma == 0:
        return [0.0 if pd.notna(v) else float("nan") for v in values]
    return [((v - mu) / sigma) if pd.notna(v) else float("nan") for v in values]


def compute_cross_sectional_scores(
    panel: dict[str, pd.DataFrame],
    candidate_codes: list[str],
    kh_strategy_weights: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """对一组候选股做横截面因子评分。

    Args:
        panel: 所有股的 K 线 (来自 scanner 的 _panel)
        candidate_codes: 要评分的股
        kh_strategy_weights: {code: max_kh_weight} — 该股命中的最高 KHunter 策略权重

    Returns:
        {code: {raw, z, weighted, total_score (0-10), confidence, data_quality_notes,
                risk_penalty, risk_notes}}
    """
    kh_w = kh_strategy_weights or {}

    # 1. 算每只股的 raw + 数据质量 + 风险扣分
    per_stock_raw: dict[str, dict] = {}
    for code in candidate_codes:
        per_stock_raw[code] = compute_raw_factors_for_code(
            code, panel, kh_strategy_weight=kh_w.get(code, 0.0))

    # 2. 横截面 z-score (在候选 universe 内做)
    factor_names = list(FACTOR_WEIGHTS.keys())
    z_per_factor: dict[str, list[float]] = {}
    for f in factor_names:
        vals = [per_stock_raw[c]["raw"].get(f, float("nan")) for c in candidate_codes]
        z_per_factor[f] = cross_sectional_zscore(vals)

    # 3. 加权 + 总分
    out: dict[str, dict[str, Any]] = {}
    for i, code in enumerate(candidate_codes):
        raw = per_stock_raw[code]["raw"]
        quality = per_stock_raw[code]["quality"]
        risk = per_stock_raw[code]["risk_penalty"]

        z: dict[str, float] = {}
        weighted: dict[str, float] = {}
        total_z = 0.0
        weight_used = 0.0
        for f in factor_names:
            zv = z_per_factor[f][i]
            z[f] = zv if pd.notna(zv) else float("nan")
            w = FACTOR_WEIGHTS[f]
            if pd.notna(zv):
                weighted[f] = (zv * w) / 100.0
                total_z += weighted[f]
                weight_used += abs(w) / 100.0
            else:
                weighted[f] = float("nan")

        # 缩放到 0-10 (假设 z 总分通常在 [-3, 3]),再扣风险
        # base_score: z 总分映射 — z=0 → 5,z=+2 → 8,z=-2 → 2
        base_score = 5.0 + total_z * 1.5
        base_score = max(0.0, min(10.0, base_score))
        final_score = max(0.0, base_score - risk)

        confidence = quality  # A/B/C 直接来自数据质量

        out[code] = {
            "raw": raw,
            "z": z,
            "weighted": weighted,
            "total_score": round(final_score, 2),
            "base_score": round(base_score, 2),
            "confidence": confidence,
            "data_quality_notes": per_stock_raw[code]["quality_notes"],
            "risk_penalty": round(risk, 2),
            "risk_notes": per_stock_raw[code]["risk_notes"],
        }
    return out
