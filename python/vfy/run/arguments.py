"""Prepare the pytest arguments passed to workers."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vfy._pytest.plugin_support import (
    installed_plugin_distribution,
)
from vfy.errors import VfyError
from vfy.independent.configuration import (
    PytestConfiguration as _PytestConfiguration,
    full_selection_message,
)
from vfy.native import (
    stage_watch_outputs,
)
from vfy.output import OutputPolicy

from .execution import (
    _version_query,
)

if TYPE_CHECKING:
    from vfy.config_types import Config


from .engine import (
    TestEngine,
    _configured_pytest_cov_override,
    _explicit_numprocesses,
    _native_xdist_plugin_args,
    _pytest_plugin_names,
    _report_native_plugin_omissions,
    _select_test_engine,
    _without_pytest_cov_loads,
    native_adapter_plugin_args,
)
from .plan import SelectionPlan, _selection_message
from .replay import _TestInvocation
from .reports import _controller_message, _pytest_option


def _selected_pytest_arguments(
    args: argparse.Namespace,
    invocation: _TestInvocation,
    plan: SelectionPlan,
    *,
    collection_scope: Literal["selected", "complete"] = "selected",
) -> list[str]:
    pytest_args = list(invocation.pytest_args)
    source_roots = invocation.source_roots
    location = invocation.location
    selected_engine = getattr(args, "_test_engine", None)
    if selected_engine is None:
        engine = _select_test_engine(args, pytest_args, invocation.configuration)
    else:
        engine = TestEngine(selected_engine)
    if collection_scope == "selected" and not _version_query(pytest_args):
        if engine == TestEngine.INDEPENDENT:
            OutputPolicy.from_args(args).diagnostic(full_selection_message())
        else:
            _controller_message(args, _selection_message(plan))
    if getattr(args, "verbose", 0):
        _controller_message(
            args,
            f"Backend: {engine.value} (vfy owns selection and evidence)",
        )
    args._watch_full_run = plan.fallback_full
    selected = list(plan.selected)
    if location and not selected:
        raise VfyError(
            f"no historical test reaches {location}; run a baseline with --cov-context=test"
        )
    pytest_args = invocation.controller.resolve_pytest_args(
        pytest_args,
        selected_count=None
        if plan.fallback_full or collection_scope == "complete"
        else len(selected),
        route=engine,
        configuration=invocation.configuration,
    )
    if not plan.fallback_full and collection_scope == "selected":
        # Final node IDs can be rewritten by collection hooks. Limit discovery
        # to the selected files, then let the plug-in match final identities.
        pytest_args.extend(dict.fromkeys(nodeid.split("::", 1)[0] for nodeid in selected))

    configured_arguments = invocation.configuration.effective_args
    has_cov = any(
        argument == "--cov" or argument.startswith("--cov=") for argument in configured_arguments
    )
    # Without measurement there are no contexts, and pytest-cov warns about a
    # --cov option that follows --no-cov.
    if invocation.configuration.coverage_enabled and not any(
        argument.startswith("--cov-context") for argument in configured_arguments
    ):
        pytest_args.append("--cov-context=test")
    if not has_cov and invocation.configuration.coverage_enabled:
        if source_roots:
            pytest_args.extend(f"--cov={root}" for root in source_roots)
        else:
            pytest_args.append("--cov")
    return _execution_plugin_arguments(args, invocation.configuration, pytest_args, engine)


def _execution_plugin_arguments(
    args: argparse.Namespace,
    configuration: _PytestConfiguration,
    pytest_args: list[str],
    engine: TestEngine,
) -> list[str]:
    if engine is TestEngine.INDEPENDENT:
        from vfy.native import NativeTestInvocation  # noqa: PLC0415 - shared invocation policy

        return [
            *NativeTestInvocation.independent_plugin_prefix(configuration.plugins, pytest_args),
            *pytest_args,
        ]
    native_pytest = engine is TestEngine.NATIVE
    # vfy's coverage plug-in owns --cov; drop explicit pytest-cov loads.
    pytest_args, _ = _without_pytest_cov_loads(pytest_args)
    pytest_args.extend(_configured_pytest_cov_override(configuration))
    plugin_names = _pytest_plugin_names(pytest_args)
    plugins: list[str] = []
    if "vfy.pytest_plugin" not in plugin_names:
        # Block competing coverage plug-ins so installed tools cannot silently
        # replace vfy's collector.
        plugins.extend(
            (
                "-p",
                "no:pytest_cov",
                "-p",
                "no:slipcover",
                "-p",
                "no:html",
                "-p",
                "no:html_fixtures",
                "-p",
                "no:metadata",
            )
        )
    plugins.extend(("-p", "vfy.pytest_plugin", "-p", "vfy.pytest_reporting"))
    if native_pytest:
        _report_native_plugin_omissions(args, configuration)
        # Block the stock cache provider before registering the owning engine.
        plugins.extend(("-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"))
        plugins.extend(_native_xdist_plugin_args(configuration, plugin_names))
        plugins.extend(native_adapter_plugin_args(configuration, plugin_names))
    elif "no:vfy._pytest.native_pytest" not in plugin_names:
        # Keep implicit plug-in discovery on the selected stock route.
        plugins.extend(("-p", "no:vfy._pytest.native_pytest"))
    if plugins and not native_pytest and os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        configured = [*configuration.effective_args, *pytest_args]
        if (
            _explicit_numprocesses(configured)
            or _pytest_option(configured, "--dist") is not None
            or args.confirm_failures
        ):
            plugins.extend(("-p", "xdist.plugin"))
    pytest_args[0:0] = plugins
    pytest_args[0:0] = _asyncio_plugin_args(configuration, _pytest_plugin_names(pytest_args))
    return pytest_args


def _asyncio_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    if "vfy.asyncio_plugin" in plugin_names:
        return ("-p", "no:asyncio")
    requested = set(configuration.plugins)
    upstream_enabled = (
        not (
            os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
            or "--disable-plugin-autoload" in configuration.effective_args
            or "no:asyncio" in requested
        )
        and installed_plugin_distribution("pytest-asyncio") is not None
    )
    plugin = (
        "vfy.asyncio_plugin"
        if "vfy.asyncio_plugin" in requested or upstream_enabled
        else "vfy._pytest.asyncio_builtin"
    )
    return ("-p", "no:asyncio", "-p", plugin)


def _staged_path(path: str | Path, directory: Path, label: str) -> Path:
    return Path(stage_watch_outputs(str(directory), [(label, str(path))])[0][1])


def _stage_pytest_arguments(
    pytest_args: list[str], config: Config, directory: Path
) -> tuple[list[str], dict[Path, Path | None]]:
    """Redirect file-producing pytest options into one disposable tree."""
    defaults: dict[str, str | None] = {
        "annotate": None,
        "html": config.get("html", {}).get("directory", "htmlcov"),
        "xml": config.get("xml", {}).get("output", "coverage.xml"),
        "json": config.get("json", {}).get("output", "coverage.json"),
        "markdown": "coverage.md",
        "markdown-append": "coverage.md",
        "lcov": config.get("lcov", {}).get("output", "coverage.lcov"),
    }
    staged: dict[Path, Path | None] = {}
    result: list[str] = []
    index = 0
    while index < len(pytest_args):
        argument = pytest_args[index]
        if argument.startswith("--cov-report="):
            value = argument.partition("=")[2]
            report, _separator, destination = value.partition(":")
            if report in defaults and destination != "-":
                original = destination or defaults[report]
                if original is None:
                    target = _staged_path(".", directory, "annotate-default")
                    staged[target] = None
                else:
                    target = _staged_path(original, directory, report)
                    staged[target] = Path(original).resolve()
                result.append(f"--cov-report={report}:{target}")
            else:
                result.append(argument)
            index += 1
            continue
        if argument == "--cov-report" and index + 1 < len(pytest_args):
            value = pytest_args[index + 1]
            report, _separator, destination = value.partition(":")
            if report in defaults and destination != "-":
                original = destination or defaults[report] or "."
                kind = "annotate-default" if report == "annotate" and not destination else report
                target = _staged_path(original, directory, kind)
                staged[target] = (
                    None if report == "annotate" and not destination else Path(original).resolve()
                )
                result.append("--cov-report")
                result.append(f"{report}:{target}")
            else:
                result.extend((argument, value))
            index += 2
            continue
        if argument.startswith("--cov-decision-report="):
            value = argument.partition("=")[2]
            report, _separator, destination = value.partition(":")
            if report == "json" and destination != "-":
                original = destination or "vfy-decisions.json"
                target = _staged_path(original, directory, "decisions")
                staged[target] = Path(original).resolve()
                result.append(f"--cov-decision-report=json:{target}")
            else:
                result.append(argument)
            index += 1
            continue
        if argument in {"--cov-decision-report", "--junitxml", "--junit-xml"} and index + 1 < len(
            pytest_args
        ):
            value = pytest_args[index + 1]
            if argument == "--cov-decision-report":
                report, _separator, destination = value.partition(":")
                if report == "json" and destination != "-":
                    original = destination or "vfy-decisions.json"
                    target = _staged_path(original, directory, "decisions")
                    staged[target] = Path(original).resolve()
                    result.extend((argument, f"json:{target}"))
                else:
                    result.extend((argument, value))
            else:
                target = _staged_path(value, directory, "junit")
                staged[target] = Path(value).resolve()
                result.extend((argument, str(target)))
            index += 2
            continue
        if argument.startswith(("--junitxml=", "--junit-xml=")):
            option, _separator, value = argument.partition("=")
            target = _staged_path(value, directory, "junit")
            staged[target] = Path(value).resolve()
            result.append(f"{option}={target}")
            index += 1
            continue
        if argument.startswith("--html="):
            value = argument.partition("=")[2]
            target = _staged_path(value, directory, "pytest-html")
            staged[target] = Path(value).resolve()
            staged[target.parent / "assets"] = Path(value).resolve().parent / "assets"
            result.append(f"--html={target}")
            index += 1
            continue
        if argument == "--html" and index + 1 < len(pytest_args):
            value = pytest_args[index + 1]
            target = _staged_path(value, directory, "pytest-html")
            staged[target] = Path(value).resolve()
            staged[target.parent / "assets"] = Path(value).resolve().parent / "assets"
            result.extend((argument, str(target)))
            index += 2
            continue
        result.append(argument)
        index += 1
    return result, staged
