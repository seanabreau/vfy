"""Implementations of the run, check, erase, combine, debug, and report commands."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .cli_output import _message, _report_color, _writes_stdout
from .errors import NoDataError, VfyError
from .native import pytest_coverage_threshold_failed
from .store_loader import _load_store

if TYPE_CHECKING:
    from .config_types import Config, ReportConfig
    from .native import ReportRow


def _run_python(args: argparse.Namespace) -> int:
    from .api import Coverage  # noqa: PLC0415 - only the run command collects Python code
    from .process_startup import (  # noqa: PLC0415 - only collection needs subprocess bootstrap
        subprocess_environment,
    )

    coverage = Coverage(
        data_file=args.data_file,
        branch=args.branch,
        decisions=args.decisions,
        # Leave the suffix unset so [run] concurrency can select a parallel shard.
        data_suffix=True if args.parallel_mode else None,
        source=args.source,
        include=args.include,
        omit=args.omit,
        context=args.context,
        config_file=args.rcfile or (not args.isolated),
    )
    if args.append:
        coverage.load()
    old_argv = sys.argv[:]
    old_path = sys.path[:]
    extras = {}
    if args.module and args.target == "pytest":
        extras = {
            "VFY_CLI_ACTIVE": "1",
            "VFY_DATA_FILE": coverage.data.base_filename(),
            "VFY_BRANCH": "1" if args.branch else "0",
            "VFY_DECISIONS": "1" if args.decisions else "0",
        }
    exit_code = 0
    coverage.start()
    try:
        with subprocess_environment(coverage, extras):
            try:
                sys.argv = [args.target, *args.args]
                if args.module:
                    runpy.run_module(args.target, run_name="__main__", alter_sys=True)
                else:
                    target = Path(args.target).resolve()
                    if not target.is_file():
                        raise VfyError(f"program file does not exist: {target}")
                    sys.argv[0] = str(target)
                    sys.path[0] = str(target.parent)
                    runpy.run_path(str(target), run_name="__main__")
            except SystemExit as exc:
                if exc.code is None:
                    exit_code = 0
                elif isinstance(exc.code, int):
                    exit_code = exc.code
                else:
                    print(exc.code, file=sys.stderr)
                    exit_code = 1
            except BrokenPipeError as exc:
                # The measured program may have written to a pipe other than stdout.
                print(f"vfy: error: {exc}", file=sys.stderr)
                exit_code = 1
    finally:
        coverage.stop()
        coverage.save()
        sys.argv = old_argv
        sys.path[:] = old_path
    return exit_code


def _check_threshold(rows: list[ReportRow], threshold: float | None, precision: int) -> int:
    from .reports import totals  # noqa: PLC0415 - thresholds consume report rows

    if threshold is None:
        return 0
    percent = totals(rows).percent
    # Coverage.py compares the total rounded to the report precision, and 100 exactly.
    if pytest_coverage_threshold_failed(percent, threshold, precision):
        print(
            f"vfy: required coverage of {threshold:g}% was not reached; total: {percent:.2f}%",
            file=sys.stderr,
        )
        return 2
    return 0


def _check_semantic_threshold(value: float | None, actual: float, name: str) -> bool:
    if value is None:
        return True
    if actual + 1e-9 < value:
        print(
            f"vfy: required {name} coverage of {value:g}% not reached (actual {actual:.2f}%)",
            file=sys.stderr,
        )
        return False
    return True


def _semantic_check(args: argparse.Namespace, config: Config) -> int:
    from .semantic_reports import (  # noqa: PLC0415 - only semantic checks need these renderers
        build_semantic_report,
        render_semantic_text,
        semantic_json,
    )

    store = _load_store(args.data_file, [list(paths) for paths in config.get("paths", {}).values()])
    report = build_semantic_report(
        store,
        include=args.include,
        omit=args.omit,
        contexts=args.contexts,
        source_roots=args.source,
        exclude_decisions=args.exclude_decisions,
    )
    content = (
        semantic_json(report, pretty=args.pretty_print)
        if args.format == "json"
        else render_semantic_text(
            report,
            history_path=args.history,
        )
    )
    if not args.output or args.output == "-":
        sys.stdout.write(content)
    else:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(content, encoding="utf-8")
    decision_threshold = args.fail_under_decisions
    mcdc_threshold = args.fail_under_mcdc
    if args.strict and mcdc_threshold is None:
        mcdc_threshold = 100.0
    if args.strict and decision_threshold is None:
        decision_threshold = 100.0
    if args.strict and args.fail_under_conditions is None:
        args.fail_under_conditions = 100.0
    checks = [
        (decision_threshold, report.decision_percent, "decision"),
        (args.fail_under_conditions, report.condition_percent, "condition"),
        (mcdc_threshold, report.mcdc_percent, "MC/DC"),
    ]
    # A strict check is reviewed in decision -> MC/DC -> condition order so a
    # missing independent-effect witness is the first actionable diagnostic;
    # ordinary ``check`` retains the natural report order.
    if args.strict:
        checks[1], checks[2] = checks[2], checks[1]
    ok = True
    for threshold, actual, name in checks:
        ok = _check_semantic_threshold(threshold, actual, name) and ok
    return 0 if ok else 2


def _data_command(args: argparse.Namespace, config: Config) -> int:
    if args.command == "erase":
        from .data import (  # noqa: PLC0415 - shard discovery is only needed by erase
            discover_parallel_files,
        )

        paths = [args.data_file]
        if args.parallel:
            paths.extend(discover_parallel_files(args.data_file))
        for path in paths:
            Path(path).unlink(missing_ok=True)
        return 0

    if args.command == "combine":
        from .data import CoverageStore  # noqa: PLC0415 - storage is only needed by combine

        store, count = CoverageStore.combine_transaction(
            args.paths,
            output=args.data_file,
            keep=args.keep,
            strict=args.strict,
            aliases=[list(paths) for paths in config.get("paths", {}).values()],
        )
        if store is None:
            raise NoDataError("No data to combine")
        _message(args, f"Combined {count} data file(s) into {args.data_file}")
        return 0

    if args.command == "debug":
        store = _load_store(
            args.data_file, [list(paths) for paths in config.get("paths", {}).values()]
        )
        print(f"path: {Path(args.data_file).resolve()}")
        print("schema: 7")
        print(f"has_arcs: {store.has_arcs!r}")
        print(f"files: {len(store.files)}")
        for path in store.measured_files():
            print(f"  {path}: {len(store.lines(path) or set())} lines")
        return 0

    raise VfyError(f"unknown data command {args.command!r}")


def _report_command(args: argparse.Namespace, report_config: ReportConfig, config: Config) -> int:
    from .analysis import (  # noqa: PLC0415 - report source analysis is command-specific
        DEFAULT_EXCLUDES,
        DEFAULT_PARTIALS,
    )
    from .reports import (  # noqa: PLC0415 - only report commands load every renderer
        build_rows,
        render_json,
        render_lcov,
        render_markdown,
        render_terminal,
        render_total,
        render_xml,
        write_annotation_report,
        write_html_report,
        write_output,
    )

    aliases = [list(paths) for paths in config.get("paths", {}).values()]
    store = _load_store(args.data_file, aliases)
    if aliases:
        store = store.remap_paths_for_reporting(aliases, relative_files=args.relative_files)
    rows = build_rows(
        store,
        include=args.include,
        omit=args.omit,
        excludes=[
            *report_config.get("exclude_lines", DEFAULT_EXCLUDES),
            *report_config.get("exclude_also", []),
        ],
        partials=[
            *report_config.get("partial_branches", DEFAULT_PARTIALS),
            *report_config.get("partial_also", []),
        ],
        relative_files=args.relative_files,
        contexts=args.contexts,
        ignore_errors=args.ignore_errors,
        line_contexts=args.command == "json" and bool(args.show_contexts),
    )
    if not rows:
        raise NoDataError("No data to report.")
    if args.command == "report":
        if args.format == "markdown":
            content = render_markdown(
                rows,
                args.precision,
                args.show_missing,
                skip_covered=args.skip_covered,
                skip_empty=args.skip_empty,
                sort=args.sort,
            )
        elif args.format == "total":
            content = render_total(
                rows,
                args.precision,
                skip_covered=args.skip_covered,
                skip_empty=args.skip_empty,
                sort=args.sort,
            )
        else:
            content = render_terminal(
                rows,
                precision=args.precision,
                show_missing=args.show_missing,
                color=_report_color(args.color),
                skip_covered=args.skip_covered,
                skip_empty=args.skip_empty,
                sort=args.sort,
            )
        sys.stdout.write(content)
    elif args.command == "xml":
        write_output(
            render_xml(
                rows,
                args.xml_package_depth,
                config.get("run", {}).get("source", []),
                args.relative_files,
            ),
            args.output,
        )
        if not _writes_stdout(args.output):
            _message(args, f"Wrote XML report to {args.output}")
    elif args.command == "json":
        write_output(
            render_json(
                rows,
                pretty=args.pretty_print,
                show_contexts=args.show_contexts,
                precision=args.precision,
            ),
            args.output,
        )
        if not _writes_stdout(args.output):
            _message(args, f"Wrote JSON report to {args.output}")
    elif args.command == "lcov":
        write_output(render_lcov(rows), args.output)
        if not _writes_stdout(args.output):
            _message(args, f"Wrote LCOV report to {args.output}")
    elif args.command == "html":
        html = config.get("html", {})
        write_html_report(
            rows,
            args.directory,
            title=args.title,
            skip_covered=(
                args.skip_covered
                if args.skip_covered is not None
                else html.get("skip_covered", report_config.get("skip_covered", False))
            ),
            skip_empty=(
                args.skip_empty
                if args.skip_empty is not None
                else html.get("skip_empty", report_config.get("skip_empty", False))
            ),
        )
        _message(args, f"Wrote HTML report to {Path(args.directory) / 'index.html'}")
    else:
        count = write_annotation_report(rows, args.directory)
        _message(args, f"Annotated {count} file(s)")
        return 0
    return _check_threshold(rows, args.fail_under, args.precision)
