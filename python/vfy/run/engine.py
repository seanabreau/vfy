"""Select the test engine and resolve pytest configuration for it."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import shlex
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from vfy._pytest.benchmark_support import (
    PYTEST_BENCHMARK_PLUGIN_NAMES,
    pytest_benchmark_supported_version,
)
from vfy._pytest.compatibility import pytest_compatibility_policy
from vfy._pytest.mock_support import (
    PYTEST_MOCK_PLUGIN_NAMES,
    PYTEST_MOCK_VERSION_RANGE,
    pytest_mock_supported_version,
)
from vfy._pytest.platformdirs_support import (
    PLATFORMDIRS_PLUGIN_NAMES,
    PLATFORMDIRS_REVIEWED_VERSIONS,
    platformdirs_entrypoint_installed,
    platformdirs_plugin_reviewed,
)
from vfy._pytest.plugin_support import (
    PYTESTER_PLUGIN_NAMES,
    installed_plugin_distribution,
    installed_pytest_entry_points,
    native_compatible_entry_points,
    native_compatible_plugin_names,
)
from vfy._pytest.timeout_support import (
    PYTEST_TIMEOUT_PLUGIN_NAMES,
    PYTEST_TIMEOUT_VERSION_RANGE,
    pytest_timeout_installed,
    pytest_timeout_supported_version,
)
from vfy._pytest.xdist_support import (
    XDIST_PLUGIN_NAMES,
    XDIST_VERSION_RANGE,
    xdist_requested,
    xdist_supported_version,
)
from vfy.errors import PytestConfigurationError, VfyError
from vfy.independent.configuration import (
    PytestConfiguration as _PytestConfiguration,
    resolve,
    unavailability_reason,
    validate,
)
from vfy.native import NativeTestInvocation, pytest_version_less_than, resolve_pytest_configuration

if TYPE_CHECKING:
    from vfy.native_types import (
        PytestConfigPolicy,
        PytestConfigRecord,
    )


def _explicit_numprocesses(pytest_args: Sequence[str]) -> bool:
    return any(
        argument in {"-n", "--numprocesses"}
        or (argument.startswith("-n") and argument != "-n")
        or argument.startswith("--numprocesses=")
        for argument in pytest_args
    )


def _native_pytest_available() -> bool:
    """Return whether vfy ships its native pytest adapter."""
    try:
        return importlib.util.find_spec("vfy._pytest.native_pytest") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class TestEngine(StrEnum):
    """Engine for ``vfy test`` and internal differential runs."""

    NATIVE = "pytest-native"
    PYTEST = "pytest"
    INDEPENDENT = "native"


def _runner_platform_unavailability_reason(engine: TestEngine) -> str | None:
    """Check the platforms supported by vfy's native runners."""
    if not sys.platform.startswith(("linux", "darwin")):
        return f"{engine.value} pytest is supported only on Linux and macOS"
    return None


def _independent_runner_unavailability_reason() -> str | None:
    return unavailability_reason()


def _native_runner_unavailability_reason(
    configuration: _PytestConfiguration | None = None,
) -> str | None:
    """Explain why this installation cannot run vfy's native pytest engine."""
    if reason := _runner_platform_unavailability_reason(TestEngine.NATIVE):
        return reason
    if not _native_pytest_available():
        return "the native pytest adapter is not installed in this Python environment"
    installed, error = (
        _installed_pytest_version()
        if configuration is None
        else (configuration.version, configuration.version_error)
    )
    if error is not None:
        return error
    if installed is None:
        return "failed to inspect pytest in this Python environment"
    try:
        pytest_compatibility_policy(installed)
    except ValueError as exc:
        return str(exc)
    return None


def _native_packaged_pytest_plugins(admitted: frozenset[tuple[str, str]]) -> set[str]:
    """Return plug-ins accepted by the native engine in this process."""
    packaged = {
        "vfy.pytest_plugin",
        "vfy.pytest_reporting",
        "vfy.asyncio_plugin",
        "vfy._pytest.asyncio_builtin",
        *PYTESTER_PLUGIN_NAMES,
        *PLATFORMDIRS_PLUGIN_NAMES,
        *PYTEST_MOCK_PLUGIN_NAMES,
        *PYTEST_TIMEOUT_PLUGIN_NAMES,
        *PYTEST_BENCHMARK_PLUGIN_NAMES,
        *XDIST_PLUGIN_NAMES,
        *native_compatible_plugin_names(admitted),
    }
    if os.environ.get("VFY_MUTATION"):
        packaged.add("vfy.mutation")
    if os.environ.get("VFY_MUTATION_TEST_ISOLATION") == "file":
        packaged.add("vfy.mutation_parallel")
    return packaged


PYTEST_COV_PLUGIN_NAMES = frozenset({"pytest_cov", "pytest_cov.plugin"})


def _pytest_builtin_plugin(name: str) -> bool:
    """Recognize short names such as ``warnings`` that pytest maps to ``_pytest``."""
    from _pytest.config import builtin_plugins  # noqa: PLC0415 - only for explicit -p names

    return name in builtin_plugins


def _external_pytest_plugins_enabled(configured_plugins: Sequence[str]) -> bool:
    """Detect third-party plug-ins the project explicitly asks pytest to load."""
    names = configured_plugins
    if "no:vfy._pytest.native_pytest" in names:
        return True
    packaged = _native_packaged_pytest_plugins(native_compatible_entry_points())
    return any(
        not name.startswith(("no:", "_pytest."))
        and name not in packaged
        and name not in PYTEST_COV_PLUGIN_NAMES
        and not _pytest_builtin_plugin(name)
        for name in names
    )


def _without_pytest_cov_loads(arguments: Sequence[str]) -> tuple[list[str], bool]:
    """Drop pytest-cov loads using pytest's ``-p NAME`` and ``-pNAME`` forms."""
    kept: list[str] = []
    removed = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-p" and index + 1 < len(arguments):
            if arguments[index + 1].strip() in PYTEST_COV_PLUGIN_NAMES:
                removed = True
            else:
                kept.extend(arguments[index : index + 2])
            index += 2
            continue
        if argument.startswith("-p") and argument[2:].strip() in PYTEST_COV_PLUGIN_NAMES:
            removed = True
        else:
            kept.append(argument)
        index += 1
    return kept, removed


def _environment_addopts() -> list[str]:
    value = os.environ.get("PYTEST_ADDOPTS")
    return shlex.split(value) if value else []


def _pytest_cov_environment_conflict() -> str | None:
    if _without_pytest_cov_loads(_environment_addopts())[1]:
        return (
            "PYTEST_ADDOPTS loads pytest-cov, which vfy's coverage adapter replaces; "
            "remove -p pytest_cov from PYTEST_ADDOPTS"
        )
    return None


def native_adapter_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    """Return the installed plug-ins that native test and mutation runs both load."""
    return (
        *_native_mock_plugin_args(configuration, plugin_names),
        *_native_timeout_plugin_args(configuration, plugin_names),
        *_native_benchmark_plugin_args(configuration, plugin_names),
        *_native_compatible_plugin_args(
            configuration, plugin_names, native_compatible_entry_points()
        ),
        *_native_platformdirs_plugin_args(configuration, plugin_names),
    )


def _configured_pytest_cov_override(configuration: _PytestConfiguration) -> tuple[str, ...]:
    """Re-apply configured ``addopts`` without their pytest-cov loads.

    pytest applies configured ``addopts`` before command-line plug-in blocks, so
    ``-p pytest_cov`` there registers ``--cov`` before vfy's coverage plug-in and
    the two option sets conflict. pytest applies the last ``-o addopts`` value.
    """
    configured = list(configuration.projection["configured_args"])
    addopts = configured[: len(configured) - len(_environment_addopts())]
    kept, removed = _without_pytest_cov_loads(addopts)
    return ("-o", f"addopts={shlex.join(kept)}") if removed else ()


def _native_engine_ineligibility(
    configuration: _PytestConfiguration,
) -> str | None:
    """Return the reason a native run must be rejected before launch."""
    if _xdist_requested(configuration) and not xdist_supported_version():
        return (
            f"--engine=pytest-native requires stable pytest-xdist{XDIST_VERSION_RANGE}. "
            "Install a supported version or use --engine=pytest with an installed xdist release"
        )
    if (
        PYTEST_MOCK_PLUGIN_NAMES.intersection(configuration.plugins)
        or _native_mock_plugin_args(configuration, ())
    ) and not pytest_mock_supported_version():
        return f"the native pytest engine requires pytest-mock{PYTEST_MOCK_VERSION_RANGE}"
    if (
        PYTEST_TIMEOUT_PLUGIN_NAMES.intersection(configuration.plugins)
        or _native_timeout_plugin_args(configuration, ())
    ) and not pytest_timeout_supported_version():
        return (
            f"the native pytest engine requires pytest-timeout{PYTEST_TIMEOUT_VERSION_RANGE}; "
            "pass -p no:timeout to run without it"
        )
    if (
        PLATFORMDIRS_PLUGIN_NAMES.intersection(configuration.plugins)
        or _native_platformdirs_plugin_args(configuration, ())
    ) and not platformdirs_plugin_reviewed():
        return (
            "the native pytest engine requires the Platformdirs pytest plugin from "
            f"{PLATFORMDIRS_REVIEWED_VERSIONS}; pass -p no:platformdirs to run without it"
        )
    if environment_conflict := _pytest_cov_environment_conflict():
        return environment_conflict
    if _conflicting_asyncio_plugin_names(configuration.plugins):
        return (
            "native execution uses vfy's built-in asyncio support; remove the explicit "
            "pytest-asyncio plug-in load"
        )
    runner_reason = _native_runner_unavailability_reason(configuration)
    if runner_reason is not None:
        return runner_reason
    if _external_pytest_plugins_enabled(configuration.plugins):
        return "the native pytest engine does not support explicitly requested third-party plug-ins"
    return None


def _xdist_requested(configuration: _PytestConfiguration) -> bool:
    """Detect explicit upstream worker options and plug-in declarations."""
    return xdist_requested(configuration.effective_args, configuration.plugins)


def _native_xdist_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    if not _xdist_requested(configuration):
        return ()
    requested_plugins = {*configuration.plugins, *plugin_names}
    if XDIST_PLUGIN_NAMES.intersection(requested_plugins) or {
        f"no:{name}" for name in XDIST_PLUGIN_NAMES
    }.intersection(requested_plugins):
        return ()
    return ("-p", "xdist.plugin")


def _native_mock_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    requested = {*configuration.plugins, *plugin_names}
    if PYTEST_MOCK_PLUGIN_NAMES.intersection(requested) or "no:pytest_mock" in requested:
        return ()
    if (
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        or "--disable-plugin-autoload" in configuration.effective_args
        or installed_plugin_distribution("pytest-mock") is None
    ):
        return ()
    return ("-p", "pytest_mock")


def _native_timeout_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    requested = {*configuration.plugins, *plugin_names}
    if PYTEST_TIMEOUT_PLUGIN_NAMES.intersection(requested) or "no:timeout" in requested:
        return ()
    if (
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        or "--disable-plugin-autoload" in configuration.effective_args
        or not pytest_timeout_installed()
    ):
        return ()
    return ("-p", "timeout")


def _native_compatible_plugin_args(
    configuration: _PytestConfiguration,
    plugin_names: Sequence[str],
    admitted: frozenset[tuple[str, str]],
) -> tuple[str, ...]:
    """Load installed plug-ins whose hooks all run unchanged under native execution."""
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") or (
        "--disable-plugin-autoload" in configuration.effective_args
    ):
        return ()
    requested = {*configuration.plugins, *plugin_names}
    arguments: list[str] = []
    for name, module in sorted(admitted):
        # Pytest registers an entry-point plug-in under its entry-point name,
        # so only ``no:<entry-point name>`` blocks it.
        if {name, module} & requested or f"no:{name}" in requested:
            continue
        arguments.extend(("-p", name))
    return tuple(arguments)


def _native_benchmark_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    """Load an installed, supported pytest-benchmark as pytest's autoload would."""
    requested = {*configuration.plugins, *plugin_names}
    if PYTEST_BENCHMARK_PLUGIN_NAMES.intersection(requested) or "no:benchmark" in requested:
        return ()
    if (
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        or "--disable-plugin-autoload" in configuration.effective_args
        or not pytest_benchmark_supported_version()
    ):
        return ()
    return ("-p", "benchmark")


def _native_platformdirs_plugin_args(
    configuration: _PytestConfiguration, plugin_names: Sequence[str]
) -> tuple[str, ...]:
    requested = {*configuration.plugins, *plugin_names}
    if PLATFORMDIRS_PLUGIN_NAMES.intersection(requested) or "no:platformdirs" in requested:
        return ()
    if (
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        or "--disable-plugin-autoload" in configuration.effective_args
        or not platformdirs_entrypoint_installed()
    ):
        return ()
    return ("-p", "platformdirs")


class NativePluginRecord(TypedDict):
    name: str
    module: str
    distribution: str
    version: str
    native_status: Literal["loaded", "replaced", "omitted"]
    reason: str
    differs_from_pytest: bool


def _native_plugin_inventory(configuration: _PytestConfiguration) -> list[NativePluginRecord]:
    """Describe the native load plan, not a claim that collection has loaded a plug-in."""
    requested = set(configuration.plugins)
    admitted = native_compatible_entry_points()
    packaged = _native_packaged_pytest_plugins(admitted)
    compatible = native_compatible_plugin_names(admitted)
    adapter_args = (
        *_native_mock_plugin_args(configuration, ()),
        *_native_timeout_plugin_args(configuration, ()),
        *_native_benchmark_plugin_args(configuration, ()),
        *_native_compatible_plugin_args(configuration, (), admitted),
        *_native_platformdirs_plugin_args(configuration, ()),
        *_native_xdist_plugin_args(configuration, ()),
    )
    adapters = requested | set(_pytest_plugin_names(adapter_args))
    autoload = not (
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")
        or "--disable-plugin-autoload" in configuration.effective_args
    )
    inventory: list[NativePluginRecord] = []
    for name, module, distribution, version in installed_pytest_entry_points():
        aliases = {name, module.partition(":")[0]}
        disabled = any(f"no:{alias}" in requested for alias in aliases)
        if name == "timeout" and name in adapters:
            # A module-name block does not block the upstream entry point.
            disabled = "no:timeout" in requested
        status: Literal["loaded", "replaced", "omitted"] = "omitted"
        reason = "native execution does not autoload this installed plug-in"
        if disabled:
            reason = "disabled by an explicit -p no: request"
        elif (
            aliases.intersection(requested)
            and not aliases.intersection(packaged)
            and name != "pytest_cov"
        ):
            reason = "explicit request is unsupported; native execution rejects this plug-in"
        elif name in {"pytest_cov", "asyncio"}:
            status = "replaced"
            reason = (
                "vfy's coverage adapter replaces pytest-cov "
                + (
                    "with collection enabled"
                    if configuration.coverage_enabled
                    else "with collection disabled"
                )
                if name == "pytest_cov"
                else "vfy provides its own asyncio adapter; pytest-asyncio is not loaded"
            )
        elif aliases.intersection(adapters) and aliases.intersection(packaged):
            status = "loaded"
            reason = (
                "its hooks run unchanged under native execution"
                if aliases.intersection(compatible)
                else "included in the native adapter load plan"
            )
        elif aliases.intersection(requested):
            reason = "explicit request is unsupported; native execution rejects this plug-in"
        elif not autoload and not aliases.intersection(requested):
            reason = "pytest plug-in autoload is disabled for this invocation"
        elif name in {"html", "html_fixtures", "metadata"}:
            reason = (
                "vfy handles supported report options itself; "
                "the upstream plug-in's fixtures and hooks are not loaded"
            )
        inventory.append(
            {
                "name": name,
                "module": module,
                "distribution": distribution,
                "version": version,
                "native_status": status,
                "reason": reason,
                "differs_from_pytest": status == "omitted" and autoload and not disabled,
            }
        )
    return inventory


def _report_native_plugin_omissions(
    args: argparse.Namespace, configuration: _PytestConfiguration
) -> None:
    if not getattr(args, "_plugin_omissions_reported", False):
        omitted = [
            row["distribution"] + " (" + row["name"] + ")"
            for row in _native_plugin_inventory(configuration)
            if row["differs_from_pytest"]
        ]
        if omitted:
            # This semantic difference must remain visible even in quiet runs.
            print(
                "vfy: native execution omits installed pytest plug-ins: "
                + ", ".join(omitted)
                + ". Their fixtures, hooks, and markers may not behave as under pytest. "
                "To keep pytest plug-ins with vfy coverage, run "
                "`pytest -p no:pytest_cov -p vfy.pytest_plugin --cov`.",
                file=sys.stderr,
            )
        args._plugin_omissions_reported = True


def _select_test_engine(
    args: argparse.Namespace,
    pytest_args: list[str],
    configuration: _PytestConfiguration | None = None,
) -> TestEngine:
    """Validate the requested engine before child launch."""
    requested = TestEngine(getattr(args, "engine", TestEngine.INDEPENDENT.value))
    if configuration is None:
        configuration = _resolve_pytest_configuration(pytest_args, engine=requested)
    if requested is TestEngine.INDEPENDENT:
        validate(
            args,
            configuration.plugins,
            _xdist_requested(configuration),
            _independent_runner_unavailability_reason(),
        )
        args._test_engine = requested.value
        return requested
    if getattr(args, "native_scheduling", None) is not None:
        if requested not in {TestEngine.NATIVE, TestEngine.INDEPENDENT}:
            raise VfyError("--native-scheduling requires the native or independent engine")
        if _xdist_requested(configuration):
            raise VfyError("--native-scheduling cannot be combined with pytest-xdist; use --dist")
    if "vfy._pytest.native_pytest" in configuration.plugins:
        raise VfyError(
            "vfy manages its native pytest adapter; remove the explicit "
            "vfy._pytest.native_pytest plug-in request"
        )
    if requested is TestEngine.PYTEST and configuration.version_error is not None:
        raise VfyError(f"pytest execution is unavailable: {configuration.version_error}")
    if requested is TestEngine.PYTEST and (
        environment_conflict := _pytest_cov_environment_conflict()
    ):
        raise VfyError(environment_conflict)
    if requested is TestEngine.NATIVE:
        reason = _native_engine_ineligibility(configuration)
        if reason is not None:
            raise VfyError(f"native pytest cannot run this workflow: {reason}")
        if _xdist_requested(configuration) and getattr(args, "workers", None) not in (None, 1, "1"):
            raise VfyError(
                "native pytest cannot combine vfy's --workers/-j/-n with pytest-xdist; "
                "remove pytest-xdist options or plug-in requests"
            )
    # Watch copies the argparse namespace and revalidates config each iteration.
    args._test_engine = requested.value
    return requested


def _pytest_plugin_names(pytest_args: Sequence[str]) -> tuple[str, ...]:
    """Extract explicit plug-in names with the shared invocation owner."""
    return tuple(NativeTestInvocation.plugin_names(pytest_args))


def _conflicting_asyncio_plugin(pytest_args: list[str]) -> str | None:
    """Find an upstream asyncio plug-in named directly on this invocation."""
    return _conflicting_asyncio_plugin_names(_pytest_plugin_names(pytest_args))


def _conflicting_asyncio_plugin_names(names: Sequence[str]) -> str | None:
    """Find an upstream asyncio plug-in among already parsed module names."""
    for raw_name in names:
        name = raw_name.strip()
        if name.startswith("no:"):
            continue
        normalized = name.removeprefix("pytest-")
        if normalized in {"asyncio", "pytest_asyncio", "pytest_asyncio.plugin"}:
            return name
    return None


def _installed_pytest_version() -> tuple[str | None, str | None]:
    try:
        return importlib.metadata.version("pytest"), None
    except importlib.metadata.PackageNotFoundError:
        return None, (
            "pytest is not installed; install vfy[pytest] "
            "to use --engine=pytest or --engine=pytest-native"
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return None, "failed to inspect pytest in this Python environment"


def _pytest_config_policy(version: str) -> PytestConfigPolicy:
    # Use the first development release at each configuration boundary.
    if pytest_version_less_than(version, "8.2.dev0"):
        return "legacy_without_argument_files"
    if pytest_version_less_than(version, "9.0.dev0"):
        return "legacy"
    if pytest_version_less_than(version, "9.1.dev0"):
        return "toml"
    if pytest_version_less_than(version, "9.2.dev0"):
        return "toml_with_addopts_overrides"
    return "toml_with_non_file_config"


def _resolve_pytest_configuration(
    pytest_args: list[str],
    *,
    invocation_dir: Path | None = None,
    engine: TestEngine = TestEngine.PYTEST,
) -> _PytestConfiguration:
    if engine is TestEngine.INDEPENDENT and os.name == "posix":
        return resolve(pytest_args, invocation_dir)

    version, version_error = (
        ("9.1.1", None) if engine is TestEngine.INDEPENDENT else _installed_pytest_version()
    )
    # Without a readable installed version, project the pinned pytest's policy;
    # `version_error` makes pytest execution fail before that projection runs it.
    policy: PytestConfigPolicy = "toml_with_addopts_overrides"
    if version is not None:
        try:
            policy = _pytest_config_policy(version)
        except ValueError:
            version_error = f"cannot parse the installed pytest version {version!r}"

    try:
        projection = resolve_pytest_configuration(
            pytest_args,
            str(invocation_dir or Path.cwd()),
            os.environ.get("PYTEST_ADDOPTS"),
            policy,
        )
    except ValueError as exc:
        raise PytestConfigurationError(str(exc)) from exc
    effective_args = tuple(projection["effective_args"])
    plugins = (
        # pytest's plugin bootstrap scans original tokens, unlike option parsing.
        *_pytest_plugin_names([*projection["configured_args"], *pytest_args]),
        *(name.strip() for name in os.environ.get("PYTEST_PLUGINS", "").split(",") if name.strip()),
    )
    return _PytestConfiguration(
        projection,
        effective_args,
        version,
        version_error,
        policy,
        plugins,
        "--no-cov" not in effective_args,
    )


def _pytest_configuration(
    pytest_args: list[str], *, invocation_dir: Path | None = None
) -> PytestConfigRecord:
    return _resolve_pytest_configuration(pytest_args, invocation_dir=invocation_dir).projection
