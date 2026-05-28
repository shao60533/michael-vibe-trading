#!/usr/bin/env python3
"""Run distilled KHunter A-share strategies over recent daily bars.

This script is intentionally self-contained. It reuses the user's workspace
Sina daily-bar adapter when available and rewrites the KHunter strategy rules
as compact pandas predicates suitable for skill usage.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


STRATEGY_WEIGHTS = {
    "BottomTrendInflection": 50,
    "TrendAccelerationInflection": 30,
    "ResistanceBreakout": 50,
    "WBottom": 50,
    "MultiGoldenCross": 50,
    "MorningStar": 30,
    "MultiPartyCannon": 30,
    "LimitUpPullback": 50,
    "LimitUpSideways": 70,
    "StrongWashWeakToStrong": 50,
    "ImmortalGuidance": 70,
}


STRATEGY_NAMES_CN = {
    "BottomTrendInflection": "底部趋势拐点",
    "TrendAccelerationInflection": "趋势加速拐点",
    "ResistanceBreakout": "阻力位突破",
    "WBottom": "W底策略",
    "MultiGoldenCross": "多金叉共振",
    "MorningStar": "启明星策略",
    "MultiPartyCannon": "多方炮策略",
    "LimitUpPullback": "涨停回马枪",
    "LimitUpSideways": "涨停横盘",
    "StrongWashWeakToStrong": "强势洗盘弱转强",
    "ImmortalGuidance": "仙人指路",
}


@dataclass(frozen=True)
class StockMeta:
    code: str
    name: str
    sina_symbol: str
    amount_snapshot: float = 0.0
    change_pct_snapshot: float = math.nan


def _to_float(value: Any, default: float = math.nan) -> float:
    if value in (None, "", "-"):
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _get_json(url: str, params: dict[str, Any], timeout: int = 20, retries: int = 3) -> Any:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    last_exc: Exception | None = None
    for attempt in range(max(retries, 1)):
        req = Request(
            f"{url}?{query}",
            headers={
                "User-Agent": UA,
                "Referer": "https://quote.eastmoney.com/",
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
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}")


def to_sina_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def fetch_active_universe(max_symbols: int, include_st: bool = False) -> list[StockMeta]:
    try:
        universe = fetch_eastmoney_active_universe(max_symbols=max_symbols, include_st=include_st)
        if universe:
            return universe
    except Exception as exc:
        print(f"[khunter] Eastmoney universe failed, fallback to Sina: {exc}", file=sys.stderr, flush=True)
    return fetch_sina_active_universe(max_symbols=max_symbols, include_st=include_st)


def fetch_eastmoney_active_universe(max_symbols: int, include_st: bool = False) -> list[StockMeta]:
    rows: list[dict[str, Any]] = []
    page_size = min(max(max_symbols, 20), 500)
    pages = max(1, math.ceil(max_symbols / page_size))
    for page in range(1, pages + 1):
        data = _get_json(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": page,
                "pz": page_size,
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f6",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f6,f12,f14",
            },
        )
        batch = data.get("data", {}).get("diff", []) or []
        rows.extend(batch)
        if len(rows) >= max_symbols or not batch:
            break

    universe: list[StockMeta] = []
    seen: set[str] = set()
    for item in rows:
        code = str(item.get("f12") or "").strip()
        name = str(item.get("f14") or code).strip()
        if not code or code in seen:
            continue
        if not include_st and is_invalid_name(name):
            continue
        seen.add(code)
        universe.append(
            StockMeta(
                code=code,
                name=name,
                sina_symbol=to_sina_symbol(code),
                amount_snapshot=_to_float(item.get("f6"), 0.0),
                change_pct_snapshot=_to_float(item.get("f3")),
            )
        )
        if len(universe) >= max_symbols:
            break
    return universe


def fetch_sina_active_universe(max_symbols: int, include_st: bool = False) -> list[StockMeta]:
    universe: list[StockMeta] = []
    seen: set[str] = set()
    page_size = min(max(max_symbols, 20), 80)
    pages = max(1, math.ceil(max_symbols / page_size))
    for page in range(1, pages + 1):
        data = _get_json(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            {
                "page": page,
                "num": page_size,
                "sort": "amount",
                "asc": "0",
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "init",
            },
            timeout=20,
            retries=3,
        )
        if not isinstance(data, list) or not data:
            break
        for item in data:
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or code).strip()
            sina_symbol = str(item.get("symbol") or to_sina_symbol(code)).strip()
            if not code or code in seen:
                continue
            if not include_st and is_invalid_name(name):
                continue
            seen.add(code)
            universe.append(
                StockMeta(
                    code=code,
                    name=name,
                    sina_symbol=sina_symbol,
                    amount_snapshot=_to_float(item.get("amount"), 0.0),
                    change_pct_snapshot=_to_float(item.get("changepercent")),
                )
            )
            if len(universe) >= max_symbols:
                return universe
        if len(universe) >= max_symbols:
            break
    return universe


def load_workspace_fetcher(workspace: str) -> Callable[[str, int], list[dict[str, Any]]]:
    ws = Path(workspace).expanduser().resolve()
    if str(ws) not in sys.path:
        sys.path.insert(0, str(ws))
    from factor_analysis.data_sources import fetch_sina_daily_kline

    return fetch_sina_daily_kline


def normalize_frame(rows: list[dict[str, Any]], meta: StockMeta) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        if col not in df.columns:
            df[col] = math.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    df = df[df["volume"].fillna(0) > 0].sort_values("date")
    if "amount" not in df or df["amount"].isna().all():
        df["amount"] = df["close"] * df["volume"]
    df["code"] = meta.code
    df["name"] = meta.name
    return df.reset_index(drop=True)


def is_invalid_name(stock_name: str) -> bool:
    if not stock_name:
        return False
    upper = stock_name.upper()
    return upper.startswith("ST") or upper.startswith("*ST") or "退" in stock_name or "未知" in stock_name or "已退" in stock_name


def pct_change(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def macd(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    dif = ema(df["close"], 12) - ema(df["close"], 26)
    dea = ema(dif, 9)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> tuple[pd.Series, pd.Series, pd.Series]:
    lowest = df["low"].rolling(n, min_periods=1).min()
    highest = df["high"].rolling(n, min_periods=1).max()
    denom = (highest - lowest).replace(0, np.nan)
    rsv = ((df["close"] - lowest) / denom * 100).fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def ma(series: pd.Series, window: int, include_current: bool = True) -> pd.Series:
    src = series if include_current else series.shift(1)
    return src.rolling(window, min_periods=1).mean()


def linear_metrics(values: np.ndarray) -> tuple[float, float, float]:
    values = np.asarray(values, dtype=float)
    if len(values) < 3 or np.isnan(values).any():
        return 0.0, 1.0, 0.0
    x = np.arange(len(values), dtype=float)
    try:
        from scipy import stats

        slope, _intercept, r_value, p_value, _stderr = stats.linregress(x, values)
        return float(slope), float(p_value), float(r_value**2)
    except Exception:
        x_mean = x.mean()
        y_mean = values.mean()
        denom = np.sum((x - x_mean) ** 2)
        if denom == 0:
            return 0.0, 1.0, 0.0
        slope = float(np.sum((x - x_mean) * (values - y_mean)) / denom)
        pred = y_mean + slope * (x - x_mean)
        ss_res = float(np.sum((values - pred) ** 2))
        ss_tot = float(np.sum((values - y_mean) ** 2))
        r2 = 0.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
        return slope, 1.0 if slope <= 0 else 0.0, r2


def previous_volume_mean(df: pd.DataFrame, idx: int, days: int = 5) -> float:
    start = max(0, idx - days)
    prev = df["volume"].iloc[start:idx]
    return float(prev.mean()) if len(prev) else 0.0


def append_common(signal: dict[str, Any], df: pd.DataFrame) -> dict[str, Any]:
    latest = df.iloc[-1]
    signal.setdefault("key_date", str(latest["date"].date()))
    signal.setdefault("key_date_type", "信号日")
    signal["close"] = round(float(latest["close"]), 3)
    signal["amount"] = round(float(latest.get("amount", 0.0)), 2)
    return signal


def bottom_trend_inflection(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 120:
        return None
    tmp = df.copy()
    dif, dea, hist = macd(tmp)
    tmp["macd"] = dif - dea
    tmp["volume_ma10"] = ma(tmp["volume"], 10, include_current=False)
    look = tmp.tail(120).reset_index(drop=True)
    highest_pos = int(look["high"].idxmax())
    highest = float(look["high"].iloc[highest_pos])
    after_high = look.iloc[highest_pos + 1 :]
    if highest <= 0 or after_high.empty:
        return None
    lowest = float(after_high["low"].min())
    if (highest - lowest) / highest <= 0.45:
        return None
    recent = look.tail(20)
    if len(recent) < 2:
        return None
    current = recent.iloc[-1]
    prev = recent.iloc[:-1]
    if not (current["close"] < prev["close"].mean() and current["macd"] > prev["macd"].mean()):
        return None
    for idx in range(max(1, len(tmp) - 10), len(tmp)):
        row = tmp.iloc[idx]
        prev_close = tmp["close"].iloc[idx - 1]
        if prev_close <= 0 or row["volume_ma10"] <= 0:
            continue
        rise = (row["close"] - prev_close) / prev_close
        vol_ratio = row["volume"] / row["volume_ma10"]
        distance = (row["close"] - look["low"].min()) / look["low"].min() if look["low"].min() > 0 else 1.0
        after_surge = tmp.iloc[idx + 1 :]
        support_ok = after_surge.empty or bool((after_surge["low"] >= row["open"]).all())
        if rise > 0.08 and vol_ratio >= 2.5 and distance <= 0.15 and support_ok:
            return append_common(
                {
                    "key_date": str(row["date"].date()),
                    "key_date_type": "放量长阳日",
                    "reasons": ["深度下跌45%以上", "MACD底背离", f"放量反弹{vol_ratio:.1f}倍"],
                },
                df,
            )
    return None


def trend_acceleration_inflection(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 50:
        return None
    tmp = df.copy()
    tmp["volume_ma5"] = ma(tmp["volume"], 5, include_current=False)
    tmp["pct"] = pct_change(tmp)
    recent = tmp.tail(6)
    candidates = recent[(recent["pct"] >= 0.08) & (recent["volume"] >= recent["volume_ma5"] * 2.0)]
    if candidates.empty:
        return None
    trend_values = tmp.tail(20)["close"].to_numpy()
    slope, p_value, r2 = linear_metrics(trend_values)
    if not (slope > 0 and p_value < 0.01 and r2 > 0.5):
        return None
    for idx in reversed(list(candidates.index)):
        if idx < 1:
            continue
        prev_close = tmp.loc[idx - 1, "close"]
        look = tmp.iloc[max(0, idx - 40) : idx]
        if look.empty or prev_close <= 0:
            continue
        lowest = float(look["low"].min())
        if lowest <= 0 or (prev_close - lowest) / lowest > 0.15:
            continue
        after = tmp.iloc[idx + 1 :]
        if not after.empty and (after["low"] < tmp.loc[idx, "open"]).any():
            continue
        row = tmp.loc[idx]
        return append_common(
            {
                "key_date": str(row["date"].date()),
                "key_date_type": "放量长阳日",
                "reasons": [f"20日上升趋势R2={r2:.2f}", "放量长阳", "起涨点距低点<=15%", "回调不破开盘"],
            },
            df,
        )
    return None


def resistance_breakout(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 70:
        return None
    n = len(df)
    for idx in range(n - 1, max(60, n - 5) - 1, -1):
        if idx < 60:
            continue
        row = df.iloc[idx]
        prev_close = df["close"].iloc[idx - 1]
        if prev_close <= 0:
            continue
        rise = (row["close"] - prev_close) / prev_close
        vol_ma = previous_volume_mean(df, idx, 5)
        if rise < 0.09 or vol_ma <= 0 or row["volume"] < vol_ma * 2.2:
            continue
        prior = df.iloc[idx - 60 : idx]
        resistance = float(prior["high"].max())
        high_pos = int(prior["high"].idxmax())
        if row["close"] < resistance:
            continue
        if idx - high_pos < 30:
            continue
        after = df.iloc[idx + 1 :]
        if not after.empty and after["low"].min() < row["close"] * 0.95:
            continue
        return append_common(
            {
                "key_date": str(row["date"].date()),
                "key_date_type": "阻力位突破日",
                "resistance": round(resistance, 3),
                "breakout_ratio": round(float((row["close"] - resistance) / resistance), 4) if resistance > 0 else 0.0,
                "reasons": [f"放量长阳{rise:.1%}", "突破60日阻力", "突破后回踩不破95%"],
            },
            df,
        )
    return None


def w_bottom(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 60:
        return None
    tmp = df.copy()
    tmp["short_ma"] = ma(tmp["close"], 10)
    tmp["long_ma"] = ma(tmp["close"], 30)
    tmp["volume_ma"] = ma(tmp["volume"], 5, include_current=False)
    tmp["pct"] = pct_change(tmp)
    if not (tmp.tail(5)["pct"] > 0.05).any():
        return None
    recent5 = tmp.tail(5)
    volume_breaks = recent5[(recent5["pct"] > 0.08) & (recent5["volume"] >= recent5["volume_ma"] * 1.2)]
    if volume_breaks.empty:
        return None
    break_idx = int(volume_breaks.index[-1])
    scan_end = max(0, len(tmp) - 5)
    scan_start = max(0, scan_end - 40)
    scan = tmp.iloc[scan_start:scan_end]
    if len(scan) < 10:
        return None
    lows: list[tuple[int, float]] = []
    low_window = 5
    for idx in scan.index:
        left = max(scan_start, idx - low_window // 2)
        right = min(scan_end, idx + low_window // 2 + 1)
        row_low = float(tmp.loc[idx, "low"])
        if row_low <= 0:
            continue
        if row_low <= float(tmp["low"].iloc[left:right].min()) + 1e-9:
            if lows and idx - lows[-1][0] < 10:
                if row_low < lows[-1][1]:
                    lows[-1] = (idx, row_low)
            else:
                lows.append((idx, row_low))
    if len(lows) < 2:
        return None
    l1_idx, l1_price = lows[-2]
    l2_idx, l2_price = lows[-1]
    if l2_idx - l1_idx <= 10:
        return None
    if l1_price <= 0 or abs(l2_price - l1_price) / l1_price > 0.03:
        return None
    between = tmp.iloc[l1_idx + 1 : l2_idx]
    if between.empty:
        return None
    neckline = float(between["high"].max())
    if neckline < l1_price * 1.1:
        return None
    if tmp.loc[break_idx, "close"] < neckline * 1.01:
        return None
    if break_idx < 1 or tmp.loc[break_idx - 1, "close"] >= neckline:
        return None
    latest = tmp.iloc[-1]
    if latest["short_ma"] <= latest["long_ma"]:
        return None
    before = tmp.iloc[max(0, l1_idx - 30) : l1_idx]
    if before.empty or before["high"].max() <= l1_price * 1.2:
        return None
    after_break = tmp.iloc[break_idx + 1 :]
    if not after_break.empty and (after_break["close"] < neckline * 0.98).any():
        return None
    vol_ratio = tmp.loc[break_idx, "volume"] / tmp.loc[break_idx, "volume_ma"] if tmp.loc[break_idx, "volume_ma"] > 0 else 0.0
    return append_common(
        {
            "key_date": str(tmp.loc[break_idx, "date"].date()),
            "key_date_type": "颈线突破日",
            "neckline": round(neckline, 3),
            "l1_price": round(l1_price, 3),
            "l2_price": round(l2_price, 3),
            "volume_ratio": round(float(vol_ratio), 2),
            "reasons": ["W底双底结构", "颈线突破", "10日均线在30日均线之上", "突破后支撑不破"],
        },
        df,
    )


def multi_golden_cross(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 35:
        return None
    tmp = df.copy()
    tmp["ma5"] = ma(tmp["close"], 5)
    tmp["ma20"] = ma(tmp["close"], 20)
    k, d, j = kdj(tmp)
    dif, dea, hist = macd(tmp)
    tmp["K"] = k
    tmp["D"] = d
    tmp["J"] = j
    tmp["DIF"] = dif
    tmp["DEA"] = dea
    tmp["MACD"] = hist
    tmp["volume_ma5"] = ma(tmp["volume"], 5)
    tmp["volume_ratio"] = tmp["volume"] / tmp["volume_ma5"]
    tmp["ma_cross"] = (tmp["ma5"] > tmp["ma20"]) & (tmp["ma5"].shift(1) <= tmp["ma20"].shift(1))
    tmp["kdj_cross"] = (tmp["K"] > tmp["D"]) & (tmp["K"].shift(1) <= tmp["D"].shift(1))
    tmp["macd_cross"] = (tmp["DIF"] > tmp["DEA"]) & (tmp["DIF"].shift(1) <= tmp["DEA"].shift(1))
    latest = tmp.iloc[-1]
    if latest["close"] < latest["ma5"] or latest["close"] < latest["ma20"] or latest["volume_ratio"] < 1.0:
        return None
    recent = tmp.tail(3)
    dates: list[pd.Timestamp] = []
    for col in ["ma_cross", "kdj_cross", "macd_cross"]:
        hits = recent[recent[col]]
        if hits.empty:
            return None
        dates.append(pd.to_datetime(hits.iloc[-1]["date"]))
    if (max(dates) - min(dates)).days > 1:
        return None
    return append_common(
        {
            "key_date": str(min(dates).date()),
            "key_date_type": "多金叉共振日",
            "volume_ratio": round(float(latest["volume_ratio"]), 2),
            "reasons": ["均线金叉", "KDJ金叉", "MACD金叉"],
        },
        df,
    )


def morning_star(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 3:
        return None
    max_start = max(0, len(df) - 5)
    for start in range(len(df) - 3, max_start - 1, -1):
        old = df.iloc[start]
        mid = df.iloc[start + 1]
        new = df.iloc[start + 2]
        old_body = abs(old["close"] - old["open"])
        mid_body = abs(mid["close"] - mid["open"])
        new_body = abs(new["close"] - new["open"])
        if not (old["close"] < old["open"] and old_body >= 0.01):
            continue
        if mid_body > old_body * 0.3:
            continue
        if not (new["close"] > new["open"] and new_body >= 0.01):
            continue
        if new["open"] <= 0 or (new["close"] - new["open"]) / new["open"] <= 0.05:
            continue
        if new_body < old_body * 0.5:
            continue
        if new["close"] <= old["open"]:
            continue
        if mid["volume"] <= 0 or new["volume"] / mid["volume"] < 1.5:
            continue
        return append_common(
            {
                "key_date": str(new["date"].date()),
                "key_date_type": "启明星确认日",
                "volume_ratio": round(float(new["volume"] / mid["volume"]), 2),
                "reasons": ["长阴线", "小实体整理", "长阳反包突破并放量"],
            },
            df,
        )
    return None


def multi_party_cannon(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 22:
        return None
    tmp = df.copy()
    tmp["ma20"] = ma(tmp["close"], 20)
    tmp["pct"] = pct_change(tmp)
    first = tmp.iloc[-3]
    second = tmp.iloc[-2]
    third = tmp.iloc[-1]
    if not (first["close"] > first["open"] and first["pct"] >= 0.03):
        return None
    if not (second["close"] < second["open"]):
        return None
    first_body = abs(first["close"] - first["open"])
    second_body = abs(second["close"] - second["open"])
    if first_body <= 0 or second_body > first_body * 0.5:
        return None
    fallback = (first["close"] - second["close"]) / first_body
    if fallback > 0.5:
        return None
    if not (third["close"] > third["open"] and third["pct"] >= 0.03 and third["close"] > first["close"]):
        return None
    if second["volume"] > first["volume"] * 0.8 or third["volume"] <= first["volume"]:
        return None
    if third["close"] < third["ma20"]:
        return None
    if first["pct"] >= 0.07 and third["pct"] >= 0.07:
        category = "strong"
    elif first["pct"] < 0.03 and third["pct"] < 0.03:
        category = "weak"
    else:
        category = "standard"
    return append_common(
        {
            "key_date": str(third["date"].date()),
            "key_date_type": "多方炮确认日",
            "category": category,
            "volume_ratio": round(float(third["volume"] / first["volume"]), 2),
            "reasons": [f"两阳夹一阴{category}", "第二根缩量", "第三根放量突破前高"],
        },
        df,
    )


def limit_up_pullback(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 12:
        return None
    tmp = df.copy()
    tmp["pct"] = pct_change(tmp)
    start = max(1, len(tmp) - 6)
    for idx in range(len(tmp) - 1, start - 1, -1):
        row = tmp.iloc[idx]
        vol_ma = previous_volume_mean(tmp, idx, 5)
        if row["pct"] < 0.095 or vol_ma <= 0 or row["volume"] / vol_ma < 2.2:
            continue
        after = tmp.iloc[idx + 1 :]
        days = len(after)
        if days < 1 or days > 9:
            continue
        highest = float(after["high"].max())
        lowest = float(after["low"].min())
        if highest <= 0:
            continue
        pullback_range = (highest - lowest) / highest
        if pullback_range > 0.15:
            continue
        closes = after["close"]
        if (closes < row["close"] * 0.95).any() or (closes > row["close"] * 1.05).any():
            continue
        if not (closes < row["close"]).any():
            continue
        if not (after["volume"] <= row["volume"] * 0.5).any():
            continue
        return append_common(
            {
                "key_date": str(row["date"].date()),
                "key_date_type": "涨停日",
                "pullback_days": days,
                "pullback_range": round(float(pullback_range), 4),
                "support_price": round(float(row["close"] * 0.95), 3),
                "reasons": ["放量涨停", "涨停后1-9日缩量回调", "不破涨停收盘95%"],
            },
            df,
        )
    return None


def limit_up_sideways(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 60:
        return None
    tmp = df.copy()
    tmp["pct"] = pct_change(tmp)
    start = max(1, len(tmp) - 10)
    for idx in range(len(tmp) - 2, start - 1, -1):
        row = tmp.iloc[idx]
        vol_ma = previous_volume_mean(tmp, idx, 5)
        if row["pct"] < 0.095 or vol_ma <= 0 or row["volume"] / vol_ma < 1.8:
            continue
        after = tmp.iloc[idx + 1 :]
        days = len(after)
        if days < 1 or days > 10:
            continue
        if after["high"].max() > row["close"] * 1.08:
            continue
        if after["low"].min() < row["close"] * 0.95:
            continue
        if (after["close"] < row["close"] * 0.99).any():
            continue
        if not (after["volume"] <= row["volume"] * 0.7).any():
            continue
        return append_common(
            {
                "key_date": str(row["date"].date()),
                "key_date_type": "涨停日",
                "sideways_days": days,
                "support_level": round(float(row["close"]), 3),
                "reasons": ["放量涨停", "涨停后横盘整理", "横盘缩量且支撑不破"],
            },
            df,
        )
    return None


def strong_wash_weak_to_strong(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 12:
        return None
    tmp = df.copy()
    tmp["pct"] = pct_change(tmp)
    tmp["volume_ma5"] = ma(tmp["volume"], 5)
    tmp["volume_ratio"] = tmp["volume"] / tmp["volume_ma5"]
    start = max(1, len(tmp) - 6)
    for big_idx in range(len(tmp) - 2, start - 1, -1):
        big = tmp.iloc[big_idx]
        if not (big["pct"] >= 0.08 and big["close"] > big["open"] and big["volume_ratio"] >= 1.5):
            continue
        wash_idx = big_idx + 1
        if wash_idx >= len(tmp):
            continue
        wash = tmp.iloc[wash_idx]
        if not (wash["close"] < wash["open"] and wash["volume"] >= big["volume"] * 1.2):
            continue
        for rev_idx in range(wash_idx + 1, min(wash_idx + 4, len(tmp))):
            rev = tmp.iloc[rev_idx]
            if not (rev["close"] > rev["open"] and (rev["close"] > big["close"] or rev["close"] > wash["open"])):
                continue
            if (tmp.iloc[rev_idx + 1 :]["close"] <= big["close"]).any():
                continue
            latest_delta_days = (tmp.iloc[-1]["date"] - rev["date"]).days
            if latest_delta_days > 5:
                continue
            return append_common(
                {
                    "key_date": str(rev["date"].date()),
                    "key_date_type": "反包阳线日",
                    "big_candle_date": str(big["date"].date()),
                    "wash_candle_date": str(wash["date"].date()),
                    "volume_ratio": round(float(big["volume_ratio"]), 2),
                    "wash_volume_ratio": round(float(wash["volume"] / big["volume"]), 2),
                    "reasons": ["放量大阳线", "次日放量阴线洗盘", "3日内反包", "反包后持续强势"],
                },
                df,
            )
    return None


def immortal_guidance(df: pd.DataFrame) -> dict[str, Any] | None:
    if len(df) < 30:
        return None
    tmp = df.copy()
    tmp["ma5"] = ma(tmp["close"], 5)
    tmp["ma10"] = ma(tmp["close"], 10)
    tmp["ma20"] = ma(tmp["close"], 20)
    tmp["volume_ma5"] = ma(tmp["volume"], 5, include_current=False)
    today = tmp.iloc[-1]
    if today["close"] < today["ma5"] or today["volume"] <= 0:
        return None
    slope, _p_value, r2 = linear_metrics(tmp.tail(20)["close"].to_numpy())
    if not (slope > 0 and r2 >= 0.5):
        return None
    for offset in range(1, min(4, len(tmp) - 1)):
        signal_idx = len(tmp) - 1 - offset
        prev_idx = signal_idx - 1
        if prev_idx < 0:
            continue
        signal = tmp.iloc[signal_idx]
        prev_close = tmp.iloc[prev_idx]["close"]
        if prev_close <= 0:
            continue
        surge_pct = (signal["high"] - prev_close) / prev_close
        if surge_pct < 0.08:
            continue
        upper_shadow = signal["high"] - (signal["close"] if signal["close"] > signal["open"] else signal["open"])
        upper_ratio = upper_shadow / signal["high"] if signal["high"] > 0 else 0.0
        if upper_ratio < 0.04:
            continue
        if signal["volume_ma5"] > 0 and signal["volume"] / signal["volume_ma5"] < 1.5:
            continue
        if not (signal["ma5"] > signal["ma10"] > signal["ma20"] > 0):
            continue
        upper_50 = ((signal["close"] if signal["close"] > signal["open"] else signal["open"]) + signal["high"]) / 2
        between = tmp.iloc[signal_idx + 1 : -1]
        if not between.empty and (between["close"] >= upper_50).any():
            continue
        if today["close"] < upper_50:
            continue
        return append_common(
            {
                "key_date": str(signal["date"].date()),
                "key_date_type": "仙人指路信号日",
                "volume_ratio": round(float(signal["volume"] / max(1, signal["volume_ma5"])), 2),
                "upper_shadow_ratio": round(float(upper_ratio), 4),
                "upper_shadow_50_price": round(float(upper_50), 3),
                "reasons": ["冲高8%+", "长上影", "放量", "MA5>MA10>MA20", "今日收复上影线50%"],
            },
            df,
        )
    return None


STRATEGIES: dict[str, Callable[[pd.DataFrame], dict[str, Any] | None]] = {
    "BottomTrendInflection": bottom_trend_inflection,
    "TrendAccelerationInflection": trend_acceleration_inflection,
    "ResistanceBreakout": resistance_breakout,
    "WBottom": w_bottom,
    "MultiGoldenCross": multi_golden_cross,
    "MorningStar": morning_star,
    "MultiPartyCannon": multi_party_cannon,
    "LimitUpPullback": limit_up_pullback,
    "LimitUpSideways": limit_up_sideways,
    "StrongWashWeakToStrong": strong_wash_weak_to_strong,
    "ImmortalGuidance": immortal_guidance,
}


def collect_panel(
    workspace: str,
    universe: list[StockMeta],
    datalen: int,
    pause_seconds: float,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, str]]]:
    fetcher = load_workspace_fetcher(workspace)
    panel: dict[str, pd.DataFrame] = {}
    errors: list[dict[str, str]] = []
    for meta in universe:
        try:
            rows = fetcher(meta.sina_symbol, datalen=datalen)
            df = normalize_frame(rows, meta)
            if df.empty:
                errors.append({"code": meta.code, "name": meta.name, "error": "empty kline"})
            else:
                panel[meta.code] = df
        except Exception as exc:
            errors.append({"code": meta.code, "name": meta.name, "error": f"{type(exc).__name__}: {exc}"})
        if pause_seconds:
            time.sleep(pause_seconds)
    return panel, errors


def last_trading_dates(panel: dict[str, pd.DataFrame], days: int, end_date: str | None) -> list[pd.Timestamp]:
    all_dates = pd.concat([df["date"] for df in panel.values()], ignore_index=True).dropna().drop_duplicates()
    all_dates = all_dates.sort_values()
    if end_date:
        end_ts = pd.to_datetime(end_date)
        all_dates = all_dates[all_dates <= end_ts]
    return list(all_dates.tail(days))


def scan_dates(
    panel: dict[str, pd.DataFrame],
    eval_dates: list[pd.Timestamp],
    top_per_strategy: int,
    top_union: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for d in eval_dates:
        strategy_hits: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATEGIES}
        union: dict[str, dict[str, Any]] = {}

        for code, full_df in panel.items():
            df = full_df[full_df["date"] <= d].copy()
            if df.empty or df.iloc[-1]["date"] != d:
                continue
            stock_name = str(df.iloc[-1].get("name", code))
            if is_invalid_name(stock_name):
                continue
            base = {
                "code": code,
                "name": stock_name,
                "close": round(float(df.iloc[-1]["close"]), 3),
                "amount": round(float(df.iloc[-1].get("amount", 0.0)), 2),
            }
            for strategy, func in STRATEGIES.items():
                try:
                    signal = func(df)
                except Exception as exc:
                    signal = {"error": f"{type(exc).__name__}: {exc}"}
                if not signal or "error" in signal:
                    continue
                hit = dict(base)
                hit.update(signal)
                hit["strategy"] = strategy
                hit["strategy_cn"] = STRATEGY_NAMES_CN[strategy]
                hit["weight"] = STRATEGY_WEIGHTS[strategy]
                strategy_hits[strategy].append(hit)

                item = union.setdefault(
                    code,
                    {
                        "code": code,
                        "name": stock_name,
                        "close": base["close"],
                        "amount": base["amount"],
                        "score": 0,
                        "strategies": [],
                    },
                )
                item["score"] += STRATEGY_WEIGHTS[strategy]
                item["strategies"].append(STRATEGY_NAMES_CN[strategy])

        compact_hits: dict[str, dict[str, Any]] = {}
        for strategy, hits in strategy_hits.items():
            hits = sorted(hits, key=lambda x: (x.get("weight", 0), x.get("amount", 0.0)), reverse=True)
            compact_hits[strategy] = {
                "name_cn": STRATEGY_NAMES_CN[strategy],
                "weight": STRATEGY_WEIGHTS[strategy],
                "count": len(hits),
                "top": hits[:top_per_strategy],
            }

        union_top = sorted(union.values(), key=lambda x: (x["score"], x.get("amount", 0.0)), reverse=True)[:top_union]
        output.append({"date": str(d.date()), "strategies": compact_hits, "union_top": union_top})
    return output


def format_markdown(result: dict[str, Any]) -> str:
    params = result["parameters"]
    coverage = result["coverage"]
    lines = [
        "# KHunter 蒸馏策略最近扫描",
        "",
        f"- 数据源: {result['data_source']}",
        f"- 股票池: {params['universe_label']}；来源 {params['universe_source']}；成功拉取 {coverage['fetched_symbols']} / {coverage['requested_symbols']} 只",
        f"- 日期: {', '.join(result['dates']) or '-'}",
        "- 口径: 仅蒸馏 KHunter 技术形态策略；不包含原 Web 系统的资金面、基本面、板块强度和事件驱动评分。",
        "",
    ]
    for day in result["daily_results"]:
        lines.append(f"## {day['date']}")
        if day["union_top"]:
            desc = "；".join(
                f"{item['name']}({item['code']}) score={item['score']} [{'/'.join(item['strategies'])}]"
                for item in day["union_top"]
            )
            lines.append(f"- 综合技术得分Top: {desc}")
        else:
            lines.append("- 综合技术得分Top: 无。")
        for strategy, payload in day["strategies"].items():
            count = payload["count"]
            top = payload["top"]
            if not count:
                continue
            desc = "；".join(
                f"{item['name']}({item['code']}) close={item['close']} key={item.get('key_date', '-')}"
                for item in top
            )
            lines.append(f"- {payload['name_cn']}({strategy}, 权重{payload['weight']}): {count} 只；{desc}")
        lines.append("")
    if result["errors_sample"]:
        lines.append("## 缺数样例")
        for item in result["errors_sample"]:
            lines.append(f"- {item['code']} {item['name']}: {item['error']}")
        lines.append("")
    lines.append("以上为量化研究与历史回测/扫描，不构成投资建议。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Distilled KHunter recent A-share scanner.")
    parser.add_argument("--workspace", default="/Users/zhangshuai/michael-vibe-trading")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=300)
    parser.add_argument("--symbols", help="Comma-separated six-digit stock codes. Overrides active universe.")
    parser.add_argument("--datalen", type=int, default=260)
    parser.add_argument("--pause-seconds", type=float, default=0.03)
    parser.add_argument("--top-per-strategy", type=int, default=8)
    parser.add_argument("--top-union", type=int, default=12)
    parser.add_argument("--end-date", help="YYYY-MM-DD inclusive evaluation cutoff.")
    parser.add_argument("--include-st", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", help="Optional JSON output path.")
    args = parser.parse_args()

    if args.symbols:
        universe = [
            StockMeta(code=code.strip(), name=code.strip(), sina_symbol=to_sina_symbol(code.strip()))
            for code in args.symbols.split(",")
            if code.strip()
        ]
        universe_label = f"指定股票 {len(universe)} 只"
        universe_source = "manual symbols"
    else:
        try:
            universe = fetch_eastmoney_active_universe(args.max_symbols, include_st=args.include_st)
            universe_source = "Eastmoney amount ranking"
            if not universe:
                raise RuntimeError("empty Eastmoney universe")
        except Exception as exc:
            print(f"[khunter] Eastmoney universe failed, fallback to Sina: {exc}", file=sys.stderr, flush=True)
            universe = fetch_sina_active_universe(args.max_symbols, include_st=args.include_st)
            universe_source = "Sina market center amount ranking"
        universe_label = f"成交额排序前 {len(universe)} 只"

    panel, errors = collect_panel(
        workspace=args.workspace,
        universe=universe,
        datalen=args.datalen,
        pause_seconds=args.pause_seconds,
    )
    if not panel:
        raise SystemExit("No kline data fetched; cannot run KHunter scan.")

    eval_dates = last_trading_dates(panel, days=args.days, end_date=args.end_date)
    daily_results = scan_dates(
        panel=panel,
        eval_dates=eval_dates,
        top_per_strategy=args.top_per_strategy,
        top_union=args.top_union,
    )
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": "workspace factor_analysis.data_sources.fetch_sina_daily_kline + Eastmoney/Sina active universe",
        "parameters": {
            "workspace": str(Path(args.workspace).expanduser().resolve()),
            "days": args.days,
            "max_symbols": len(universe),
            "universe_label": universe_label,
            "universe_source": universe_source,
            "datalen": args.datalen,
            "end_date": args.end_date or "",
        },
        "coverage": {
            "requested_symbols": len(universe),
            "fetched_symbols": len(panel),
            "error_symbols": len(errors),
        },
        "dates": [str(d.date()) for d in eval_dates],
        "daily_results": daily_results,
        "errors_sample": errors[:20],
        "strategy_weights": STRATEGY_WEIGHTS,
    }
    result["report_markdown"] = format_markdown(result)

    if args.output:
        out = Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["report_markdown"])


if __name__ == "__main__":
    main()
