"""Orchestrate A-share industry factor research."""

from __future__ import annotations

import os
from typing import Any

from .data_sources import fetch_industry_panel, fetch_recent_reports
from .factors import FEATURE_COLUMNS, add_cross_sectional_scores, build_industry_features, compute_index_timing
from .model import train_predict_backtest
from .reports import attach_report_scores, summarize_report_industries


def _zscore(series):
    std = series.std(ddof=0)
    if not std:
        return series * 0.0
    return (series - series.mean()) / std


def _row_to_dict(row) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if hasattr(value, "item"):
            value = value.item()
        if hasattr(value, "date"):
            value = str(value.date())
        out[key] = value
    return out


def run_industry_factor_research(
    lookback_days: int = 260,
    test_days: int = 22,
    horizon_days: int = 5,
    top_k: int = 5,
    board_limit: int = 80,
    report_days: int = 7,
    include_reports: bool = True,
    panel_csv: str | None = None,
) -> dict[str, Any]:
    """Run industry factor generation, ML prediction, backtest, and recommendation."""
    panel_csv = panel_csv or os.environ.get("FACTOR_INDUSTRY_PANEL_CSV", "").strip() or None
    if panel_csv:
        import pandas as pd

        panel = pd.read_csv(panel_csv)
        panel["date"] = pd.to_datetime(panel["date"])
    else:
        panel = fetch_industry_panel(limit=board_limit, lookback_days=lookback_days)
    features = add_cross_sectional_scores(build_industry_features(panel, horizon_days=horizon_days))
    model_run = train_predict_backtest(
        features,
        feature_columns=FEATURE_COLUMNS + ["factor_score"],
        test_days=test_days,
        top_k=top_k,
    )

    report_summary: dict[str, Any] = {"total_reports": 0, "industry_count": 0, "top_industries": []}
    report_error = None
    if include_reports:
        try:
            reports = fetch_recent_reports(days=report_days)
            report_summary = summarize_report_industries(reports)
        except Exception as exc:
            report_error = f"{type(exc).__name__}: {exc}"

    latest = model_run.latest_predictions.copy()
    latest = attach_report_scores(latest, report_summary)
    latest["prediction_z"] = _zscore(latest["prediction"])
    latest["factor_score_z"] = _zscore(latest["factor_score"])
    latest["composite_score"] = (
        0.50 * latest["prediction_z"].fillna(0)
        + 0.28 * latest["factor_score_z"].fillna(0)
        + 0.22 * latest["report_heat_z"].fillna(0)
    )
    latest = latest.sort_values("composite_score", ascending=False)

    index_timing = compute_index_timing(features)
    top = latest.head(top_k)
    bottom = latest.tail(top_k).sort_values("composite_score")
    recommendations = []
    for _, row in top.iterrows():
        recommendations.append(
            {
                "code": row["code"],
                "name": row["name"],
                "date": str(row["date"].date() if hasattr(row["date"], "date") else row["date"]),
                "prediction": round(float(row["prediction"]), 6),
                "factor_score": round(float(row["factor_score"]), 4),
                "report_heat_score": round(float(row.get("report_heat_score", 0.0)), 4),
                "report_count": int(row.get("report_count", 0)),
                "composite_score": round(float(row["composite_score"]), 4),
                "momentum_5d": round(float(row.get("momentum_5d", 0.0)), 4),
                "momentum_20d": round(float(row.get("momentum_20d", 0.0)), 4),
                "timing": index_timing["state_zh"],
                "leader": row.get("leader", ""),
            }
        )

    result = {
        "source": {
            "market_data": "Eastmoney industry board snapshot + daily kline",
            "research_reports": "Eastmoney reportapi recent reports" if include_reports else "disabled",
            "method_reference": "QuantsPlaybook factor construction, timing and LightGBM workflow",
        },
        "parameters": {
            "lookback_days": lookback_days,
            "test_days": test_days,
            "horizon_days": horizon_days,
            "top_k": top_k,
            "board_limit": board_limit,
            "report_days": report_days,
            "panel_csv": panel_csv or "",
        },
        "model": {
            "name": model_run.model_name,
            "feature_columns": model_run.feature_columns,
            "feature_importance": model_run.feature_importance,
        },
        "index_timing": index_timing,
        "backtest": model_run.backtest,
        "report_summary": report_summary,
        "report_error": report_error,
        "recommendations": recommendations,
        "avoid_or_watch": [
            {
                "code": row["code"],
                "name": row["name"],
                "prediction": round(float(row["prediction"]), 6),
                "factor_score": round(float(row["factor_score"]), 4),
                "composite_score": round(float(row["composite_score"]), 4),
            }
            for _, row in bottom.iterrows()
        ],
        "latest_snapshot": [_row_to_dict(row) for _, row in latest.head(20).iterrows()],
    }
    result["report_markdown"] = format_markdown_report(result)
    return result


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_markdown_report(result: dict[str, Any]) -> str:
    params = result["parameters"]
    timing = result["index_timing"]
    bt = result["backtest"]
    model = result["model"]
    lines = [
        "# A股行业因子量化分析",
        "",
        f"- 样本: 最近 {params['lookback_days']} 个交易日行业板块，最近 {params['test_days']} 个可回测交易日评估",
        f"- 预测周期: T+1 开盘到 T+{params['horizon_days']} 收盘",
        f"- 模型: {model['name']}",
        f"- 股指/宽基择时: {timing.get('state_zh', '未知')} "
        f"(5日上涨行业占比 {_pct(timing.get('advance_ratio_5d', 0.0))}, "
        f"20日强势行业占比 {_pct(timing.get('above_ma20_ratio', 0.0))})",
        "",
        "## 最近一月回测",
        "",
        f"- Top{bt['top_k']} 轮动累计收益: {_pct(bt['strategy_cumulative_return'])}",
        f"- 全行业等权基准累计收益: {_pct(bt['benchmark_cumulative_return'])}",
        f"- 超额收益: {_pct(bt['excess_cumulative_return'])}",
        f"- 跑赢天数占比: {_pct(bt['win_rate'])}",
        f"- 最大回撤: {_pct(bt['max_drawdown'])}",
        "",
        "## 综合推荐",
        "",
    ]
    for i, item in enumerate(result["recommendations"], start=1):
        lines.append(
            f"{i}. **{item['name']}({item['code']})** | 综合分 {item['composite_score']:.2f} | "
            f"模型预测 {_pct(item['prediction'])} | 因子分 {item['factor_score']:.2f} | "
            f"研报热度 {item['report_heat_score']:.2f}/{item['report_count']}篇 | "
            f"5日 {item['momentum_5d']:.2%} 20日 {item['momentum_20d']:.2%} | "
            f"领涨: {item.get('leader') or '-'}"
        )
    if not result["recommendations"]:
        lines.append("暂无推荐。")

    report_summary = result.get("report_summary") or {}
    lines += ["", "## 近一周热门研报行业", ""]
    for item in (report_summary.get("top_industries") or [])[:8]:
        stocks = "、".join(item.get("mentioned_stocks") or []) or "-"
        lines.append(
            f"- {item['industry']}: {item['report_count']} 篇，评级热度 {item['rating_score']:.2f}，"
            f"高频标的 {stocks}"
        )
    if result.get("report_error"):
        lines.append(f"- 研报抓取失败: {result['report_error']}")

    if model.get("feature_importance"):
        lines += ["", "## 模型主要因子", ""]
        for item in model["feature_importance"][:8]:
            lines.append(f"- {item['feature']}: {item['importance']:.2f}")

    lines += [
        "",
        "## 方法说明",
        "",
        "- 因子框架参考 QuantsPlaybook 的行业有效量价、QRS/鳄鱼线择时、NH-NL 情绪和 LightGBM 工作流。",
        "- 综合分 = 模型预测 50% + 当期横截面因子 28% + 近一周研报热度 22%。",
        "- 以上为量化研究与历史回测，不构成投资建议。",
    ]
    return "\n".join(lines)
