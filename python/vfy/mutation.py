"""Load a source mutation in an isolated test process.

A child-process environment variable identifies one validated byte-range
replacement. The import hook applies it before pytest or application imports,
without editing the checkout.

The bootstrap imports this module before pytest loads it with `-p vfy.mutation`.
The PYTEST_DONT_REWRITE marker keeps pytest from warning that it cannot rewrite
an imported module, which `filterwarnings = error` turns into a startup error.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast, override

if TYPE_CHECKING:
    import pytest


@dataclass(frozen=True)
class MutationSpec:
    """One validated source substitution passed to a child process."""

    path: str
    source_hash: str
    start: int
    end: int
    original: str
    replacement: str

    @classmethod
    def from_mapping(cls, value: object) -> MutationSpec:
        if not isinstance(value, dict):
            raise ValueError("mutation specification must be an object")
        fields = ("path", "source_hash", "start", "end", "original", "replacement")
        if not all(field in value for field in fields):
            raise ValueError("mutation specification is missing a required field")
        path, source_hash = value["path"], value["source_hash"]
        start, end = value["start"], value["end"]
        original, replacement = value["original"], value["replacement"]
        if (
            not isinstance(path, str)
            or not isinstance(source_hash, str)
            or not path
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(original, str)
            or not isinstance(replacement, str)
            or start < 0
            or end <= start
            or len(source_hash) != 64
        ):
            raise ValueError("mutation specification has invalid fields")
        return cls(path, source_hash, start, end, original, replacement)


def _patch_bytes(source_bytes: bytes, spec: MutationSpec) -> bytes:
    """Apply a validated UTF-8 byte range without normalizing newlines or BOMs."""
    actual_hash = hashlib.sha256(source_bytes).hexdigest()
    if actual_hash != spec.source_hash:
        raise ImportError(
            f"mutation source digest changed for {spec.path}: "
            f"expected {spec.source_hash}, got {actual_hash}"
        )
    original_bytes = spec.original.encode("utf-8")
    if spec.start > len(source_bytes) or spec.end > len(source_bytes):
        raise ImportError(f"mutation source range is outside {spec.path}")
    if source_bytes[spec.start : spec.end] != original_bytes:
        raise ImportError(f"mutation source range changed for {spec.path}")
    return source_bytes[: spec.start] + spec.replacement.encode("utf-8") + source_bytes[spec.end :]


def _patch_source(source: str, spec: MutationSpec) -> str:
    """Apply a UTF-8 byte range to decoded source text."""
    return _patch_bytes(source.encode("utf-8"), spec).decode("utf-8")


def _same_file(left: str, right: str) -> bool:
    """Compare import paths using the platform's filesystem semantics."""
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


class _MutationLoader(importlib.abc.Loader):
    def __init__(
        self,
        loader: importlib.abc.Loader,
        spec: MutationSpec,
        patch: Callable[[str, bytes], bytes] | None = None,
    ) -> None:
        self._loader = loader
        self._spec = spec
        self._patch = patch

    @override
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self._loader.create_module(spec)

    @override
    def exec_module(self, module: ModuleType) -> None:
        filename = getattr(self._loader, "get_filename", lambda _name: self._spec.path)(
            module.__name__
        )
        get_data = getattr(self._loader, "get_data", None)
        patched_bytes: bytes | None = None
        if get_data is not None:
            try:
                source_bytes = get_data(filename)
                if not isinstance(source_bytes, bytes):
                    raise TypeError("source loader did not return bytes")
                patched_bytes = (
                    _patch_bytes(source_bytes, self._spec)
                    if self._patch is None
                    else self._patch(filename, source_bytes)
                )
            except (AttributeError, TypeError, UnicodeDecodeError, OSError) as error:
                raise ImportError(
                    f"loader returned unreadable source for {self._spec.path}"
                ) from error
        else:
            get_source = getattr(self._loader, "get_source", None)
            if get_source is None:
                raise ImportError(
                    f"loader cannot provide source for mutation target {self._spec.path}"
                )
            source = get_source(module.__name__)
            if source is None:
                raise ImportError(
                    f"loader returned no source for mutation target {self._spec.path}"
                )
            patched_bytes = (
                _patch_source(source, self._spec).encode("utf-8")
                if self._patch is None
                else self._patch(filename, source.encode("utf-8"))
            )
        assert patched_bytes is not None
        code = compile(patched_bytes, filename, "exec", dont_inherit=True)
        if self._patch is None:
            global _mutation_applied
            _mutation_applied = True
        exec(code, module.__dict__)


class _MutationFinder(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        spec: MutationSpec,
        patch: Callable[[str, bytes], bytes] | None = None,
    ) -> None:
        self._spec = spec
        self._patch = patch
        self._target = str(Path(spec.path).resolve())

    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        module_spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if module_spec is None or module_spec.loader is None:
            return module_spec
        loader = module_spec.loader
        get_filename = getattr(loader, "get_filename", None)
        if get_filename is None:
            return module_spec
        try:
            filename = os.fspath(get_filename(fullname))
            if not isinstance(filename, str):
                return module_spec
            # samefile follows aliases itself; resolving every imported module
            # also walks each directory before the identity check.
            if not _same_file(filename, self._target):
                return module_spec
        except (ImportError, OSError, TypeError):
            return module_spec
        if not isinstance(loader, importlib.abc.Loader) or not (
            hasattr(loader, "get_data") or hasattr(loader, "get_source")
        ):
            raise ImportError(f"unsupported loader for mutation target {self._spec.path}")
        module_spec.loader = _MutationLoader(loader, self._spec, self._patch)
        return module_spec


def install_from_environment() -> None:
    """Install the child-process hook, if the mutation runner requested one."""
    payload = os.environ.get("VFY_MUTATION")
    if not payload:
        return
    try:
        spec = MutationSpec.from_mapping(json.loads(payload))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ImportError(f"invalid VFY_MUTATION payload: {error}") from error
    selection_path = os.environ.pop("VFY_MUTATION_SELECTED_TESTS", None)
    if selection_path is not None:
        try:
            selected = json.loads(Path(selection_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ImportError("failed to read mutation selected tests") from error
        if (
            not isinstance(selected, list)
            or not selected
            or not all(isinstance(nodeid, str) and nodeid for nodeid in selected)
        ):
            raise ImportError("invalid mutation selected tests")
        global _mutation_selected_tests
        _mutation_selected_tests = cast("list[str]", selected)
    _install_spec(spec)


def _install_spec(
    spec: MutationSpec,
    patch: Callable[[str, bytes], bytes] | None = None,
) -> _MutationFinder:
    """Install the actual import resource before application startup."""
    target = str(Path(spec.path).resolve())
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        try:
            imported = str(Path(filename).resolve())
        except OSError:
            continue
        if imported == target:
            raise ImportError(f"mutation target was imported before the mutation hook: {target}")
    finder = _MutationFinder(spec, patch)
    sys.meta_path.insert(0, finder)

    return finder


def read_mutation_source(path: str) -> bytes | None:
    """Read an active target through its existing mutation import resource."""
    for finder in sys.meta_path:
        if isinstance(finder, _MutationFinder) and _same_file(path, finder._target):
            source = Path(path).read_bytes()
            if finder._patch is not None:
                return finder._patch(path, source)
            patched = _patch_bytes(source, finder._spec)
            global _mutation_applied
            _mutation_applied = True
            return patched
    return None


class _RewritingLoader(Protocol):
    exec_module: Callable[[ModuleType], None]
    _rewritten_names: dict[str, Path]


def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """Retain the validated replay scope and compose source assertion rewriting."""
    if _mutation_selected_tests is not None:
        from .pytest_plugin import _VfyTestSelection  # noqa: PLC0415 - installed pytest lifecycle

        selection = _VfyTestSelection(_mutation_selected_tests)
        # Mutation children own serial test execution; no xdist node hook is needed.
        hooks = ModuleType("_vfy_mutation_selection")
        hooks.__dict__.update(
            pytest_ignore_collect=selection.pytest_ignore_collect,
            pytest_collection_modifyitems=selection.pytest_collection_modifyitems,
        )
        early_config.pluginmanager.register(hooks, "_vfy_mutation_selection")
    from _pytest.assertion import assertstate_key  # noqa: PLC0415 - installed pytest boundary
    from _pytest.assertion.rewrite import rewrite_asserts  # noqa: PLC0415 - same loader boundary

    state = early_config.stash.get(assertstate_key, None)
    if state is None or state.hook is None:
        return
    hook = cast("_RewritingLoader", state.hook)
    original = hook.exec_module

    def exec_module(module: ModuleType) -> None:
        path = module.__spec__.origin if module.__spec__ is not None else None
        source = read_mutation_source(path) if path is not None else None
        if source is None:
            original(module)
            return
        assert path is not None
        tree = ast.parse(source, filename=path)
        rewrite_asserts(tree, source, path, early_config)
        hook._rewritten_names[module.__name__] = Path(path)
        # Mutated code must neither consume nor replace the clean rewrite cache.
        exec(compile(tree, path, "exec", dont_inherit=True), module.__dict__)

    hook.exec_module = exec_module

    def restore() -> None:
        if hook.exec_module is exec_module:
            hook.exec_module = original

    early_config.add_cleanup(restore)


_mutation_applied = False
_mutation_selected_tests: list[str] | None = None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to report a portable result if its target was never imported."""
    if not os.environ.get("VFY_MUTATION") or _mutation_applied:
        return
    session.exitstatus = 3
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = "vfy: mutation target was not imported by the selected tests"
    if reporter is None:
        sys.stderr.write(f"{message}\n")
    else:
        reporter.write_line(message, red=True)


__all__ = ["MutationSpec", "install_from_environment"]
