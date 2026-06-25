# -*- coding: utf-8 -*-
"""KHunter 收盘复盘:读取最新选股 → 取实时价 → 算收益 → Pillow 渲染长图。

设计目标:服务端常驻(无浏览器),纯 Python 出图,中文用 Noto/PingFang。
对外主入口:
  build_recap_data()  -> dict | None   (None 表示无可复盘数据,调用方应跳过)
  render_recap_png(data, out_path)      -> bytes  (同时写文件并返回 bytes)
"""
from __future__ import annotations

import glob
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
    """从 rankings.json 取 top_overall,返回 [{code,name,entry,strat}, ...]。"""
    try:
        r = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = []
    for it in (r.get("top_overall") or []):
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "name": it.get("name") or code,
            "entry": it.get("close"),
            "strat": "、".join(it.get("strategies_cn") or []),
        })
    return rows


def _tencent_prefix(code: str) -> str:
    return ("sh" if code[0] == "6" else "sz") + code


def fetch_quotes(codes: list[str]) -> dict[str, float]:
    """腾讯行情批量取最新价。返回 {code: price}。失败的 code 不在结果里。"""
    px: dict[str, float] = {}
    if not codes:
        return px
    codes = list(dict.fromkeys(codes))
    # 分批,避免 URL 过长
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
    """组装复盘数据。无最新选股则返回 None。"""
    dirs = _all_ranking_dirs()
    if not dirs:
        return None
    today_date, today_path = dirs[-1]
    today_picks = _picks_from_rankings(today_path)
    if not today_picks:
        return None

    # 所有历史选股(累计追踪),按 (date, code) 列表
    hist: list[dict] = []
    for date, path in dirs:
        for p in _picks_from_rankings(path):
            hist.append({**p, "date": date})

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

    today = sorted(enrich(today_picks), key=lambda r: -r["ret"])
    allrecs = enrich(hist)

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

    # 按策略(累计)
    bs: dict[str, list[float]] = defaultdict(list)
    for r in allrecs:
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

    return {
        "today_date": today_date,
        "today": today,
        "today_stats": stats(today),
        "cum_stats": stats(allrecs),
        "strat_rows": strat_rows,
        "days": len({d for d, _ in dirs}),
        "ucodes": len(codes),
    }


# ───────────────────────── 渲染层 (Pillow) ─────────────────────────

S = 2  # 超采样:所有尺寸已按 2x 直接书写
W = 1080 * S

# 颜色
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

    H = 9000 * S  # 画大,最后裁切
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    M = 56 * S          # 左右外边距
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
        ("最佳", f"+{ts['best']['ret']:.1f}%", UP, ts["best"]["name"][:6]),
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

    # ── 区块标题 helper ──
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

    # ── 今日选股明细表 ──
    section_title("今日操作标的", "按今日收益排序 · 入选价 → 现价")
    rows = data["today"]
    row_h = 70 * S
    head_h = 56 * S
    tbl_h = head_h + row_h * len(rows)
    rrect([cx0, y, cx1, y + tbl_h], 16 * S, fill=CARD, outline=LINE, width=S)
    # 列 x(逻辑偏移 ×S,内容区右界约 968)
    c_rank = cx0 + 22 * S
    c_name = cx0 + 70 * S
    c_entry = cx0 + 460 * S   # 入选价 右对齐界
    c_cur = cx0 + 580 * S     # 现价 右对齐界
    c_ret = cx0 + 730 * S     # 收益胶囊 右界
    c_strat = cx0 + 752 * S   # 策略 左起
    # 表头
    hf = _font(23 * S, True)
    text(c_rank, y + 18 * S, "#", hf, MUT)
    text(c_name, y + 18 * S, "个股", hf, MUT)
    text(c_entry, y + 18 * S, "入选价", hf, MUT, anchor="ra")
    text(c_cur, y + 18 * S, "现价", hf, MUT, anchor="ra")
    text(c_ret, y + 18 * S, "收益", hf, MUT, anchor="ra")
    text(c_strat, y + 18 * S, "策略", hf, MUT)
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
        # 收益胶囊
        ps = f"{_sgn(r['ret'])}%"
        pf = _font(25 * S, True)
        pw = tw(ps, pf) + 26 * S
        rrect([c_ret - pw, midy - 22 * S, c_ret, midy + 22 * S], 22 * S, fill=_colbg(r["ret"]))
        text(c_ret - pw / 2, midy, ps, pf, _col(r["ret"]), anchor="mm")
        # 策略(截断)
        st = r["strat"]
        sf = _font(21 * S, False)
        maxw = cx1 - 24 * S - c_strat
        while st and tw(st, sf) > maxw:
            st = st[:-1]
        text(c_strat, midy, st, sf, (107, 118, 134), anchor="lm")
        ry += row_h
    y += tbl_h + 44 * S

    # ── 累计追踪 ──
    cs = data["cum_stats"]
    if cs["n"] > ts["n"]:  # 有历史才显示
        section_title("累计追踪",
                      f"{data['days']} 个交易日 · {cs['n']} 笔 · {data['ucodes']} 只个股")
        # KPI strip
        cards = [
            ("累计平均", f"{_sgn(cs['avg'])}%", _col(cs["avg"])),
            ("累计胜率", f"{cs['winrate']:.0f}%", INK),
            ("最佳", f"+{cs['best']['ret']:.1f}%", UP),
            ("最差", f"{cs['worst']['ret']:.1f}%", DN),
        ]
        kh2 = 120 * S
        for i, (lab, val, vc) in enumerate(cards):
            bx = cx0 + i * (kw + gap)
            rrect([bx, y, bx + kw, y + kh2], 16 * S, fill=CARD, outline=LINE, width=S)
            text(bx + 20 * S, y + 22 * S, lab, _font(23 * S, True), MUT)
            text(bx + 20 * S, y + 54 * S, val, _font(44 * S, True), vc)
        y += kh2 + 30 * S

        # 按策略条形
        srows = data["strat_rows"]
        if srows:
            section_title("按策略表现(校准)", "累计平均收益 · 0 轴居中 红涨绿跌")
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
                # track
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

    # ── 页脚 ──
    d.line([(cx0, y), (cx1, y)], fill=LINE, width=S)
    y += 24 * S
    foot = [
        "※ 口径:KHunter 每日综合排名榜 top_overall,等权、不计仓位与止损,衡量信号质量。",
        "※ 入选价＝选股当日收盘;现价为实时行情(腾讯),收盘后推送即当日收盘表现。",
        "※ 仅供策略校准,非投资建议。",
    ]
    for line in foot:
        text(cx0, y, line, _font(22 * S, False), MUT)
        y += 32 * S
    y += 16 * S
    text(cx0, y, "📈 KHunter 选股引擎", _font(28 * S, True), INK)
    y += 44 * S

    # 裁切
    final = img.crop((0, 0, W, min(y + 30 * S, H)))
    final.save(out_path, "PNG")
    import io
    buf = io.BytesIO()
    final.save(buf, "PNG")
    return buf.getvalue()
