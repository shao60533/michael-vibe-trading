"""Data adapters for A-share industry factor research.

The adapters intentionally use public Eastmoney endpoints already documented in
the bundled a-stock-data skill, so the module can run without adding a new data
vendor account. Premium data sources such as JQData/Tushare can replace these
functions later behind the same return shapes.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

SECTOR_ETF_PROXIES = {
    "sh512480": "半导体ETF",
    "sh512760": "芯片ETF",
    "sz159995": "芯片ETF",
    "sh515790": "光伏ETF",
    "sh516160": "新能源ETF",
    "sh515030": "新能源车ETF",
    "sz159806": "新能源车ETF",
    "sh512880": "证券ETF",
    "sh512800": "银行ETF",
    "sh512690": "酒ETF",
    "sz159928": "消费ETF",
    "sh512170": "医疗ETF",
    "sh512010": "医药ETF",
    "sh512660": "军工ETF",
    "sh512400": "有色金属ETF",
    "sh515220": "煤炭ETF",
    "sh515210": "钢铁ETF",
    "sz159870": "化工ETF",
    "sh515880": "通信ETF",
    "sh512980": "传媒ETF",
    "sh516950": "基建ETF",
    "sh515050": "5GETF",
    "sz159869": "游戏ETF",
    "sz159766": "旅游ETF",
    "sh515000": "科技ETF",
    "sh515230": "软件ETF",
    "sh516510": "云计算ETF",
    "sz159851": "金融科技ETF",
    "sh516970": "基建50ETF",
    "sz159865": "养殖ETF",
    "sh516670": "畜牧ETF",
    "sh515170": "食品饮料ETF",
    "sh516110": "汽车ETF",
    "sh516150": "稀土ETF",
    "sh516800": "智能制造ETF",
    "sh516910": "物流ETF",
    "sh516070": "碳中和ETF",
    "sh512200": "房地产ETF",
    "sh515060": "地产ETF",
    "sh512070": "非银ETF",
    "sh512000": "券商ETF",
}


def _to_float(value: Any, default: float = math.nan) -> float:
    if value in (None, "", "-"):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


def _get_json(
    url: str,
    params: dict[str, Any],
    timeout: int = 20,
    retries: int = 3,
) -> dict[str, Any]:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    last_exc: Exception | None = None
    for attempt in range(max(retries, 1)):
        req = Request(
            f"{url}?{query}",
            headers={
                "User-Agent": UA,
                "Referer": "https://data.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
                "Connection": "close",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw)
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < max(retries, 1):
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}")


def _fmt_date(value: date | datetime | str | None, default: date) -> str:
    if value is None:
        return default.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.date().strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def fetch_industry_boards(limit: int = 120) -> list[dict[str, Any]]:
    """Fetch Eastmoney industry board snapshot rows."""
    data = _get_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "pn": "1",
            "pz": str(max(limit, 20)),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
        },
    )
    rows = data.get("data", {}).get("diff", []) or []
    out: list[dict[str, Any]] = []
    for rank, item in enumerate(rows[:limit], start=1):
        code = str(item.get("f12") or "").strip()
        market = str(item.get("f13") or "90").strip() or "90"
        if not code:
            continue
        out.append(
            {
                "rank": rank,
                "code": code,
                "secid": f"{market}.{code}",
                "name": item.get("f14") or code,
                "price": _to_float(item.get("f2")),
                "change_pct": _to_float(item.get("f3")),
                "change": _to_float(item.get("f4")),
                "up_count": _to_int(item.get("f104")),
                "down_count": _to_int(item.get("f105")),
                "leader": item.get("f140") or "",
                "leader_code": item.get("f128") or "",
                "leader_change": _to_float(item.get("f136")),
            }
        )
    return out


def fetch_board_kline(
    secid: str,
    lookback_days: int = 260,
    end_date: date | datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch daily kline rows for an Eastmoney industry board secid."""
    end_dt = datetime.strptime(_fmt_date(end_date, date.today()), "%Y-%m-%d").date()
    # Calendar days > trading days. Keep a cushion for suspensions/holidays.
    begin_dt = end_dt - timedelta(days=int(lookback_days * 1.8) + 40)
    data = _get_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {
            "secid": secid,
            "klt": "101",
            "fqt": "1",
            "beg": begin_dt.strftime("%Y%m%d"),
            "end": end_dt.strftime("%Y%m%d"),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        timeout=20,
    )
    klines = data.get("data", {}).get("klines", []) or []
    rows: list[dict[str, Any]] = []
    for line in klines[-lookback_days:]:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        rows.append(
            {
                "date": parts[0],
                "open": _to_float(parts[1]),
                "close": _to_float(parts[2]),
                "high": _to_float(parts[3]),
                "low": _to_float(parts[4]),
                "volume": _to_float(parts[5]),
                "amount": _to_float(parts[6]),
                "amplitude": _to_float(parts[7]),
                "change_pct": _to_float(parts[8]),
                "change": _to_float(parts[9]),
                "turnover": _to_float(parts[10]),
            }
        )
    return rows


def fetch_industry_panel(
    limit: int = 80,
    lookback_days: int = 260,
    end_date: date | datetime | str | None = None,
    pause_seconds: float = 0.05,
):
    """Return a pandas DataFrame with daily rows for industry boards."""
    import pandas as pd

    boards = fetch_industry_boards(limit=limit)
    rows: list[dict[str, Any]] = []
    for board in boards:
        try:
            for kline in fetch_board_kline(board["secid"], lookback_days, end_date):
                item = dict(kline)
                item.update(
                    {
                        "code": board["code"],
                        "secid": board["secid"],
                        "name": board["name"],
                        "snapshot_rank": board["rank"],
                        "snapshot_change_pct": board["change_pct"],
                        "leader": board["leader"],
                        "leader_change": board["leader_change"],
                    }
                )
                rows.append(item)
        except Exception as exc:
            print(f"[factor/data] skip board {board.get('name')} {board.get('secid')}: {exc}", flush=True)
        if pause_seconds:
            time.sleep(pause_seconds)
    if not rows:
        raise RuntimeError("No industry kline data returned from Eastmoney.")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    num_cols = [
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "amplitude",
        "change_pct",
        "change",
        "turnover",
        "snapshot_change_pct",
        "leader_change",
    ]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def fetch_sina_daily_kline(symbol: str, datalen: int = 400) -> list[dict[str, Any]]:
    """A 股日K(不复权)。**Tushare 优先,Sina 兜底**。

    名字保留为 `fetch_sina_daily_kline` 以兼容所有调用方(KHunter / Sequoia /
    factor 都 import 它)。配了 `TUSHARE_TOKEN` 时走 Tushare Pro(更稳、不被封 IP);
    未配或 Tushare 失败/空 → 回退到原 Sina 接口。返回 row 结构两路一致。

    `symbol` 用 Sina 记法,例如 `sh512480` / `sz159995` / `sh600519`。
    """
    try:
        from . import tushare_client
        if tushare_client.enabled():
            rows = tushare_client.daily_kline(symbol, datalen=datalen)
            if rows:
                return rows
    except Exception:
        pass  # 任何异常都回退到 Sina,绝不因 Tushare 故障而中断
    return _fetch_sina_daily_kline_raw(symbol, datalen=datalen)


def _fetch_sina_daily_kline_raw(symbol: str, datalen: int = 400) -> list[dict[str, Any]]:
    """原 Sina 实现(兜底)。`symbol` 用 Sina 记法,如 `sh512480` / `sz159995`。"""
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)}
    try:
        data = _get_json(
            "https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData",
            params,
            timeout=20,
            retries=3,
        )
        data = data.get("result", {}).get("data", []) if isinstance(data, dict) else data
    except Exception:
        data = _get_json(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params,
            timeout=20,
            retries=2,
        )
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in data:
        rows.append(
            {
                "date": item.get("day"),
                "open": _to_float(item.get("open")),
                "close": _to_float(item.get("close")),
                "high": _to_float(item.get("high")),
                "low": _to_float(item.get("low")),
                "volume": _to_float(item.get("volume")),
                # Sina endpoint does not return amount. Use price * volume as a stable proxy.
                "amount": _to_float(item.get("volume")) * _to_float(item.get("close")),
            }
        )
    return rows


def fetch_sector_etf_panel(
    start_date: date | datetime | str,
    end_date: date | datetime | str,
    symbols: dict[str, str] | None = None,
    datalen: int = 500,
    pause_seconds: float = 0.03,
):
    """Fetch sector ETF proxy panel for historical model validation."""
    import pandas as pd

    def adjust_price_discontinuities(group):
        g = group.sort_values("date").copy()
        price_cols = ["open", "high", "low", "close"]
        if len(g) < 2:
            g["split_adjustment_count"] = 0
            return g
        adjustment_count = 0
        for idx in range(1, len(g)):
            prev_close = float(g.iloc[idx - 1]["close"])
            this_open = float(g.iloc[idx]["open"])
            if not prev_close or not this_open:
                continue
            ratio = this_open / prev_close
            if ratio < 0.55 or ratio > 1.80:
                prior_index = g.index[:idx]
                g.loc[prior_index, price_cols] = g.loc[prior_index, price_cols] * ratio
                if "volume" in g.columns and ratio:
                    g.loc[prior_index, "volume"] = g.loc[prior_index, "volume"] / ratio
                adjustment_count += 1
        g["amount"] = g["close"] * g["volume"]
        g["split_adjustment_count"] = adjustment_count
        return g

    symbols = symbols or SECTOR_ETF_PROXIES
    start = pd.to_datetime(_fmt_date(start_date, date.today()))
    end = pd.to_datetime(_fmt_date(end_date, date.today()))
    rows: list[dict[str, Any]] = []
    for symbol, name in symbols.items():
        try:
            for kline in fetch_sina_daily_kline(symbol, datalen=datalen):
                item = dict(kline)
                item["code"] = symbol
                item["name"] = name
                rows.append(item)
        except Exception as exc:
            print(f"[factor/data] skip sina symbol {symbol} {name}: {exc}", flush=True)
        if pause_seconds:
            time.sleep(pause_seconds)
    if not rows:
        raise RuntimeError("No sector ETF proxy data returned from Sina.")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    num_cols = ["open", "high", "low", "close", "volume", "amount"]
    for col in num_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "code", "open", "high", "low", "close"]).sort_values(
        ["date", "code"]
    ).reset_index(drop=True)
    df = df.groupby("code", group_keys=False).apply(adjust_price_discontinuities)
    return df.sort_values(["date", "code"]).reset_index(drop=True)


def fetch_recent_reports(
    days: int = 7,
    max_pages: int = 8,
    page_size: int = 100,
    end_date: date | datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Fetch recent Eastmoney research reports across all covered stocks."""
    end_dt = datetime.strptime(_fmt_date(end_date, date.today()), "%Y-%m-%d").date()
    begin_dt = end_dt - timedelta(days=max(days - 1, 0))
    all_rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        data = _get_json(
            "https://reportapi.eastmoney.com/report/list",
            {
                "industryCode": "*",
                "pageSize": str(page_size),
                "industry": "*",
                "rating": "*",
                "ratingChange": "*",
                "beginTime": begin_dt.strftime("%Y-%m-%d"),
                "endTime": end_dt.strftime("%Y-%m-%d"),
                "pageNo": str(page),
                "fields": "",
                "qType": "0",
                "orgCode": "",
                "code": "",
                "rcode": "",
                "p": str(page),
                "pageNum": str(page),
                "pageNumber": str(page),
            },
            timeout=30,
        )
        rows = data.get("data") or []
        if not rows:
            break
        all_rows.extend(rows)
        total_pages = int(data.get("TotalPage") or data.get("totalPage") or page)
        if page >= total_pages:
            break
        time.sleep(0.2)
    return all_rows
