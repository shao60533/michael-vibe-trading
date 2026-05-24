"""Research-report industry heat utilities."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

RATING_SCORE = {
    "强烈推荐": 1.0,
    "买入": 0.9,
    "推荐": 0.8,
    "增持": 0.7,
    "优于大市": 0.6,
    "跑赢行业": 0.6,
    "中性": 0.0,
    "持有": 0.0,
    "谨慎推荐": 0.3,
    "减持": -0.6,
    "卖出": -1.0,
}


def _industry_name(row: dict[str, Any]) -> str:
    for key in ("indvInduName", "industryName", "industry", "emIndustryName"):
        val = row.get(key)
        if val:
            return str(val).strip()
    return "未分类"


def _rating_score(row: dict[str, Any]) -> float:
    rating = str(row.get("emRatingName") or row.get("rating") or "").strip()
    for key, score in RATING_SCORE.items():
        if key in rating:
            return score
    return 0.0


def summarize_report_industries(reports: list[dict[str, Any]], top_n: int = 15) -> dict[str, Any]:
    by_industry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in reports:
        by_industry[_industry_name(row)].append(row)

    summary = []
    for industry, rows in by_industry.items():
        ratings = [_rating_score(r) for r in rows]
        orgs = Counter(str(r.get("orgSName") or "未知") for r in rows)
        tickers = Counter(str(r.get("stockName") or r.get("code") or "") for r in rows if r.get("stockName") or r.get("code"))
        heat = math.log1p(len(rows)) + (sum(ratings) / len(ratings) if ratings else 0.0)
        titles = []
        for r in rows[:3]:
            title = str(r.get("title") or "").strip()
            if title:
                titles.append(title[:80])
        summary.append(
            {
                "industry": industry,
                "report_count": len(rows),
                "rating_score": round(sum(ratings) / len(ratings), 4) if ratings else 0.0,
                "heat_score": round(heat, 4),
                "top_orgs": [name for name, _ in orgs.most_common(3)],
                "mentioned_stocks": [name for name, _ in tickers.most_common(5)],
                "sample_titles": titles,
            }
        )
    summary.sort(key=lambda x: (x["heat_score"], x["report_count"]), reverse=True)
    return {
        "total_reports": len(reports),
        "industry_count": len(summary),
        "top_industries": summary[:top_n],
    }


def attach_report_scores(latest_frame, report_summary: dict[str, Any]):
    """Attach report heat to latest industry rows by fuzzy industry-name matching."""
    import numpy as np

    df = latest_frame.copy()
    hot = report_summary.get("top_industries") or []
    score_map = {item["industry"]: float(item.get("heat_score", 0.0)) for item in hot}
    count_map = {item["industry"]: int(item.get("report_count", 0)) for item in hot}

    def match_score(name: str) -> tuple[float, int, str]:
        name = str(name)
        best = (0.0, 0, "")
        for industry, score in score_map.items():
            if not industry or industry == "未分类":
                continue
            if industry in name or name in industry:
                cnt = count_map.get(industry, 0)
                if score > best[0]:
                    best = (score, cnt, industry)
        return best

    matches = df["name"].map(match_score)
    df["report_heat_score"] = [x[0] for x in matches]
    df["report_count"] = [x[1] for x in matches]
    df["report_industry_match"] = [x[2] for x in matches]
    std = df["report_heat_score"].std(ddof=0)
    if std:
        df["report_heat_z"] = (df["report_heat_score"] - df["report_heat_score"].mean()) / std
    else:
        df["report_heat_z"] = np.zeros(len(df))
    return df
