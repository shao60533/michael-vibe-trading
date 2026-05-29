"""Intraday docx markdown builder — 选股 + 板块异动 + KH 串联 + 分布图。

跟 KHunter docx 一样的可视化风格(progress bar / heat emoji / 仪表盘)。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# 复用 KHunter 的可视化 helper
from khunter_x.visuals import (
    score_bar, heat_emoji, confidence_emoji,
    pct_bar, inflow_bar, histogram, risk_badge,
)

from . import eastmoney, khunter_link
from .service import Pick


def _fmt_yi(v: float) -> str:
    if abs(v) < 1e6:
        return f"{v / 1e4:+,.0f}万"
    return f"{v / 1e8:+.2f}亿"


def _pct_heat(pct: float) -> str:
    """涨幅 → 热度 emoji。"""
    if pct >= 9.5:
        return "🔥"  # 涨停区
    if pct >= 5:
        return "🌡️"
    if pct >= 2:
        return "💧"
    return "❄️"


def build_intraday_report_markdown(snap: dict[str, Any],
                                    label: str = "盘中异动") -> str:
    """构造 intraday docx 用 markdown。

    snap 来自 service.build_snapshot(),包含 boards_concept / picks /
    limit_up_all / limit_up_total / kh_index / ts_bj。
    """
    ts = snap["ts_bj"]
    boards: list[eastmoney.BoardSnapshot] = snap["boards_concept"]
    pool: list[eastmoney.LimitUpStock] = snap.get("limit_up_all", []) or snap["limit_up_top"]
    total_zt = snap["limit_up_total"]
    kh: khunter_link.KHunterIndex = snap["kh_index"]
    picks: list[Pick] = snap["picks"]

    # 统计:涨停板数分布
    boards_dist = Counter()
    for s in pool:
        if s.boards <= 1:
            boards_dist["1 板"] += 1
        elif s.boards == 2:
            boards_dist["2 板"] += 1
        elif s.boards == 3:
            boards_dist["3 板"] += 1
        elif s.boards == 4:
            boards_dist["4 板"] += 1
        else:
            boards_dist["5 板+"] += 1
    dist_buckets = [(k, boards_dist[k]) for k in ["1 板", "2 板", "3 板", "4 板", "5 板+"]
                    if boards_dist.get(k, 0) > 0]

    # 涨停股 KH 串联
    zt_with_kh: list[tuple[eastmoney.LimitUpStock, dict]] = []
    for s in pool:
        hit = kh.lookup_score(s.code)
        if hit is not None:
            zt_with_kh.append((s, hit))

    lines: list[str] = []

    # ── 标题 + metadata ──
    lines += [
        f"# {label} — {ts.strftime('%Y-%m-%d %H:%M')} 北京",
        "",
        (f"> 📦 涨停 **{total_zt}** 只 · "
         f"🥇 异动板块 **{len(boards)}** 个 · "
         f"🎯 异动选股 **{len(picks)}** 只" +
         (f" · 🔬 与 KH {kh.analysis_date} 重合 **{len(zt_with_kh)}** 只"
          if kh.loaded_count > 0 else " · _无 KHunter 串联_")),
        "",
    ]

    # ── 仪表盘 ──
    avg_board_pct = (
        sum(b.pct for b in boards) / len(boards) if boards else None
    )
    top_board_pct = boards[0].pct if boards else None
    n_consec = sum(1 for s in pool if s.boards >= 2)
    n_open = sum(1 for s in pool if s.open_count > 0)
    lines += [
        "## 📊 盘面仪表盘",
        "",
        "| 指标 | 值 | 可视化 |",
        "|---|---|---|",
        f"| 涨停总数 | **{total_zt}** 只 | "
        f"{histogram([('涨停', total_zt)], max_bar_width=20)[0].split(' ', 1)[1] if total_zt else '—'} |",
        f"| 连板股(≥2 板) | **{n_consec}** 只 | "
        f"{'█' * min(n_consec, 30)}{'░' * max(0, 30 - n_consec) if n_consec < 30 else ''} |"
        if n_consec > 0
        else "| 连板股(≥2 板) | 0 只 | — |",
        f"| 炸板股(开板>0) | **{n_open}** 只 | "
        f"{'⚠️' * min(n_open, 6)}{'…' if n_open > 6 else ''} |"
        if n_open > 0
        else "| 炸板股 | 0 只 | — |",
        (f"| 顶部板块涨幅 | **{top_board_pct:+.2f}%** {_pct_heat(top_board_pct)} | "
         f"{pct_bar(top_board_pct)} |"
         if top_board_pct is not None else "| 顶部板块涨幅 | — | — |"),
        (f"| 异动板块平均涨幅 | **{avg_board_pct:+.2f}%** | "
         f"{pct_bar(avg_board_pct)} |"
         if avg_board_pct is not None else "| 异动板块平均涨幅 | — | — |"),
        "",
    ]

    # ── 涨停板数分布(直方图) ──
    if dist_buckets:
        lines += [
            "## 📈 涨停板数分布",
            "",
        ]
        for ln in histogram(dist_buckets, max_bar_width=30):
            lines.append(ln)
        lines.append("")

    # ── 异动选股 Top N(主段) ──
    if picks:
        n_lu = sum(1 for p in picks if p.is_limit_up)
        n_leader = len(picks) - n_lu
        lines += [
            f"## 🎯 异动选股 Top {len(picks)} "
            f"(涨停 {n_lu} · 板块领涨 {n_leader})",
            "",
            "| # | 时间 | 名称 | 代码 | 涨幅/状态 | 主力 | 所属异动 | KH |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, p in enumerate(picks, 1):
            if p.is_limit_up:
                board_tag = f"{p.boards}板" if p.boards >= 2 else "首板"
                extra = f" 开{p.open_count}" if p.open_count > 0 else ""
                status = f"涨停 · {board_tag}{extra}"
                main_col = f"封 {_fmt_yi(p.seal_amount)}"
                source = "涨停"
            else:
                status = f"{p.pct:+.2f}% {_pct_heat(p.pct)}"
                main_col = _fmt_yi(p.board_main_inflow)
                source = p.source_label
            kh_col = (f"{p.kh_score:.1f}/#{p.kh_rank}"
                      if p.kh_score is not None else "—")
            lines.append(
                f"| {i} | `{p.first_seal_hhmm or '—'}` | **{p.name}** | "
                f"`{p.code}` | {status} | {main_col} | {source} | {kh_col} |"
            )
        lines.append("")

    # ── 涨停全榜(按首封时间升序,table) ──
    if pool:
        lines += [
            f"## ⚡ 今日涨停全榜 ({total_zt} 只 · 按首封时间升序)",
            "",
            "| # | 首封 | 名称 | 代码 | 连板 | 封单 | 开板 | KH |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, s in enumerate(pool, 1):
            board_tag = f"**{s.boards}板**" if s.boards >= 2 else "首板"
            seal = _fmt_yi(s.seal_amount)
            open_tag = f"⚠️ 开{s.open_count}" if s.open_count > 0 else "—"
            hit = kh.lookup_score(s.code)
            kh_col = (f"{hit['score']:.1f}/#{hit['rank']}"
                      if hit is not None else "—")
            lines.append(
                f"| {i} | `{s.first_seal_hhmm}` | **{s.name}** | "
                f"`{s.code}` | {board_tag} | {seal} | {open_tag} | {kh_col} |"
            )
        lines.append("")

    # ── 异动板块 Top 20(table with bars) ──
    if boards:
        lines += [
            f"## 🥇 异动板块 Top {min(len(boards), 20)}(按涨幅降序)",
            "",
            "| # | 板块 | 涨幅 (bar) | 主力净流入 | 领涨股 | 领涨涨幅 | KH |",
            "|---|---|---|---|---|---|---|",
        ]
        for i, b in enumerate(boards[:20], 1):
            hit = kh.lookup_score(b.leader_code) if b.leader_code else None
            kh_col = (f"{hit['score']:.1f}/#{hit['rank']}"
                      if hit is not None else "—")
            inflow = _fmt_yi(b.main_inflow)
            lines.append(
                f"| {i} | **{b.name}** | {b.pct:+.2f}% {pct_bar(b.pct)} | "
                f"{inflow} | {b.leader_name or '—'} `{b.leader_code or ''}` | "
                f"{b.leader_pct:+.2f}% {_pct_heat(b.leader_pct)} | {kh_col} |"
            )
        lines.append("")

    # ── KHunter 串联 section ──
    if kh.loaded_count > 0 and zt_with_kh:
        lines += [
            "## 🔬 与 KHunter 日报榜重合(双信号股)",
            "",
            f"> 今日 {total_zt} 只涨停中,**{len(zt_with_kh)}** 只在 KHunter "
            f"{kh.analysis_date} Top{kh.loaded_count} 榜内 — **双信号股**",
            "",
            "| # | 涨停时间 | 名称 | 代码 | 连板 | 封单 | KH 综合分 (bar) | KH 排名 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, (s, hit) in enumerate(zt_with_kh, 1):
            seal = _fmt_yi(s.seal_amount)
            score = hit.get("score")
            score_str = (score_bar(score)
                         if isinstance(score, (int, float)) else "—")
            heat = heat_emoji(score)
            lines.append(
                f"| {i} | `{s.first_seal_hhmm}` | **{s.name}** | "
                f"`{s.code}` | {s.boards}板 | {seal} | {score_str} {heat} | "
                f"#{hit.get('rank', '?')} |"
            )
        lines.append("")
    elif kh.loaded_count > 0:
        lines += [
            "## 🔬 与 KHunter 日报榜重合",
            "",
            f"> 今日涨停股与 KHunter {kh.analysis_date} Top{kh.loaded_count} "
            "榜**无重合** — 涨停股不在今日 KH 综合榜内,纯情绪 / 板块驱动。",
            "",
        ]

    # ── 数据 & 方法 注脚 ──
    lines += [
        "## 📚 数据 & 方法 (注脚)",
        "",
        "- 板块数据: EastMoney push2 (`m:90+t:3` 概念板块榜单)",
        "- 涨停数据: EastMoney `getTopicZTPool` 涨停股池",
        "- 选股逻辑: 涨停股(按首封时间)+ 异动板块领涨股(去重),最多 Top 10",
        "- KH 串联: 加载当日 / 最新的 KHunter `rankings.json` "
        "Top20 做综合分 lookup",
        "- 时间窗: 当下盘面快照 (北京时间 "
        f"{ts.strftime('%Y-%m-%d %H:%M')})",
        "",
        "**热度图例**: 🔥 ≥9.5%(涨停) / 🌡️ 5-9.5% / 💧 2-5% / ❄️ <2%",
        "",
        "**置信度图例**: ✅ A 数据强 / ⚠️ B 数据弱 / ❌ C 数据差",
        "",
        "---",
        "**免责声明**: 量化盘面快照,仅供参考,不构成投资建议。",
        "",
    ]
    return "\n".join(lines)
