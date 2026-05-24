#!/usr/bin/env python3
"""Run distilled Sequoia-X A-share strategies over recent trading days.

The script intentionally reuses the user's workspace data adapter when present:
`factor_analysis.data_sources.fetch_sina_daily_kline`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


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


def _get_json(url: str, params: dict[str, Any], timeout: int = 20, retries: int = 3) -> dict[str, Any]:
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
    """Fetch a liquid A-share universe sorted by amount."""
    try:
        universe = fetch_eastmoney_active_universe(max_symbols=max_symbols, include_st=include_st)
        if universe:
            return universe
    except Exception as exc:
        print(f"[sequoia-x] Eastmoney universe failed, fallback to Sina: {exc}", file=sys.stderr, flush=True)
    return fetch_sina_active_universe(max_symbols=max_symbols, include_st=include_st)


def fetch_eastmoney_active_universe(max_symbols: int, include_st: bool = False) -> list[StockMeta]:
    """Fetch a liquid A-share universe from Eastmoney sorted by amount."""
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
        if not include_st and ("ST" in name.upper() or "退" in name):
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
    """Fetch a liquid A-share universe from Sina sorted by amount."""
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
            if not include_st and ("ST" in name.upper() or "退" in name):
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


def load_workspace_fetcher(workspace: str):
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
    df["code"] = meta.code
    df["name"] = meta.name
    return df.reset_index(drop=True)


def ma_volume(df: pd.DataFrame) -> bool:
    if len(df) < 20:
        return False
    tmp = df.copy()
    tmp["ma5"] = tmp["close"].rolling(5).mean()
    tmp["ma20"] = tmp["close"].rolling(20).mean()
    tmp["vol_ma20"] = tmp["volume"].rolling(20).mean()
    prev = tmp.iloc[-2]
    last = tmp.iloc[-1]
    return bool(prev["ma5"] < prev["ma20"] and last["ma5"] > last["ma20"] and last["volume"] > last["vol_ma20"] * 1.5)


def turtle_trade(df: pd.DataFrame, min_amount: float) -> bool:
    if len(df) < 21:
        return False
    high_20 = df["high"].shift(1).rolling(20).max().iloc[-1]
    if pd.isna(high_20):
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    return bool(
        last["close"] > high_20
        and last["amount"] > min_amount
        and last["close"] > last["open"]
        and last["close"] > prev["close"]
    )


def high_tight_flag(df: pd.DataFrame) -> bool:
    if len(df) < 40:
        return False
    tail40 = df.tail(40)
    tail10 = df.tail(10)
    high40 = tail40["high"].max()
    low40 = tail40["low"].min()
    high10 = tail10["high"].max()
    low10 = tail10["low"].min()
    if low40 <= 0 or low10 <= 0:
        return False
    vol_ma20 = df["volume"].iloc[-21:-1].mean()
    last_volume = df["volume"].iloc[-1]
    return bool(high40 / low40 > 1.6 and high10 / low10 < 1.15 and low10 >= high40 * 0.8 and last_volume < vol_ma20 * 0.6)


def limit_up_shakeout(df: pd.DataFrame) -> bool:
    if len(df) < 3:
        return False
    prev2 = df.iloc[-3]
    prev1 = df.iloc[-2]
    today = df.iloc[-1]
    return bool(
        prev1["close"] >= prev2["close"] * 1.095
        and today["close"] < today["open"]
        and today["volume"] > prev1["volume"] * 2.0
        and today["low"] >= prev1["close"]
    )


def uptrend_limit_down(df: pd.DataFrame) -> bool:
    if len(df) < 60:
        return False
    tmp = df.copy()
    tmp["ma20"] = tmp["close"].rolling(20).mean()
    tmp["ma60"] = tmp["close"].rolling(60).mean()
    tmp["vol_ma20"] = tmp["volume"].rolling(20).mean()
    prev = tmp.iloc[-2]
    today = tmp.iloc[-1]
    if pd.isna(prev["ma20"]) or pd.isna(prev["ma60"]) or pd.isna(today["vol_ma20"]):
        return False
    return bool(prev["ma20"] > prev["ma60"] and today["close"] <= prev["close"] * 0.905 and today["volume"] > today["vol_ma20"] * 2.0)


def build_metrics(panel: dict[str, pd.DataFrame], eval_dates: list[pd.Timestamp], rps_period: int) -> dict[pd.Timestamp, pd.DataFrame]:
    frames = []
    for code, df in panel.items():
        if len(df) < 2:
            continue
        tmp = df[["date", "code", "name", "close", "high", "amount"]].copy()
        tmp["rps_return"] = tmp["close"] / tmp["close"].shift(rps_period) - 1.0
        tmp["roll_high"] = tmp["high"].rolling(rps_period, min_periods=max(20, rps_period // 2)).max()
        frames.append(tmp)
    if not frames:
        return {d: pd.DataFrame() for d in eval_dates}
    all_metrics = pd.concat(frames, ignore_index=True)
    out: dict[pd.Timestamp, pd.DataFrame] = {}
    for d in eval_dates:
        day = all_metrics[all_metrics["date"] == d].copy()
        if day.empty:
            out[d] = day
            continue
        day = day.dropna(subset=["rps_return", "roll_high"])
        if day.empty:
            out[d] = day
            continue
        day["rps"] = day["rps_return"].rank(pct=True) * 100.0
        out[d] = day
    return out


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
    min_amount: float,
    rps_period: int,
    rps_threshold: float,
    top_per_strategy: int,
) -> list[dict[str, Any]]:
    rps_metrics = build_metrics(panel, eval_dates, rps_period)
    output: list[dict[str, Any]] = []
    for d in eval_dates:
        strategy_hits: dict[str, list[dict[str, Any]]] = {
            "MaVolume": [],
            "TurtleTrade": [],
            "HighTightFlag": [],
            "LimitUpShakeout": [],
            "UptrendLimitDown": [],
            "RpsBreakout": [],
        }
        for code, full_df in panel.items():
            df = full_df[full_df["date"] <= d].copy()
            if df.empty or df.iloc[-1]["date"] != d:
                continue
            last = df.iloc[-1]
            base = {
                "code": code,
                "name": str(last.get("name", code)),
                "close": round(float(last["close"]), 3),
                "amount": round(float(last.get("amount", 0.0)), 2),
            }
            if ma_volume(df):
                strategy_hits["MaVolume"].append(dict(base))
            if turtle_trade(df, min_amount=min_amount):
                strategy_hits["TurtleTrade"].append(dict(base))
            if high_tight_flag(df):
                strategy_hits["HighTightFlag"].append(dict(base))
            if limit_up_shakeout(df):
                strategy_hits["LimitUpShakeout"].append(dict(base))
            if uptrend_limit_down(df):
                strategy_hits["UptrendLimitDown"].append(dict(base))

        day_rps = rps_metrics.get(d, pd.DataFrame())
        if day_rps is not None and not day_rps.empty:
            selected = day_rps[(day_rps["rps"] >= rps_threshold) & (day_rps["close"] >= day_rps["roll_high"] * 0.90)].copy()
            selected = selected.sort_values(["rps", "amount"], ascending=False)
            for _, row in selected.iterrows():
                strategy_hits["RpsBreakout"].append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "close": round(float(row["close"]), 3),
                        "amount": round(float(row.get("amount", 0.0)), 2),
                        "rps": round(float(row["rps"]), 2),
                        "rps_return": round(float(row["rps_return"]), 4),
                    }
                )

        compact_hits: dict[str, dict[str, Any]] = {}
        for strategy, hits in strategy_hits.items():
            hits = sorted(hits, key=lambda x: (x.get("rps", 0.0), x.get("amount", 0.0)), reverse=True)
            compact_hits[strategy] = {"count": len(hits), "top": hits[:top_per_strategy]}

        output.append({"date": str(d.date()), "strategies": compact_hits})
    return output


def format_markdown(result: dict[str, Any]) -> str:
    params = result["parameters"]
    coverage = result["coverage"]
    lines = [
        "# Sequoia-X 最近一周策略扫描",
        "",
        f"- 数据源: {result['data_source']}",
        f"- 股票池: 成交额排序前 {params['max_symbols']}；成功拉取 {coverage['fetched_symbols']} / {coverage['requested_symbols']} 只",
        f"- 日期: {', '.join(result['dates']) or '-'}",
        f"- 说明: amount 使用本地 Sina adapter 的 close * volume 代理值；RPS 为本次股票池内横截面排名。",
        "",
    ]
    for day in result["daily_results"]:
        lines.append(f"## {day['date']}")
        for strategy, payload in day["strategies"].items():
            count = payload["count"]
            top = payload["top"]
            if not count:
                continue
            desc = "；".join(
                f"{item['name']}({item['code']}) close={item['close']}"
                + (f" rps={item['rps']}" if "rps" in item else "")
                for item in top
            )
            lines.append(f"- {strategy}: {count} 只；{desc}")
        if all(payload["count"] == 0 for payload in day["strategies"].values()):
            lines.append("- 无策略命中。")
        lines.append("")
    if result["errors_sample"]:
        lines.append("## 缺数样例")
        for item in result["errors_sample"]:
            lines.append(f"- {item['code']} {item['name']}: {item['error']}")
        lines.append("")
    lines.append("以上为量化研究与历史回测/扫描，不构成投资建议。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Distilled Sequoia-X recent-week A-share scanner.")
    parser.add_argument("--workspace", default="/Users/zhangshuai/michael-vibe-trading")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=300)
    parser.add_argument("--symbols", help="Comma-separated six-digit stock codes. Overrides Eastmoney universe.")
    parser.add_argument("--datalen", type=int, default=180)
    parser.add_argument("--pause-seconds", type=float, default=0.03)
    parser.add_argument("--min-amount", type=float, default=100_000_000)
    parser.add_argument("--rps-period", type=int, default=120)
    parser.add_argument("--rps-threshold", type=float, default=90.0)
    parser.add_argument("--top-per-strategy", type=int, default=10)
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
    else:
        universe = fetch_active_universe(args.max_symbols, include_st=args.include_st)

    panel, errors = collect_panel(
        workspace=args.workspace,
        universe=universe,
        datalen=args.datalen,
        pause_seconds=args.pause_seconds,
    )
    if not panel:
        raise SystemExit("No kline data fetched; cannot run Sequoia-X scan.")

    eval_dates = last_trading_dates(panel, days=args.days, end_date=args.end_date)
    daily_results = scan_dates(
        panel=panel,
        eval_dates=eval_dates,
        min_amount=args.min_amount,
        rps_period=args.rps_period,
        rps_threshold=args.rps_threshold,
        top_per_strategy=args.top_per_strategy,
    )
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": "workspace factor_analysis.data_sources.fetch_sina_daily_kline + Eastmoney/Sina active universe",
        "parameters": {
            "workspace": str(Path(args.workspace).expanduser().resolve()),
            "days": args.days,
            "max_symbols": len(universe),
            "datalen": args.datalen,
            "min_amount": args.min_amount,
            "rps_period": args.rps_period,
            "rps_threshold": args.rps_threshold,
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
