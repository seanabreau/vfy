"""Command-line interface for vfy."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .cli_coverage import _data_command, _report_command, _run_python, _semantic_check
from .cli_diagnostics import _diagnostics_command
from .cli_explain import _config_command, _explain
from .cli_output import _message
from .cli_parser import _parse_args, build_parser
from .command_config import prepare_command
from .errors import PytestConfigurationError, VfyError
from .native import clean_cache
from .output import OutputPolicy

if TYPE_CHECKING:
    from .config_types import Config, ReportConfig

__all__ = ["build_parser", "main"]


def _cache_command(args: argparse.Namespace) -> int:
    cache_dir = Path.cwd() / ".vfy"
    if args.cache_command == "dir":
        print(cache_dir)
        return 0
    if clean_cache(cache_dir):
        _message(args, f"Removed vfy cache: {cache_dir}")
    else:
        _message(args, f"Cache directory does not exist: {cache_dir}")
    return 0


def _prepare_command(argv: list[str] | None) -> tuple[argparse.Namespace, ReportConfig, Config]:
    return prepare_command(_parse_args(argv))


def _main(argv: list[str] | None = None) -> int:
    args, report_config, config = _prepare_command(argv)
    if args.command == "test":
        from .run.command import run_test  # noqa: PLC0415 - test command only

        return run_test(args, config)
    output = OutputPolicy.from_args(args)
    output.diagnostic(
        f"Configuration: {args.config_source or 'defaults'} ({args.config_discovery})"
    )
    for name in ("mode", "workers", "history", "timeout", "data_file"):
        if hasattr(args, name):
            output.diagnostic(f"{name}: {getattr(args, name)}")
    if args.command == "run":
        return _run_python(args)
    if args.command == "explain":
        return _explain(args, config)
    if args.command == "mutate":
        from .mutate import run_mutation  # noqa: PLC0415 - mutation is an optional workflow

        if args.max_mutations < 0:
            raise VfyError(f"--max-mutations must be 0 or greater, not {args.max_mutations}")
        return run_mutation(args, config)
    if args.command == "check":
        return _semantic_check(args, config)

    if args.command == "config":
        return _config_command(args, config)
    if args.command == "diagnostics":
        return _diagnostics_command(args, config)
    if args.command == "cache":
        return _cache_command(args)

    if args.command in {"erase", "combine", "debug"}:
        return _data_command(args, config)
    return _report_command(args, report_config, config)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        os.environ["VFY_IN_PROCESS_PYTEST"] = "1"
    try:
        return _main(argv)
    except PytestConfigurationError as exc:
        print(f"ERROR: {exc}\n", file=sys.stderr)
        return 4
    except (VfyError, OSError, re.error, ValueError) as exc:
        print(f"vfy: error: {exc}", file=sys.stderr)
        return 1
