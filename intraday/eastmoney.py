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


# push2 是一组 nginx 镜像节点,同一时刻部分节点会 502/挂起。原来的 retry
# 一直打同一个 host,所谓"换健康节点"并没发生 —— 这里真正轮换镜像。
_PUSH2_MIRRORS = (
    "push2.eastmoney.com", "1.push2.eastmoney.com", "7.push2.eastmoney.com",
    "13.push2.eastmoney.com", "29.push2.eastmoney.com",
    "48.push2.eastmoney.com", "92.push2.eastmoney.com",
)


def _rotate_push2_host(url: str, attempt: int) -> str:
    """把 push2.eastmoney.com 轮换到不同镜像节点。

    只动 push2.eastmoney.com;push2ex(涨停池)等其它 host 原样返回。
    """
    parts = urllib.parse.urlsplit(url)
    if parts.netloc != "push2.eastmoney.com":
        return url
    mirror = _PUSH2_MIRRORS[attempt % len(_PUSH2_MIRRORS)]
    return parts._replace(netloc=mirror).geturl()


def _get_json(url: str, timeout: float = 10.0,
              retries: int = 6, backoff_sec: float = 0.4) -> dict[str, Any]:
    """带 retry + 镜像轮换 — push2 nginx 负载均衡间歇 502,换台节点常能命中。"""
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try_url = _rotate_push2_host(url, attempt - 1)
        try:
            req = urllib.request.Request(try_url, headers=_HEADERS)
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


def _fetch_concept_boards_sina(limit: int) -> list[BoardSnapshot]:
    """独立兜底源(与 push2 不同集群):Sina 板块榜 newFLJK。

    2026-06 实测 push2 概念板块网关可整段 502(镜像轮换也救不回),而 Sina
    仍可达。GBK 编码,逗号分隔字段:
      code,name,n,avg_price,avg_chg,涨跌幅%,vol,amount,
      leader_code,leader_price,leader_chg,leader_pct,leader_name
    实测 Sina 领涨股字段不可靠(会给出 >20% 的不可能值)→ 只取板块名 + 涨幅,
    领涨股留空,不塞假数据(领涨个股仍由涨停池提供)。
    """
    url = ("https://vip.stock.finance.sina.com.cn/q/view/"
           "newFLJK.php?param=class")
    req = urllib.request.Request(
        url, headers={**_HEADERS, "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        body = resp.read().decode("gbk", "replace")
    try:
        obj = json.loads(body[body.index("{"):body.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise EastMoneyError(f"sina boards parse err: {exc}") from exc
    out: list[BoardSnapshot] = []
    for _key, val in obj.items():
        f = str(val).split(",")
        if len(f) < 6:
            continue
        try:
            out.append(BoardSnapshot(
                code=str(f[0]), name=str(f[1]), pct=float(f[5]),
                main_inflow=0.0, leader_name="", leader_code="",
                leader_pct=0.0,
            ))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda b: b.pct, reverse=True)
    return out[:limit]


def fetch_concept_boards(limit: int = 50) -> list[BoardSnapshot]:
    """概念板块 Top N 按涨幅降序(包含负数,需调用方再过滤)。

    push2(含镜像轮换)整段不可用时,自动兜底到 Sina 独立源;两边都失败
    才抛 EastMoneyError(由 fetch_snapshot 优雅降级)。
    """
    try:
        return _fetch_boards("m:90+t:3", limit)
    except EastMoneyError as exc:
        print(f"[intraday/em] push2 concept boards down, "
              f"fallback to Sina: {exc}", flush=True)
        return _fetch_concept_boards_sina(limit)


def fetch_industry_boards(limit: int = 30) -> list[BoardSnapshot]:
    """行业板块 Top N 按涨幅降序。"""
    return _fetch_boards("m:90+t:2", limit)


# ─────────── Board members 板块成分股 ───────────


@dataclass(frozen=True)
class BoardMember:
    code: str            # 6 位代码
    name: str
    price: float         # 最新价
    pct: float           # 涨跌幅 %
    main_inflow: float   # 主力净流入 (元)
    is_limit_up: bool    # 是否涨停(按板制近似判定)

    def main_inflow_yi(self) -> float:
        return self.main_inflow / 1e8


def _limit_pct_for(code: str) -> float:
    """按代码近似判断涨停幅度上限(忽略 ST 的 5%)。"""
    c = code or ""
    if c[:3] in ("300", "301") or c[:3] == "688":
        return 20.0
    if c[:2] in ("83", "87", "43", "92") or c[:3] == "920":
        return 30.0  # 北交所
    return 10.0


def _fetch_board_members_sina(node_code: str, limit: int = 30) -> list[BoardMember]:
    """Sina 板块成分股(push2 挂时兜底)。node_code 形如 gn_xxx / new_xxx
    (来自 newFLJK)。Sina 无主力净流入 → main_inflow=0;按涨幅降序返回。
    """
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData?page=1&num=" + str(max(1, min(limit, 100)))
           + "&sort=changepercent&asc=0&node=" + urllib.parse.quote(node_code)
           + "&symbol=")
    req = urllib.request.Request(
        url, headers={**_HEADERS, "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        body = resp.read().decode("gbk", "replace")
    try:
        arr = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EastMoneyError(f"sina members parse err: {exc}") from exc
    if not isinstance(arr, list):
        return []
    out: list[BoardMember] = []
    for r in arr:
        try:
            sym = str(r.get("symbol") or "")
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            pct = float(r.get("changepercent") or 0.0)
            out.append(BoardMember(
                code=code, name=str(r.get("name") or ""),
                price=float(r.get("trade") or 0.0), pct=pct,
                main_inflow=0.0,  # Sina 无主力净流入
                is_limit_up=(pct >= _limit_pct_for(code) - 0.6),
            ))
        except (TypeError, ValueError):
            continue
    return out


def fetch_board_members(board_code: str, limit: int = 30,
                        retries: int = 3) -> list[BoardMember]:
    """板块成分股。BKxxxx → push2(按主力净流入降序);gn_/new_(Sina 节点)
    → Sina getHQNodeData(按涨幅降序,无主力净流入)。两源都不可用才抛错。
    """
    if not board_code:
        return []
    # Sina 节点码(push2 挂时板块榜来自 Sina)
    if not board_code.upper().startswith("BK"):
        try:
            return _fetch_board_members_sina(board_code, limit)
        except Exception as exc:  # noqa: BLE001
            raise EastMoneyError(f"sina members {board_code} err: {exc}") from exc
    params = {
        "pn": "1", "pz": str(max(1, min(limit, 100))),
        "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f62",  # 主力净流入 降序
        "fs": f"b:{board_code}",
        "fields": "f12,f14,f2,f3,f62",
    }
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           + urllib.parse.urlencode(params))
    d = _get_json(url, retries=retries)
    if d.get("rc") != 0:
        raise EastMoneyError(f"members rc={d.get('rc')} body={str(d)[:160]}")
    rows = (d.get("data") or {}).get("diff") or []
    out: list[BoardMember] = []
    for r in rows:
        try:
            code = str(r.get("f12") or "")
            pct = float(r.get("f3") or 0.0)
            out.append(BoardMember(
                code=code,
                name=str(r.get("f14") or ""),
                price=float(r.get("f2") or 0.0),
                pct=pct,
                main_inflow=float(r.get("f62") or 0.0),
                is_limit_up=(pct >= _limit_pct_for(code) - 0.6),
            ))
        except (TypeError, ValueError):
            continue
    return out


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
        "pagesize": str(max(1, min(limit, 1000))),
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

    # 拉全量(pagesize 1000)→ total_zt 反映真实全市场涨停数(原来截在30/200)
    pool = fetch_limit_up_pool(limit=1000)
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
