"""Register doctest options and defer collectors until a file matches."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .facade import fixture, hookimpl

if TYPE_CHECKING:
    from .protocol import Collector, Config, File, Parser


@fixture(scope="session")
def doctest_namespace() -> dict[str, object]:
    """Return the mutable namespace shared by doctests in this session."""
    return {}


class DoctestProvider:
    """Register doctest options and the namespace fixture; collect only when enabled."""

    doctest_namespace = staticmethod(doctest_namespace)

    @hookimpl
    def pytest_addoption(self, parser: Parser) -> None:
        parser.addini(
            "doctest_optionflags", "Option flags for doctests", type="args", default=["ELLIPSIS"]
        )
        parser.addini("doctest_encoding", "Encoding used for doctest files", default="utf-8")
        group = parser.getgroup("collect")
        group.addoption(
            "--doctest-modules",
            action="store_true",
            default=False,
            help="Run doctests in all .py modules",
            dest="doctestmodules",
        )
        group.addoption(
            "--doctest-report",
            type=str.lower,
            default="udiff",
            help="Choose another output format for diffs on doctest failure",
            choices=("none", "cdiff", "ndiff", "udiff", "only_first_failure"),
            dest="doctestreport",
        )
        group.addoption(
            "--doctest-glob",
            action="append",
            default=[],
            metavar="pat",
            help="Doctests file matching pattern, default: test*.txt",
            dest="doctestglob",
        )
        group.addoption(
            "--doctest-ignore-import-errors",
            action="store_true",
            default=False,
            help="Ignore doctest collection errors",
            dest="doctest_ignore_import_errors",
        )
        group.addoption(
            "--doctest-continue-on-failure",
            action="store_true",
            default=False,
            help="For a given doctest, continue to run after the first failure",
            dest="doctest_continue_on_failure",
        )

    @hookimpl
    def pytest_configure(self, config: Config) -> None:
        # The default test*.txt glob makes text doctests collectible in every run.
        config.pluginmanager.register(DoctestCollection(), "doctest-collection")


class DoctestCollection:
    """Offer doctest collectors for Python modules and matching text files."""

    @hookimpl
    def pytest_collect_file(self, file_path: Path, parent: Collector) -> File | None:
        config = parent.config
        if file_path.suffix == ".py":
            if (
                config.option.doctestmodules
                and not _is_setup_py(file_path)
                and file_path.name != "__main__.py"
            ):
                from .doctest_support import (  # noqa: PLC0415 - matching collector only
                    DoctestModule,
                )

                return DoctestModule.from_parent(parent, path=file_path)
        elif _is_doctest(config, file_path, parent):
            from .doctest_support import (  # noqa: PLC0415 - matching collector only
                DoctestTextfile,
            )

            return DoctestTextfile.from_parent(parent, path=file_path)
        return None


def _is_setup_py(path: Path) -> bool:
    if path.name != "setup.py":
        return False
    contents = path.read_bytes()
    return b"setuptools" in contents or b"distutils" in contents


def _is_doctest(config: Config, path: Path, parent: Collector) -> bool:
    if path.suffix in (".txt", ".rst") and parent.session.isinitpath(path):
        return True
    globs = cast("Iterable[str]", config.getoption("doctestglob") or ["test*.txt"])
    return any(_fnmatch_ex(glob, path) for glob in globs)


def _fnmatch_ex(pattern: str, path: Path) -> bool:
    """Match like pytest: a pattern without a separator matches the file name."""
    name = path.name if "/" not in pattern else str(path)
    if "/" in pattern and not Path(pattern).is_absolute():
        pattern = f"*/{pattern}"
    return fnmatch.fnmatch(name, pattern)
