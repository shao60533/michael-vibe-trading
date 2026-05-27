"""HTTP data fetchers for hot-event analysis. V1 仅接东财全球资讯 7x24。

后续可扩:东财个股新闻 / 财联社 / 同花顺热点。每次只加一个 endpoint,
直接 HTTP,不引入 mootdx 等重依赖。"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def fetch_global_news(page_size: int = 200, timeout: int = 15) -> list[dict[str, Any]]:
    """东财全球资讯 7x24 滚动。返回 [{title, summary, time}]。

    失败时抛 httpx.HTTPError / json.JSONDecodeError,由上游决定降级策略。
    """
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    params = {
        "client": "web", "biz": "web_724",
        "fastColumn": "102", "sortEnd": "",
        "pageSize": str(page_size),
        "req_trace": str(uuid.uuid4()),
    }
    headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url, params=params, headers=headers)
    r.raise_for_status()
    d = r.json()
    out: list[dict[str, Any]] = []
    for item in (d.get("data") or {}).get("fastNewsList") or []:
        out.append({
            "title": item.get("title", "") or "",
            "summary": ((item.get("summary") or "")[:400]),
            "time": item.get("showTime") or item.get("publishTime") or "",
            "link": item.get("infoCode") or "",
        })
    return out


def filter_news_by_keywords(news: list[dict[str, Any]],
                             keywords: list[str],
                             max_keep: int = 40) -> list[dict[str, Any]]:
    """Loose substring match — any keyword in title or summary keeps the news."""
    kws = [k.strip() for k in (keywords or []) if k and k.strip()]
    if not kws:
        return news[:max_keep]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for n in news:
        blob = ((n.get("title") or "") + " " + (n.get("summary") or "")).lower()
        for k in kws:
            if k.lower() in blob:
                key = n.get("title") or ""
                if key in seen:
                    break
                seen.add(key)
                out.append(n)
                break
        if len(out) >= max_keep:
            break
    return out
