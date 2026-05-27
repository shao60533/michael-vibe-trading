"""Orchestrate hot-event A-share research.

Hardening:
  - 路由 LLM 调用失败 → fallback 到 event_name 自身作 entity/keyword
  - 新闻抓取失败 → 降级到「无数据上下文」纯框架分析,不中止
  - 主分析 LLM 调用失败 / 截断 → HotEventError 抛出由上游处理
  - 所有 httpx 调用都有显式 timeout
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

from . import data_sources


class HotEventError(Exception):
    """Hard failure during hot-event analysis (LLM 主分析无法产出)."""


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


def _route_event(event_name: str) -> dict[str, Any]:
    """LLM 路由:从事件名抽 entity / industry_hint / keywords。

    失败时 fallback,不抛 — 让主分析尽量能跑。
    """
    api_key, base_url, model = _llm_creds()
    fallback = {"entity": event_name, "industry_hint": "",
                "keywords": [event_name]}
    if not api_key:
        return fallback
    sysprompt = (
        "你是 A 股热点事件解析助手。给一个事件名,返回严格 JSON:\n"
        '{"entity": "<主体名,如 华为/比亚迪/锂电池/数据中心>",\n'
        ' "industry_hint": "<最相关的行业中文短名,如「半导体」「先进封装」「储能」,'
        '不确定时返回空字符串>",\n'
        ' "keywords": ["关键词1", "关键词2", ...]}\n\n'
        "keywords 用于在新闻流里 substring 匹配,放 3-8 个高频别名/子词/英文等价,"
        "不要句子也不要标点。\n只输出 JSON,不要 markdown 围栏。"
    )
    body = {
        "model": model,
        "messages": [{"role": "system", "content": sysprompt},
                     {"role": "user", "content": event_name}],
        "response_format": {"type": "json_object"},
        "max_tokens": 1000,
        "temperature": 0.1,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10, read=60, write=15, pool=5),
        ) as c:
            r = c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
        if r.status_code != 200:
            print(f"[hot-event/route] HTTP {r.status_code}: {r.text[:200]}",
                  flush=True)
            return fallback
        d = r.json()
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        if not content:
            return fallback
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return fallback
        parsed = json.loads(m.group(0))
        # Normalize
        entity = (parsed.get("entity") or "").strip() or event_name
        industry_hint = (parsed.get("industry_hint") or "").strip()
        raw_kws = parsed.get("keywords") or []
        keywords = [str(k).strip() for k in raw_kws if k and str(k).strip()]
        if not keywords:
            keywords = [event_name]
        return {"entity": entity, "industry_hint": industry_hint,
                "keywords": keywords[:10]}
    except Exception as exc:
        print(f"[hot-event/route] exception: {type(exc).__name__}: {exc}",
              flush=True)
        return fallback


def _main_analysis(event_name: str,
                   news_context: list[dict],
                   industry_hint: str) -> str:
    """主分析 LLM。失败 / 截断 抛 HotEventError。"""
    api_key, base_url, model = _llm_creds()
    if not api_key:
        raise HotEventError("DEEPSEEK_API_KEY 未配,无法做主分析")

    sysprompt = (
        "你是 A 股事件研究员。基于给定的事件名 + 近期相关新闻流(可能稀疏),"
        "按下面 markdown 结构输出深度分析报告。\n\n"
        "----- 输出 schema -----\n"
        "# {事件名} 深度解析\n\n"
        "## 事件概况\n"
        "- 时间: YYYY-MM-DD 或区间\n"
        "- 主体: 涉及公司/产品/政策的主体名\n"
        "- 类型: 政策 / 产品发布 / 财报 / 并购 / 监管 / 技术突破\n"
        "- 一句话: 30 字以内核心\n\n"
        "## 核心题材逻辑\n"
        "**催化方向**: 这事件解决了什么 / 验证了什么。\n\n"
        "**炒作路径**(上游 → 中游 → 下游):\n"
        "▶ 上游: ...\n"
        "▶ 中游: ...\n"
        "▶ 下游: ...\n\n"
        "## 产业链\n"
        "| 环节 | 标签 | 核心股(代码.SH/.SZ) | 受益逻辑 |\n"
        "|---|---|---|---|\n"
        "| ... | ... | ... | ... |\n\n"
        "## 重点个股逻辑\n"
        "### {股名}({代码.SH/.SZ})\n"
        "50-100 字阐述该股在本事件中的具体地位 + 受益机制。\n\n"
        "(重复 3-6 只)\n\n"
        "## 预期差\n"
        "- **市场标签**: 市场目前怎么定义这事件\n"
        "- **真实逻辑**: 我认为应该怎么定义,差在哪\n"
        "- **持续性判断**: 短期事件 / 中期主题 / 长期产业\n\n"
        "## 风险提示\n"
        "3-5 条具体风险点。\n\n"
        "## 数据证据\n"
        "近期相关新闻(if any),最多 5 条:\n"
        "- [时间] 标题\n\n"
        "## 免责\n"
        "本分析为研究参考,不构成投资建议。\n"
        "----- schema 结束 -----\n\n"
        "硬规则:\n"
        "- 全中文\n"
        "- 个股代码必须是 6 位 + .SH/.SZ,**不要造代码**;不确定时只给股名 + '(代码待查)'\n"
        "- 没把握的字段写 '(待验证)' 或 '(未提及)',不要 hallucinate\n"
        "- 新闻流稀疏时在『数据证据』段明确写「证据较弱,本报告为框架分析」\n"
        "- 不要 markdown 围栏,直接输出 markdown 正文"
    )
    user_msg = f"事件: {event_name}\n"
    if industry_hint:
        user_msg += f"行业 hint: {industry_hint}\n"
    user_msg += "\n--- 近期相关新闻流(关键词过滤后) ---\n"
    if news_context:
        for n in news_context[:30]:
            t = n.get("time") or ""
            title = (n.get("title") or "").replace("\n", " ")
            summary = (n.get("summary") or "").replace("\n", " ")[:180]
            user_msg += f"[{t}] {title}\n  {summary}\n\n"
    else:
        user_msg += "(无匹配新闻 — 请在『数据证据』段说明证据较弱)\n"

    body = {
        "model": model,
        "messages": [{"role": "system", "content": sysprompt},
                     {"role": "user", "content": user_msg}],
        "max_tokens": 6500,
        "temperature": 0.3,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10, read=120, write=15, pool=5),
        ) as c:
            r = c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
    except Exception as exc:
        raise HotEventError(
            f"主分析 LLM 网络异常: {type(exc).__name__}: {exc}") from exc
    if r.status_code != 200:
        raise HotEventError(
            f"主分析 LLM HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    choice = (d.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or ""
    content = (msg.get("content") or "").strip()
    if finish_reason == "length":
        # 截断 — 仍返回已生成部分,但加标记
        content += "\n\n_(报告因长度限制被截断 — 已尽量完整,关键结论可参考已生成部分)_"
    if not content:
        reasoning_len = len(msg.get("reasoning_content") or "")
        raise HotEventError(
            f"主分析 LLM 返回空 content "
            f"(reasoning_len={reasoning_len}, finish={finish_reason})")
    return content


def pick_daily_event_name(avoid_recent: list[str] | None = None) -> str:
    """让 LLM 从今日新闻流里挑一个最值得做 A 股产业链拆解的事件。

    Args:
        avoid_recent: 最近已推过的 event_name 列表(scheduler 持有),让 LLM
                      尽量挑不重复的。空列表 / None 时不加约束。
    Returns:
        事件名(str)。失败时降级到通用 fallback,不抛异常 — 调度器希望
        无论如何能跑下去。
    """
    fallback = "今日 A 股热点"
    try:
        news = data_sources.fetch_global_news(page_size=120)
    except Exception as exc:
        print(f"[hot-event/pick] news fetch failed: {type(exc).__name__}: {exc}",
              flush=True)
        return f"{fallback}(数据稀薄)"
    if not news:
        return f"{fallback}(无新闻)"

    api_key, base_url, model = _llm_creds()
    if not api_key:
        return fallback

    headlines = "\n".join(
        f"[{n.get('time', '')}] {(n.get('title') or '').strip()}"
        for n in news[:60]
    )

    avoid_block = ""
    recent_clean = [n.strip() for n in (avoid_recent or []) if n and n.strip()]
    if recent_clean:
        names = "\n".join(f"- {n}" for n in recent_clean[-5:])
        avoid_block = (
            f"\n\n最近已推过的事件(请优先避免重复):\n{names}\n"
            "若今日确实没有新热点,允许选相同事件,但 reason 段说明"
            "「无新热点,延续此前事件」。"
        )

    sysprompt = (
        "你是 A 股选题编辑。从今日新闻流里挑一个**最值得做产业链拆解**的事件。\n\n"
        "返回严格 JSON:\n"
        '{"event_name": "<8-25 字事件名>", "reason": "<30 字以内选择理由>"}\n\n'
        "硬规则:\n"
        "- 选有 A 股产业链联动 + 短期催化效应的事件\n"
        "- 优先级:政策利好 > 技术突破 > 龙头公司动作 > 板块异动\n"
        "- 避免选纯宏观财经(PMI / CPI / 美联储议息)、单独港股 / 美股事件 — 不便于做 A 股产业链分析\n"
        "- event_name 要短、具体、可分析,如「华为韬定律」「锂电池价格回升」「AI 应用变现」「光模块涨价」\n"
        "- 多个候选时,选 A 股市场影响力最大的"
        + avoid_block
    )
    user_msg = f"今日新闻流(取前 60 条):\n\n{headlines}"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": sysprompt},
                     {"role": "user", "content": user_msg}],
        "response_format": {"type": "json_object"},
        "max_tokens": 800,
        "temperature": 0.2,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=10, read=60, write=15, pool=5),
        ) as c:
            r = c.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json=body,
            )
        if r.status_code != 200:
            print(f"[hot-event/pick] HTTP {r.status_code}: {r.text[:200]}",
                  flush=True)
            return fallback
        d = r.json()
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            return fallback
        parsed = json.loads(m.group(0))
        event_name = (parsed.get("event_name") or "").strip()
        reason = (parsed.get("reason") or "").strip()
        if not event_name:
            return fallback
        print(f"[hot-event/pick] selected event={event_name!r} reason={reason!r}",
              flush=True)
        return event_name
    except Exception as exc:
        print(f"[hot-event/pick] exception: {type(exc).__name__}: {exc}",
              flush=True)
        return fallback


def run_hot_event_analysis(event_name: str) -> dict[str, Any]:
    """Entry point. 返回 {event_name, routed, coverage, news_sample,
    report_markdown}。失败抛 HotEventError。"""
    event_name = (event_name or "").strip()
    if not event_name:
        raise HotEventError("event_name 为空")
    started = time.time()

    # 1. Route
    routed = _route_event(event_name)
    keywords = routed.get("keywords") or [event_name]
    industry_hint = routed.get("industry_hint") or ""
    print(f"[hot-event] routed: entity={routed.get('entity')!r} "
          f"industry={industry_hint!r} keywords={keywords}", flush=True)

    # 2. Fetch news (non-fatal — degrade to no news on failure)
    news: list[dict] = []
    news_err: str | None = None
    try:
        all_news = data_sources.fetch_global_news(page_size=200)
        news = data_sources.filter_news_by_keywords(
            all_news, keywords, max_keep=40)
        print(f"[hot-event] news: pulled {len(all_news)} total, "
              f"matched {len(news)} on keywords", flush=True)
    except Exception as exc:
        news_err = f"{type(exc).__name__}: {exc}"
        print(f"[hot-event] news fetch failed: {news_err}", flush=True)

    # 3. Main analysis (fatal on failure)
    report_markdown = _main_analysis(event_name, news, industry_hint)

    elapsed = round(time.time() - started, 1)
    return {
        "event_name": event_name,
        "routed": routed,
        "coverage": {
            "news_fetched": len(news),
            "news_error": news_err,
            "elapsed_seconds": elapsed,
        },
        "news_sample": news[:10],
        "report_markdown": report_markdown,
    }
