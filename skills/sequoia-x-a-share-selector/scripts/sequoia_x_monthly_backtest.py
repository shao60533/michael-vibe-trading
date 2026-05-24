#!/usr/bin/env python3
"""Generate Sequoia-X daily signals and expert-backtest inputs for a month."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sequoia_x_weekly_scan import (  # noqa: E402
    StockMeta,
    build_metrics,
    collect_panel,
    fetch_active_universe,
    high_tight_flag,
    limit_up_shakeout,
    ma_volume,
    to_sina_symbol,
    turtle_trade,
    uptrend_limit_down,
)


STRATEGY_CONFIDENCE = {
    "MaVolume": 0.62,
    "TurtleTrade": 0.66,
    "HighTightFlag": 0.64,
    "LimitUpShakeout": 0.58,
    "UptrendLimitDown": 0.52,
    "RpsBreakout": 0.65,
}


def parse_date(value: str) -> pd.Timestamp:
    return pd.to_datetime(value).normalize()


def get_eval_dates(panel: dict[str, pd.DataFrame], start: str, end: str) -> list[pd.Timestamp]:
    start_ts = parse_date(start)
    end_ts = parse_date(end)
    all_dates = pd.concat([df["date"] for df in panel.values()], ignore_index=True).dropna().drop_duplicates()
    all_dates = all_dates.sort_values()
    dates = all_dates[(all_dates >= start_ts) & (all_dates <= end_ts)]
    return list(dates)


def row_base(df: pd.DataFrame, code: str) -> dict[str, Any]:
    last = df.iloc[-1]
    return {
        "code": code,
        "name": str(last.get("name", code)),
        "close": float(last["close"]),
        "amount": float(last.get("amount", 0.0)),
    }


def make_prediction(strategy: str, item: dict[str, Any], date_text: str, horizon_days: int) -> dict[str, Any]:
    code = item["code"]
    return {
        "case_id": f"sequoia_{strategy}_{code}_{date_text}_{horizon_days}d",
        "date": date_text,
        "code": code,
        "action": "buy",
        "horizon_days": horizon_days,
        "confidence": STRATEGY_CONFIDENCE.get(strategy, 0.60),
        "expert": strategy,
        "expert_version": "sequoia-x-distilled-444c0db",
        "tags": ["sequoia-x", strategy],
        "reason": f"{strategy} signal triggered using data through {date_text} close.",
    }


def generate_signals(
    panel: dict[str, pd.DataFrame],
    eval_dates: list[pd.Timestamp],
    horizon_days: int,
    min_amount: float,
    rps_period: int,
    rps_threshold: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    predictions: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    rps_metrics = build_metrics(panel, eval_dates, rps_period)

    for d in eval_dates:
        date_text = str(d.date())
        day_hits: dict[str, list[dict[str, Any]]] = {
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
            base = row_base(df, code)
            if ma_volume(df):
                day_hits["MaVolume"].append(dict(base))
            if turtle_trade(df, min_amount=min_amount):
                day_hits["TurtleTrade"].append(dict(base))
            if high_tight_flag(df):
                day_hits["HighTightFlag"].append(dict(base))
            if limit_up_shakeout(df):
                day_hits["LimitUpShakeout"].append(dict(base))
            if uptrend_limit_down(df):
                day_hits["UptrendLimitDown"].append(dict(base))

        day_rps = rps_metrics.get(d, pd.DataFrame())
        if day_rps is not None and not day_rps.empty:
            selected = day_rps[(day_rps["rps"] >= rps_threshold) & (day_rps["close"] >= day_rps["roll_high"] * 0.90)].copy()
            selected = selected.sort_values(["rps", "amount"], ascending=False)
            for _, row in selected.iterrows():
                day_hits["RpsBreakout"].append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "close": float(row["close"]),
                        "amount": float(row.get("amount", 0.0)),
                        "rps": float(row["rps"]),
                        "rps_return": float(row["rps_return"]),
                    }
                )

        for strategy, hits in day_hits.items():
            hits = sorted(hits, key=lambda x: (x.get("rps", 0.0), x.get("amount", 0.0)), reverse=True)
            daily_rows.append(
                {
                    "date": date_text,
                    "strategy": strategy,
                    "signal_count": len(hits),
                    "codes": ",".join(item["code"] for item in hits),
                    "names": ",".join(item["name"] for item in hits),
                }
            )
            for item in hits:
                predictions.append(make_prediction(strategy, item, date_text, horizon_days))
    return predictions, daily_rows


def write_panel_prices(panel: dict[str, pd.DataFrame], prices_dir: Path) -> None:
    prices_dir.mkdir(parents=True, exist_ok=True)
    for code, df in panel.items():
        out = prices_dir / f"{code}.csv"
        fields = ["date", "code", "open", "high", "low", "close", "volume", "amount"]
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for _, row in df.iterrows():
                writer.writerow(
                    {
                        "date": str(row["date"].date() if hasattr(row["date"], "date") else row["date"])[:10],
                        "code": code,
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row.get("volume", ""),
                        "amount": row.get("amount", ""),
                    }
                )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_universe(args: argparse.Namespace) -> list[StockMeta]:
    if args.symbols:
        return [
            StockMeta(code=code.strip(), name=code.strip(), sina_symbol=to_sina_symbol(code.strip()))
            for code in args.symbols.split(",")
            if code.strip()
        ]
    return fetch_active_universe(args.max_symbols, include_st=args.include_st)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="/Users/zhangshuai/michael-vibe-trading")
    parser.add_argument("--start", default="2026-04-01")
    parser.add_argument("--end", default="2026-04-30")
    parser.add_argument("--horizon-days", type=int, default=3)
    parser.add_argument("--max-symbols", type=int, default=300)
    parser.add_argument("--symbols")
    parser.add_argument("--datalen", type=int, default=260)
    parser.add_argument("--pause-seconds", type=float, default=0.02)
    parser.add_argument("--min-amount", type=float, default=100_000_000)
    parser.add_argument("--rps-period", type=int, default=120)
    parser.add_argument("--rps-threshold", type=float, default=90.0)
    parser.add_argument("--include-st", action="store_true")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    universe = build_universe(args)
    panel, errors = collect_panel(
        workspace=args.workspace,
        universe=universe,
        datalen=args.datalen,
        pause_seconds=args.pause_seconds,
    )
    if not panel:
        raise SystemExit("No K-line data fetched.")

    eval_dates = get_eval_dates(panel, args.start, args.end)
    predictions, daily_rows = generate_signals(
        panel=panel,
        eval_dates=eval_dates,
        horizon_days=args.horizon_days,
        min_amount=args.min_amount,
        rps_period=args.rps_period,
        rps_threshold=args.rps_threshold,
    )

    prices_dir = output_dir / "prices"
    write_panel_prices(panel, prices_dir)
    write_jsonl(output_dir / "predictions.jsonl", predictions)
    write_csv(output_dir / "daily_signals.csv", daily_rows)
    (output_dir / "fetch_errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")

    metadata = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workspace": str(Path(args.workspace).expanduser().resolve()),
        "start": args.start,
        "end": args.end,
        "horizon_days": args.horizon_days,
        "requested_symbols": len(universe),
        "fetched_symbols": len(panel),
        "error_symbols": len(errors),
        "eval_dates": [str(d.date()) for d in eval_dates],
        "prediction_count": len(predictions),
        "data_source": "workspace factor_analysis.data_sources.fetch_sina_daily_kline + Eastmoney/Sina active universe",
        "universe_note": "Signals are no-lookahead per decision date; the engineering test uses a fixed liquid universe fetched at run time, so full-market/survivorship conclusions require a historical universe source.",
        "amount_note": "The workspace Sina adapter approximates amount as close * volume.",
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
