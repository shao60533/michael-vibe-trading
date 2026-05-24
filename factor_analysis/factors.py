"""QuantsPlaybook-inspired A-share industry factor generation."""

from __future__ import annotations

from collections.abc import Iterable

FEATURE_COLUMNS = [
    "momentum_5d",
    "momentum_20d",
    "second_order_momentum",
    "momentum_term_spread",
    "amount_volatility",
    "volume_volatility",
    "net_position",
    "position_change",
    "price_volume_corr",
    "first_order_divergence",
    "volume_amplitude_comovement",
    "trend_efficiency",
    "qrs_beta_z",
    "alligator_signal",
    "nhnl_board_signal",
]

DEFAULT_FACTOR_WEIGHTS = {
    "momentum_5d": 0.10,
    "momentum_20d": 0.10,
    "second_order_momentum": 0.08,
    "momentum_term_spread": 0.08,
    "amount_volatility": 0.06,
    "volume_volatility": 0.06,
    "net_position": 0.06,
    "position_change": 0.08,
    "price_volume_corr": 0.08,
    "first_order_divergence": 0.08,
    "volume_amplitude_comovement": 0.06,
    "trend_efficiency": 0.07,
    "qrs_beta_z": 0.06,
    "alligator_signal": 0.08,
    "nhnl_board_signal": 0.05,
}


def _rolling_zscore(series, window: int):
    mean = series.rolling(window, min_periods=max(5, window // 3)).mean()
    std = series.rolling(window, min_periods=max(5, window // 3)).std()
    return (series - mean) / std.replace(0, float("nan"))


def _safe_div(num, den):
    return num / den.replace(0, float("nan"))


def _calc_one_board(group, horizon_days: int):
    import numpy as np
    import pandas as pd

    g = group.sort_values("date").copy()
    close = g["close"]
    open_ = g["open"]
    high = g["high"]
    low = g["low"]
    volume = g["volume"].replace(0, np.nan)
    amount = g["amount"].replace(0, np.nan)
    ret_1d = close.pct_change()
    intraday_ret = close / open_.replace(0, np.nan) - 1
    volume_pct = volume.pct_change()

    g["momentum_5d"] = close.pct_change(5)
    g["momentum_20d"] = close.pct_change(20)
    base_mom = (close - close.rolling(20, min_periods=8).mean().shift(1)) / close
    g["second_order_momentum"] = (base_mom - base_mom.shift(5)).ewm(span=5, adjust=False).mean()
    g["momentum_term_spread"] = close.pct_change(20) - close.pct_change(5)
    g["amount_volatility"] = -amount.pct_change().rolling(20, min_periods=8).std()
    g["volume_volatility"] = -volume_pct.rolling(20, min_periods=8).std()
    g["net_position"] = -((close - low) / (high - close + 1e-9)).rolling(20, min_periods=8).sum()
    pressure = volume * ((close - low - high + close) / (high - low + 1e-9))
    g["position_change"] = pressure.ewm(span=20, adjust=False).mean() - pressure.ewm(span=5, adjust=False).mean()
    g["price_volume_corr"] = -ret_1d.rolling(20, min_periods=8).corr(volume_pct)
    g["first_order_divergence"] = -volume_pct.rolling(20, min_periods=8).corr(intraday_ret)
    amplitude = high / low.replace(0, np.nan) - 1
    g["volume_amplitude_comovement"] = volume_pct.rolling(20, min_periods=8).corr(amplitude)
    path = ret_1d.abs().rolling(20, min_periods=8).sum()
    g["trend_efficiency"] = close.pct_change(20).abs() / path.replace(0, np.nan)

    beta = high.rolling(18, min_periods=10).corr(low) * (
        high.rolling(18, min_periods=10).std() / low.rolling(18, min_periods=10).std()
    )
    g["qrs_beta_z"] = _rolling_zscore(beta, 60)

    ma5 = close.rolling(5, min_periods=5).mean().shift(3)
    ma8 = close.rolling(8, min_periods=8).mean().shift(5)
    ma13 = close.rolling(13, min_periods=13).mean().shift(8)
    g["alligator_signal"] = np.select([ma5 > ma8, ma5 < ma8], [1.0, -1.0], default=0.0)
    g.loc[(ma5 > ma8) & (ma8 > ma13), "alligator_signal"] = 1.5
    g.loc[(ma5 < ma8) & (ma8 < ma13), "alligator_signal"] = -1.5

    new_high = close >= close.rolling(20, min_periods=10).max().shift(1)
    new_low = close <= close.rolling(20, min_periods=10).min().shift(1)
    g["nhnl_board_signal"] = new_high.astype(float) - new_low.astype(float)

    # Decision after T close, enter at T+1 open, exit at T+horizon close.
    g["entry_date"] = g["date"].shift(-1)
    g["future_exit_date"] = g["date"].shift(-horizon_days)
    g["future_return"] = close.shift(-horizon_days) / open_.shift(-1) - 1
    return g


def build_industry_features(panel, horizon_days: int = 5):
    """Create factor frame with one row per date x industry board."""
    import numpy as np

    required = {"date", "code", "name", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"industry panel missing columns: {sorted(missing)}")

    df = panel.copy()
    df = df.dropna(subset=["date", "code", "open", "high", "low", "close"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = df[col].astype(float)
    out = df.groupby("code", group_keys=False).apply(_calc_one_board, horizon_days=horizon_days)
    out = out.replace([np.inf, -np.inf], np.nan)
    return out.sort_values(["date", "code"]).reset_index(drop=True)


def add_cross_sectional_scores(features, factor_columns: Iterable[str] = FEATURE_COLUMNS):
    """Add per-date z-scored factor columns and a weighted factor_score."""
    import numpy as np

    df = features.copy()
    z_cols: list[str] = []
    for col in factor_columns:
        if col not in df.columns:
            continue
        z_col = f"{col}_z"
        z_cols.append(z_col)
        df[z_col] = df.groupby("date")[col].transform(
            lambda x: (x - x.mean()) / (x.std(ddof=0) if x.std(ddof=0) else np.nan)
        )
    weighted = []
    for col in factor_columns:
        z_col = f"{col}_z"
        if z_col in df:
            weighted.append(df[z_col].fillna(0) * DEFAULT_FACTOR_WEIGHTS.get(col, 0.0))
    df["factor_score"] = sum(weighted) if weighted else 0.0
    return df


def compute_index_timing(features):
    """Summarize broad industry-index timing from the latest factor snapshot."""
    latest_date = features["date"].max()
    latest = features[features["date"] == latest_date].copy()
    if latest.empty:
        return {"state": "unknown", "score": 0.0, "date": ""}

    advance_ratio_5d = float((latest["momentum_5d"] > 0).mean())
    above_ma_ratio = float((latest["momentum_20d"] > 0).mean())
    avg_5d = float(latest["momentum_5d"].mean())
    avg_20d = float(latest["momentum_20d"].mean())
    risk_score = (
        0.35 * (advance_ratio_5d - 0.5) * 2
        + 0.30 * (above_ma_ratio - 0.5) * 2
        + 0.20 * max(min(avg_5d / 0.05, 1), -1)
        + 0.15 * max(min(avg_20d / 0.10, 1), -1)
    )
    if risk_score >= 0.35:
        state = "risk_on"
        zh = "进攻"
    elif risk_score <= -0.35:
        state = "risk_off"
        zh = "防守"
    else:
        state = "neutral"
        zh = "震荡"
    return {
        "date": str(latest_date.date() if hasattr(latest_date, "date") else latest_date),
        "state": state,
        "state_zh": zh,
        "score": round(float(risk_score), 4),
        "advance_ratio_5d": round(advance_ratio_5d, 4),
        "above_ma20_ratio": round(above_ma_ratio, 4),
        "avg_5d_return": round(avg_5d, 4),
        "avg_20d_return": round(avg_20d, 4),
    }
