from __future__ import annotations

import argparse
import json

from .service import run_industry_factor_research
from .validation import run_february_model_validation, run_period_model_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run A-share industry factor research.")
    parser.add_argument("--lookback-days", type=int, default=260)
    parser.add_argument("--test-days", type=int, default=22)
    parser.add_argument("--horizon-days", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--board-limit", type=int, default=80)
    parser.add_argument("--report-days", type=int, default=7)
    parser.add_argument("--panel-csv", help="Optional local industry OHLCV CSV input.")
    parser.add_argument("--feb-validation", action="store_true", help="Run 2026-02 train / 2026-03..05 validation.")
    parser.add_argument("--period-validation", action="store_true", help="Run a custom train/validation period check.")
    parser.add_argument("--train-start", default="2025-10-01")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--validate-start", default="2026-01-01")
    parser.add_argument("--validate-end", default="2026-05-24")
    parser.add_argument("--warmup-start", default="2025-05-01")
    parser.add_argument("--datalen", type=int, default=700)
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of markdown.")
    args = parser.parse_args()

    if args.feb_validation:
        result = run_february_model_validation(top_k=args.top_k, horizon_days=args.horizon_days)
    elif args.period_validation:
        result = run_period_model_validation(
            train_start=args.train_start,
            train_end=args.train_end,
            validate_start=args.validate_start,
            validate_end=args.validate_end,
            warmup_start=args.warmup_start,
            horizon_days=args.horizon_days,
            top_k=args.top_k,
            datalen=args.datalen,
        )
    else:
        result = run_industry_factor_research(
            lookback_days=args.lookback_days,
            test_days=args.test_days,
            horizon_days=args.horizon_days,
            top_k=args.top_k,
            board_limit=args.board_limit,
            report_days=args.report_days,
            panel_csv=args.panel_csv,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    else:
        print(result["report_markdown"])


if __name__ == "__main__":
    main()
