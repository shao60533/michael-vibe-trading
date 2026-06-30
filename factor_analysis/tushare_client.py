"""共享 Tushare Pro HTTP 客户端(免装 tushare 库,直连 api.tushare.pro)。

读环境变量 `TUSHARE_TOKEN`。未配 token 时 `enabled()` 返回 False,调用方应回退
到免费源(Sina/腾讯/东财)。仅用于**盘后/历史/EOD** 数据;盘中实时不走这里。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

_API = "https://api.tushare.pro"


def token() -> str:
    return (os.getenv("TUSHARE_TOKEN") or "").strip()


def enabled() -> bool:
    return bool(token())


def query(api_name: str, fields: str = "", *, timeout: int = 20,
          retries: int = 3, **params: Any) -> list[dict[str, Any]]:
    """调用 Tushare Pro 接口,返回 list[dict](字段→值)。失败抛异常。"""
    tok = token()
    if not tok:
        raise RuntimeError("TUSHARE_TOKEN not set")
    body = json.dumps({
        "api_name": api_name, "token": tok,
        "params": params, "fields": fields,
    }).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                _API, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
            if obj.get("code") != 0:
                raise RuntimeError(f"tushare {api_name} err: {obj.get('msg')}")
            data = obj.get("data") or {}
            cols = data.get("fields") or []
            rows = data.get("items") or []
            return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("tushare query failed")


# ── Sina 代码 ↔ Tushare ts_code ──
_FUND_PREFIXES = ("15", "16", "50", "51", "52", "56", "58")


def sina_to_ts_code(symbol: str) -> tuple[str, bool]:
    """`sh600519`/`sz159995` → (`600519.SH`/`159995.SZ`, is_fund)。

    返回 (ts_code, 是否为基金/ETF)。无法识别时按股票处理。
    """
    s = (symbol or "").strip().lower()
    if s.startswith("sh"):
        code, suf = s[2:], "SH"
    elif s.startswith("sz"):
        code, suf = s[2:], "SZ"
    elif s.startswith("bj"):
        code, suf = s[2:], "BJ"
    else:
        # 纯 6 位:按交易所首位猜
        code = s
        suf = "SH" if code[:1] in ("5", "6", "9") else "SZ"
    is_fund = code[:2] in _FUND_PREFIXES
    return f"{code}.{suf}", is_fund


def daily_kline(symbol: str, datalen: int = 400) -> list[dict[str, Any]]:
    """Tushare 日K(不复权),返回与 Sina 版同构的 row:

    `{date, open, close, high, low, volume(股), amount(元)}`,按日期升序,取最近 datalen 根。
    股票走 `daily`,ETF/基金走 `fund_daily`。单位对齐 Sina:vol 手→股(×100)、amount 千元→元(×1000)。
    失败抛异常(交给调用方回退)。
    """
    ts_code, is_fund = sina_to_ts_code(symbol)
    api = "fund_daily" if is_fund else "daily"
    # 限定起始日,避免单次拉回多年历史(KHunter 扫 ~300 只时尤其重要)。
    # datalen 是交易日数,×1.6 折算成日历天兜住周末/节假日。
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=int(max(datalen, 30) * 1.6) + 10))
    rows = query(
        api,
        fields="trade_date,open,high,low,close,vol,amount",
        ts_code=ts_code,
        start_date=start.strftime("%Y%m%d"),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        td = str(r.get("trade_date") or "")
        if len(td) == 8:
            td = f"{td[:4]}-{td[4:6]}-{td[6:]}"
        vol = float(r.get("vol") or 0.0)        # 手
        amt = float(r.get("amount") or 0.0)     # 千元
        out.append({
            "date": td,
            "open": float(r.get("open") or 0.0),
            "close": float(r.get("close") or 0.0),
            "high": float(r.get("high") or 0.0),
            "low": float(r.get("low") or 0.0),
            "volume": vol * 100.0,              # 股
            "amount": amt * 1000.0,             # 元
        })
    out.sort(key=lambda x: x["date"])
    if datalen and len(out) > datalen:
        out = out[-datalen:]
    return out
