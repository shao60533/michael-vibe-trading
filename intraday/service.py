"""Orchestrate intraday snapshot + Feishu card render.

Flow:
  1. eastmoney.fetch_snapshot — 板块 + 涨停
  2. khunter_link.load_latest_index — leader 因子分查找
  3. llm_briefing.make_briefings — Top3 板块单句简评 (async, 可跳过)
  4. build_feishu_card — interactive card
"""
from __future__ import annotations

import datetime
import time
from typing import Any

from . import eastmoney, khunter_link, llm_briefing


async def build_snapshot(top_boards: int = 10, top_limit_up: int = 5,
                          with_briefing: bool = True) -> dict[str, Any]:
    """完整快照,返回可用于 Feishu card 渲染的结构。

    Raises EastMoneyError if 板块/涨停都拿不到 — 上层决定怎么呈现。
    """
    started = time.time()
    snap = eastmoney.fetch_snapshot(
        top_boards=top_boards, top_limit_up=top_limit_up,
    )
    kh_index = khunter_link.load_latest_index()

    boards = snap["boards_concept"]
    # Top3 板块去做 LLM 简评(空跑也快)
    briefings: dict[str, str] = {}
    if with_briefing and len(boards) > 0:
        top3 = boards[:3]
        try:
            briefings = await llm_briefing.make_briefings([
                {
                    "name": b.name, "pct": b.pct,
                    "main_inflow_yi": b.main_inflow_yi(),
                    "leader_name": b.leader_name,
                    "leader_pct": b.leader_pct,
                }
                for b in top3
            ])
        except Exception as exc:
            print(f"[intraday/service] briefing err: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            briefings = {}

    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)

    return {
        "ts_bj": now,
        "boards_concept": boards,
        "limit_up_top": snap["limit_up_top"],
        "limit_up_total": snap["limit_up_total"],
        "kh_index": kh_index,
        "briefings": briefings,
        "elapsed_sec": round(time.time() - started, 2),
    }


# ─────────── Feishu card render ───────────


def _fmt_yi(v: float) -> str:
    """元 → 「±X.XX 亿」字符串。"""
    yi = v / 1e8
    if abs(yi) < 0.01:
        # 直接显示万
        wan = v / 1e4
        return f"{wan:+,.0f}万"
    return f"{yi:+.2f}亿"


def _board_line(idx: int, b: eastmoney.BoardSnapshot,
                 kh: khunter_link.KHunterIndex) -> str:
    """单板块一行 markdown。"""
    leader_part = ""
    if b.leader_name:
        leader_part = f" 领涨 **{b.leader_name}** ({b.leader_pct:+.2f}%)"
        if b.leader_code:
            kh_hit = kh.lookup_score(b.leader_code)
            if kh_hit is not None and kh_hit.get("score") is not None:
                leader_part += f" `[KH {kh_hit['score']:.1f}/#{kh_hit['rank']}]`"
    inflow = _fmt_yi(b.main_inflow)
    return (f"`{idx:>2}.` **{b.name}**  **{b.pct:+.2f}%**  "
            f"主力 {inflow}{leader_part}")


def _limit_up_line(idx: int, s: eastmoney.LimitUpStock) -> str:
    seal = _fmt_yi(s.seal_amount)
    board_tag = f"{s.boards}板" if s.boards >= 2 else "首板"
    extra = f" 开{s.open_count}" if s.open_count > 0 else ""
    return (f"`{idx:>2}.` `{s.first_seal_hhmm}` **{s.name}**  "
            f"{board_tag} 封 {seal}{extra}")


def render_feishu_card(snap: dict[str, Any], title_prefix: str = "🔥 盘中异动") -> dict:
    """构造飞书 interactive card。title_prefix 可加时点("09:55")。"""
    ts: datetime.datetime = snap["ts_bj"]
    boards: list[eastmoney.BoardSnapshot] = snap["boards_concept"]
    pool: list[eastmoney.LimitUpStock] = snap["limit_up_top"]
    total_zt = snap["limit_up_total"]
    kh = snap["kh_index"]
    briefings: dict[str, str] = snap["briefings"]

    elements: list[dict[str, Any]] = []

    # Top10 板块
    if boards:
        board_lines = [_board_line(i + 1, b, kh) for i, b in enumerate(boards)]
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**📊 Top10 板块(按涨幅)**\n"
                                  + "\n".join(board_lines)},
        })
    else:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**📊 Top10 板块**\n_暂无涨幅 > 0 的概念板块_"},
        })

    elements.append({"tag": "hr"})

    # Top5 涨停
    if pool:
        zt_lines = [_limit_up_line(i + 1, s) for i, s in enumerate(pool)]
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**🚀 Top5 涨停(按首封时间)**\n"
                                  + "\n".join(zt_lines)},
        })
    else:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**🚀 涨停**\n_当前还没有涨停封板_"},
        })

    # LLM 简评 — 有的话才加这一段
    if briefings:
        elements.append({"tag": "hr"})
        # 按板块 Top3 的顺序排
        brief_lines: list[str] = []
        for b in boards[:3]:
            txt = briefings.get(b.name)
            if not txt:
                continue
            brief_lines.append(f"**{b.name}**: {txt}")
        if brief_lines:
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md",
                          "content": "**💬 Top3 简评 (AI)**\n"
                                      + "\n".join(brief_lines)},
            })

    # 底部 note
    elements.append({"tag": "hr"})
    kh_note = ""
    if kh.loaded_count > 0:
        kh_note = f" · KH榜 {kh.analysis_date} Top{kh.loaded_count}"
    elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": (f"共 {total_zt} 涨停 · 数据 EastMoney push2 · "
                         f"{ts.strftime('%Y-%m-%d %H:%M')} 北京{kh_note}"),
        }],
    })

    title = f"{title_prefix} · {ts.strftime('%H:%M')}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:120]},
            "template": "red",
        },
        "elements": elements,
    }


def render_error_card(reason: str, title_prefix: str = "🔥 盘中异动") -> dict:
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                       "content": f"{title_prefix} · {now.strftime('%H:%M')} (失败)"},
            "template": "grey",
        },
        "elements": [{
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": f"**抓取盘中异动失败**\n{reason}"},
        }, {
            "tag": "note",
            "elements": [{"tag": "plain_text",
                           "content": f"{now.strftime('%Y-%m-%d %H:%M')} 北京"}],
        }],
    }
