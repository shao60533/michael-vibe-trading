"""Historical validation for sector ETF factor models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data_sources import fetch_sector_etf_panel
from .factors import FEATURE_COLUMNS, add_cross_sectional_scores, build_industry_features
from .model import _fit_predict_model


@dataclass
class PeriodSpec:
    label: str
    signal_date: str
    start_date: str
    end_date: str


def _zscore(series):
    std = series.std(ddof=0)
    if not std:
        return series * 0.0
    return (series - series.mean()) / std


def _spearman_corr(left, right) -> float:
    value = left.rank().corr(right.rank())
    if value != value:
        return 0.0
    return float(value)


def _actual_period_returns(panel, signal_date, end_date):
    import pandas as pd

    signal_date = pd.to_datetime(signal_date)
    end_date = pd.to_datetime(end_date)
    rows = []
    for code, frame in panel.sort_values("date").groupby("code"):
        future = frame[frame["date"] > signal_date]
        future = future[future["date"] <= end_date]
        if future.empty:
            continue
        entry = future.iloc[0]
        exit_ = future.iloc[-1]
        rows.append(
            {
                "code": code,
                "actual_entry_date": entry["date"],
                "actual_exit_date": exit_["date"],
                "actual_return": float(exit_["close"] / entry["open"] - 1),
            }
        )
    return pd.DataFrame(rows)


def _period_eval(pred_frame, panel, period: PeriodSpec, top_k: int) -> dict[str, Any]:
    import numpy as np
    import pandas as pd

    actual = _actual_period_returns(panel, period.signal_date, period.end_date)
    merged = pred_frame.merge(actual, on="code", how="inner")
    if merged.empty:
        return {"label": period.label, "error": "no overlapping prediction/actual rows"}
    merged = merged.sort_values("prediction", ascending=False)
    top = merged.head(top_k)
    bottom = merged.tail(top_k)
    benchmark = float(merged["actual_return"].mean())
    top_return = float(top["actual_return"].mean())
    corr = _spearman_corr(merged["prediction"], merged["actual_return"])
    hit_rate = float((top["actual_return"] > benchmark).mean())
    return {
        "label": period.label,
        "signal_date": period.signal_date,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "actual_exit_date": str(merged["actual_exit_date"].max().date()),
        "universe_count": int(len(merged)),
        "top_k": int(top_k),
        "top_k_return": round(top_return, 6),
        "benchmark_return": round(benchmark, 6),
        "excess_return": round(top_return - benchmark, 6),
        "spearman_ic": round(corr, 4),
        "hit_rate_vs_benchmark": round(hit_rate, 4),
        "top_selected": [
            {
                "code": r["code"],
                "name": r["name"],
                "prediction": round(float(r["prediction"]), 6),
                "factor_score": round(float(r["factor_score"]), 4),
                "actual_return": round(float(r["actual_return"]), 6),
            }
            for _, r in top.iterrows()
        ],
        "bottom_selected": [
            {
                "code": r["code"],
                "name": r["name"],
                "prediction": round(float(r["prediction"]), 6),
                "actual_return": round(float(r["actual_return"]), 6),
            }
            for _, r in bottom.iterrows()
        ],
        "ranked": [
            {
                "code": r["code"],
                "name": r["name"],
                "prediction": round(float(r["prediction"]), 6),
                "actual_return": round(float(r["actual_return"]), 6),
            }
            for _, r in merged.head(20).iterrows()
        ],
    }


def _last_trade_day(available_dates, before_or_equal: str):
    import pandas as pd

    target = pd.to_datetime(before_or_equal)
    eligible = [d for d in available_dates if d <= target]
    if not eligible:
        raise RuntimeError(f"No trading day <= {before_or_equal}")
    return eligible[-1]


def _month_periods(train_end, validate_start, validate_end, available_dates) -> list[PeriodSpec]:
    import pandas as pd

    start = pd.to_datetime(validate_start)
    end = pd.to_datetime(validate_end)
    static_signal = _last_trade_day(available_dates, str(pd.to_datetime(train_end).date()))
    periods = [
        PeriodSpec(
            f"{start.strftime('%Y-%m')}至{end.strftime('%Y-%m')}静态持有",
            str(static_signal.date()),
            str(start.date()),
            str(_last_trade_day(available_dates, str(end.date())).date()),
        )
    ]

    month_starts = pd.date_range(start=start.to_period("M").start_time, end=end, freq="MS")
    for month_start in month_starts:
        month_end = min(month_start + pd.offsets.MonthEnd(0), end)
        signal_anchor = static_signal if month_start <= start else month_start - pd.Timedelta(days=1)
        signal_date = _last_trade_day(available_dates, str(signal_anchor.date()))
        period_end = _last_trade_day(available_dates, str(month_end.date()))
        periods.append(
            PeriodSpec(
                month_start.strftime("%Y-%m"),
                str(signal_date.date()),
                str(max(month_start, start).date()),
                str(period_end.date()),
            )
        )
    return periods


def _permutation_importance(train, feature_cols, baseline_pred, model_func) -> list[dict[str, Any]]:
    """Fallback feature contribution for sklearn models without native importance."""
    import numpy as np

    y = train["future_return"]
    baseline = float(np.mean((baseline_pred - y) ** 2))
    rows = []
    for col in feature_cols:
        shuffled = train[feature_cols].copy()
        shuffled[col] = shuffled[col].sample(frac=1.0, random_state=42).to_numpy()
        pred = model_func(shuffled)
        mse = float(np.mean((pred - y) ** 2))
        rows.append({"feature": col, "importance": max(mse - baseline, 0.0)})
    rows.sort(key=lambda x: x["importance"], reverse=True)
    return rows[:12]


def run_period_model_validation(
    train_start: str = "2025-10-01",
    train_end: str = "2025-12-31",
    validate_start: str = "2026-01-01",
    validate_end: str = "2026-05-24",
    warmup_start: str = "2025-05-01",
    horizon_days: int = 5,
    top_k: int = 5,
    datalen: int = 700,
) -> dict[str, Any]:
    """Train on a fixed historical window and validate later monthly sector trends."""
    import pandas as pd

    train_start_dt = pd.to_datetime(train_start)
    train_end_dt = pd.to_datetime(train_end)
    validate_end_dt = pd.to_datetime(validate_end)

    panel = fetch_sector_etf_panel(warmup_start, validate_end, datalen=datalen)
    features = add_cross_sectional_scores(build_industry_features(panel, horizon_days=horizon_days))
    feature_cols = FEATURE_COLUMNS + ["factor_score"]

    train = features[
        (features["date"] >= train_start_dt)
        & (features["date"] <= train_end_dt)
        & (features["future_exit_date"] <= train_end_dt)
    ].dropna(subset=feature_cols + ["future_return"])
    if len(train) < 80:
        raise RuntimeError(
            f"Too few training rows: {len(train)}. Need more symbols or a longer train window."
        )

    model_name, train_pred, _, native_importance = _fit_predict_model(
        train[feature_cols], train["future_return"], train[feature_cols], train[feature_cols].head(1)
    )
    train_eval = train.copy()
    train_eval["prediction"] = train_pred
    train_ic = train_eval.groupby("date").apply(
        lambda x: _spearman_corr(x["prediction"], x["future_return"])
    ).dropna()

    def predict(frame):
        _, _, pred, _ = _fit_predict_model(
            train[feature_cols],
            train["future_return"],
            train[feature_cols].head(1),
            frame[feature_cols],
        )
        return pred

    if native_importance is None:
        feature_importance = _permutation_importance(train, feature_cols, train_pred, predict)
    else:
        feature_importance = sorted(
            [
                {"feature": col, "importance": float(val)}
                for col, val in zip(feature_cols, native_importance)
            ],
            key=lambda x: x["importance"],
            reverse=True,
        )[:12]

    available_dates = sorted(pd.to_datetime(features["date"]).unique())
    actual_validate_end = _last_trade_day(available_dates, str(validate_end_dt.date()))
    periods = _month_periods(train_end, validate_start, str(actual_validate_end.date()), available_dates)

    period_results = []
    forecasts = {}
    factor_snapshots = {}
    for period in periods:
        signal_dt = pd.to_datetime(period.signal_date)
        signal_frame = features[features["date"] == signal_dt].dropna(subset=feature_cols).copy()
        if signal_frame.empty:
            period_results.append({"label": period.label, "error": f"no features on {period.signal_date}"})
            continue
        signal_frame["prediction"] = predict(signal_frame)
        signal_frame["prediction_z"] = _zscore(signal_frame["prediction"])
        signal_frame = signal_frame.sort_values("prediction", ascending=False)
        forecasts[period.label] = [
            {
                "code": r["code"],
                "name": r["name"],
                "date": str(r["date"].date()),
                "prediction": round(float(r["prediction"]), 6),
                "prediction_z": round(float(r["prediction_z"]), 4),
                "factor_score": round(float(r["factor_score"]), 4),
                "momentum_5d": round(float(r["momentum_5d"]), 6),
                "momentum_20d": round(float(r["momentum_20d"]), 6),
                "trend_efficiency": round(float(r["trend_efficiency"]), 4),
                "qrs_beta_z": round(float(r["qrs_beta_z"]), 4),
                "price_volume_corr": round(float(r["price_volume_corr"]), 4),
            }
            for _, r in signal_frame.head(12).iterrows()
        ]
        factor_snapshots[period.label] = [
            {
                "name": r["name"],
                "code": r["code"],
                "momentum_5d": round(float(r["momentum_5d"]), 4),
                "momentum_20d": round(float(r["momentum_20d"]), 4),
                "trend_efficiency": round(float(r["trend_efficiency"]), 4),
                "price_volume_corr": round(float(r["price_volume_corr"]), 4),
                "factor_score": round(float(r["factor_score"]), 4),
                "prediction": round(float(r["prediction"]), 6),
            }
            for _, r in signal_frame.head(top_k).iterrows()
        ]
        period_results.append(_period_eval(signal_frame, panel, period, top_k=top_k))

    result = {
        "method": (
            "Train on fixed pre-validation sector ETF proxy factors; validate later trends "
            "without using future returns in training."
        ),
        "train_start": str(train_start_dt.date()),
        "train_end": str(train_end_dt.date()),
        "validate_start": validate_start,
        "validate_end": str(actual_validate_end.date()),
        "train_rows": int(len(train)),
        "train_dates": int(train["date"].nunique()),
        "universe_count": int(panel["code"].nunique()),
        "model_name": model_name,
        "horizon_days": horizon_days,
        "top_k": top_k,
        "feature_columns": feature_cols,
        "train_ic_mean": round(float(train_ic.mean()), 4) if len(train_ic) else 0.0,
        "train_ic_daily": {str(k.date()): round(float(v), 4) for k, v in train_ic.items()},
        "feature_importance": feature_importance,
        "period_results": period_results,
        "forecasts": forecasts,
        "factor_snapshots": factor_snapshots,
    }
    result["report_markdown"] = format_period_validation_report(result)
    return result


def run_february_model_validation(
    train_month: str = "2026-02",
    validate_start: str = "2026-03-01",
    validate_end: str = "2026-05-24",
    warmup_start: str = "2025-09-01",
    horizon_days: int = 5,
    top_k: int = 5,
    datalen: int = 500,
) -> dict[str, Any]:
    """Train on February 2026 labels and validate March-May sector ETF trends."""
    import pandas as pd

    panel = fetch_sector_etf_panel(warmup_start, validate_end, datalen=datalen)
    features = add_cross_sectional_scores(build_industry_features(panel, horizon_days=horizon_days))
    feature_cols = FEATURE_COLUMNS + ["factor_score"]
    month_start = pd.to_datetime(f"{train_month}-01")
    month_end = month_start + pd.offsets.MonthEnd(0)
    train = features[
        (features["date"] >= month_start)
        & (features["date"] <= month_end)
        & (features["future_exit_date"] <= month_end)
    ].dropna(subset=feature_cols + ["future_return"])
    if len(train) < 50:
        raise RuntimeError(
            f"Too few February training rows: {len(train)}. Need more symbols or a longer train month."
        )

    model_name, train_pred, _, importance = _fit_predict_model(
        train[feature_cols], train["future_return"], train[feature_cols], train[feature_cols].head(1)
    )
    train_eval = train.copy()
    train_eval["prediction"] = train_pred
    train_ic = train_eval.groupby("date").apply(
        lambda x: _spearman_corr(x["prediction"], x["future_return"])
    ).dropna()

    available_dates = sorted(pd.to_datetime(features["date"]).unique())

    def last_trade_day(before_or_equal: str):
        target = pd.to_datetime(before_or_equal)
        eligible = [d for d in available_dates if d <= target]
        if not eligible:
            raise RuntimeError(f"No trading day <= {before_or_equal}")
        return eligible[-1]

    static_signal = last_trade_day(str(month_end.date()))
    mar_end = last_trade_day("2026-03-31")
    apr_end = last_trade_day("2026-04-30")
    may_end = last_trade_day(validate_end)
    periods = [
        PeriodSpec("3-5月静态持有", str(static_signal.date()), validate_start, str(may_end.date())),
        PeriodSpec("3月", str(static_signal.date()), "2026-03-01", str(mar_end.date())),
        PeriodSpec("4月", str(mar_end.date()), "2026-04-01", str(apr_end.date())),
        PeriodSpec("5月截至当前", str(apr_end.date()), "2026-05-01", str(may_end.date())),
    ]

    period_results = []
    forecasts = {}
    for period in periods:
        signal_dt = pd.to_datetime(period.signal_date)
        signal_frame = features[features["date"] == signal_dt].dropna(subset=feature_cols).copy()
        if signal_frame.empty:
            period_results.append({"label": period.label, "error": f"no features on {period.signal_date}"})
            continue
        _, _, pred, _ = _fit_predict_model(
            train[feature_cols],
            train["future_return"],
            train[feature_cols].head(1),
            signal_frame[feature_cols],
        )
        signal_frame["prediction"] = pred
        signal_frame["prediction_z"] = _zscore(signal_frame["prediction"])
        signal_frame = signal_frame.sort_values("prediction", ascending=False)
        forecasts[period.label] = [
            {
                "code": r["code"],
                "name": r["name"],
                "date": str(r["date"].date()),
                "prediction": round(float(r["prediction"]), 6),
                "factor_score": round(float(r["factor_score"]), 4),
            }
            for _, r in signal_frame.head(10).iterrows()
        ]
        period_results.append(_period_eval(signal_frame, panel, period, top_k=top_k))

    if importance is None:
        feature_importance = []
    else:
        feature_importance = sorted(
            [
                {"feature": col, "importance": float(val)}
                for col, val in zip(feature_cols, importance)
            ],
            key=lambda x: x["importance"],
            reverse=True,
        )[:12]

    result = {
        "method": "Train on February 2026 sector ETF proxy factors; validate March-May out of sample.",
        "train_month": train_month,
        "train_rows": int(len(train)),
        "universe_count": int(panel["code"].nunique()),
        "model_name": model_name,
        "horizon_days": horizon_days,
        "top_k": top_k,
        "train_ic_mean": round(float(train_ic.mean()), 4) if len(train_ic) else 0.0,
        "train_ic_daily": {str(k.date()): round(float(v), 4) for k, v in train_ic.items()},
        "feature_importance": feature_importance,
        "period_results": period_results,
        "forecasts": forecasts,
    }
    result["report_markdown"] = format_february_validation_report(result)
    return result


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_period_validation_report(result: dict[str, Any]) -> str:
    lines = [
        f"# {result['train_start']}至{result['train_end']}建模、{result['validate_start']}至{result['validate_end']}走势预测校验",
        "",
        f"- 训练窗口: {result['train_start']} 至 {result['train_end']}，仅使用窗口内可完成验证的 {result['horizon_days']} 日 forward label",
        f"- 样本: {result['universe_count']} 个 A 股行业/主题 ETF 作为板块代理，训练交易日 {result['train_dates']} 天，训练行数 {result['train_rows']}",
        f"- 模型: {result['model_name']}；训练期日均 Spearman IC {result['train_ic_mean']:.2f}",
        f"- 组合口径: 每期按预测值选 Top{result['top_k']}，与全样本等权收益比较",
        "",
        "## 样本外验收结果",
        "",
    ]
    for item in result["period_results"]:
        if item.get("error"):
            lines.append(f"- {item['label']}: {item['error']}")
            continue
        verdict = "有效" if item["excess_return"] > 0 and item["spearman_ic"] > 0 else "一般/失效"
        lines.append(
            f"- **{item['label']}**: Top{item['top_k']} {_pct(item['top_k_return'])}，"
            f"等权基准 {_pct(item['benchmark_return'])}，超额 {_pct(item['excess_return'])}，"
            f"IC {item['spearman_ic']:.2f}，命中率 {_pct(item['hit_rate_vs_benchmark'])}，结论: {verdict}"
        )
        picks = "、".join(
            f"{x['name']}({_pct(x['actual_return'])})" for x in item["top_selected"][:5]
        )
        lines.append(f"  - Top选择实际表现: {picks}")

    static_label = result["period_results"][0]["label"] if result.get("period_results") else ""
    lines += ["", f"## {result['train_end']}静态预测 Top12", ""]
    for i, row in enumerate(result.get("forecasts", {}).get(static_label, []), start=1):
        lines.append(
            f"{i}. {row['name']}({row['code']}) 预测 {row['prediction']:.4f}，"
            f"因子分 {row['factor_score']:.2f}，20日动量 {_pct(row['momentum_20d'])}"
        )
    if result.get("feature_importance"):
        lines += ["", "## 主要特征贡献", ""]
        for item in result["feature_importance"][:8]:
            lines.append(f"- {item['feature']}: {item['importance']:.6f}")
    lines += [
        "",
        "## 口径说明",
        "",
        "- 使用新浪行业/主题 ETF 日线作为 A 股板块代理；东财 push2 行业板块接口在当前环境返回空响应，未用于本次验收。",
        "- 训练阶段不使用验证期收益；验证期收益只用于样本外校验。",
        "- 以上为历史回测与模型评估，不构成投资建议。",
    ]
    return "\n".join(lines)


def format_february_validation_report(result: dict[str, Any]) -> str:
    lines = [
        "# 2026年2月建模、3-5月走势预测校验",
        "",
        f"- 训练窗口: {result['train_month']}，仅使用 2 月内可在月底前完成验证的 {result['horizon_days']} 日 forward label",
        f"- 样本: {result['universe_count']} 个 A 股行业/主题 ETF 作为板块代理，训练行数 {result['train_rows']}",
        f"- 模型: {result['model_name']}；训练期日均 Spearman IC {result['train_ic_mean']:.2f}",
        f"- 组合口径: 每期按预测值选 Top{result['top_k']}，与全样本等权收益比较",
        "",
        "## 验收结果",
        "",
    ]
    for item in result["period_results"]:
        if item.get("error"):
            lines.append(f"- {item['label']}: {item['error']}")
            continue
        verdict = "有效" if item["excess_return"] > 0 and item["spearman_ic"] > 0 else "一般/失效"
        lines.append(
            f"- **{item['label']}**: Top{item['top_k']} {_pct(item['top_k_return'])}，"
            f"等权基准 {_pct(item['benchmark_return'])}，超额 {_pct(item['excess_return'])}，"
            f"IC {item['spearman_ic']:.2f}，命中率 {_pct(item['hit_rate_vs_benchmark'])}，结论: {verdict}"
        )
        picks = "、".join(
            f"{x['name']}({_pct(x['actual_return'])})" for x in item["top_selected"][:5]
        )
        lines.append(f"  - Top选择实际表现: {picks}")
    lines += ["", "## 2月底静态预测 Top10", ""]
    static = result.get("forecasts", {}).get("3-5月静态持有", [])
    for i, row in enumerate(static, start=1):
        lines.append(
            f"{i}. {row['name']}({row['code']}) 预测分 {row['prediction']:.4f} 因子分 {row['factor_score']:.2f}"
        )
    if result.get("feature_importance"):
        lines += ["", "## 主要驱动因子", ""]
        for item in result["feature_importance"][:8]:
            lines.append(f"- {item['feature']}: {item['importance']:.4f}")
    lines += [
        "",
        "## 口径说明",
        "",
        "- 使用新浪行业/主题 ETF 日线作为 A 股板块代理；东财 push2 行业板块接口在当前环境返回空响应，未用于本次验收。",
        "- 2 月训练阶段不使用 3-5 月收益；3-5 月只作为样本外验收。",
        "- 以上为历史回测与模型评估，不构成投资建议。",
    ]
    return "\n".join(lines)
