"""Dispatch `vfy test` to CI, watch, or a single run."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from .ci_run import _run_ci_test_once
from .selected_run import _run_test_selection_once
from .watch import _run_test_selection_watch

if TYPE_CHECKING:
    from vfy.config_types import Config


def _run_test_selection(args: argparse.Namespace, config: Config) -> int:
    if getattr(args, "watch", False):
        return _run_test_selection_watch(args, config)
    if any(
        getattr(args, name, None)
        for name in ("baseline", "export_baseline", "plan_only", "plan_output", "audit_selection")
    ):
        return _run_ci_test_once(args, config)
    return _run_test_selection_once(args, config)
