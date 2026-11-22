"""Shared CLI configuration and validation after argument parsing."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
from typing import TYPE_CHECKING

from .errors import ConfigError, VfyError
from .native import resolve_cli_config_record, validate_vfy_options_json

if TYPE_CHECKING:
    from .config_types import Config, ReportConfig


def apply_config(
    args: argparse.Namespace,
) -> tuple[argparse.Namespace, ReportConfig, Config]:
    """Apply the Rust resolver's complete operation to pytest/CLI arguments."""
    _validate_command_settings(args)
    try:
        resolved = resolve_cli_config_record(
            json.dumps(vars(args)),
            os.getenv("COVERAGE_FILE"),
        )
    except ValueError as error:
        raise ConfigError(str(error)) from error
    vars(args).update(resolved["arguments"])
    config = resolved["config"]
    return args, config.get("report", {}), config


def _validate_command_settings(args: argparse.Namespace) -> None:

    fields = {
        "test": ("workers",),
        "mutate": ("workers", "timeout"),
        "check": ("format", "fail_under_decisions", "fail_under_conditions", "fail_under_mcdc"),
    }
    if args.command not in fields:
        return
    values = {
        name: getattr(args, name)
        for name in fields[args.command]
        if getattr(args, name) is not None
    }
    if "workers" in values:
        values["workers"] = str(values["workers"])
    # JSON has no non-finite numbers; let Rust reject their string form with
    # the same option-specific diagnostic used for configuration values.
    for name, value in values.items():
        if isinstance(value, float) and not math.isfinite(value):
            values[name] = str(value)
    validate_vfy_options_json(args.command, json.dumps(values))


def _validate_ci_options(args: argparse.Namespace) -> None:
    if args.command == "test":
        ci_options = any(
            (
                args.baseline,
                args.export_baseline,
                args.plan_only,
                args.plan_output,
                args.audit_selection,
            )
        )
        if ci_options and args.watch:
            raise VfyError("CI selection options cannot be combined with --watch")
        if args.audit_selection and args.plan_only:
            raise VfyError("--audit-selection cannot be combined with --plan-only")
        if args.export_baseline and not args.fresh:
            raise VfyError("--export-baseline requires --all")
        if args.export_baseline and (args.baseline or args.plan_only):
            raise VfyError("--export-baseline cannot be combined with --baseline or --plan-only")
        if args.baseline and (args.fresh or args.no_replay):
            raise VfyError("--baseline cannot be combined with --all or --no-replay")
        if ci_options and args.mode != "safe":
            raise VfyError("CI selection options support only the safe selection policy")


def prepare_command(args: argparse.Namespace) -> tuple[argparse.Namespace, ReportConfig, Config]:
    watch_seed = (
        {
            name: copy.deepcopy(getattr(args, name))
            for name in (
                "data_file",
                "history",
                "source",
                "workers",
                "native_scheduling",
                "mode",
                "fresh",
            )
            if hasattr(args, name)
        }
        if getattr(args, "watch", False)
        else {}
    )
    diagnostics_config_error: str | None = None
    try:
        args, report_config, config = apply_config(args)
    except VfyError as error:
        if args.command == "diagnostics":
            diagnostics_config_error = str(error)
            recovery = "diagnostics recovery"
        elif not getattr(args, "watch", False):
            raise
        else:
            # Resolve defaults through the ordinary path so the watcher can start
            # and observe a correcting save. Each run retries the original config.
            recovery = "watch recovery"
        fallback = copy.copy(args)
        fallback.isolated = True
        fallback, report_config, config = apply_config(fallback)
        fallback.isolated = args.isolated
        fallback.config_discovery = recovery
        args = fallback

    if diagnostics_config_error is not None:
        args._diagnostics_config_error = diagnostics_config_error

    _validate_ci_options(args)
    if getattr(args, "watch", False):
        if args.command != "test":
            raise VfyError("--watch is only supported with vfy test")
        if args.mode in {"balanced", "aggressive"} or config.get("vfy", {}).get("test", {}).get(
            "selection"
        ) in {"balanced", "aggressive"}:
            raise VfyError("--watch supports only the safe selection policy")
        if any(
            "::" in target
            or (
                ":" in target
                and target.rsplit(":", 1)[1].isdigit()
                and int(target.rsplit(":", 1)[1]) > 0
            )
            for target in (*args.target, *args.pytest_args)
        ):
            raise VfyError("--watch cannot be combined with a test location or node id")
        args._watch_seed = watch_seed
    return args, report_config, config
