"""Implementation of the diagnostics command."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from ._pytest.collection_readiness import MISSING_FIXTURES_PREFIX, UNSUPPORTED_ITEMS_PREFIX
from ._version import __version__
from .errors import VfyError
from .native import (
    build_profile,
    coverage_uses_sysmon_branch_arcs,
    native_pytest_doctor_scan,
    parser_backend,
)

if TYPE_CHECKING:
    from .config_types import Config


class _DiagnosticsFinding(TypedDict):
    kind: str
    name: str
    path: str
    line: int | None
    action: str
    scope: str


class _DiagnosticsScan(TypedDict):
    findings: list[_DiagnosticsFinding]
    runtime_hooks: list[_DiagnosticsFinding]
    conftest_count: int
    truncated: bool


def _diagnostics_pytest_arguments(arguments: list[str]) -> list[str]:
    """Remove the separator retained by argparse's diagnostics remainder."""
    return arguments[1:] if arguments and arguments[0] == "--" else arguments


def _test_readiness_status(
    collection: dict[str, object] | None,
    *,
    blocked: bool,
    potential_blockers: bool,
    scan_truncated: bool,
    trial_status: str,
) -> str:
    """Summarize static checks and the optional collection preflight."""
    if blocked:
        return "blocked"
    if collection is not None and collection["missing_fixtures"]:
        return "potentially_blocked"
    if collection is not None and collection["returncode"] == 0:
        return "collected_support_unknown"
    if potential_blockers:
        return "potentially_blocked"
    if scan_truncated:
        return "incomplete_scan_needs_native_trial"
    return trial_status


def _diagnostics_command(args: argparse.Namespace, config: Config) -> int:
    import sysconfig  # noqa: PLC0415 - platform diagnostics belong to the optional diagnostics command

    source = args.config_source
    native_engine = args.engine == "pytest-native"
    vfy_config_error = getattr(args, "_diagnostics_config_error", None)
    history = config.get("vfy", {}).get("test", {}).get("history", ".vfy/test-history.sqlite3")
    baseline_path = Path(history).resolve().parent / "replay.coverage"
    installed_plugins = [
        name for name in ("pytest_cov", "slipcover") if importlib.util.find_spec(name) is not None
    ]
    pytest_args = _diagnostics_pytest_arguments(list(args.pytest_args))
    configured_args: list[str] = []
    pytest_config: dict[str, object] = {
        "file": None,
        "rootdir": str(Path.cwd().resolve()),
        "configured_args": [],
        "error": None,
    }
    config_error: str | None = None
    pytest_version: str | None = None
    try:
        from .run.engine import (  # noqa: PLC0415 - diagnostics only; other commands skip the engine modules
            TestEngine,
            _installed_pytest_version,
            _native_plugin_inventory,
            _native_runner_unavailability_reason,
            _pytest_plugin_names,
            _resolve_pytest_configuration,
            _select_test_engine,
        )

        pytest_version = _installed_pytest_version()[0]
        configuration = _resolve_pytest_configuration(pytest_args, engine=TestEngine(args.engine))
        record = configuration.projection
        plugin_inventory = _native_plugin_inventory(configuration) if native_engine else []
        if not native_engine:
            _select_test_engine(args, pytest_args, configuration)
        configured_args = list(record["configured_args"])
        pytest_config = {
            "file": record["config_file"],
            "rootdir": record["rootdir"],
            "configured_args": configured_args,
            "error": None,
        }
        native_runner_reason = (
            _native_runner_unavailability_reason(configuration) if native_engine else None
        )
        plugin_names = _pytest_plugin_names([*configured_args, *pytest_args])
        env_plugins = [
            name.strip() for name in os.environ.get("PYTEST_PLUGINS", "").split(",") if name.strip()
        ]
        conflicts, xdist_conflicts = (
            _diagnostics_pytest_conflicts(list(record["effective_args"]), plugin_names, env_plugins)
            if native_engine
            else ([], [])
        )
    except (VfyError, OSError, ValueError) as error:
        config_error = str(error)
        pytest_config["error"] = config_error
        native_runner_reason = None
        conflicts = [
            {
                "kind": "pytest_configuration",
                "source": "pytest configuration or arguments",
                "detail": config_error,
                "action": "fix the pytest configuration before running vfy test",
            }
        ]
        xdist_conflicts = []
        plugin_names = ()
        env_plugins = []
        plugin_inventory = []

    rootdir = Path(str(pytest_config["rootdir"])).resolve()
    static_scan = (
        _diagnostics_static_scan(
            rootdir, [*configured_args, *pytest_args], [*plugin_names, *env_plugins]
        )
        if native_engine
        else None
    )
    for finding in static_scan["findings"] if static_scan is not None else ():
        if finding["kind"] in {"plugin", "dynamic_plugin"}:
            conflicts.append(
                {
                    "kind": "conftest_plugin",
                    "source": f"{finding['path']}:{finding['line']}",
                    "scope": "project-wide static scan",
                    "detail": (
                        f"{finding['name']} is declared by a conftest file"
                        if finding["kind"] == "plugin"
                        else "cannot statically resolve pytest_plugins in a conftest file"
                    ),
                    "action": "remove the declaration or run this workflow with pytest",
                }
            )

    runtime_issues: list[str] = []
    version = sys.version_info
    if sys.implementation.name != "cpython":
        runtime_issues.append(f"{args.engine} execution requires CPython")
    elif (version.major, version.minor) < (3, 12):
        runtime_issues.append(f"{args.engine} execution requires CPython 3.12 or newer")
    if not native_engine and os.name != "posix":
        runtime_issues.append("independent execution requires POSIX")
    if native_runner_reason is not None:
        runtime_issues.append(native_runner_reason)
    if config_error is not None:
        runtime_issues.append("failed to resolve the test configuration")
    collection = _diagnostics_collect(pytest_args, engine=args.engine) if args.collect else None
    if collection is not None and collection["returncode"] != 0:
        runtime_issues.append("collection preflight failed")
    if collection is not None and collection["unsupported_items"]:
        runtime_issues.append(f"{args.engine} execution does not run some collected test items")
    readiness_status = _test_readiness_status(
        collection,
        blocked=bool(
            runtime_issues
            or vfy_config_error is not None
            or any(conflict["kind"] != "conftest_plugin" for conflict in conflicts)
        ),
        potential_blockers=bool(
            conflicts or (static_scan is not None and static_scan["runtime_hooks"])
        ),
        scan_truncated=bool(static_scan is not None and static_scan["truncated"]),
        trial_status="needs_native_trial" if native_engine else "needs_test_run",
    )

    diagnostics: dict[str, object] = {
        "version": __version__,
        "python": sys.version.split()[0],
        "implementation": sys.implementation.name,
        "platform": sys.platform,
        "native_profile": build_profile(),
        "parser": parser_backend(),
        "backend": "native",
        "engine": args.engine,
        "sys_monitoring_branch_arcs": coverage_uses_sysmon_branch_arcs(),
        "python_executable": sys.executable,
        "config": str(source) if source is not None else None,
        "vfy_configuration": {
            "status": "blocked" if vfy_config_error is not None else "valid",
            "error": vfy_config_error,
            "action": (
                "remove or replace the unsupported setting, then rerun vfy diagnostics"
                if vfy_config_error is not None and "unsupported" in vfy_config_error
                else (
                    "fix the configuration error, then rerun vfy diagnostics"
                    if vfy_config_error is not None
                    else None
                )
            ),
        },
        "config_sections": sorted(config),
        "pytest": importlib.util.find_spec("pytest") is not None,
        "pytest_version": pytest_version,
        "pytest_xdist": importlib.util.find_spec("xdist") is not None,
        "pytest_cov": importlib.util.find_spec("pytest_cov") is not None,
        "plugin_conflicts": conflicts,
        "coinstalled_coverage_plugins": installed_plugins,
        "pytest_configuration": pytest_config,
        "pytest_explicit_plugins": list(plugin_names),
        "pytest_environment_plugins": env_plugins,
        "pytest_plugin_inventory": plugin_inventory,
        "xdist_conflicts": xdist_conflicts,
        "runtime_hooks": static_scan["runtime_hooks"] if static_scan is not None else [],
        "static_scan": {
            "performed": static_scan is not None,
            "conftest_count": static_scan["conftest_count"] if static_scan is not None else None,
            "truncated": static_scan["truncated"] if static_scan is not None else False,
            "scope": (
                "project-wide conftest scan under the resolved pytest root; generated and hidden "
                "directories are skipped, and nested findings may not apply to selected paths"
                if native_engine
                else "independent plug-in and hook support is checked during collection"
            ),
        },
        "test_readiness": {
            "engine": args.engine,
            "status": readiness_status,
            "confidence": "partial",
            "basis": (
                "collection with the selected engine; a successful collection does not prove "
                "that tests execute correctly"
                if collection is not None
                else (
                    "resolved pytest arguments and static source inspection; collection "
                    "and test bodies were not run"
                    if native_engine
                    else "resolved test arguments and configuration; plug-in and hook support "
                    "has not been checked, and test bodies were not run"
                )
            ),
            "runtime_issues": runtime_issues,
            "collection": collection,
            "test_support": (
                "unknown until a native test run completes"
                if native_engine
                else "unknown until a test run completes"
            ),
            "static_scan_complete": (
                not static_scan["truncated"] if static_scan is not None else None
            ),
            "limitations": (
                [
                    "native execution does not autoload arbitrary installed pytest plug-ins; "
                    "inspect pytest_plugin_inventory for the planned "
                    "loaded/replaced/omitted status",
                    "static inspection cannot detect dynamic plug-in or hook registration",
                    "collection does not prove test execution or fixture behavior is compatible",
                    "project-wide nested conftest findings may not apply to selected test paths",
                    *(
                        ["the conftest scan reached its directory or file limit"]
                        if static_scan is not None and static_scan["truncated"]
                        else []
                    ),
                ]
                if native_engine
                else [
                    "plug-in and hook support is not inspected statically; collection checks "
                    "the declarations loaded for the selected paths",
                    "collection does not prove test execution or fixture behavior is compatible",
                ]
            ),
        },
        "cache_dir": str(Path(".vfy").resolve()),
        "cache_exists": Path(".vfy").is_dir(),
        "baseline": {
            "path": str(baseline_path),
            "exists": baseline_path.is_file(),
            "readable": baseline_path.is_file() and os.access(baseline_path, os.R_OK),
        },
        "native_build": {"profile": build_profile(), "parser": parser_backend()},
        "stdlib": sysconfig.get_platform(),
    }
    if native_engine:
        diagnostics["native_readiness"] = diagnostics["test_readiness"]
    if args.json:
        print(json.dumps(diagnostics, indent=2, sort_keys=True))
    else:
        _print_diagnostics_report(diagnostics)
    return 0


def _diagnostics_pytest_conflicts(
    effective_args: list[str],
    plugin_names: tuple[str, ...],
    env_plugins: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    from ._pytest.mock_support import (  # noqa: PLC0415 - share native diagnostic policy lazily
        PYTEST_MOCK_PLUGIN_NAMES,
        pytest_mock_supported_version,
    )
    from ._pytest.plugin_support import (  # noqa: PLC0415 - diagnostics only
        native_compatible_entry_points,
    )
    from ._pytest.timeout_support import (  # noqa: PLC0415 - share native diagnostic policy lazily
        PYTEST_TIMEOUT_PLUGIN_NAMES,
        pytest_timeout_supported_version,
    )
    from ._pytest.xdist_support import (  # noqa: PLC0415 - inspect optional xdist for diagnostics
        XDIST_PLUGIN_NAMES,
        XDIST_VERSION_RANGE,
        xdist_requested,
        xdist_supported_version,
    )
    from .run.engine import (  # noqa: PLC0415 - diagnostics only; other commands skip the engine modules
        _conflicting_asyncio_plugin_names,
        _native_packaged_pytest_plugins,
    )

    all_args = effective_args
    conflicts: list[dict[str, str]] = []
    xdist_conflicts: list[str] = []
    supported_xdist = xdist_supported_version()
    if xdist_requested(all_args, [*plugin_names, *env_plugins]) and not supported_xdist:
        action = f"install a stable release matching 'pytest-xdist{XDIST_VERSION_RANGE}'"
        conflicts.append(
            {
                "kind": "xdist",
                "source": "resolved pytest addopts or diagnostics arguments",
                "detail": f"native pytest requires stable pytest-xdist{XDIST_VERSION_RANGE}",
                "action": action,
            }
        )
        xdist_conflicts.append(action)
    packaged = _native_packaged_pytest_plugins(native_compatible_entry_points())
    if not pytest_mock_supported_version():
        packaged.difference_update(PYTEST_MOCK_PLUGIN_NAMES)
    if not pytest_timeout_supported_version():
        packaged.difference_update(PYTEST_TIMEOUT_PLUGIN_NAMES)
    if not supported_xdist:
        packaged.difference_update(XDIST_PLUGIN_NAMES)
    explicit = [
        (name, "resolved pytest addopts or diagnostics arguments")
        for name in plugin_names
        if name == "no:vfy._pytest.native_pytest"
        or (not name.startswith(("no:", "_pytest.")) and name not in packaged)
    ]
    explicit.extend(
        (name, "PYTEST_PLUGINS")
        for name in env_plugins
        if name == "no:vfy._pytest.native_pytest"
        or (not name.startswith(("no:", "_pytest.")) and name not in packaged)
    )
    asyncio_conflict = _conflicting_asyncio_plugin_names([name for name, _source in explicit])
    for name, source in explicit:
        conflicts.append(
            {
                "kind": "pytest_plugin",
                "source": source,
                "detail": f"explicit plug-in request {name!r} is outside the native plug-in set",
                "action": "remove the request or run this workflow with pytest",
            }
        )
    if asyncio_conflict:
        for conflict in conflicts:
            if conflict["kind"] == "pytest_plugin" and "pytest_asyncio" in conflict["detail"]:
                conflict["detail"] += "; native vfy test already provides asyncio support"
    return conflicts, xdist_conflicts


def _diagnostics_static_scan(
    root: Path, arguments: list[str], plugins: list[str]
) -> _DiagnosticsScan:
    from ._pytest.mock_support import (  # noqa: PLC0415 - inspect optional plug-ins only for diagnostics
        PYTEST_MOCK_PLUGIN_NAMES,
        pytest_mock_supported_version,
    )
    from ._pytest.platformdirs_support import (  # noqa: PLC0415 - share reviewed fixture policy
        PLATFORMDIRS_PLUGIN_MODULE,
        platformdirs_plugin_reviewed,
    )
    from ._pytest.plugin_support import (  # noqa: PLC0415 - share built-in plug-in policy
        PYTESTER_PLUGIN_NAMES,
    )
    from ._pytest.xdist_support import (  # noqa: PLC0415 - inspect optional xdist for diagnostics
        XDIST_CONFTEST_HOOKS,
        XDIST_PLUGIN_NAMES,
        xdist_requested,
        xdist_supported_version,
    )

    records, conftest_count, truncated = native_pytest_doctor_scan(str(root))
    findings: list[_DiagnosticsFinding] = []
    runtime_hooks: list[_DiagnosticsFinding] = []
    supported_mock = pytest_mock_supported_version()
    supported_platformdirs = platformdirs_plugin_reviewed()
    supported_xdist = xdist_supported_version()
    enabled_xdist = supported_xdist and xdist_requested(
        arguments, [*plugins, *(name for kind, name, _path, _line in records if kind == "plugin")]
    )
    for kind, name, path, line in records:
        if kind == "plugin" and name in PYTEST_MOCK_PLUGIN_NAMES and supported_mock:
            continue
        if kind == "plugin" and name == PLATFORMDIRS_PLUGIN_MODULE and supported_platformdirs:
            continue
        if kind == "plugin" and name in XDIST_PLUGIN_NAMES and supported_xdist:
            continue
        if kind == "plugin" and name in PYTESTER_PLUGIN_NAMES:
            continue
        if kind == "unsupported_hook" and enabled_xdist and name in XDIST_CONFTEST_HOOKS:
            continue
        record: _DiagnosticsFinding = {
            "kind": kind,
            "name": name,
            "path": path,
            "line": line,
            "scope": "project-wide static scan",
            "action": (
                "replace the phase hook wrapper with a plain hook, or run this workflow with pytest"
                if kind == "unsupported_hook_wrapper"
                else "remove or replace the hook, or run this workflow with pytest"
                if kind == "unsupported_hook"
                else "fix the conftest syntax before collecting tests"
                if kind == "syntax_error"
                else "remove the pytest_plugins declaration or run this workflow with pytest"
            ),
        }
        findings.append(record)
        if kind in {"unsupported_hook", "unsupported_hook_wrapper", "syntax_error"}:
            runtime_hooks.append(record)

    return {
        "findings": findings,
        "runtime_hooks": runtime_hooks,
        "conftest_count": conftest_count,
        "truncated": truncated,
    }


_COLLECTED_NODE_ID = re.compile(r"\S+::\S")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _diagnostics_collect(pytest_args: list[str], *, engine: str) -> dict[str, object]:
    from .runner import capture_command  # noqa: PLC0415 - collection owns the process boundary

    command = [
        sys.executable,
        # Typeshed omits CPython's spawn adapter, which preserves caller startup flags.
        *subprocess._args_from_interpreter_flags(),  # ty: ignore[unresolved-attribute]
        "-m",
        "vfy",
        "--quiet",
        "test",
        f"--engine={engine}",
        "--no-cov",
        "--collect-only",
        "-q",
        *pytest_args,
    ]
    environment = os.environ.copy()
    for name in (
        "VFY_IN_PROCESS_PYTEST",
        "VFY_TEST_RESULTS",
        "VFY_SELECTED_TESTS",
        "VFY_CI_REQUEST",
        "VFY_CI_FINAL",
        "VFY_CI_COLLECTION",
        "VFY_CI_IGNORED",
        "VFY_REPLAY_MERGE",
        "VFY_TEST_HISTORY",
    ):
        environment.pop(name, None)
    try:
        result = capture_command(
            command,
            cwd=Path.cwd(),
            environment=environment,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "status": "timed_out",
            "returncode": None,
            "collected_count": None,
            "support": "unknown",
            "diagnostic": f"pytest collection exceeded 120 seconds: {error}",
            "unsupported_items": False,
            "missing_fixtures": False,
        }
    # Projects such as humanize set `--color=yes` in `addopts`; the captured
    # summary is reparsed and reprinted, so terminal markup must not survive.
    stdout, stderr = (_ANSI_ESCAPE.sub("", output) for output in (result.stdout, result.stderr))
    # Quiet collection lists one node ID per line; an ID that merely contains
    # "error", such as a test named for the error it checks, is not a finding.
    summary_lines = [
        line
        for line in (*stdout.splitlines(), *stderr.splitlines())
        if ("collected" in line.lower() or "error" in line.lower())
        and not _COLLECTED_NODE_ID.match(line)
    ]
    unsupported_items = any(
        line.startswith(UNSUPPORTED_ITEMS_PREFIX) for line in stdout.splitlines()
    )
    missing_fixtures = any(line.startswith(MISSING_FIXTURES_PREFIX) for line in stdout.splitlines())
    collected_count: int | None = None
    for output in (stdout, stderr):
        matches = re.findall(
            r"(?m)^\s*(\d+)\s+(?:tests?|items?)\s+collected\b[^\n]*$",
            output,
        )
        if matches:
            collected_count = int(matches[-1])
        elif re.search(r"(?m)^\s*no tests collected\b", output):
            collected_count = 0
    return {
        "status": "collected" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "collected_count": collected_count,
        "support": "unknown",
        "diagnostic": "\n".join(summary_lines[-20:]),
        "unsupported_items": unsupported_items,
        "missing_fixtures": missing_fixtures,
    }


def _print_diagnostics_report(diagnostics: dict[str, object]) -> None:
    readiness = diagnostics["test_readiness"]
    assert isinstance(readiness, dict)
    native_engine = diagnostics["engine"] == "pytest-native"
    engine_label = "Native" if native_engine else "Independent"
    runtime_status = "available" if not readiness["runtime_issues"] else "has blockers"
    print(f"vfy {diagnostics['version']} {diagnostics['engine']} diagnostics")
    print(
        f"Python {diagnostics['python']} ({diagnostics['implementation']}, "
        f"{diagnostics['platform']})"
    )
    print(f"pytest: {'available' if diagnostics['pytest'] else 'not installed'}")
    print(f"{engine_label} runtime: {runtime_status}")
    vfy_configuration = diagnostics["vfy_configuration"]
    assert isinstance(vfy_configuration, dict)
    print(f"vfy configuration: {vfy_configuration['status']}")
    if vfy_configuration["error"]:
        print(f"  - {vfy_configuration['error']}")
        print(f"    action: {vfy_configuration['action']}")
    pytest_config = diagnostics["pytest_configuration"]
    assert isinstance(pytest_config, dict)
    print(
        "pytest configuration: "
        f"{pytest_config['file'] or 'defaults'} (root: {pytest_config['rootdir']})"
    )
    readiness_labels = {
        "blocked": "blocked",
        "collected_support_unknown": "collection passed; execution support remains unknown",
        "potentially_blocked": "potential blockers found",
        "incomplete_scan_needs_native_trial": (
            "scan incomplete; a native test run is still needed"
        ),
        "needs_native_trial": "a native test run is still needed",
        "needs_test_run": "plug-in and hook support is unknown; a test run is still needed",
    }
    readiness_status = str(readiness["status"])
    readiness_label = readiness_labels.get(readiness_status, readiness_status)
    print(f"{engine_label} readiness: {readiness_label}")
    inventory = cast(list[dict[str, object]], diagnostics["pytest_plugin_inventory"])
    if native_engine:
        print("Installed pytest plug-ins (native load plan):")
    for plugin in inventory:
        print(
            f"  - {plugin['distribution']} {plugin['version']} ({plugin['name']}): "
            f"{plugin['native_status']}; {plugin['reason']}"
        )
    if any(plugin["differs_from_pytest"] for plugin in inventory):
        print(
            "  Omitted plug-ins can change test behavior even when collection succeeds. "
            "Keep their pytest behavior with "
            "`pytest -p no:pytest_cov -p vfy.pytest_plugin --cov`."
        )
    for issue in readiness["runtime_issues"]:
        print(f"  - {issue}")
    for conflict in cast(list[dict[str, str]], diagnostics["plugin_conflicts"]):
        label = "potential " if conflict.get("scope") == "project-wide static scan" else ""
        print(f"  - {label}{conflict['kind']}: {conflict['detail']} ({conflict['source']})")
        action = conflict.get("replacement", conflict.get("action"))
        if action:
            print(f"    action: {action}")
    for hook in cast(list[dict[str, object]], diagnostics["runtime_hooks"]):
        if hook["kind"] == "syntax_error":
            print(f"  - failed to inspect {hook['path']}: {hook['name']}")
        elif hook["kind"] == "unsupported_hook_wrapper":
            print(
                f"  - potential unsupported phase hook wrapper {hook['name']!r} at "
                f"{hook['path']}:{hook['line']}"
            )
        else:
            print(
                f"  - potential unsupported runtime hook {hook['name']!r} at "
                f"{hook['path']}:{hook['line']}"
            )
        print(f"    action: {hook['action']}")
    collection = readiness["collection"]
    if collection is None:
        scan = diagnostics["static_scan"]
        assert isinstance(scan, dict)
        if native_engine:
            print(
                f"Conftest files inspected: {scan['conftest_count']}"
                + (" (scan limit reached)" if scan["truncated"] else "")
            )
        engine_option = " --engine=pytest-native" if native_engine else ""
        print(f"Run `vfy diagnostics{engine_option} --collect` to check collection.")
    else:
        assert isinstance(collection, dict)
        count = collection["collected_count"]
        if count is not None:
            noun = "test" if count == 1 else "tests"
            print(f"{count} {noun} collected; execution support remains unknown")
        print(f"Collection: {collection['status']}")
        if collection["diagnostic"]:
            print(collection["diagnostic"])
    print("A passing collection does not prove test execution compatibility.")
