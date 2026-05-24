#!/usr/bin/env python3
"""A-share expert judgment blind-test evaluator.

This script intentionally uses only the Python standard library so it can run
inside the current deployment wrapper without adding Docker dependencies.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any


BULLISH = {"buy", "hold"}
BEARISH = {"avoid", "sell", "reduce"}
NEUTRAL = {"watch"}


@dataclass
class Bar:
    date: str
    code: str
    open: float
    high: float
    low: float
    close: float
    raw: dict[str, str]


def _float(value: Any, default: float = math.nan) -> float:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default


def _norm_code(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-6:].zfill(6) if digits else str(value)


def load_prices(prices_dir: Path) -> dict[str, list[Bar]]:
    prices: dict[str, list[Bar]] = defaultdict(list)
    for path in sorted(prices_dir.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                code = _norm_code(row.get("code") or path.stem)
                dt = row.get("date") or row.get("time") or row.get("datetime")
                if not dt:
                    continue
                bar = Bar(
                    date=dt[:10],
                    code=code,
                    open=_float(row.get("open")),
                    high=_float(row.get("high")),
                    low=_float(row.get("low")),
                    close=_float(row.get("close")),
                    raw=row,
                )
                values = (bar.open, bar.high, bar.low, bar.close)
                if all(math.isfinite(x) for x in values):
                    prices[code].append(bar)
    for code in list(prices):
        prices[code].sort(key=lambda bar: bar.date)
    return dict(prices)


def index_by_date(bars: list[Bar]) -> dict[str, int]:
    return {bar.date: i for i, bar in enumerate(bars)}


def pct(value: Any) -> Any:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return f"{float(value) * 100:.2f}%"
    return value


def max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for ret in returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def evaluate_prediction(
    pred: dict[str, Any],
    prices: dict[str, list[Bar]],
    default_horizon: int,
    cost_bps: float,
    slippage_bps: float,
    neutral_threshold: float,
) -> dict[str, Any]:
    code = _norm_code(pred.get("code", ""))
    bars = prices.get(code, [])
    by_date = index_by_date(bars)
    decision_date = str(pred.get("date", ""))[:10]
    if not code or not bars:
        return {"status": "skipped", "skip_reason": "missing_price_data", **pred}
    if decision_date not in by_date:
        return {"status": "skipped", "skip_reason": "decision_date_not_found", **pred}

    horizon = int(pred.get("horizon_days") or default_horizon)
    decision_idx = by_date[decision_date]
    entry_idx = decision_idx + 1
    exit_idx = min(decision_idx + horizon, len(bars) - 1)
    if entry_idx >= len(bars) or exit_idx <= decision_idx:
        return {"status": "skipped", "skip_reason": "insufficient_forward_bars", **pred}

    action = str(pred.get("action", "")).lower().strip()
    entry_bar = bars[entry_idx]
    exit_bar = bars[exit_idx]
    entry = _float(pred.get("entry"), entry_bar.open)
    raw_return = exit_bar.close / entry - 1.0
    round_trip_cost = (cost_bps + slippage_bps) / 10000.0 * 2.0
    net_return = raw_return - round_trip_cost if action in BULLISH else 0.0

    window = bars[entry_idx : exit_idx + 1]
    mfe = max(bar.high / entry - 1.0 for bar in window)
    mae = min(bar.low / entry - 1.0 for bar in window)
    exit_reason = "horizon_close"

    stop = _float(pred.get("stop_loss_pct"))
    take = _float(pred.get("take_profit_pct"))
    if action in BULLISH:
        for bar in window:
            if math.isfinite(stop) and bar.low <= entry * (1.0 - stop):
                raw_return = -stop
                net_return = raw_return - round_trip_cost
                exit_bar = bar
                exit_reason = "stop_loss"
                break
            if math.isfinite(take) and bar.high >= entry * (1.0 + take):
                raw_return = take
                net_return = raw_return - round_trip_cost
                exit_bar = bar
                exit_reason = "take_profit"
                break

    if action in BULLISH:
        correct = raw_return > neutral_threshold
    elif action in BEARISH:
        correct = raw_return < -neutral_threshold
    elif action in NEUTRAL:
        correct = abs(raw_return) <= neutral_threshold
    else:
        correct = False

    case_id = pred.get("case_id") or f"{code}_{decision_date}_{horizon}d"
    tags = pred.get("tags", "")
    if isinstance(tags, list):
        tags = ",".join(str(tag) for tag in tags)

    return {
        "status": "evaluated",
        "case_id": case_id,
        "code": code,
        "date": decision_date,
        "action": action,
        "horizon_days": horizon,
        "confidence": _float(pred.get("confidence"), math.nan),
        "entry_date": entry_bar.date,
        "entry_price": round(entry, 4),
        "exit_date": exit_bar.date,
        "exit_price": round(exit_bar.close, 4),
        "exit_reason": exit_reason,
        "raw_return": raw_return,
        "net_return": net_return,
        "mfe": mfe,
        "mae": mae,
        "correct": correct,
        "expert": pred.get("expert", ""),
        "expert_version": pred.get("expert_version", ""),
        "market_regime": pred.get("market_regime", ""),
        "tags": tags,
        "reason": pred.get("reason", ""),
    }


def safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row.get("status") == "evaluated"]
    trades = [row for row in evaluated if row.get("action") in BULLISH]
    trade_returns = [float(row["net_return"]) for row in trades]
    wins = [ret for ret in trade_returns if ret > 0]
    losses = [ret for ret in trade_returns if ret <= 0]
    correct = [row for row in evaluated if row.get("correct")]

    summary: dict[str, Any] = {
        "total_predictions": len(rows),
        "evaluated": len(evaluated),
        "skipped": len(rows) - len(evaluated),
        "directional_accuracy": len(correct) / len(evaluated) if evaluated else None,
        "trade_count": len(trades),
        "trade_win_rate": len(wins) / len(trades) if trades else None,
        "average_trade_return": safe_mean(trade_returns),
        "median_trade_return": median(trade_returns) if trade_returns else None,
        "payoff_ratio": (mean(wins) / abs(mean(losses))) if wins and losses else None,
        "max_drawdown": max_drawdown(trade_returns) if trade_returns else None,
    }

    by_action: dict[str, dict[str, Any]] = {}
    for action in sorted({str(row.get("action")) for row in evaluated}):
        group = [row for row in evaluated if row.get("action") == action]
        returns = [float(row["net_return"]) for row in group if row.get("action") in BULLISH]
        by_action[action] = {
            "count": len(group),
            "accuracy": sum(1 for row in group if row.get("correct")) / len(group),
            "average_return": safe_mean(returns),
        }
    summary["by_action"] = by_action

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluated:
        conf = row.get("confidence")
        if not isinstance(conf, float) or not math.isfinite(conf):
            bucket = "missing"
        elif conf < 0.4:
            bucket = "0.0-0.4"
        elif conf < 0.6:
            bucket = "0.4-0.6"
        elif conf < 0.8:
            bucket = "0.6-0.8"
        else:
            bucket = "0.8-1.0"
        buckets[bucket].append(row)
    summary["confidence_buckets"] = {
        key: {
            "count": len(group),
            "accuracy": sum(1 for row in group if row.get("correct")) / len(group),
            "average_trade_return": safe_mean(
                [float(row["net_return"]) for row in group if row.get("action") in BULLISH]
            ),
        }
        for key, group in sorted(buckets.items())
    }

    summary["high_confidence_misses"] = [
        row for row in evaluated
        if isinstance(row.get("confidence"), float)
        and math.isfinite(row["confidence"])
        and row["confidence"] >= 0.7
        and not row.get("correct")
    ][:20]
    return summary


def cmd_sample(args: argparse.Namespace) -> None:
    prices = load_prices(Path(args.prices_dir))
    start = args.start or "0000-00-00"
    end = args.end or "9999-99-99"
    cases: list[dict[str, Any]] = []
    for code, bars in sorted(prices.items()):
        eligible = [bar for bar in bars if start <= bar.date <= end]
        if len(eligible) <= args.horizon_days + args.lookback_days:
            continue
        by_month: dict[str, list[Bar]] = defaultdict(list)
        for bar in eligible[args.lookback_days : -args.horizon_days]:
            by_month[bar.date[:7]].append(bar)
        for _month, month_bars in sorted(by_month.items()):
            step = max(1, len(month_bars) // args.samples_per_month)
            for bar in month_bars[::step][: args.samples_per_month]:
                cases.append({
                    "case_id": f"{code}_{bar.date}_{args.horizon_days}d",
                    "date": bar.date,
                    "code": code,
                    "horizon_days": args.horizon_days,
                    "lookback_days": args.lookback_days,
                    "prompt_cutoff": f"Only use information available through {bar.date} close.",
                })
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"wrote {len(cases)} cases to {out}")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return rows


def write_details(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def format_summary(summary: dict[str, Any]) -> dict[str, Any]:
    percent_keys = {
        "directional_accuracy",
        "trade_win_rate",
        "average_trade_return",
        "median_trade_return",
        "max_drawdown",
    }
    out: dict[str, Any] = {}
    for key, value in summary.items():
        if key in {"high_confidence_misses", "confidence_buckets", "by_action"}:
            continue
        out[key] = pct(value) if key in percent_keys else value
    return out


def cmd_evaluate(args: argparse.Namespace) -> None:
    prices = load_prices(Path(args.prices_dir))
    predictions = read_jsonl(Path(args.predictions))
    rows = [
        evaluate_prediction(
            pred,
            prices,
            default_horizon=args.horizon_days,
            cost_bps=args.cost_bps,
            slippage_bps=args.slippage_bps,
            neutral_threshold=args.neutral_threshold,
        )
        for pred in predictions
    ]
    summary = summarize(rows)

    if args.details_out:
        write_details(Path(args.details_out), rows)
    if args.summary_out:
        out = Path(args.summary_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(format_summary(summary), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sample = sub.add_parser("sample", help="Generate blind-test cases from OHLCV CSV files.")
    sample.add_argument("--prices-dir", required=True)
    sample.add_argument("--start")
    sample.add_argument("--end")
    sample.add_argument("--samples-per-month", type=int, default=2)
    sample.add_argument("--horizon-days", type=int, default=5)
    sample.add_argument("--lookback-days", type=int, default=120)
    sample.add_argument("--output", required=True)
    sample.set_defaults(func=cmd_sample)

    evaluate = sub.add_parser("evaluate", help="Evaluate expert prediction JSONL.")
    evaluate.add_argument("--prices-dir", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--horizon-days", type=int, default=5)
    evaluate.add_argument("--cost-bps", type=float, default=15.0)
    evaluate.add_argument("--slippage-bps", type=float, default=5.0)
    evaluate.add_argument("--neutral-threshold", type=float, default=0.01)
    evaluate.add_argument("--details-out")
    evaluate.add_argument("--summary-out")
    evaluate.set_defaults(func=cmd_evaluate)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
