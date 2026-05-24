"""Model training and backtest utilities for industry factor research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ModelRun:
    model_name: str
    feature_columns: list[str]
    predictions: Any
    latest_predictions: Any
    backtest: dict[str, Any]
    feature_importance: list[dict[str, Any]]


def _max_drawdown(returns) -> float:
    equity = (1 + returns.fillna(0)).cumprod()
    drawdown = equity / equity.cummax() - 1
    return float(drawdown.min()) if len(drawdown) else 0.0


def _zscore(series):
    std = series.std(ddof=0)
    if not std:
        return series * 0.0
    return (series - series.mean()) / std


def _fit_predict_model(train_x, train_y, test_x, latest_x):
    """Fit LightGBM when available, otherwise use sklearn gradient boosting."""
    try:
        from lightgbm import LGBMRegressor

        model = LGBMRegressor(
            objective="regression",
            n_estimators=260,
            learning_rate=0.035,
            num_leaves=15,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_alpha=0.05,
            reg_lambda=0.10,
            random_state=42,
            verbose=-1,
        )
        model.fit(train_x, train_y)
        importance = getattr(model, "feature_importances_", None)
        model_name = "lightgbm.LGBMRegressor"
    except Exception:
        try:
            from sklearn.ensemble import HistGradientBoostingRegressor

            model = HistGradientBoostingRegressor(
                max_iter=220,
                learning_rate=0.045,
                max_leaf_nodes=15,
                l2_regularization=0.10,
                random_state=42,
            )
            model.fit(train_x, train_y)
            importance = None
            model_name = "sklearn.HistGradientBoostingRegressor"
        except Exception:
            import numpy as np

            x = train_x.to_numpy(dtype=float)
            y = train_y.to_numpy(dtype=float)
            x_mean = np.nanmean(x, axis=0)
            x_std = np.nanstd(x, axis=0)
            x_std[x_std == 0] = 1.0
            x = (np.nan_to_num(x, nan=x_mean) - x_mean) / x_std
            design = np.column_stack([np.ones(len(x)), x])
            alpha = 0.5
            penalty = np.eye(design.shape[1]) * alpha
            penalty[0, 0] = 0.0
            beta = np.linalg.pinv(design.T @ design + penalty) @ design.T @ y

            def predict(frame):
                px = frame.to_numpy(dtype=float)
                px = (np.nan_to_num(px, nan=x_mean) - x_mean) / x_std
                pdesign = np.column_stack([np.ones(len(px)), px])
                return pdesign @ beta

            return (
                "numpy_ridge_fallback",
                predict(test_x),
                predict(latest_x),
                np.abs(beta[1:]),
            )

    return (
        model_name,
        model.predict(test_x),
        model.predict(latest_x),
        importance,
    )


def _build_backtest(predictions, top_k: int) -> dict[str, Any]:
    import pandas as pd

    if predictions.empty:
        return {
            "test_days": 0,
            "top_k": top_k,
            "strategy_cumulative_return": 0.0,
            "benchmark_cumulative_return": 0.0,
            "excess_cumulative_return": 0.0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "daily_returns": [],
        }

    rows = []
    for dt, frame in predictions.groupby("date"):
        ranked = frame.sort_values("prediction", ascending=False)
        selected = ranked.head(top_k)
        strategy_ret = selected["future_return"].mean()
        benchmark_ret = frame["future_return"].mean()
        rows.append(
            {
                "date": str(dt.date() if hasattr(dt, "date") else dt),
                "strategy_return": float(strategy_ret),
                "benchmark_return": float(benchmark_ret),
                "excess_return": float(strategy_ret - benchmark_ret),
                "selected": [
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "prediction": round(float(r["prediction"]), 6),
                        "realized_return": round(float(r["future_return"]), 6),
                    }
                    for _, r in selected.iterrows()
                ],
            }
        )
    daily = pd.DataFrame(rows)
    strategy = daily["strategy_return"]
    benchmark = daily["benchmark_return"]
    return {
        "test_days": int(len(daily)),
        "top_k": int(top_k),
        "strategy_cumulative_return": round(float((1 + strategy).prod() - 1), 6),
        "benchmark_cumulative_return": round(float((1 + benchmark).prod() - 1), 6),
        "excess_cumulative_return": round(float((1 + strategy).prod() / (1 + benchmark).prod() - 1), 6),
        "win_rate": round(float((strategy > benchmark).mean()), 4),
        "max_drawdown": round(_max_drawdown(strategy), 6),
        "daily_returns": rows[-10:],
    }


def train_predict_backtest(
    features,
    feature_columns: list[str],
    test_days: int = 22,
    top_k: int = 5,
) -> ModelRun:
    """Train a time-split model and backtest top-k industry selection."""
    import pandas as pd

    usable = features.dropna(subset=feature_columns + ["future_return"]).copy()
    usable = usable.sort_values(["date", "code"])
    if usable.empty:
        raise RuntimeError("No usable rows with both factors and future_return.")

    unique_dates = sorted(pd.to_datetime(usable["date"]).unique())
    split_pos = max(1, len(unique_dates) - max(test_days, 5))
    split_date = unique_dates[split_pos]
    train = usable[usable["date"] < split_date]
    test = usable[usable["date"] >= split_date]
    latest_date = features["date"].max()
    latest = features[features["date"] == latest_date].dropna(subset=feature_columns).copy()
    if len(train) < 80 or len(test) < 5 or latest.empty:
        # Deterministic fallback for small samples.
        test = test.copy()
        test["prediction"] = test["factor_score"]
        latest = latest.copy()
        latest["prediction"] = latest["factor_score"]
        bt = _build_backtest(test, top_k)
        return ModelRun(
            model_name="factor_score_fallback",
            feature_columns=feature_columns,
            predictions=test,
            latest_predictions=latest,
            backtest=bt,
            feature_importance=[],
        )

    train_x, train_y = train[feature_columns], train["future_return"]
    test_x = test[feature_columns]
    latest_x = latest[feature_columns]
    model_name, test_pred, latest_pred, importance = _fit_predict_model(
        train_x, train_y, test_x, latest_x
    )
    test = test.copy()
    latest = latest.copy()
    test["prediction"] = test_pred
    latest["prediction"] = latest_pred
    test["prediction_z"] = test.groupby("date")["prediction"].transform(_zscore)
    latest["prediction_z"] = _zscore(latest["prediction"])

    if importance is None:
        feature_importance = []
    else:
        feature_importance = sorted(
            [
                {"feature": col, "importance": float(val)}
                for col, val in zip(feature_columns, importance)
            ],
            key=lambda x: x["importance"],
            reverse=True,
        )[:12]

    return ModelRun(
        model_name=model_name,
        feature_columns=feature_columns,
        predictions=test,
        latest_predictions=latest,
        backtest=_build_backtest(test, top_k),
        feature_importance=feature_importance,
    )
