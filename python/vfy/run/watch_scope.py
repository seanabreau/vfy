"""Resolve the project roots and ignored paths a watch session observes."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from vfy.independent.configuration import PytestConfiguration as _PytestConfiguration

from .engine import _resolve_pytest_configuration
from .reports import (
    _pytest_generated_directories,
    _pytest_option,
    _pytest_report_outputs,
    _replay_cache,
)

if TYPE_CHECKING:
    from vfy.config_types import Config


def _watch_project_roots(source_roots: list[str]) -> list[Path]:
    """Return deterministic roots for the Rust watcher."""
    roots = {Path.cwd().resolve()}
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        completed = None
    if completed is not None and completed.returncode == 0 and completed.stdout.strip():
        roots.add(Path(completed.stdout.strip()).resolve())
    for source in source_roots:
        path = Path(source).resolve()
        if path.exists():
            roots.add(path)
    return sorted(roots, key=str)


def _watch_baseline_ignored_paths(args: argparse.Namespace) -> set[Path]:
    """Ignore generated state even before pytest configuration is valid."""
    root = Path.cwd().resolve()
    ignored: set[Path] = {
        root / ".git",
        root / ".vfy",
        root / ".pytest_cache",
        root / ".ruff_cache",
        root / ".mypy_cache",
        root / ".tox",
        root / ".venv",
        root / "venv",
        root / "__pycache__",
    }
    data = Path(args.data_file).resolve()
    ignored.add(data)
    ignored.add(data.parent / f"{data.name}.")
    history = Path(args.history).resolve()
    ignored.add(history)
    ignored.add(history.parent / f"{history.name}.")
    if history.suffix == ".sqlite3":
        ignored.update(Path(f"{history}{suffix}") for suffix in ("-journal", "-wal", "-shm"))
    ignored.add(_replay_cache(args.history))
    return ignored


def _watch_ignored_paths(
    args: argparse.Namespace,
    config: Config,
    pytest_args: list[str],
    configuration: _PytestConfiguration | None = None,
) -> list[Path]:
    """Build the generated-file policy shared by replay and watch."""
    root = Path.cwd().resolve()
    if configuration is None:
        configuration = _resolve_pytest_configuration(pytest_args, invocation_dir=root)
    ignored = _watch_baseline_ignored_paths(args)
    ignored.update(_pytest_generated_directories(pytest_args, root, configuration))
    for output in _pytest_report_outputs(pytest_args, config, configuration):
        ignored.add(Path(output).resolve())
    for option in ("--junitxml", "--junit-xml"):
        value = _pytest_option(configuration.effective_args, option)
        if value:
            ignored.add(Path(value).resolve())
    return sorted(ignored, key=str)


_WATCH_INPUT_IDENTITY = "vfy_watch_input_identity"
