"""Assemble outputs/<date>-khunter-a-share-daily/ deliverables.

写入文件:
  - report.md           完整中文报告 (含 11 策略 + 综合 Top + 单股小节)
  - candidates.json     每个策略的候选 + 命中详情
  - candidates.csv      展平的候选表
  - rankings.json       综合排名 + 因子分 + 辩论摘要
  - rankings.csv        排名表 csv
  - generation.log      生成日志(累加)
  - feishu_status.json  飞书推送结果(由 caller 后填)

设计:
- 不抛异常,失败的子文件 silently skip 但 log 记录
- caller 拿到 outputs_dir 路径,负责后续 publish
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path
from typing import Any


# 综合 Top 候选挑选时,从所有命中股 union 后用 factor.total_score 排
TOP_OVERALL_N = 10
TOP_RISK_N = 10
TOP_LOW_CONFIDENCE_N = 10


def _outputs_root() -> Path:
    """优先 STATE_DIR/outputs (持久化), 否则 /tmp/outputs (ephemeral)."""
    state_dir = os.environ.get("STATE_DIR", "").strip().rstrip("/")
    if state_dir:
        return Path(state_dir) / "outputs"
    return Path("/tmp/outputs")


def make_outputs_dir(analysis_date: str) -> Path:
    """{outputs_root}/{date}-khunter-a-share-daily/"""
    folder = _outputs_root() / f"{analysis_date}-khunter-a-share-daily"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _flatten_candidates_to_rows(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """11 策略的 daily_results 最后一日的命中,展平成 [{date, strategy, code, name, ...}, ...]"""
    rows: list[dict[str, Any]] = []
    daily = scan_result.get("daily_results") or []
    if not daily:
        return rows
    last_day = daily[-1]  # analysis_date 那一天的结果
    date_str = last_day.get("date") or ""
    strats = last_day.get("strategies") or {}
    weights = scan_result.get("strategy_weights") or {}
    names_cn = scan_result.get("strategy_names_cn") or {}
    for strategy_name, info in strats.items():
        hits = info.get("top") or []
        rank_in_strat = 0
        for hit in hits:
            rank_in_strat += 1
            row = {
                "date": date_str,
                "strategy": strategy_name,
                "strategy_cn": names_cn.get(strategy_name, strategy_name),
                "weight": weights.get(strategy_name, 0),
                "rank_in_strategy": rank_in_strat,
                "code": hit.get("code", ""),
                "name": hit.get("name", ""),
                "close": hit.get("close"),
                "amount": hit.get("amount"),
                "metrics": json.dumps({k: v for k, v in hit.items()
                                        if k not in ("code", "name", "close", "amount")},
                                       ensure_ascii=False),
            }
            rows.append(row)
    return rows


def _union_candidates(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 code 聚合各策略命中,得到 {code: {code, name, strategies: [...], max_weight, ...}}"""
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = r.get("code") or ""
        if not code:
            continue
        if code not in grouped:
            grouped[code] = {
                "code": code,
                "name": r.get("name") or code,
                "strategies": [],
                "strategy_names_cn": [],
                "max_weight": 0,
                "close": r.get("close"),
                "amount": r.get("amount"),
            }
        grouped[code]["strategies"].append(r.get("strategy", ""))
        grouped[code]["strategy_names_cn"].append(r.get("strategy_cn", ""))
        w = r.get("weight") or 0
        if w > grouped[code]["max_weight"]:
            grouped[code]["max_weight"] = w
    return grouped


def write_candidates_files(outputs_dir: Path,
                            scan_result: dict[str, Any]) -> tuple[Path, Path]:
    """写 candidates.json + candidates.csv"""
    rows = _flatten_candidates_to_rows(scan_result)
    json_path = outputs_dir / "candidates.json"
    csv_path = outputs_dir / "candidates.csv"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "analysis_date": scan_result.get("analysis_date"),
            "rows": rows,
            "total_hits": len(rows),
        }, f, ensure_ascii=False, indent=2)
    if rows:
        fields = ["date", "strategy", "strategy_cn", "weight", "rank_in_strategy",
                  "code", "name", "close", "amount", "metrics"]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
    else:
        csv_path.write_text("(no hits)\n", encoding="utf-8")
    return json_path, csv_path


def build_rankings(
    scan_result: dict[str, Any],
    factor_scores: dict[str, dict[str, Any]],
    debates: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """整合 union 候选 + factor score + debate,生成排名结构。

    Returns:
        {
          "top_overall": [{code, name, ...}],   # 综合 Top10
          "top_risk": [...],                    # 风险 Top10 (高扣分)
          "top_low_confidence": [...],          # 低置信度 Top10
          "all_ranked": [...]                   # 全候选按总分降序
        }
    """
    rows = _flatten_candidates_to_rows(scan_result)
    grouped = _union_candidates(rows)
    debates = debates or {}

    all_items: list[dict[str, Any]] = []
    for code, info in grouped.items():
        score = factor_scores.get(code) or {}
        all_items.append({
            "code": code,
            "name": info["name"],
            "strategies": info["strategies"],
            "strategies_cn": info["strategy_names_cn"],
            "max_kh_weight": info["max_weight"],
            "close": info["close"],
            "amount": info["amount"],
            "total_score": score.get("total_score"),
            "base_score": score.get("base_score"),
            "confidence": score.get("confidence", "?"),
            "risk_penalty": score.get("risk_penalty", 0),
            "risk_notes": score.get("risk_notes") or [],
            "data_quality_notes": score.get("data_quality_notes") or [],
            "raw_factors": score.get("raw") or {},
            "z_factors": score.get("z") or {},
            "has_debate": code in debates,
            "debate": debates.get(code) or None,
        })

    # 排序键: total_score 降 (NaN/None 视为 -inf)
    def _sort_key(it: dict) -> float:
        v = it.get("total_score")
        if v is None or v != v:  # NaN
            return -1e9
        return float(v)

    all_ranked = sorted(all_items, key=_sort_key, reverse=True)
    top_overall = all_ranked[:TOP_OVERALL_N]
    # 风险榜:按 risk_penalty 降序
    top_risk = sorted([it for it in all_items if (it.get("risk_penalty") or 0) > 0.5],
                      key=lambda x: x.get("risk_penalty") or 0, reverse=True)[:TOP_RISK_N]
    # 低置信度榜
    top_low_conf = [it for it in all_items if it.get("confidence") in ("B", "C")][:TOP_LOW_CONFIDENCE_N]

    return {
        "analysis_date": scan_result.get("analysis_date"),
        "top_overall": top_overall,
        "top_risk": top_risk,
        "top_low_confidence": top_low_conf,
        "all_ranked": all_ranked,
    }


def write_rankings_files(outputs_dir: Path, rankings: dict[str, Any]) -> tuple[Path, Path]:
    """写 rankings.json + rankings.csv"""
    json_path = outputs_dir / "rankings.json"
    csv_path = outputs_dir / "rankings.csv"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rankings, f, ensure_ascii=False, indent=2, default=str)
    rows = rankings.get("all_ranked") or []
    if rows:
        fields = ["code", "name", "max_kh_weight", "total_score", "base_score",
                  "confidence", "risk_penalty", "strategies", "close"]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({
                    "code": r.get("code", ""),
                    "name": r.get("name", ""),
                    "max_kh_weight": r.get("max_kh_weight", 0),
                    "total_score": r.get("total_score", ""),
                    "base_score": r.get("base_score", ""),
                    "confidence": r.get("confidence", ""),
                    "risk_penalty": r.get("risk_penalty", 0),
                    "strategies": ", ".join(r.get("strategies") or []),
                    "close": r.get("close", ""),
                })
    else:
        csv_path.write_text("(no rankings)\n", encoding="utf-8")
    return json_path, csv_path


def _format_factor_row(name: str, raw: float, z: float) -> str:
    raw_str = f"{raw:.3f}" if isinstance(raw, (int, float)) and raw == raw else "NaN"
    z_str = f"{z:.2f}" if isinstance(z, (int, float)) and z == z else "NaN"
    return f"| {name} | {raw_str} | {z_str} |"


def build_report_markdown(
    scan_result: dict[str, Any],
    rankings: dict[str, Any],
    debates: dict[str, dict[str, Any]] | None = None,
) -> str:
    """完整 markdown 报告。"""
    debates = debates or {}
    analysis_date = scan_result.get("analysis_date") or "?"
    cov = scan_result.get("coverage", {}) or {}
    params = scan_result.get("parameters", {}) or {}
    weights = scan_result.get("strategy_weights") or {}
    names_cn = scan_result.get("strategy_names_cn") or {}

    lines: list[str] = []
    lines += [
        f"# KHunter A 股日报 — {analysis_date}",
        "",
        "## 方法说明",
        "- 数据源: " + (scan_result.get("data_source") or "?"),
        f"- 股票池: 东财/新浪活跃股,按成交额取前 {params.get('max_symbols', '?')} 只 (实际抓到 {cov.get('fetched_symbols', '?')})",
        f"- 回看窗口: {params.get('days', '?')} 个交易日,K 线历史 {params.get('datalen', '?')} 条",
        "- 11 类技术策略 (KHunter 蒸馏版) — 严格使用真实日线数据,严禁编造",
        f"- 横截面个股因子: 8 个量价因子 + KHunter 策略权重加成,候选股内做 z-score",
        "- 多专家辩论 (LLM 单次调用,7 类专家 + 游资视角)",
        f"- 输出目录: {analysis_date}-khunter-a-share-daily/",
        "",
        "## 数据覆盖",
        f"- 请求股池规模: {cov.get('requested_symbols', '?')} 只",
        f"- 成功拉取 K 线: {cov.get('fetched_symbols', '?')} 只",
        f"- 失败 / 数据不足: {cov.get('error_symbols', '?')} 只",
        f"- 耗时: {cov.get('elapsed_seconds', '?')} 秒",
        "",
    ]

    # 11 类策略
    lines += ["## 11 类策略候选 (按策略权重 + 命中数排序)", ""]
    daily = scan_result.get("daily_results") or []
    if daily:
        last_day = daily[-1]
        strats = last_day.get("strategies") or {}
        # 按权重降序展示
        ordered = sorted(strats.items(),
                          key=lambda kv: (weights.get(kv[0], 0), kv[1].get("count", 0)),
                          reverse=True)
        for strategy_name, info in ordered:
            cn = names_cn.get(strategy_name, strategy_name)
            w = weights.get(strategy_name, 0)
            hits = info.get("top") or []
            count = info.get("count", 0)
            lines.append(f"### {cn} ({strategy_name}) — 权重 {w}")
            lines.append(f"真实命中 **{count}** 只" +
                          (f",展示前 {len(hits)} 只" if hits else " — 无命中"))
            if count < 5:
                lines.append("> ⚠️ 真实命中不足 5 只,以下候选包含按 KHunter 策略条件接近度的弱信号股,标注「补充候选/低置信度」")
            if hits:
                lines.append("")
                lines.append("| 排名 | 代码 | 名称 | 收盘 | 主要指标 |")
                lines.append("|---|---|---|---|---|")
                for i, h in enumerate(hits, 1):
                    metrics_parts = []
                    for k, v in h.items():
                        if k in ("code", "name", "close", "amount"):
                            continue
                        if isinstance(v, float):
                            metrics_parts.append(f"{k}={v:.3f}")
                        else:
                            metrics_parts.append(f"{k}={v}")
                    metrics = " · ".join(metrics_parts[:4]) or "-"
                    close_str = f"{h.get('close', '-'):.2f}" if isinstance(h.get("close"), (int, float)) else str(h.get("close", "-"))
                    lines.append(f"| {i} | {h.get('code', '')} | {h.get('name', '')} | {close_str} | {metrics} |")
            lines.append("")
    else:
        lines.append("(无数据)")
        lines.append("")

    # 综合 Top10
    lines += ["## 综合 Top10 (跨策略 union + 因子总分)", ""]
    top = rankings.get("top_overall") or []
    if top:
        lines.append("| 排名 | 代码 | 名称 | 命中策略 | KH 权重 | 因子总分 | 置信 |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, it in enumerate(top, 1):
            strats = " / ".join((it.get("strategies_cn") or [])[:3])
            ts = it.get("total_score")
            ts_str = f"{ts:.2f}" if isinstance(ts, (int, float)) and ts == ts else "—"
            lines.append(
                f"| {i} | {it.get('code', '')} | {it.get('name', '')} | "
                f"{strats} | {it.get('max_kh_weight', 0)} | {ts_str} | {it.get('confidence', '?')} |"
            )
    else:
        lines.append("(无候选)")
    lines.append("")

    # 风险 Top
    risks = rankings.get("top_risk") or []
    if risks:
        lines += ["## ⚠️ 风险与观察清单", ""]
        lines.append("| 代码 | 名称 | 风险扣分 | 风险点 |")
        lines.append("|---|---|---|---|")
        for it in risks:
            notes = "; ".join((it.get("risk_notes") or [])[:3])
            lines.append(
                f"| {it.get('code', '')} | {it.get('name', '')} | "
                f"{it.get('risk_penalty', 0):.2f} | {notes} |"
            )
        lines.append("")

    # 低置信度
    low_conf = rankings.get("top_low_confidence") or []
    if low_conf:
        lines += ["## 低置信度名单 (数据质量 B/C)", ""]
        lines.append("| 代码 | 名称 | 置信 | 数据问题 |")
        lines.append("|---|---|---|---|")
        for it in low_conf:
            notes = "; ".join((it.get("data_quality_notes") or [])[:2])
            lines.append(
                f"| {it.get('code', '')} | {it.get('name', '')} | "
                f"{it.get('confidence', '?')} | {notes} |"
            )
        lines.append("")

    # 单股小节(综合 Top10 每只)
    lines += ["## 个股小节 (综合 Top 10)", ""]
    factor_names_cn = {
        "momentum_20d": "20 日动量",
        "momentum_60d": "60 日动量",
        "volatility_20d": "20 日波动",
        "turnover_20d": "20 日换手代理",
        "price_volume_corr": "量价相关",
        "money_flow_proxy": "资金流代理",
        "ma20_relative": "距 MA20",
        "today_amount_ratio": "当日活跃度",
        "kh_strategy_weight": "KH 策略权重",
    }
    for i, it in enumerate(top, 1):
        code = it.get("code", "")
        name = it.get("name", "")
        strats = ", ".join(it.get("strategies_cn") or [])
        debate = debates.get(code) or {}

        lines.append(f"### #{i} {code} {name}")
        lines.append("")
        if debate.get("consensus"):
            lines.append(f"**核心结论**: {debate['consensus']}")
        else:
            lines.append("**核心结论**: (LLM 辩论未生成)")
        lines.append("")
        lines.append(f"**KHunter 命中策略**: {strats}  ·  最大权重 {it.get('max_kh_weight', 0)}")
        ts = it.get("total_score")
        ts_str = f"{ts:.2f}" if isinstance(ts, (int, float)) and ts == ts else "—"
        lines.append(f"**因子总分**: {ts_str}  ·  置信度 {it.get('confidence', '?')}  ·  风险扣分 {it.get('risk_penalty', 0):.2f}")
        lines.append("")

        # 因子明细表
        raw = it.get("raw_factors") or {}
        z = it.get("z_factors") or {}
        if raw:
            lines.append("**因子明细**:")
            lines.append("")
            lines.append("| 因子 | raw | z | 含义 |")
            lines.append("|---|---|---|---|")
            for f_key, cn in factor_names_cn.items():
                rv = raw.get(f_key)
                zv = z.get(f_key)
                rv_str = f"{rv:.3f}" if isinstance(rv, (int, float)) and rv == rv else "NaN"
                zv_str = f"{zv:.2f}" if isinstance(zv, (int, float)) and zv == zv else "NaN"
                lines.append(f"| {cn} | {rv_str} | {zv_str} |")
            lines.append("")

        # 多空辩论
        if debate.get("bulls"):
            lines.append("**多方观点**:")
            for b in debate["bulls"]:
                lines.append(f"- {b}")
            lines.append("")
        if debate.get("bears"):
            lines.append("**空方观点**:")
            for b in debate["bears"]:
                lines.append(f"- {b}")
            lines.append("")
        if debate.get("key_disputes"):
            lines.append("**关键分歧**:")
            for d in debate["key_disputes"]:
                lines.append(f"- {d}")
            lines.append("")
        if debate.get("guru_takeaways"):
            lines.append("**游资视角**:")
            for g in debate["guru_takeaways"]:
                if isinstance(g, dict):
                    lines.append(f"- **{g.get('guru', '游资')}**({g.get('school', '')}): {g.get('view', '')}")
            lines.append("")
        if debate.get("next_day_validation"):
            lines.append(f"**次日验证**: {debate['next_day_validation']}")
            lines.append("")

        # 简易交易计划(规则生成 — 不靠 LLM 编)
        lines.append("**交易计划**(规则生成,仅供参考):")
        close = it.get("close")
        close_str = f"{close:.2f}" if isinstance(close, (int, float)) else "?"
        if isinstance(close, (int, float)):
            stop_loss = close * 0.93
            target1 = close * 1.08
            target2 = close * 1.15
            lines.append(f"- 触发条件: 站稳 {close_str},放量阳线确认")
            lines.append(f"- 失效条件: 跌破 {close * 0.95:.2f} 或量能持续萎缩")
            lines.append(f"- 止损: {stop_loss:.2f} (-7%)")
            lines.append(f"- 止盈: 一档 {target1:.2f} (+8%) / 二档 {target2:.2f} (+15%)")
        else:
            lines.append("- (收盘价缺失,无法生成具体止盈止损)")
        risk = it.get("risk_penalty") or 0
        if risk > 1.5:
            lines.append("- 仓位建议: 1/10 (高风险,小仓位试)")
        elif risk > 0.5:
            lines.append("- 仓位建议: 1/7 (有风险,半仓试)")
        else:
            lines.append("- 仓位建议: 1/5 (常规)")
        lines.append("- 跟踪频率: 每个交易日盘后复盘")
        lines.append("")

    lines += ["", "---",
              "**免责声明**: 以上为量化研究与历史回测/扫描,不构成投资建议。",
              ""]
    return "\n".join(lines)


def write_report_markdown(outputs_dir: Path, report_md: str) -> Path:
    """写 report.md"""
    path = outputs_dir / "report.md"
    path.write_text(report_md, encoding="utf-8")
    return path


def append_generation_log(outputs_dir: Path, line: str) -> None:
    """追加生成日志(累加,带时间戳)。"""
    log_path = outputs_dir / "generation.log"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


def write_feishu_status(outputs_dir: Path, status: dict[str, Any]) -> Path:
    """写 / 覆盖 feishu_status.json"""
    path = outputs_dir / "feishu_status.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2, default=str)
    return path
