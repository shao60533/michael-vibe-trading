# -*- coding: utf-8 -*-
"""KHunter 收盘复盘:读取最新选股 → 取实时价 → 算收益 → Pillow 渲染长图。

设计目标:服务端常驻(无浏览器),纯 Python 出图,中文用 Noto/PingFang。
三段:当日选股(带 swarm 买/观/避 标签) / 近7天复盘 / swarm 深度分析交叉分桶。
对外主入口:
  build_recap_data()  -> dict | None   (None 表示无可复盘数据,调用方应跳过)
  render_recap_png(data, out_path)      -> bytes  (同时写文件并返回 bytes)
"""
from __future__ import annotations

import json
import os
import re
import statistics
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

# ───────────────────────── 数据层 ─────────────────────────

_DIR_RE = re.compile(r"(\d{4}-\d{2}-\d{2})-khunter-a-share-daily$")


def _outputs_root() -> Path:
    """与 delivery._outputs_root 保持一致:STATE_DIR/outputs 优先,否则 /tmp/outputs。"""
    state_dir = os.environ.get("STATE_DIR", "").strip().rstrip("/")
    if state_dir:
        return Path(state_dir) / "outputs"
    return Path("/tmp/outputs")


def _all_ranking_dirs() -> list[tuple[str, Path]]:
    """[(date, rankings.json path), ...] 按日期升序。"""
    out = []
    root = _outputs_root()
    if not root.exists():
        return out
    for d in root.iterdir():
        m = _DIR_RE.search(d.name)
        if not m:
            continue
        rj = d / "rankings.json"
        if rj.exists():
            out.append((m.group(1), rj))
    out.sort(key=lambda x: x[0])
    return out


def _picks_from_rankings(path: Path) -> list[dict]:
    """从 rankings.json 取 top_overall,返回 [{code,name,entry,strat,report}, ...]。

    report = swarm investment_committee 深度分析报告(status=completed 才有),供 verdict 分类。
    """
    try:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = []
    for it in (r.get("top_overall") or []):
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        db = it.get("debate") or {}
        report = ""
        if isinstance(db, dict) and db.get("status") == "completed":
            report = db.get("final_report") or ""
        rows.append({
            "code": code,
            "name": it.get("name") or code,
            "entry": it.get("close"),
            "strat": "、".join(it.get("strategies_cn") or []),
            "report": report,
        })
    return rows


# ───────────────────────── swarm verdict 分类 ─────────────────────────

_VERDICT_SYS = (
    "你是A股投资助理。下面是某只股票投资委员会(swarm)深度分析报告的结论部分。"
    "判断报告对【当前价位立即建仓买入】的最终态度,只输出JSON,不要多余文字:"
    '{"verdict":"买入|观望|回避","reason":"≤12字理由"}。'
    "定义:买入=明确建议现价/当前即可建仓;观望=建议等回调、有条件才买、或暂不建仓;"
    "回避=看空/不建议参与。"
)


def _ds_creds() -> tuple[str, str]:
    key = (os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com/v1").rstrip("/")
    return key, base


def _classify_verdict(report: str) -> dict | None:
    """对单份 swarm 报告分类 → {verdict, reason}。失败返回 None。"""
    key, base = _ds_creds()
    if not key or not report:
        return None
    body = {
        "model": os.environ.get("RECAP_VERDICT_MODEL", "deepseek-chat"),
        "temperature": 0, "max_tokens": 80,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _VERDICT_SYS},
            {"role": "user", "content": "报告结论:\n" + report[-5000:]},
        ],
    }
    for _attempt in range(2):
        try:
            req = urllib.request.Request(
                base + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            v = json.loads(d["choices"][0]["message"]["content"])
            verdict = str(v.get("verdict", "")).strip()
            if verdict in ("买入", "观望", "回避"):
                return {"verdict": verdict, "reason": str(v.get("reason", ""))[:20]}
        except Exception:
            continue
    return None


def _verdicts_for_dir(ranking_path: Path, picks: list[dict],
                      classify_missing: bool) -> dict[str, dict]:
    """读/建该日 verdicts.json 缓存。classify_missing=True 时对缺失且有报告的现分类。"""
    cache_path = ranking_path.parent / "verdicts.json"
    cache: dict[str, dict] = {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if classify_missing:
        changed = False
        for p in picks:
            code = p["code"]
            if code in cache:
                continue
            if p.get("report"):
                v = _classify_verdict(p["report"])
                if v:
                    cache[code] = v
                    changed = True
        if changed:
            try:
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=1),
                    encoding="utf-8")
            except Exception:
                pass
    return cache


def _tencent_prefix(code: str) -> str:
    return ("sh" if code[0] == "6" else "sz") + code


def fetch_quotes(codes: list[str]) -> dict[str, float]:
    """腾讯行情批量取最新价。返回 {code: price}。失败的 code 不在结果里。"""
    px: dict[str, float] = {}
    if not codes:
        return px
    codes = list(dict.fromkeys(codes))
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        q = ",".join(_tencent_prefix(c) for c in batch)
        url = "https://qt.gtimg.cn/q=" + q
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Referer": "https://gu.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
        except Exception:
            continue
        for line in raw.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            val = line.split("=", 1)[1].strip().strip('"')
            if not val:
                continue
            f = val.split("~")
            if len(f) < 5:
                continue
            try:
                p = float(f[3])
            except ValueError:
                continue
            if p > 0:
                px[f[2]] = p
    return px


def build_recap_data() -> dict | None:
    """组装复盘数据(当日 / 近7天 / swarm交叉)。无最新选股则返回 None。"""
    dirs = _all_ranking_dirs()
    if not dirs:
        return None
    today_date, today_path = dirs[-1]
    today_picks = _picks_from_rankings(today_path)
    if not today_picks:
        return None

    # 所有历史选股 + swarm verdict(最新一天现场分类,历史天只读缓存)
    hist: list[dict] = []
    for date, path in dirs:
        ps = _picks_from_rankings(path)
        vmap = _verdicts_for_dir(path, ps, classify_missing=(path == today_path))
        for p in ps:
            v = vmap.get(p["code"]) or {}
            hist.append({**p, "date": date,
                         "verdict": v.get("verdict"), "vreason": v.get("reason", "")})

    codes = sorted({p["code"] for p in hist})
    px = fetch_quotes(codes)

    def enrich(items: list[dict]) -> list[dict]:
        out = []
        for p in items:
            entry = p.get("entry")
            cur = px.get(p["code"])
            if not entry or not cur:
                continue
            ret = (cur - entry) / entry * 100.0
            out.append({**p, "cur": cur, "ret": ret})
        return out

    allrecs = enrich(hist)
    today = sorted([r for r in allrecs if r["date"] == today_date],
                   key=lambda r: -r["ret"])

    def stats(recs: list[dict]) -> dict:
        if not recs:
            return {"n": 0}
        rets = [r["ret"] for r in recs]
        wins = sum(1 for x in rets if x > 0)
        return {
            "n": len(recs),
            "avg": sum(rets) / len(rets),
            "med": statistics.median(rets),
            "win": wins,
            "winrate": wins / len(rets) * 100.0,
            "best": max(recs, key=lambda r: r["ret"]),
            "worst": min(recs, key=lambda r: r["ret"]),
        }

    # 近 7 个交易日窗口
    all_dates = sorted({d for d, _ in dirs})
    recent_dates = set(all_dates[-7:])
    recent = [r for r in allrecs if r["date"] in recent_dates]

    # 按策略(近 7 天)
    bs: dict[str, list[float]] = defaultdict(list)
    for r in recent:
        for s in (r["strat"].split("、") if r["strat"] else ["(无)"]):
            s = s.strip()
            if s:
                bs[s].append(r["ret"])
    strat_rows = []
    for s, a in bs.items():
        w = sum(1 for x in a if x > 0)
        strat_rows.append({"name": s, "n": len(a), "avg": sum(a) / len(a),
                           "win": w, "winrate": w / len(a) * 100.0})
    strat_rows.sort(key=lambda x: -x["avg"])

    # swarm verdict 分桶(近 7 天)
    vbuckets = {}
    for vk in ("买入", "观望", "回避"):
        sub = [r for r in recent if r.get("verdict") == vk]
        vbuckets[vk] = stats(sub)
    n_classified = sum(1 for r in recent if r.get("verdict"))
    today_buy = [r for r in today if r.get("verdict") == "买入"]

    return {
        "today_date": today_date,
        "today": today,
        "today_stats": stats(today),
        "recent_stats": stats(recent),
        "recent_days": len(recent_dates),
        "strat_rows": strat_rows,
        "vbuckets": vbuckets,
        "n_classified": n_classified,
        "today_buy": today_buy,
        "ucodes": len({r["code"] for r in recent}),
    }


# ───────────────────────── 渲染层 (Pillow) ─────────────────────────

S = 2  # 超采样:所有尺寸已按 2x 直接书写
W = 1080 * S

BG = (245, 246, 248)
CARD = (255, 255, 255)
LINE = (236, 238, 242)
INK = (26, 29, 36)
MUT = (138, 147, 163)
UP = (226, 59, 59)      # 涨 红
DN = (19, 164, 99)      # 跌 绿
FLAT = (138, 147, 163)
UP_BG = (253, 236, 236)
DN_BG = (232, 247, 239)
FLAT_BG = (240, 242, 245)
WAIT = (201, 142, 22)
WAIT_BG = (255, 244, 224)
HERO_TOP = (16, 19, 26)
HERO_BOT = (36, 48, 73)

_FONT_CACHE: dict[tuple, Any] = {}

_REG_CANDS = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Light.ttc", 0),
]
_BOLD_CANDS = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    cands = _BOLD_CANDS if bold else _REG_CANDS
    for path, idx in cands:
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size, index=idx)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _sgn(v: float) -> str:
    return f"+{v:.2f}" if v > 0 else f"{v:.2f}"


def _col(v: float):
    return UP if v > 0 else (DN if v < 0 else FLAT)


def _colbg(v: float):
    return UP_BG if v > 0 else (DN_BG if v < 0 else FLAT_BG)


def render_recap_png(data: dict, out_path: str) -> bytes:
    from PIL import Image, ImageDraw

    H = 9000 * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    M = 56 * S
    cx0, cx1 = M, W - M

    def text(x, y, s, font, fill, anchor="la"):
        d.text((x, y), s, font=font, fill=fill, anchor=anchor)

    def tw(s, font):
        return d.textlength(s, font=font)

    def rrect(box, r, fill=None, outline=None, width=1):
        d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)

    y = 0

    # ── HERO ──
    hero_h = 230 * S
    for i in range(hero_h):
        t = i / hero_h
        c = tuple(int(HERO_TOP[k] + (HERO_BOT[k] - HERO_TOP[k]) * t) for k in range(3))
        d.line([(0, i), (W, i)], fill=c)
    text(M, 52 * S, "K H U N T E R  ·  A 股收盘复盘", _font(26 * S, True), (159, 176, 201))
    text(M, 92 * S, "今日选股表现", _font(72 * S, True), (255, 255, 255))
    td = data["today_date"]
    text(M, 178 * S,
         f"入选价＝{td} 收盘 · 现价为当日实时 · 红涨绿跌",
         _font(27 * S, False), (174, 187, 210))
    y = hero_h + 40 * S

    # ── 今日 KPI 卡片 ──
    ts = data["today_stats"]
    kpis = [
        ("今日笔数", f"{ts['n']}", INK, f"{data['today_date']} 选股"),
        ("平均收益", f"{_sgn(ts['avg'])}%", _col(ts["avg"]), f"中位 {_sgn(ts['med'])}%"),
        ("胜率", f"{ts['winrate']:.0f}%", INK, f"{ts['win']} 胜 / {ts['n']-ts['win']} 负"),
        ("最佳", f"{ts['best']['ret']:+.1f}%", _col(ts['best']['ret']), ts["best"]["name"][:6]),
    ]
    gap = 16 * S
    kw = (cx1 - cx0 - gap * 3) // 4
    kh = 150 * S
    for i, (lab, val, vc, ex) in enumerate(kpis):
        bx = cx0 + i * (kw + gap)
        rrect([bx, y, bx + kw, y + kh], 16 * S, fill=CARD, outline=LINE, width=S)
        text(bx + 22 * S, y + 24 * S, lab, _font(25 * S, True), MUT)
        text(bx + 22 * S, y + 58 * S, val, _font(50 * S, True), vc)
        text(bx + 22 * S, y + 116 * S, ex, _font(23 * S, False), MUT)
    y += kh + 44 * S

    def section_title(title, note):
        nonlocal y
        d.rounded_rectangle([cx0, y, cx0 + 8 * S, y + 38 * S], radius=3 * S, fill=UP)
        text(cx0 + 22 * S, y - 2 * S, title, _font(40 * S, True), INK)
        y += 50 * S
        if note:
            text(cx0 + 22 * S, y, note, _font(24 * S, False), MUT)
            y += 38 * S
        else:
            y += 6 * S

    # ── 今日选股明细表(带 swarm 买/观/避 标签) ──
    section_title("今日操作标的", "按今日收益排序 · 入选价 → 现价 · 标签为 swarm 深度分析结论")
    rows = data["today"]
    row_h = 70 * S
    head_h = 56 * S
    tbl_h = head_h + row_h * len(rows)
    rrect([cx0, y, cx1, y + tbl_h], 16 * S, fill=CARD, outline=LINE, width=S)
    c_rank = cx0 + 22 * S
    c_name = cx0 + 70 * S
    c_entry = cx0 + 460 * S
    c_cur = cx0 + 580 * S
    c_ret = cx0 + 730 * S
    c_strat = cx0 + 752 * S
    hf = _font(23 * S, True)
    text(c_rank, y + 18 * S, "#", hf, MUT)
    text(c_name, y + 18 * S, "个股", hf, MUT)
    text(c_entry, y + 18 * S, "入选价", hf, MUT, anchor="ra")
    text(c_cur, y + 18 * S, "现价", hf, MUT, anchor="ra")
    text(c_ret, y + 18 * S, "收益", hf, MUT, anchor="ra")
    text(c_strat, y + 18 * S, "结论/策略", hf, MUT)
    d.line([(cx0 + 14 * S, y + head_h), (cx1 - 14 * S, y + head_h)], fill=LINE, width=S)
    ry = y + head_h
    for i, r in enumerate(rows, 1):
        if i % 2 == 0:
            d.rectangle([cx0 + S, ry, cx1 - S, ry + row_h], fill=(252, 252, 253))
        midy = ry + row_h // 2
        rkc = UP if i <= 3 else MUT
        text(c_rank, midy, str(i), _font(25 * S, True), rkc, anchor="lm")
        text(c_name, midy - 16 * S, r["name"][:7], _font(28 * S, True), INK, anchor="lm")
        text(c_name, midy + 16 * S, r["code"], _font(21 * S, False), MUT, anchor="lm")
        text(c_entry, midy, f"{r['entry']:.2f}", _font(26 * S, False), INK, anchor="rm")
        text(c_cur, midy, f"{r['cur']:.2f}", _font(26 * S, False), INK, anchor="rm")
        ps = f"{_sgn(r['ret'])}%"
        pf = _font(25 * S, True)
        pw = tw(ps, pf) + 26 * S
        rrect([c_ret - pw, midy - 22 * S, c_ret, midy + 22 * S], 22 * S, fill=_colbg(r["ret"]))
        text(c_ret - pw / 2, midy, ps, pf, _col(r["ret"]), anchor="mm")
        # swarm 结论小标签
        sx = c_strat
        vd = r.get("verdict")
        if vd in ("买入", "观望", "回避"):
            vc = {"买入": UP, "观望": WAIT, "回避": DN}[vd]
            vbg = {"买入": UP_BG, "观望": WAIT_BG, "回避": DN_BG}[vd]
            vlab = {"买入": "买", "观望": "观", "回避": "避"}[vd]
            vf = _font(20 * S, True)
            vw = tw(vlab, vf) + 18 * S
            rrect([sx, midy - 18 * S, sx + vw, midy + 18 * S], 18 * S, fill=vbg)
            text(sx + vw / 2, midy, vlab, vf, vc, anchor="mm")
            sx += vw + 8 * S
        st = r["strat"]
        sf = _font(21 * S, False)
        maxw = cx1 - 18 * S - sx
        while st and tw(st, sf) > maxw:
            st = st[:-1]
        text(sx, midy, st, sf, (107, 118, 134), anchor="lm")
        ry += row_h
    y += tbl_h + 44 * S

    # ── 近 7 天复盘 ──
    rs = data["recent_stats"]
    if rs["n"] > ts["n"]:
        section_title("近 7 天选股复盘",
                      f"{data['recent_days']} 个交易日 · {rs['n']} 笔 · "
                      f"{data['ucodes']} 只个股 · 入选价→现价")
        cards = [
            ("近7天平均", f"{_sgn(rs['avg'])}%", _col(rs["avg"])),
            ("胜率", f"{rs['winrate']:.0f}%", INK),
            ("最佳", f"{rs['best']['ret']:+.1f}%", _col(rs['best']['ret'])),
            ("最差", f"{rs['worst']['ret']:+.1f}%", _col(rs['worst']['ret'])),
        ]
        kh2 = 120 * S
        for i, (lab, val, vc) in enumerate(cards):
            bx = cx0 + i * (kw + gap)
            rrect([bx, y, bx + kw, y + kh2], 16 * S, fill=CARD, outline=LINE, width=S)
            text(bx + 20 * S, y + 22 * S, lab, _font(23 * S, True), MUT)
            text(bx + 20 * S, y + 54 * S, val, _font(44 * S, True), vc)
        y += kh2 + 30 * S

        srows = data["strat_rows"]
        if srows:
            section_title("按策略表现(近7天·校准)", "平均收益 · 0 轴居中 红涨绿跌")
            maxabs = max(abs(s["avg"]) for s in srows) or 1.0
            bar_h = 60 * S
            tbl2_h = bar_h * len(srows)
            rrect([cx0, y, cx1, y + tbl2_h], 16 * S, fill=CARD, outline=LINE, width=S)
            nm_w = 210 * S
            meta_w = 130 * S
            pct_w = 150 * S
            track_x0 = cx0 + nm_w + meta_w
            track_x1 = cx1 - pct_w
            mid_x = (track_x0 + track_x1) // 2
            by = y
            for s in srows:
                cy = by + bar_h // 2
                text(cx0 + 24 * S, cy, s["name"][:7], _font(26 * S, True), INK, anchor="lm")
                text(cx0 + nm_w + 10 * S, cy, f"{s['n']}笔 {s['winrate']:.0f}%",
                     _font(21 * S, False), MUT, anchor="lm")
                d.line([(mid_x, by + 14 * S), (mid_x, by + bar_h - 14 * S)],
                       fill=(207, 213, 222), width=S)
                half = (track_x1 - track_x0) / 2
                ln = abs(s["avg"]) / maxabs * half
                if s["avg"] > 0:
                    d.rounded_rectangle([mid_x, cy - 13 * S, mid_x + ln, cy + 13 * S],
                                        radius=6 * S, fill=UP)
                elif s["avg"] < 0:
                    d.rounded_rectangle([mid_x - ln, cy - 13 * S, mid_x, cy + 13 * S],
                                        radius=6 * S, fill=DN)
                text(cx1 - 20 * S, cy, f"{_sgn(s['avg'])}%",
                     _font(27 * S, True), _col(s["avg"]), anchor="rm")
                by += bar_h
            y += tbl2_h + 44 * S

    # ── swarm 深度分析交叉(近7天) ──
    vb = data.get("vbuckets") or {}
    n_cl = data.get("n_classified") or 0
    if n_cl > 0:
        section_title("swarm 深度分析交叉(近7天)",
                      f"每只逐只跑投委会深度分析 · 已分类 {n_cl} 笔 · 按结论分桶")
        order = [("买入", UP), ("观望", WAIT), ("回避", DN)]
        kh3 = 152 * S  # 容两行底部指标,避免溢出
        kw3 = (cx1 - cx0 - gap * 2) // 3
        for i, (vk, vc) in enumerate(order):
            st = vb.get(vk) or {"n": 0}
            bx = cx0 + i * (kw3 + gap)
            hl = (vk == "买入")
            rrect([bx, y, bx + kw3, y + kh3], 16 * S, fill=CARD,
                  outline=(UP if hl else LINE), width=(2 * S if hl else S))
            text(bx + 20 * S, y + 20 * S, f"swarm 判「{vk}」",
                 _font(23 * S, True), vc)
            if st["n"]:
                text(bx + 20 * S, y + 54 * S, f"{_sgn(st['avg'])}%",
                     _font(40 * S, True), _col(st["avg"]))
                # 底部指标拆两行,避免单行溢出卡片宽度
                text(bx + 20 * S, y + 104 * S,
                     f"{st['n']}笔 · 胜率{st['winrate']:.0f}%",
                     _font(20 * S, False), MUT)
                text(bx + 20 * S, y + 128 * S,
                     f"中位 {_sgn(st['med'])}%",
                     _font(20 * S, False), MUT)
            else:
                text(bx + 20 * S, y + 64 * S, "—", _font(40 * S, True), MUT)
        y += kh3 + 22 * S

        tb = data.get("today_buy") or []
        if tb:
            names = "  ".join(f"{r['name']}({_sgn(r['ret'])}%)" for r in tb[:6])
            text(cx0, y, f"✅ 今日 swarm 建议买入: {names}", _font(23 * S, True), UP)
        else:
            text(cx0, y, "今日 swarm 无「买入」判定(多为观望/回避)",
                 _font(23 * S, False), MUT)
        y += 44 * S

    # ── 页脚 ──
    d.line([(cx0, y), (cx1, y)], fill=LINE, width=S)
    y += 24 * S
    foot = [
        "※ 口径:KHunter 每日综合排名榜 top_overall,等权、不计仓位与止损,衡量信号质量。",
        "※ 入选价＝选股当日收盘;现价为实时行情(腾讯),收盘后推送即当日收盘表现。",
        "※ swarm 交叉:每只逐只跑投委会深度分析,DeepSeek 将结论分类为 买/观/避;样本越长越可信。",
        "※ 仅供策略校准,非投资建议。",
    ]
    for line in foot:
        text(cx0, y, line, _font(22 * S, False), MUT)
        y += 32 * S
    y += 16 * S
    text(cx0, y, "📈 KHunter 选股引擎", _font(28 * S, True), INK)
    y += 44 * S

    final = img.crop((0, 0, W, min(y + 30 * S, H)))
    final.save(out_path, "PNG")
    import io
    buf = io.BytesIO()
    final.save(buf, "PNG")
    return buf.getvalue()
