"""Orchestrate intraday snapshot + Feishu card render.

设计原则: 卡片以**个股**为主,板块只做 context tag。
Flow:
  1. eastmoney.fetch_snapshot — 板块 + 涨停
  2. khunter_link.load_latest_index — leader 个股因子分查找
  3. assemble_picks — 合并涨停 + 板块领涨,去重,排序
  4. build_feishu_card — interactive card,Top N 选股表是主段
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Any

from . import eastmoney, khunter_link


@dataclass(frozen=True)
class Pick:
    """异动选股 — 涨停股 或 板块领涨股。"""
    code: str
    name: str
    pct: float                # 个股涨跌幅 %
    source_label: str         # "涨停" 或 板块名 (e.g. "退税商店")
    is_limit_up: bool
    # 涨停才有:
    boards: int               # 连板数
    seal_amount: float        # 封单 ¥
    first_seal_hhmm: str
    open_count: int
    # 板块领涨才有:
    board_pct: float          # 所属板块涨幅 %
    board_main_inflow: float  # 板块主力净流入 ¥
    # 通用:KHunter 串联
    kh_score: float | None
    kh_rank: int | None


def _assemble_picks(snap: dict[str, Any],
                    kh: khunter_link.KHunterIndex,
                    top_n_boards_for_leaders: int = 8) -> list[Pick]:
    """合并涨停 + Top 板块领涨股,去重(code 优先涨停),返回排序好的 Pick 列表。

    排序规则:
      1. 涨停股按首封时间升序(早封 = 更强势)
      2. 板块领涨股(非涨停)按所属板块涨幅降序
      3. 涨停优先于板块领涨
    """
    boards: list[eastmoney.BoardSnapshot] = snap["boards_concept"]
    pool: list[eastmoney.LimitUpStock] = snap["limit_up_all"]

    seen: set[str] = set()
    picks_lu: list[Pick] = []
    picks_leader: list[Pick] = []

    for s in pool:
        if not s.code or s.code in seen:
            continue
        seen.add(s.code)
        kh_hit = kh.lookup_score(s.code) or {}
        picks_lu.append(Pick(
            code=s.code, name=s.name,
            pct=10.0 if s.open_count == 0 else 9.95,
            source_label="涨停",
            is_limit_up=True,
            boards=s.boards, seal_amount=s.seal_amount,
            first_seal_hhmm=s.first_seal_hhmm,
            open_count=s.open_count,
            board_pct=0.0, board_main_inflow=0.0,
            kh_score=kh_hit.get("score"), kh_rank=kh_hit.get("rank"),
        ))

    for b in boards[:top_n_boards_for_leaders]:
        if not b.leader_code or b.leader_code in seen:
            continue
        seen.add(b.leader_code)
        kh_hit = kh.lookup_score(b.leader_code) or {}
        picks_leader.append(Pick(
            code=b.leader_code, name=b.leader_name,
            pct=b.leader_pct,
            source_label=b.name,
            is_limit_up=False,
            boards=0, seal_amount=0.0,
            first_seal_hhmm="", open_count=0,
            board_pct=b.pct, board_main_inflow=b.main_inflow,
            kh_score=kh_hit.get("score"), kh_rank=kh_hit.get("rank"),
        ))

    picks_lu.sort(key=lambda p: p.first_seal_hhmm or "99:99")
    picks_leader.sort(key=lambda p: p.board_pct, reverse=True)
    return picks_lu + picks_leader


def _build_sector_movers(boards: list[eastmoney.BoardSnapshot],
                         kh: khunter_link.KHunterIndex,
                         top_sectors: int,
                         movers_per_sector: int) -> list[dict[str, Any]]:
    """对 Top 异动板块逐个抓成分股,挑板块内异动个股(主力净流入降序)。

    板块排序: 有主力净流入数据时按资金降序(异动信号),否则保留涨幅序。
    push2 成分股不可用的板块优雅跳过(只展示板块,不展示个股)。
    """
    ranked = list(boards)
    if any(b.main_inflow for b in ranked):
        ranked = sorted(ranked, key=lambda b: b.main_inflow, reverse=True)

    out: list[dict[str, Any]] = []
    for b in ranked[:top_sectors]:
        try:
            mem = eastmoney.fetch_board_members(b.code, limit=30, retries=2)
        except eastmoney.EastMoneyError as exc:
            print(f"[intraday/em] board members {b.code} {b.name} down: {exc}",
                  flush=True)
            mem = []
        movers = [m for m in mem if m.pct >= 2.0][:movers_per_sector]
        if not movers and mem:
            movers = mem[:movers_per_sector]
        zt = sum(1 for m in mem if m.is_limit_up)
        enr = []
        for m in movers:
            hit = kh.lookup_score(m.code) or {}
            enr.append({"m": m, "kh_score": hit.get("score"),
                        "kh_rank": hit.get("rank")})
        out.append({"board": b, "movers": enr,
                    "zt_count": zt, "member_total": len(mem)})
    return out


async def build_snapshot(top_boards: int = 10, top_limit_up: int = 5,
                          top_picks: int = 10,
                          top_sectors: int = 5,
                          movers_per_sector: int = 4,
                          with_briefing: bool = False) -> dict[str, Any]:
    """完整快照。

    重点已切换为**板块异动**: sector_movers = Top 异动板块 + 板块内异动个股。
    picks(涨停/领涨个股)仍保留供 docx 用,但卡片不再以其为主线。
    """
    started = time.time()
    snap = eastmoney.fetch_snapshot(
        top_boards=top_boards, top_limit_up=top_limit_up,
    )
    kh_index = khunter_link.load_latest_index()
    picks = _assemble_picks(snap, kh_index)[:top_picks]
    sector_movers = _build_sector_movers(
        snap["boards_concept"], kh_index, top_sectors, movers_per_sector)

    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)

    return {
        "ts_bj": now,
        "boards_concept": snap["boards_concept"],
        "boards_error": snap.get("boards_error"),
        "sector_movers": sector_movers,
        "limit_up_top": snap["limit_up_top"],
        "limit_up_all": snap["limit_up_all"],
        "limit_up_total": snap["limit_up_total"],
        "kh_index": kh_index,
        "picks": picks,
        "elapsed_sec": round(time.time() - started, 2),
    }


# ─────────── Feishu card render ───────────


def _fmt_yi(v: float) -> str:
    if abs(v) < 1e6:
        return f"{v / 1e4:+,.0f}万"
    return f"{v / 1e8:+.2f}亿"


def _pick_line(idx: int, p: Pick) -> str:
    """单股一行。"""
    kh_tag = ""
    if p.kh_score is not None:
        kh_tag = f" `[KH {p.kh_score:.1f}/#{p.kh_rank}]`"

    if p.is_limit_up:
        board_tag = f"{p.boards}板" if p.boards >= 2 else "首板"
        seal = _fmt_yi(p.seal_amount)
        extra = f" 开{p.open_count}" if p.open_count > 0 else ""
        return (f"`{idx:>2}.` `{p.first_seal_hhmm}` **{p.name}** "
                f"`{p.code}` · {board_tag} 封 {seal}{extra}{kh_tag}")
    inflow = _fmt_yi(p.board_main_inflow)
    return (f"`{idx:>2}.` **{p.name}** `{p.code}`  **{p.pct:+.2f}%**  "
            f"← _{p.source_label}_ (板块 {p.board_pct:+.2f}%, 主力 {inflow}){kh_tag}")


def _board_brief_line(b: eastmoney.BoardSnapshot) -> str:
    """板块速览一行(纯 context,缩到一行)。"""
    inflow = _fmt_yi(b.main_inflow)
    return (f"**{b.name}** {b.pct:+.2f}% 主力 {inflow} 领涨 {b.leader_name}")


def _mover_line(item: dict[str, Any]) -> str:
    """板块内异动个股一行。"""
    m: eastmoney.BoardMember = item["m"]
    kh_tag = ""
    if item.get("kh_score") is not None:
        kh_tag = f" `[KH{item['kh_score']:.0f}/#{item['kh_rank']}]`"
    flag = " 🔴涨停" if m.is_limit_up else ""
    inflow = _fmt_yi(m.main_inflow)
    return (f"　▸ **{m.name}** `{m.code}` {m.pct:+.2f}% "
            f"主力{inflow}{flag}{kh_tag}")


def _sector_block_lines(s: dict[str, Any]) -> list[str]:
    """一个异动板块 + 其板块内异动个股,多行。"""
    b: eastmoney.BoardSnapshot = s["board"]
    inflow = _fmt_yi(b.main_inflow)
    zt = s.get("zt_count") or 0
    head = f"**🔸 {b.name}** {b.pct:+.2f}% · 主力 {inflow}"
    if zt:
        head += f" · 涨停 {zt} 家"
    lines = [head]
    movers = s.get("movers") or []
    if movers:
        lines.extend(_mover_line(it) for it in movers)
    else:
        lines.append("　▸ _板块内个股数据暂不可用_")
    return lines


def render_feishu_card(snap: dict[str, Any],
                       title_prefix: str = "🔥 盘中异动",
                       feishu_doc_url: str | None = None,
                       notion_url: str | None = None) -> dict:
    """构造飞书 interactive card — 选股表是主段,可选挂 docx/notion 按钮。"""
    ts: datetime.datetime = snap["ts_bj"]
    boards: list[eastmoney.BoardSnapshot] = snap["boards_concept"]
    sectors: list[dict[str, Any]] = snap.get("sector_movers") or []
    total_zt = snap["limit_up_total"]
    kh = snap["kh_index"]

    elements: list[dict[str, Any]] = []

    # 主段: 异动板块 + 板块内异动个股 (板块为主线,个股为佐证)
    if sectors:
        blocks = []
        for s in sectors:
            blocks.append("\n".join(_sector_block_lines(s)))
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": (f"**🔥 异动板块 Top {len(sectors)}** "
                                   f"(资金/涨幅领先 · 含板块内异动个股)\n\n"
                                   + "\n\n".join(blocks))},
        })
    elif boards:
        # 有板块榜但拿不到成分股(如 Sina 兜底源)→ 退化为板块速览
        brief_lines = [f"• {_board_brief_line(b)}" for b in boards[:6]]
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**🔥 异动板块速览**\n" + "\n".join(brief_lines)},
        })
    elif snap.get("boards_error"):
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": ("**🔥 异动板块**\n"
                                   "⚠️ 板块榜单数据源临时不可用 (EastMoney push2 "
                                   "`clist/get` 502),涨停温度计不受影响")},
        })
    else:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                      "content": "**🔥 异动板块**\n_当前无明显异动板块_"},
        })

    # 次段: 涨停温度计 (纯 context,不再是主角)
    elements.append({"tag": "hr"})
    lu_top: list[eastmoney.LimitUpStock] = snap.get("limit_up_top") or []
    max_boards = max((s.boards for s in snap.get("limit_up_all") or []),
                     default=0)
    therm = f"**🌡 涨停温度计** 全市场 {total_zt} 家涨停"
    if max_boards >= 2:
        therm += f" · 最高 {max_boards} 连板"
    if lu_top:
        names = "、".join(f"{s.name}({s.boards}板)" if s.boards >= 2 else s.name
                          for s in lu_top[:5])
        therm += f"\n早封强势: {names}"
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": therm},
    })

    # 链接按钮
    actions: list[dict] = []
    if feishu_doc_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "📄 完整盘面文档"},
            "url": feishu_doc_url,
            "type": "primary",
        })
    if notion_url:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🗂 Notion 备份"},
            "url": notion_url,
            "type": "default",
        })
    if actions:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": actions})

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
