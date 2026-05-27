"""Hot event A-share research — auto-researcher style.

Workflow:
  1. LLM 路由:事件名 → entity / industry_hint / keywords
  2. 抓近期新闻并按 keyword 过滤
  3. LLM 主分析:事件 + 新闻上下文 → 结构化 markdown (事件概况 /
     催化方向 / 炒作路径 / 产业链 / 重点个股 / 预期差 / 风险)
  4. 调用方走标准 publish 管道 (卡片 + 飞书 docx + Notion)
"""

from .service import run_hot_event_analysis, HotEventError

__all__ = ["run_hot_event_analysis", "HotEventError"]
