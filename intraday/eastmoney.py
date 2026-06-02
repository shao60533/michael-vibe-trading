"""EastMoney push2 client: concept boards + limit-up pool.

实测端点(2026-05-29):
- 板块榜单: https://push2.eastmoney.com/api/qt/clist/get
  fs=m:90+t:3 概念板块,fs=m:90+t:2 行业板块
- 涨停股池: https://push2ex.eastmoney.com/getTopicZTPool

Railway 数据中心 IP 直接可达,但需要 Referer: https://quote.eastmoney.com/
否则部分时段返 403。
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# ─────────── HTTP ───────────

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


class EastMoneyError(Exception):
    """Hard failure fetching from EastMoney push2."""


def _get_json(url: str, timeout: float = 10.0,
              retries: int = 3, backoff_sec: float = 0.4) -> dict[str, Any]:
    """带 retry — push2 实测 nginx 负载均衡间歇 502,重试常能换到健康节点。"""
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise EastMoneyError(
                    f"non-JSON body[0:200]={body[:200]!r}") from exc
        except EastMoneyError:
            raise  # JSON 错不重试
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                _time.sleep(backoff_sec * attempt)
                continue
    raise EastMoneyError(
        f"network err after {retries} retries: "
        f"{type(last_exc).__name__}: {last_exc} url={url[:120]}"
    ) from last_exc


# ─────────── Boards (概念 / 行业) ───────────


@dataclass(frozen=True)
class BoardSnapshot:
    code: str            # BK0933
    name: str            # 退税商店
    pct: float           # 涨跌幅 %
    main_inflow: float   # 主力净流入 (元)
    leader_name: str     # 领涨股名 (可能为空字符串)
    leader_code: str     # 领涨股 6 位代码 (f140,可能为空)
    leader_pct: float    # 领涨股涨跌幅 %

    def main_inflow_yi(self) -> float:
        """主力净流入 → 亿元。"""
        return self.main_inflow / 1e8


def _fetch_boards(fs: str, limit: int) -> list[BoardSnapshot]:
    """fs=m:90+t:3 概念,fs=m:90+t:2 行业。按涨幅降序。"""
    params = {
        "pn": "1", "pz": str(max(1, min(limit, 200))),
        "po": "1", "np": "1",
        "fltt": "2", "invt": "2",
        "fid": "f3",  # sort by pct
        "fs": fs,
        # f3 板块涨幅 / f12 板块代码 / f14 板块名 / f62 主力净流入
        # f128 领涨股名 / f140 领涨股代码 / f136 领涨股涨跌幅(实测正确)
        "fields": "f3,f12,f14,f62,f128,f140,f136",
    }
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           + urllib.parse.urlencode(params))
    d = _get_json(url)
    if d.get("rc") != 0:
        raise EastMoneyError(f"boards rc={d.get('rc')} body={str(d)[:200]}")
    data = d.get("data") or {}
    rows = data.get("diff") or []
    out: list[BoardSnapshot] = []
    for r in rows:
        try:
            out.append(BoardSnapshot(
                code=str(r.get("f12") or ""),
                name=str(r.get("f14") or ""),
                pct=float(r.get("f3") or 0.0),
                main_inflow=float(r.get("f62") or 0.0),
                leader_name=str(r.get("f128") or ""),
                leader_code=str(r.get("f140") or ""),
                leader_pct=float(r.get("f136") or 0.0),
            ))
        except (TypeError, ValueError):
            continue
    return out


def fetch_concept_boards(limit: int = 50) -> list[BoardSnapshot]:
    """概念板块 Top N 按涨幅降序(包含负数,需调用方再过滤)。"""
    return _fetch_boards("m:90+t:3", limit)


def fetch_industry_boards(limit: int = 30) -> list[BoardSnapshot]:
    """行业板块 Top N 按涨幅降序。"""
    return _fetch_boards("m:90+t:2", limit)


# ─────────── Limit-up Pool 涨停股池 ───────────


@dataclass(frozen=True)
class LimitUpStock:
    code: str             # 301439
    name: str             # 泓淋电力
    boards: int           # 连板数 (lbc)
    seal_amount: float    # 封单金额 (元)
    first_seal_hhmm: str  # "09:25" 首次封板时间
    open_count: int       # 开板次数 (zbc)

    def seal_amount_yi(self) -> float:
        return self.seal_amount / 1e8


def _hhmmss_to_hhmm(v: Any) -> str:
    """fbt 字段是 HHMMSS 整数(92500 = 09:25:00)。截到 HH:MM。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    s = f"{n:06d}"
    return f"{s[:2]}:{s[2:4]}"


def fetch_limit_up_pool(date_str: str | None = None,
                        limit: int = 50) -> list[LimitUpStock]:
    """涨停股池(按首封时间升序 — 早封的更强势)。

    Args:
        date_str: YYYYMMDD,默认今天(北京时间)。
        limit: 最多返回多少只。

    Returns: 列表,按首封时间从早到晚。
    """
    if not date_str:
        tz = datetime.timezone(datetime.timedelta(hours=8))
        date_str = datetime.datetime.now(tz).strftime("%Y%m%d")
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": str(max(1, min(limit, 200))),
        "sort": "fbt:asc",
        "date": date_str,
    }
    url = ("https://push2ex.eastmoney.com/getTopicZTPool?"
           + urllib.parse.urlencode(params))
    d = _get_json(url)
    if d.get("rc") != 0:
        raise EastMoneyError(
            f"limit-up rc={d.get('rc')} body={str(d)[:200]}")
    data = d.get("data") or {}
    pool = data.get("pool") or []
    out: list[LimitUpStock] = []
    for r in pool:
        try:
            out.append(LimitUpStock(
                code=str(r.get("c") or ""),
                name=str(r.get("n") or ""),
                boards=int(r.get("lbc") or 1),
                seal_amount=float(r.get("fund") or 0.0),
                first_seal_hhmm=_hhmmss_to_hhmm(r.get("fbt")),
                open_count=int(r.get("zbc") or 0),
            ))
        except (TypeError, ValueError):
            continue
    return out


# ─────────── Convenience ───────────


def fetch_snapshot(top_boards: int = 10, top_limit_up: int = 5,
                   include_industry: bool = False) -> dict[str, Any]:
    """一次取板块 + 涨停,过滤负涨幅板块,返回字典。

    设计: 板块榜单失败 → 返空 list + 在 boards_error 标错(prod 2026-06-02
    起 push2 clist/get 时不时 502)。涨停池 push2ex 仍稳定。
    涨停池失败才抛 EastMoneyError。
    """
    boards_concept: list[BoardSnapshot] = []
    boards_error: str | None = None
    try:
        boards_concept = fetch_concept_boards(limit=max(top_boards * 2, 30))
        # 只保留涨的(负的代表跌幅板块,异动定义里只看涨)
        boards_concept = [b for b in boards_concept if b.pct > 0][:top_boards]
    except EastMoneyError as exc:
        boards_error = f"{type(exc).__name__}: {exc}"
        print(f"[intraday/em] concept boards fetch failed (graceful): "
              f"{boards_error}", flush=True)

    boards_industry: list[BoardSnapshot] = []
    if include_industry:
        try:
            bi = fetch_industry_boards(limit=20)
            boards_industry = [b for b in bi if b.pct > 0][:5]
        except EastMoneyError as exc:
            print(f"[intraday/em] industry fetch err: {exc}", flush=True)

    pool = fetch_limit_up_pool(limit=max(top_limit_up * 4, 30))
    pool_top = pool[:top_limit_up]
    total_zt = len(pool)

    return {
        "boards_concept": boards_concept,
        "boards_industry": boards_industry,
        "boards_error": boards_error,
        "limit_up_top": pool_top,
        "limit_up_total": total_zt,
        "limit_up_all": pool,
    }
