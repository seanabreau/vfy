"""Parse `vfy test` arguments with the Rust parser and report errors."""

from __future__ import annotations

import argparse
import re
import sys
from typing import TYPE_CHECKING

from vfy._cli_arguments import escape_arguments, restore_record
from vfy.cli_parser import _add_test_options, _build_test_parser, _is_test_command
from vfy.command_config import prepare_command
from vfy.errors import PytestConfigurationError, VfyError
from vfy.native import parse_test_cli

from .command import run_test

if TYPE_CHECKING:
    from collections.abc import Sequence


def _render_arguments(argv: Sequence[str]) -> list[str]:
    """Preserve the parser's bare global color handling when rendering errors."""
    raw = list(argv)
    for index in range(len(raw) - 1):
        if (
            raw[index : index + 2] == ["--color", "test"]
            and not _is_test_command(raw[: index + 1])
            and _is_test_command(raw[: index + 2])
        ):
            raw[index] = "--color=always"
            break
    return raw


def _native_arguments(argv: Sequence[str]) -> dict[str, object]:
    try:
        return parse_test_cli(list(argv))
    except UnicodeEncodeError:
        escaped, marker = escape_arguments(argv)
        return restore_record(parse_test_cli(escaped), marker)


def parse(argv: Sequence[str]) -> argparse.Namespace:
    if "--" in argv:
        raise VfyError("vfy test accepts pytest options directly; remove the `--` separator")
    if "--help" in argv or "-h" in argv:
        _build_test_parser().parse_known_args(_render_arguments(argv))
    try:
        values = _native_arguments(argv)
    except ValueError as error:
        parser = _build_test_parser()
        parsed, _ = parser.parse_known_args(_render_arguments(argv))
        if parsed.pytest_args:
            tail_parser = argparse.ArgumentParser(
                prog="vfy test", add_help=False, allow_abbrev=False
            )
            _add_test_options(tail_parser)
            tail_parser.parse_known_args(parsed.pytest_args)
        parser.error(str(error))
    return argparse.Namespace(**values)


def main(argv: Sequence[str]) -> int:
    try:
        args, _, config = prepare_command(parse(argv))
        return run_test(args, config)
    except PytestConfigurationError as exc:
        print(f"ERROR: {exc}\n", file=sys.stderr)  # noqa: T201 - CLI diagnostic
        return 4
    except (VfyError, OSError, re.error, ValueError) as exc:
        print(f"vfy: error: {exc}", file=sys.stderr)  # noqa: T201 - CLI diagnostic
        return 1
