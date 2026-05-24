"""A-share industry factor research toolkit."""

from .service import format_markdown_report, run_industry_factor_research
from .validation import run_february_model_validation

__all__ = [
    "format_markdown_report",
    "run_industry_factor_research",
    "run_february_model_validation",
]
