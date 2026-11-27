"""Label installed plug-in startup errors without replacing their exceptions."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from importlib.metadata import Distribution, EntryPoint

    from .protocol import Config, PluginManager, Session


STARTUP_PLUGIN_ERROR = "vfy plug-in startup error"
KEEP_PLUGIN_WITH_PYTEST = (
    "To keep the plug-in, run `pytest -p no:pytest_cov -p vfy.pytest_plugin --cov`"
)


def startup_error_marker(result_path: str | Path) -> Path:
    return Path(result_path).with_suffix(".plugin-startup-error")


def _startup_notes(error: BaseException) -> list[str]:
    # An exception subclass can override attribute access. Read the base
    # instance dictionary so that inspecting notes cannot replace the error.
    attributes = BaseException.__dict__["__dict__"].__get__(error)
    notes = dict.get(attributes, "__notes__")
    if not isinstance(notes, list):
        return []
    return [
        note
        for note in list.__iter__(notes)
        if isinstance(note, str) and str.startswith(note, STARTUP_PLUGIN_ERROR)
    ]


def publish_startup_error(error: BaseException) -> list[str]:
    """Tell the controller a named startup error ended the run before results."""
    notes = _startup_notes(error)
    if notes and (result_path := os.environ.get("VFY_TEST_RESULTS")):
        # Failure of this optional marker must leave the original error visible.
        with suppress(OSError, ValueError):
            startup_error_marker(result_path).touch()
    return notes


def note_entry_point_error(
    error: Exception, distribution: Distribution, entry: EntryPoint, operation: str
) -> None:
    """Add the failed entry point and the stock pytest recovery to the traceback."""
    if _startup_notes(error):
        return
    cause = " ".join(str(error).splitlines())
    BaseException.add_note(
        error,
        f"{STARTUP_PLUGIN_ERROR} in {distribution.metadata['Name']} {distribution.version} "
        f"(pytest11 {entry.name} = {entry.value}) during {operation}. "
        f"{type(error).__name__}: {cause}\n"
        f"{KEEP_PLUGIN_WITH_PYTEST}. Place your pytest options and test paths before `--cov`.",
    )


def note_distribution_error(
    error: Exception, distribution: Distribution, name: str, operation: str
) -> None:
    """Find the entry point registered under this plug-in name."""
    for entry in distribution.entry_points:
        if entry.group == "pytest11" and entry.name == name:
            note_entry_point_error(error, distribution, entry, operation)
            return


def note_hook_error(
    error: Exception, plugin: object, name: str, hook: str, kwargs: dict[str, object]
) -> None:
    """Look up the distribution that registered the selected hook implementation."""
    manager: PluginManager
    if hook == "pytest_plugin_registered":
        manager = cast("PluginManager", kwargs["manager"])
        config = cast("Config | None", manager.get_plugin("pytestconfig"))
        if manager._registering_distinfo is None and (config is None or config._configured):
            return
    elif hook in {"pytest_addhooks", "pytest_addoption"}:
        manager = cast("PluginManager", kwargs["pluginmanager"])
    elif hook == "pytest_sessionstart":
        manager = cast("Session", kwargs["session"]).config.pluginmanager
    else:
        key = "early_config" if hook == "pytest_load_initial_conftests" else "config"
        manager = cast("Config", kwargs[key]).pluginmanager
    registering = manager._registering_distinfo
    if registering is not None and registering[0] is plugin:
        note_distribution_error(error, registering[1], name, hook)
        return
    for registered, distribution in manager._plugin_distinfo:
        if registered is plugin:
            note_distribution_error(error, distribution, name, hook)
            return
