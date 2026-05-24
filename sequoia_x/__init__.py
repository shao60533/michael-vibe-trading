"""Sequoia-X A-share strategy scanner — production wrapper.

The actual scanning logic lives in
`skills/sequoia-x-a-share-selector/scripts/sequoia_x_weekly_scan.py`,
which is also distributed as a swarm-loadable skill (so agents can call the
CLI form per SKILL.md). This package wraps the same reusable functions for
direct in-process use by:
  - MCP tool (`run_sequoia_x_scan`)
  - Feishu webhook handler (`_feishu_handle_sequoia_scan`)
"""

from .service import run_weekly_scan, SequoiaScanError

__all__ = ["run_weekly_scan", "SequoiaScanError"]
