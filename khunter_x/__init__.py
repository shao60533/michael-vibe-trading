"""KHunter A-share scanner + cross-sectional factor scoring + multi-voice debate.

Public API:
  - run_khunter_pipeline_sync()   同步,只 scan + factor,不写文件
  - run_khunter_pipeline_async()  完整 async pipeline,scan + factor + debate + 写 outputs/
  - KHunterScanError              硬失败异常
"""

from .scanner import run_weekly_scan, KHunterScanError
from .service import (
    run_khunter_pipeline_async,
    run_khunter_pipeline_sync,
)

__all__ = [
    "run_weekly_scan",
    "KHunterScanError",
    "run_khunter_pipeline_async",
    "run_khunter_pipeline_sync",
]
