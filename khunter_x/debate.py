"""Multi-voice debate per candidate stock (single LLM call per stock, parallel).

设计:
- 单只股 1 次 DeepSeek 调用 (response_format=json),提示 LLM 模拟 5-7 个专家立场
- 加 1-2 位 A 股游资视角 (从 _GURU_SKILLS 选)
- 并行 asyncio.gather (上限并发,防限速)
- 失败一只不影响其他

输出 schema (per code):
{
  "bulls": ["多方观点1", ...],          # 3-5 条
  "bears": ["空方观点1", ...],          # 3-5 条
  "key_disputes": ["分歧 1", ...],     # 1-3 条
  "consensus": "共识结论",              # 1-2 句
  "next_day_validation": "次日验证点",
  "guru_takeaways": [{"guru": "X", "school": "Y", "view": "..."}]
}
"""
from __future__ import annotations

import asyncio
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


def _pick_gurus_for_stock(strategies: list[str]) -> list[tuple[str, str]]:
    """根据命中策略挑 1-2 位最合适的游资 (display_name, school)。
    映射启发式 — 不调 LLM,确定性。"""
    # 策略 → 游资偏好
    s_set = set(strategies or [])
    candidates: list[tuple[str, str]] = []
    if any(s in s_set for s in ("LimitUpSideways", "LimitUpPullback")):
        candidates.append(("北京炒家", "模式派"))
    if any(s in s_set for s in ("BottomTrendInflection", "WBottom", "MorningStar")):
        candidates.append(("归因", "资讯派"))
    if any(s in s_set for s in ("ResistanceBreakout", "ImmortalGuidance", "MultiGoldenCross")):
        candidates.append(("陈小群", "龙头信仰派"))
    if any(s in s_set for s in ("TrendAccelerationInflection", "MultiPartyCannon")):
        candidates.append(("一瞬流光", "高位接力派"))
    if "StrongWashWeakToStrong" in s_set:
        candidates.append(("涅盘重升", "资金流派"))
    # 小鳄鱼 通用兜底 (理解力派)
    if not candidates:
        candidates.append(("小鳄鱼", "理解力派"))
    # 去重 + cap 2
    seen = set()
    out: list[tuple[str, str]] = []
    for n, s in candidates:
        if n not in seen:
            seen.add(n)
            out.append((n, s))
        if len(out) >= 2:
            break
    return out


async def _debate_one(
    client: httpx.AsyncClient,
    code: str,
    name: str,
    strategies: list[str],
    factor_score: dict[str, Any],
    news_excerpt: str = "",
) -> dict[str, Any] | None:
    """单只股一次 LLM 调用,返回 debate dict 或 None。"""
    api_key, base_url, model = _llm_creds()
    if not api_key:
        return None

    gurus = _pick_gurus_for_stock(strategies)
    gurus_block = ", ".join(f"{n}({s})" for n, s in gurus) or "小鳄鱼(理解力派)"
    strats_zh = ", ".join(strategies) if strategies else "(无)"

    total = factor_score.get("total_score") if factor_score else None
    confidence = factor_score.get("confidence") if factor_score else "?"
    raw_factors = factor_score.get("raw", {}) if factor_score else {}
    factor_brief = "; ".join(
        f"{k}={v:.3f}" if isinstance(v, (int, float)) and v == v else f"{k}=NaN"
        for k, v in list(raw_factors.items())[:6]
    )

    sysprompt = (
        "你是 A 股多专家投研主持。模拟以下 7 类专家围绕一只候选股给出辩论:\n"
        "1. 技术派(K线/形态/MACD/KDJ)\n"
        "2. 资金面分析师(净流入/北向/龙虎榜)\n"
        "3. 行业景气派(产业链/景气度/政策)\n"
        "4. 基本面派(财报/估值/成长性)\n"
        "5. 事件催化派(公告/突发/季节性)\n"
        "6. 风险暴露派(限售/质押/雷点)\n"
        f"7. 游资视角({gurus_block}) — 必须按指定的派别风格,口语化\n\n"
        "返回严格 JSON:\n"
        '{\n'
        '  "bulls": ["多方观点1(标专家身份)", "多方观点2", "..."],\n'
        '  "bears": ["空方观点1(标专家身份)", "..."],\n'
        '  "key_disputes": ["核心分歧 1", "..."],\n'
        '  "consensus": "共识结论(1-2 句)",\n'
        '  "next_day_validation": "次日要验证的关键数据/事件",\n'
        '  "guru_takeaways": [{"guru": "X", "school": "Y", "view": "30-60 字游资视角"}]\n'
        '}\n\n'
        "硬规则:\n"
        "- 全中文,口语化\n"
        "- bulls / bears 各 3-5 条;每条前面括号标专家身份,如「(技术派)MA20 上穿 60 周转向」\n"
        "- key_disputes 1-3 条\n"
        "- 不要 hallucinate 具体数字,K 线 / 因子数字以输入为准\n"
        "- guru_takeaways 严格按指定的游资派别风格,每位 30-60 字\n"
        "- 没把握的字段写 '(待验证)',不要编造"
    )
    user_msg = (
        f"标的: {code} {name}\n"
        f"KHunter 命中策略: {strats_zh}\n"
        f"因子总分: {total} (置信度 {confidence})\n"
        f"因子明细: {factor_brief}\n"
        f"指定的游资视角: {gurus_block}\n"
    )
    if news_excerpt:
        user_msg += f"\n近期相关新闻片段:\n{news_excerpt[:1500]}\n"

    body = {
        "model": model,
        "messages": [{"role": "system", "content": sysprompt},
                     {"role": "user", "content": user_msg}],
        "response_format": {"type": "json_object"},
        "max_tokens": 2500,
        "temperature": 0.35,
    }
    try:
        r = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=body,
        )
    except Exception as exc:
        print(f"[debate/{code}] network exc: {type(exc).__name__}: {exc}", flush=True)
        return None
    if r.status_code != 200:
        print(f"[debate/{code}] HTTP {r.status_code}: {r.text[:200]}", flush=True)
        return None
    try:
        d = r.json()
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            print(f"[debate/{code}] no JSON in response", flush=True)
            return None
        return json.loads(m.group(0))
    except Exception as exc:
        print(f"[debate/{code}] parse exc: {type(exc).__name__}: {exc}", flush=True)
        return None


async def run_debates_for_top(
    candidates: list[dict[str, Any]],
    factor_scores: dict[str, dict[str, Any]],
    max_concurrent: int = 4,
    timeout_per_call: int = 90,
) -> dict[str, dict[str, Any]]:
    """并行跑多只股的 debate,带并发上限。

    Args:
        candidates: [{code, name, strategies (list of english strategy names)}, ...]
        factor_scores: {code: factor_score_dict}
        max_concurrent: 同时进行的 LLM 调用数 (防限速)

    Returns:
        {code: debate_dict_or_None}
    """
    api_key, _, _ = _llm_creds()
    if not api_key:
        print("[debate] no LLM api_key, skip debates entirely", flush=True)
        return {}
    if not candidates:
        return {}

    sem = asyncio.Semaphore(max_concurrent)
    results: dict[str, dict[str, Any] | None] = {}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=timeout_per_call, write=15, pool=5),
    ) as client:

        async def _one(cand: dict[str, Any]) -> tuple[str, dict | None]:
            code = cand["code"]
            name = cand.get("name") or code
            strategies = cand.get("strategies") or []
            score = factor_scores.get(code, {})
            async with sem:
                view = await _debate_one(client, code, name, strategies, score)
            return code, view

        tasks = [_one(c) for c in candidates]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for item in gathered:
            if isinstance(item, Exception):
                print(f"[debate] task exception: {type(item).__name__}: {item}",
                      flush=True)
                continue
            code, view = item
            if view:
                results[code] = view

    print(f"[debate] produced {len(results)}/{len(candidates)} debates", flush=True)
    return results
