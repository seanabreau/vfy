"""Print configuration diagnostics and dispatch a parsed `vfy test` command."""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

from vfy.errors import PytestConfigurationError
from vfy.output import OutputPolicy

if TYPE_CHECKING:
    from vfy.config_types import Config


def run_test(args: argparse.Namespace, config: Config) -> int:
    output = OutputPolicy.from_args(args)
    output.diagnostic(
        f"Configuration: {args.config_source or 'defaults'} ({args.config_discovery})"
    )
    for name in ("mode", "workers", "history", "timeout", "data_file"):
        if hasattr(args, name):
            output.diagnostic(f"{name}: {getattr(args, name)}")
    if args.engine == "native":
        unsupported = (
            ("--baseline", args.baseline),
            ("--export-baseline", args.export_baseline),
            ("--audit-selection", args.audit_selection),
            ("--confirm-failures", args.confirm_failures),
        )
        for option, value in unsupported:
            if value:
                raise PytestConfigurationError(
                    f"independent execution does not support {option}; "
                    "use `vfy test --engine=pytest-native` "
                    "with vfy[pytest] installed for this option"
                )
    if (
        args.engine == "native"
        and not getattr(args, "watch", False)
        and not (args.plan_only or args.plan_output)
    ):
        if os.getenv("VFY_PROFILE_DIR"):
            from .controller import _run_test_selection  # noqa: PLC0415 - profiled route

            return _run_test_selection(args, config)
        from vfy.independent.controller import run_once  # noqa: PLC0415 - independent engine

        return run_once(args, config)
    from .controller import _run_test_selection  # noqa: PLC0415 - test execution only

    return _run_test_selection(args, config)
