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


def _demote_markdown_headings(md: str) -> str:
    """把嵌入文本里的 #/##/### 类标题降级为 bold 段落,
    避免 swarm 投委会的内置 ## 标题污染外层 docx 目录层级。"""
    import re as _re
    out: list[str] = []
    for ln in md.split("\n"):
        m = _re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", ln)
        if m:
            out.append(f"**{m.group(2).strip()}**")
        else:
            out.append(ln)
    return "\n".join(out)


def build_report_markdown(
    scan_result: dict[str, Any],
    rankings: dict[str, Any],
    debates: dict[str, dict[str, Any]] | None = None,
) -> str:
    """完整 markdown 报告 — 选股优先布局。

    顺序: 一览 → 综合 Top10 → 风险榜 → 低置信榜 → 个股深度 →
          11 类策略命中明细(支撑)→ 方法说明 & 数据(注脚)。
    """
    debates = debates or {}
    analysis_date = scan_result.get("analysis_date") or "?"
    cov = scan_result.get("coverage", {}) or {}
    params = scan_result.get("parameters", {}) or {}
    weights = scan_result.get("strategy_weights") or {}
    names_cn = scan_result.get("strategy_names_cn") or {}
    top = rankings.get("top_overall") or []

    lines: list[str] = []

    # ── 标题 + 一行 metadata ──
    lines += [
        f"# KHunter A 股日报 — {analysis_date}",
        "",
        (f"> 股池 {cov.get('fetched_symbols', '?')}/{cov.get('requested_symbols', '?')} 只 · "
         f"回看 {params.get('days', '?')} 天 · "
         f"耗时 {cov.get('elapsed_seconds', '?')}s · "
         f"综合 Top{len(top)}"),
        "",
    ]

    # ── 选股一览 (Top10 一行带过,让读者第一眼看到名单) ──
    if top:
        names_line = "  ·  ".join(
            f"#{i+1} **{it.get('name', '')}** `{it.get('code', '')}` "
            f"({it.get('total_score', 0):.1f}/10)"
            if isinstance(it.get("total_score"), (int, float)) and it.get("total_score") == it.get("total_score")
            else f"#{i+1} **{it.get('name', '')}** `{it.get('code', '')}` (—)"
            for i, it in enumerate(top[:10])
        )
        lines += ["## 🎯 今日选股一览", "", names_line, ""]

    # ── 综合 Top10 详细表 ──
    lines += ["## 🥇 综合 Top10 详细表", ""]
    if top:
        lines.append("| # | 代码 | 名称 | 综合分 | 置信 | 命中策略 | KH权重 | 风险扣分 | 收盘 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, it in enumerate(top, 1):
            strats = " / ".join((it.get("strategies_cn") or [])[:3])
            ts = it.get("total_score")
            ts_str = f"{ts:.2f}" if isinstance(ts, (int, float)) and ts == ts else "—"
            close = it.get("close")
            close_str = f"{close:.2f}" if isinstance(close, (int, float)) else "—"
            risk = it.get("risk_penalty") or 0
            risk_str = f"-{risk:.1f}" if risk > 0 else "—"
            lines.append(
                f"| {i} | {it.get('code', '')} | {it.get('name', '')} | "
                f"**{ts_str}** | {it.get('confidence', '?')} | {strats} | "
                f"{it.get('max_kh_weight', 0)} | {risk_str} | {close_str} |"
            )
    else:
        lines.append("(无候选)")
    lines.append("")

    # ── 风险榜 ──
    risks = rankings.get("top_risk") or []
    if risks:
        lines += ["## ⚠️ 风险榜", ""]
        lines.append("| 代码 | 名称 | 风险扣分 | 风险点 |")
        lines.append("|---|---|---|---|")
        for it in risks:
            notes = "; ".join((it.get("risk_notes") or [])[:3])
            lines.append(
                f"| {it.get('code', '')} | {it.get('name', '')} | "
                f"-{it.get('risk_penalty', 0):.2f} | {notes} |"
            )
        lines.append("")

    # ── 低置信度榜 ──
    low_conf = rankings.get("top_low_confidence") or []
    if low_conf:
        lines += ["## 📉 低置信度榜 (数据质量 B/C)", ""]
        lines.append("| 代码 | 名称 | 置信 | 数据问题 |")
        lines.append("|---|---|---|---|")
        for it in low_conf:
            notes = "; ".join((it.get("data_quality_notes") or [])[:2])
            lines.append(
                f"| {it.get('code', '')} | {it.get('name', '')} | "
                f"{it.get('confidence', '?')} | {notes} |"
            )
        lines.append("")

    # ── 个股深度 (Top10 每只) — 选股的核心支撑 ──
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
    factor_names_cn_unsupported = {
        "valuation_pe_pb": "估值 (PE/PB)",
        "growth_yoy": "成长 (营收/利润同比)",
        "quality_roe": "质量 (ROE)",
        "research_heat": "研报/公告热度",
    }

    lines += ["## 📂 个股深度 (Top10 每只 — 命中逻辑 / 交易计划 / 辩论)", ""]
    for i, it in enumerate(top, 1):
        code = it.get("code", "")
        name = it.get("name", "")
        strats = ", ".join(it.get("strategies_cn") or [])
        debate = debates.get(code) or {}

        lines.append(f"### #{i} {name} {code}")
        lines.append("")

        # 1) 一行关键指标 (选股决策最关心的)
        ts = it.get("total_score")
        ts_str = f"{ts:.2f}" if isinstance(ts, (int, float)) and ts == ts else "—"
        risk = it.get("risk_penalty") or 0
        close = it.get("close")
        close_str = f"{close:.2f}" if isinstance(close, (int, float)) else "—"
        lines.append(
            f"**综合分 {ts_str}/10**  ·  置信度 {it.get('confidence', '?')}  ·  "
            f"风险扣分 {risk:.2f}  ·  收盘 {close_str}  ·  "
            f"命中 {len(it.get('strategies_cn') or [])} 类 KH 策略 "
            f"(最大权重 {it.get('max_kh_weight', 0)})"
        )
        lines.append("")
        lines.append(f"**KHunter 命中策略**: {strats or '(无)'}")
        lines.append("")

        # 2) 核心结论 (辩论压缩)
        consensus = debate.get("consensus")
        if not consensus:
            sw_md = (debate.get("final_report") or "").strip()
            if sw_md:
                snippet = sw_md.split("\n\n", 1)[0][:250]
                consensus = snippet + ("…" if len(sw_md) > 250 else "")
        if consensus:
            lines.append(f"**核心结论**: {consensus}")
        elif debate.get("status") and debate.get("status") != "completed":
            lines.append(
                f"**核心结论**: ⚠️ 辩论未完成 (`{debate.get('status')}`),"
                f"完整报告查 run `{debate.get('run_id', '')}`"
            )
        else:
            lines.append("**核心结论**: (本次未生成辩论)")
        lines.append("")

        # 3) 交易计划 — 选股报告里最 actionable 的部分,提到辩论之前
        lines.append("**交易计划** (规则生成):")
        if isinstance(close, (int, float)):
            stop_loss = close * 0.93
            target1 = close * 1.08
            target2 = close * 1.15
            lines.append(f"- 触发: 站稳 {close_str},放量阳线确认")
            lines.append(f"- 失效: 跌破 {close * 0.95:.2f} 或量能持续萎缩")
            lines.append(f"- 止损: {stop_loss:.2f} (-7%)")
            lines.append(f"- 止盈: 一档 {target1:.2f} (+8%) / 二档 {target2:.2f} (+15%)")
        else:
            lines.append("- (收盘价缺失,无法生成具体止盈止损)")
        if risk > 1.5:
            lines.append("- 仓位: **1/10** (高风险)")
        elif risk > 0.5:
            lines.append("- 仓位: **1/7** (有风险)")
        else:
            lines.append("- 仓位: **1/5** (常规)")
        lines.append("- 跟踪: 每个交易日盘后复盘")
        lines.append("")

        # 4) 多专家辩论展开
        swarm_md = (debate.get("final_report") or "").strip()
        swarm_status = debate.get("status", "")
        if swarm_md and swarm_status == "completed":
            lines.append("**多专家投委会辩论** (swarm bull/bear/risk/PM):")
            lines.append("")
            lines.append(
                f"> run_id `{debate.get('run_id', '')}` · "
                f"elapsed {debate.get('elapsed', 0):.0f}s"
            )
            lines.append("")
            lines.append(_demote_markdown_headings(swarm_md))
            lines.append("")
        elif swarm_status and swarm_status != "completed":
            lines.append(
                f"**多专家辩论**: ❌ 未完成 — status `{swarm_status}`,"
                f"error: {debate.get('error', '')}  ·  "
                f"run_id `{debate.get('run_id', '')}`"
            )
            lines.append("")
        else:
            # LLM 模式 (structured JSON)
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
                        lines.append(
                            f"- **{g.get('guru', '游资')}** ({g.get('school', '')}): "
                            f"{g.get('view', '')}"
                        )
                lines.append("")
            if debate.get("next_day_validation"):
                lines.append(f"**次日验证**: {debate['next_day_validation']}")
                lines.append("")

        # 5) 因子明细 (附录性质 — 评分的支撑数据)
        raw = it.get("raw_factors") or {}
        z = it.get("z_factors") or {}
        if raw:
            lines.append("**因子明细 (附录)**")
            lines.append("")
            lines.append("| 因子 | raw | z | 备注 |")
            lines.append("|---|---|---|---|")
            for f_key, cn in factor_names_cn.items():
                rv = raw.get(f_key)
                zv = z.get(f_key)
                rv_str = f"{rv:.3f}" if isinstance(rv, (int, float)) and rv == rv else "NaN"
                zv_str = f"{zv:.2f}" if isinstance(zv, (int, float)) and zv == zv else "NaN"
                lines.append(f"| {cn} | {rv_str} | {zv_str} | 已接入 |")
            for f_key, cn in factor_names_cn_unsupported.items():
                lines.append(f"| {cn} | NaN | NaN | 未接入数据源 |")
            lines.append(
                f"| 风险惩罚 | {risk:.2f} | — | 涨幅/波动/流动性扣分 |"
            )
            lines.append("")

    # ── 11 类策略命中明细 (支撑材料,挪到 Top10 之后) ──
    lines += ["## 🧪 11 类策略命中明细 (支撑材料)", ""]
    daily = scan_result.get("daily_results") or []
    if daily:
        last_day = daily[-1]
        strats_dict = last_day.get("strategies") or {}
        ordered = sorted(
            strats_dict.items(),
            key=lambda kv: (weights.get(kv[0], 0), kv[1].get("count", 0)),
            reverse=True,
        )
        for strategy_name, info in ordered:
            cn = names_cn.get(strategy_name, strategy_name)
            w = weights.get(strategy_name, 0)
            hits = info.get("top") or []
            count = info.get("count", 0)
            lines.append(f"### {cn}  ·  权重 {w}  ·  命中 {count} 只")
            if count < 5 and hits:
                lines.append("> ⚠️ 真实命中不足 5 只,以下含按策略条件接近度的弱信号股")
            if hits:
                lines.append("")
                lines.append("| # | 代码 | 名称 | 收盘 | 主要指标 |")
                lines.append("|---|---|---|---|---|")
                for j, h in enumerate(hits, 1):
                    metrics_parts = []
                    for k, v in h.items():
                        if k in ("code", "name", "close", "amount"):
                            continue
                        if isinstance(v, float):
                            metrics_parts.append(f"{k}={v:.3f}")
                        else:
                            metrics_parts.append(f"{k}={v}")
                    metrics = " · ".join(metrics_parts[:4]) or "-"
                    close_str = (f"{h.get('close', '-'):.2f}"
                                  if isinstance(h.get("close"), (int, float))
                                  else str(h.get("close", "-")))
                    lines.append(
                        f"| {j} | {h.get('code', '')} | {h.get('name', '')} | "
                        f"{close_str} | {metrics} |"
                    )
            else:
                lines.append("无命中。")
            lines.append("")
    else:
        lines += ["(无 daily_results 数据)", ""]

    # ── 方法 & 数据 (注脚) ──
    lines += [
        "## 📚 方法说明 & 数据 (注脚)", "",
        "**选股流程**: 11 类技术策略扫描 → 候选股 union → 横截面 9 因子 z-score "
        "→ 风险扣分 → 综合分 (0-10) → Top10 多专家辩论。",
        "",
        f"- 数据源: {scan_result.get('data_source') or '?'}",
        f"- 股池: {params.get('universe_label', '?')} (按成交额排序)",
        f"- 回看窗口: {params.get('days', '?')} 个交易日,K 线历史 "
        f"{params.get('datalen', '?')} 条",
        f"- 实际抓取: {cov.get('fetched_symbols', '?')}/"
        f"{cov.get('requested_symbols', '?')} 只 (失败 {cov.get('error_symbols', '?')})",
        f"- 耗时: {cov.get('elapsed_seconds', '?')} 秒",
        f"- 输出目录: {analysis_date}-khunter-a-share-daily/",
        "",
        "**已接入个股因子**: 20/60 日动量, 20 日波动, 20 日换手代理, 量价相关, "
        "资金流代理, 距 MA20, 当日活跃度, KH 策略权重",
        "",
        "**未接入因子(占位)**: 估值 (PE/PB)、成长 (同比)、质量 (ROE)、研报热度 "
        "— 当前数据源仅覆盖量价/资金面。估值/基本面因子需另接腾讯财经 + 东财研报接口。",
        "",
        "**置信度档位**: A 数据完整且 KH 强信号 / B 数据不足或弱信号 / C 数据质量差",
        "",
        "---",
        "**免责声明**: 以上为量化研究与历史回测/扫描,不构成投资建议。",
        "",
    ]
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
