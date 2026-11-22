"""Resolve importable source names for filesystem consumers."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path


def source_root_paths(roots: Iterable[str]) -> list[str]:
    """Resolve Coverage.py source entries to the paths they measure.

    Coverage.py treats an entry that is not an existing file or directory as an
    importable package or module name. Unresolvable entries stay unchanged so
    callers can report them.
    """
    resolved: list[str] = []
    for root in roots:
        if Path(root).exists():
            resolved.append(root)
            continue
        # find_spec() imports parents of dotted names. Their first import can
        # replace __path__, so leave an unloaded parent unresolved for Rust's
        # conservative full-run fallback.
        try:
            if "." in root:
                parent_name, _, _ = root.rpartition(".")
                parent = sys.modules.get(parent_name)
                locations = getattr(parent, "__path__", None)
                if locations is None:
                    resolved.append(root)
                    continue
                from .api import (  # noqa: PLC0415 - needed only for loaded dotted parents
                    _find_source_package_spec,
                )

                module = _find_source_package_spec(root, locations)
            else:
                module = importlib.util.find_spec(root)
        except (ImportError, ValueError):
            module = None
        if module is not None and module.submodule_search_locations:
            resolved.extend(module.submodule_search_locations)
        elif module is not None and module.origin and Path(module.origin).is_file():
            resolved.append(module.origin)
        else:
            resolved.append(root)
    return resolved


def mutation_source_paths(roots: Iterable[str]) -> list[str]:
    """Find static dotted mutation sources without importing their parents."""
    resolved: list[str] = []
    for root in roots:
        paths = source_root_paths([root])
        if Path(root).exists() or "." not in root or paths != [root]:
            resolved.extend(paths)
            continue
        parts = root.split(".")
        try:
            spec = importlib.util.find_spec(parts[0])
            from .api import (  # noqa: PLC0415 - only dotted mutation roots need the finder
                _find_source_package_spec,
            )

            for index in range(1, len(parts)):
                parent = sys.modules.get(".".join(parts[:index]))
                locations = getattr(parent, "__path__", None)
                if parent is None and spec is not None:
                    locations = spec.submodule_search_locations
                if not locations:
                    spec = None
                    break
                spec = _find_source_package_spec(".".join(parts[: index + 1]), locations)
        except (ImportError, ValueError):
            spec = None
        if spec is not None and spec.submodule_search_locations:
            resolved.extend(spec.submodule_search_locations)
        elif spec is not None and spec.origin and Path(spec.origin).is_file():
            resolved.append(spec.origin)
        else:
            resolved.append(root)
    return resolved


def source_graph_roots(roots: Iterable[str]) -> list[str]:
    """Give the import graph the search bases of importable source names."""
    graph_roots: list[str] = []
    for root in roots:
        for resolved in source_root_paths([root]):
            path = Path(resolved)
            if resolved == root or not path.exists():
                graph_roots.append(resolved)
                continue
            # A package at src/pkg is imported as pkg from src. The graph
            # scans that search base so imports retain their qualified names.
            module_path = path if path.is_dir() else path.with_suffix("")
            parts = root.split(".")
            if tuple(module_path.parts[-len(parts) :]) != tuple(parts):
                graph_roots.append(root)
                continue
            graph_roots.append(str(path.parents[len(parts) - 1]))
    return graph_roots
