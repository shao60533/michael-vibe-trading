"""Top3 board single-line LLM briefing.

输入: Top3 板块的快照(name, pct, main_inflow, leader)
输出: {board_name: 一句话简评} (每条 ≤ 40 字)
失败:返空 dict,卡片照发只是少这一段。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


def _llm_creds() -> tuple[str, str, str]:
    api_key = (os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("OPENROUTER_API_KEY")
               or os.environ.get("OPENAI_API_KEY") or "").strip()
    base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("OPENROUTER_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LANGCHAIN_MODEL_NAME", "deepseek-v4-pro").strip()
    return api_key, base_url, model


_SYS_PROMPT = """你是 A 股盘中板块异动的简评助理。

要求:
- 给每个输入板块写**一句话**简评(≤40 个汉字),解释「为什么涨」。
- 优先点明短期催化:政策 / 业绩 / 资金抱团 / 概念轮动 / 题材发酵。
- 不要说「短期可关注」「注意风险」这种废话。
- 输出严格 JSON,key 是板块名,value 是一句话。
"""


async def make_briefings(boards: list[dict[str, Any]],
                          timeout: float = 30.0) -> dict[str, str]:
    """对 Top3 板块生成单句简评。

    Args:
        boards: [{name, pct, main_inflow_yi, leader_name, leader_pct}, ...]
    Returns:
        {board_name: "一句话"} —— LLM 没返到的板块不会出现在 dict 里
    """
    if not boards:
        return {}
    api_key, base_url, model = _llm_creds()
    if not api_key:
        print("[intraday/briefing] no LLM api_key, skip", flush=True)
        return {}

    lines = []
    for b in boards:
        lines.append(
            f"- {b.get('name')}  涨{b.get('pct'):.2f}%  "
            f"主力净流入 {b.get('main_inflow_yi'):.2f} 亿  "
            f"领涨 {b.get('leader_name') or '—'} "
            f"({b.get('leader_pct'):.2f}%)"
        )
    user_msg = (
        "下面是 A 股盘中 Top3 板块快照,给每个板块一句话简评(≤40 字),"
        "JSON 格式 {板块名: 简评}:\n" + "\n".join(lines)
    )

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYS_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 400,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=timeout, write=10, pool=5),
        ) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
    except Exception as exc:
        print(f"[intraday/briefing] network err: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return {}
    if r.status_code != 200:
        print(f"[intraday/briefing] HTTP {r.status_code}: "
              f"{r.text[:200]}", flush=True)
        return {}
    try:
        d = r.json()
        content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            print(f"[intraday/briefing] no JSON in content[:200]={content[:200]}",
                  flush=True)
            return {}
        parsed = json.loads(m.group(0))
        # 净化:只保留字符串 value
        out: dict[str, str] = {}
        for k, v in parsed.items():
            if isinstance(v, str) and v.strip():
                out[str(k)] = v.strip()
        return out
    except Exception as exc:
        print(f"[intraday/briefing] parse err: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return {}
