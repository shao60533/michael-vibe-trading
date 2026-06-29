"""产业链全栈拆解 长图渲染 (Pillow)。

输入 = value_chain_teardown_team swarm agent 产出的结构化 JSON(schema 见
swarm_presets/value_chain_teardown_team.yaml)。复用 khunter_x.recap 的字体 /
配色基建,红涨绿跌(A股习惯)。verdict / tag 颜色语义:
  机会 / 买入  → 红 (正面)
  咽喉 / 观望  → 金 (中性 / 警示)
  回避        → 绿 (负面)
"""

from __future__ import annotations

import io
import os
from typing import Any

from khunter_x.recap import (
    S, W, BG, CARD, LINE, INK, MUT, UP, DN, FLAT,
    UP_BG, DN_BG, FLAT_BG, WAIT, WAIT_BG, _font,
)

ACCENT = (62, 96, 178)          # 至行 蓝
HERO_TOP = (16, 19, 26)
HERO_BOT = (36, 48, 73)


def _verdict_colors(v: str) -> tuple:
    """返回 (前景, 背景) 配色。"""
    v = (v or "").strip()
    if any(k in v for k in ("机会", "买入", "买")):
        return UP, UP_BG
    if any(k in v for k in ("回避", "卖", "避")):
        return DN, DN_BG
    if any(k in v for k in ("咽喉", "观望", "中性", "持")):
        return WAIT, WAIT_BG
    return FLAT, FLAT_BG


def render_valuechain_png(data: dict, out_path: str) -> bytes:
    """渲染产业链拆解长图,写 out_path 并返回 PNG bytes。"""
    from PIL import Image, ImageDraw

    H = 14000 * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    M = 56 * S
    cx0, cx1 = M, W - M
    cw = cx1 - cx0

    def text(x, y, s, font, fill, anchor="la"):
        d.text((x, y), s, font=font, fill=fill, anchor=anchor)

    def tw(s, font):
        return d.textlength(s, font=font)

    def rrect(box, r, fill=None, outline=None, width=1):
        d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)

    def wrap(s, font, maxw):
        """按像素宽度折行(中英文混排逐字)。"""
        s = str(s or "")
        lines, cur = [], ""
        for ch in s:
            if ch == "\n":
                lines.append(cur); cur = ""; continue
            if tw(cur + ch, font) <= maxw:
                cur += ch
            else:
                lines.append(cur); cur = ch
        if cur:
            lines.append(cur)
        return lines or [""]

    def para(x, y, s, font, fill, maxw, lh):
        for ln in wrap(s, font, maxw):
            text(x, y, ln, font, fill)
            y += lh
        return y

    f_h1 = _font(46, bold=True)
    f_h2 = _font(34, bold=True)
    f_h3 = _font(28, bold=True)
    f_bd = _font(26)
    f_sm = _font(23)
    f_xs = _font(20)

    y = 0

    # ── Hero ──
    hero_h = 200 * S
    for i in range(hero_h):
        t = i / hero_h
        c = tuple(int(HERO_TOP[k] + (HERO_BOT[k] - HERO_TOP[k]) * t) for k in range(3))
        d.line([(0, i), (W, i)], fill=c)
    sector = data.get("sector") or "—"
    text(M, 46 * S, "产业链全栈拆解", _font(30, bold=True), (150, 170, 210))
    text(M, 86 * S, sector, _font(60, bold=True), (255, 255, 255))
    sub = data.get("as_of") or ""
    text(M, 158 * S, f"至行·Zenith Research   {sub}".strip(),
         f_sm, (170, 185, 215))
    y = hero_h + 30 * S

    # ── 单位成本主轴 ──
    ue = data.get("unit_economics")
    if ue:
        bx0, bx1 = cx0, cx1
        lines = wrap(ue, f_bd, cw - 60 * S)
        bh = 36 * S + len(lines) * 36 * S + 30 * S
        rrect((bx0, y, bx1, y + bh), 18 * S, fill=(238, 242, 250),
              outline=(206, 218, 240), width=2)
        text(bx0 + 30 * S, y + 22 * S, "▍单位成本主轴(拆解钻头)", f_h3, ACCENT)
        yy = y + 22 * S + 40 * S
        for ln in lines:
            text(bx0 + 30 * S, yy, ln, f_bd, INK); yy += 36 * S
        y += bh + 26 * S

    # ── 总体结论 ──
    summ = data.get("summary")
    if summ:
        text(cx0, y, "总体结论", f_h2, INK); y += 50 * S
        y = para(cx0, y, summ, f_bd, (60, 66, 78), cw, 38 * S)
        y += 24 * S

    # ── 逐层拆解 (按成本占比) ──
    layers = data.get("layers") or []
    if layers:
        text(cx0, y, "逐层拆解 · 按成本占比下钻", f_h2, INK); y += 56 * S
        for lay in layers:
            name = lay.get("name") or "—"
            cost = lay.get("cost_pct") or ""
            verdict = lay.get("verdict") or ""
            fg, bg = _verdict_colors(verdict)
            desc = lay.get("desc") or ""
            leaders = "、".join(lay.get("leaders") or [])
            profit = lay.get("profit") or ""
            valu = lay.get("valuation") or ""

            desc_lines = wrap(desc, f_sm, cw - 60 * S)
            meta_lines = []
            if leaders:
                meta_lines.append(("龙头", leaders))
            if profit:
                meta_lines.append(("盈利", profit))
            if valu:
                meta_lines.append(("估值", valu))
            card_h = (24 * S + 40 * S + len(desc_lines) * 32 * S
                      + len(meta_lines) * 32 * S + 26 * S)
            rrect((cx0, y, cx1, y + card_h), 16 * S, fill=CARD,
                  outline=LINE, width=2)
            # 左侧成本占比色条
            rrect((cx0, y, cx0 + 10 * S, y + card_h), 16 * S, fill=fg)

            ix = cx0 + 32 * S
            iy = y + 24 * S
            text(ix, iy, name, f_h3, INK)
            # verdict 标签(右上)
            if verdict:
                tag = verdict
                pad = 16 * S
                tagw = tw(tag, f_sm) + pad * 2
                rrect((cx1 - 24 * S - tagw, iy - 4 * S,
                       cx1 - 24 * S, iy + 36 * S), 10 * S, fill=bg)
                text(cx1 - 24 * S - tagw / 2, iy + 16 * S, tag, f_sm, fg,
                     anchor="mm")
            # 成本占比(verdict 左侧)
            if cost:
                ctxt = f"成本 {cost}"
                cw2 = tw(ctxt, f_sm) + 28 * S
                rx1 = cx1 - 24 * S - (tw(verdict, f_sm) + 32 * S + 24 * S
                                      if verdict else 0)
                rrect((rx1 - cw2, iy - 4 * S, rx1, iy + 36 * S), 10 * S,
                      fill=(238, 242, 250))
                text(rx1 - cw2 / 2, iy + 16 * S, ctxt, f_sm, ACCENT,
                     anchor="mm")
            iy += 44 * S
            for ln in desc_lines:
                text(ix, iy, ln, f_sm, (70, 78, 92)); iy += 32 * S
            for label, val in meta_lines:
                text(ix, iy, f"{label}｜", f_sm, MUT)
                text(ix + tw(f"{label}｜", f_sm), iy, val, f_sm, INK)
                iy += 32 * S
            y += card_h + 18 * S
        y += 12 * S

    # ── Tier 0 地基 ──
    tier0 = data.get("tier0") or []
    if tier0:
        text(cx0, y, "Tier 0 · 封装/材料/设备/IP 地基", f_h2, INK); y += 56 * S
        for t0 in tier0:
            name = t0.get("name") or "—"
            desc = t0.get("desc") or ""
            leaders = "、".join(t0.get("leaders") or [])
            body = desc + (f"  ｜龙头：{leaders}" if leaders else "")
            blines = wrap(body, f_sm, cw - 60 * S)
            ch = 24 * S + 38 * S + len(blines) * 32 * S + 22 * S
            rrect((cx0, y, cx1, y + ch), 16 * S, fill=(250, 248, 244),
                  outline=(232, 224, 208), width=2)
            text(cx0 + 30 * S, y + 22 * S, f"◆ {name}", f_h3, (150, 110, 40))
            yy = y + 22 * S + 40 * S
            for ln in blines:
                text(cx0 + 30 * S, yy, ln, f_sm, (90, 80, 64)); yy += 32 * S
            y += ch + 16 * S
        y += 12 * S

    # ── 多棱镜洞察 ──
    prisms = data.get("prisms") or []
    if prisms:
        text(cx0, y, "多棱镜洞察", f_h2, INK); y += 56 * S
        for p in prisms:
            name = p.get("name") or "—"
            insight = p.get("insight") or ""
            line = f"◆ {name}：{insight}"
            ilines = wrap(line, f_bd, cw - 20 * S)
            for j, ln in enumerate(ilines):
                text(cx0 + (0 if j == 0 else 34 * S), y, ln, f_bd,
                     ACCENT if j == 0 else (70, 78, 92))
                y += 38 * S
            y += 8 * S
        y += 16 * S

    # ── 全栈总览表 ──
    overview = data.get("overview") or []
    if overview:
        text(cx0, y, "全栈总览", f_h2, INK); y += 56 * S
        cols = [("环节", 0.28), ("最值得投", 0.24), ("最便宜", 0.24),
                ("最核心", 0.24)]
        xs = [cx0]
        for _, frac in cols:
            xs.append(xs[-1] + int(cw * frac))
        rowh = 56 * S
        # 表头
        rrect((cx0, y, cx1, y + rowh), 10 * S, fill=(238, 242, 250))
        for i, (cname, _) in enumerate(cols):
            text(xs[i] + 18 * S, y + 16 * S, cname, f_h3, ACCENT)
        y += rowh
        for r in overview:
            vals = [r.get("link") or "", r.get("invest") or "",
                    r.get("cheap") or "", r.get("core") or ""]
            cellw = [xs[i + 1] - xs[i] - 28 * S for i in range(4)]
            wrapped = [wrap(vals[i], f_sm, cellw[i]) for i in range(4)]
            rh = max(40 * S, max(len(w) for w in wrapped) * 30 * S + 18 * S)
            d.line([(cx0, y + rh), (cx1, y + rh)], fill=LINE, width=1)
            for i in range(4):
                yy = y + 12 * S
                fnt = f_h3 if i == 0 else f_sm
                for ln in wrapped[i]:
                    text(xs[i] + 18 * S, yy, ln,
                         fnt, INK if i == 0 else (70, 78, 92))
                    yy += 30 * S
            y += rh
        y += 30 * S

    # ── 标的 ──
    picks = data.get("picks") or []
    if picks:
        text(cx0, y, "相关标的", f_h2, INK); y += 56 * S
        for p in picks:
            name = p.get("name") or "—"
            code = p.get("code") or ""
            note = p.get("note") or ""
            tag = p.get("tag") or ""
            fg, bg = _verdict_colors(tag)
            rh = 64 * S
            rrect((cx0, y, cx1, y + rh), 12 * S, fill=CARD, outline=LINE,
                  width=2)
            text(cx0 + 26 * S, y + 18 * S, name, f_h3, INK)
            if code:
                text(cx0 + 26 * S + tw(name, f_h3) + 16 * S, y + 22 * S,
                     code, f_sm, MUT)
            if tag:
                pad = 16 * S
                tagw = tw(tag, f_sm) + pad * 2
                rrect((cx1 - 22 * S - tagw, y + 14 * S, cx1 - 22 * S,
                       y + rh - 14 * S), 10 * S, fill=bg)
                text(cx1 - 22 * S - tagw / 2, y + rh / 2, tag, f_sm, fg,
                     anchor="mm")
            if note:
                nx = cx0 + 26 * S
                text(nx, y + rh - 28 * S, note[:40], f_xs, (110, 118, 132))
            y += rh + 14 * S
        y += 20 * S

    # ── 免责 + 品牌 ──
    disc = data.get("disclaimer") or "方向性/示意,非投资建议。"
    d.line([(cx0, y), (cx1, y)], fill=LINE, width=2); y += 24 * S
    y = para(cx0, y, "※ " + disc, f_xs, MUT, cw, 28 * S)
    y += 10 * S
    text(cx0, y, "至行·Zenith Research · 产业链全栈拆解", f_xs, (150, 160, 178))
    y += 40 * S

    final = img.crop((0, 0, W, min(y + 30 * S, H)))
    final.save(out_path, "PNG")
    buf = io.BytesIO()
    final.save(buf, "PNG")
    return buf.getvalue()
