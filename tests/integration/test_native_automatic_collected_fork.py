"""Explicit native workers import and collect once when the run can fork a snapshot."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)

_UNSET_ROUTE_ENV: dict[str, str | None] = {
    "VFY_WORKER_START": None,
    "VFY_WORKER_COLLECTION": None,
    "VFY_NATIVE_SCHEDULING": None,
}

_MODULE_PREAMBLE = """\
import os
import threading

import pytest

with open("imports.log", "a") as stream:
    stream.write(f"{os.getpid()}\\n")
@HAZARD@
def ran():
    with open("ran.log", "a") as stream:
        stream.write(f"{os.getpid()}\\n")

"""


class _ImportHazard(Enum):
    """State a project leaves behind at import."""

    NONE = ""
    # Forking an initialized interpreter with a live thread is unsafe.
    THREAD = "threading.Thread(target=threading.Event().wait, daemon=True).start()\n"
    # An open file belongs to no one the supervisor can identify.
    DESCRIPTOR = "held = open('held.txt', 'w')\n"
    # CPython caches this descriptor without getrandom(2); the interpreter owns it.
    URANDOM = "os.urandom(8)\n"


@dataclass(frozen=True)
class _Route:
    """Command options for one route and the processes that import the project."""

    arguments: tuple[str, ...]
    importing_processes: int
    workers_import: bool
    hazard: _ImportHazard = _ImportHazard.NONE
    env: Mapping[str, str | None] = field(default_factory=dict)
    coverage_plugin: bool = False


_ROUTES = {
    # One controller import; the workers inherit it.
    "fork": _Route(("--no-cov", "--capture=sys"), 1, workers_import=False),
    # Each of the two fresh workers imports the project.
    "exec": _Route(
        ("--no-cov", "--capture=sys"),
        2,
        workers_import=True,
        env={"VFY_WORKER_START": "exec"},
    ),
    # Pytest's default fd capture and active coverage fork like any other run.
    "fd-capture": _Route(("--no-cov",), 1, workers_import=False),
    "coverage": _Route(("--capture=sys",), 1, workers_import=False),
    "default": _Route((), 1, workers_import=False),
    # A coverage plug-in keeps its tracer state in the interpreter that imported it,
    # which a forked worker's fresh collector cannot own.
    "coverage-plugin": _Route(
        ("--cov-config=.coveragerc",), 3, workers_import=True, coverage_plugin=True
    ),
    # The controller imports, then fresh workers import again.
    "thread": _Route(
        ("--no-cov", "--capture=sys"), 3, workers_import=True, hazard=_ImportHazard.THREAD
    ),
    "descriptor": _Route(
        ("--no-cov", "--capture=sys"), 3, workers_import=True, hazard=_ImportHazard.DESCRIPTOR
    ),
    "interpreter-descriptor": _Route(
        ("--no-cov", "--capture=sys"), 1, workers_import=False, hazard=_ImportHazard.URANDOM
    ),
}


def _write_project(
    project: VfyProject, hazard: _ImportHazard, *, coverage_plugin: bool = False
) -> None:
    if coverage_plugin:
        project.write(
            "noop_plugin.py",
            "class Plugin: pass\n"
            "def coverage_init(registration, options):\n"
            "    registration.add_noop(Plugin())\n",
        )
        project.write(".coveragerc", "[run]\nplugins = noop_plugin\n")
    preamble = _MODULE_PREAMBLE.replace("@HAZARD@\n", hazard.value)
    project.write(
        "tests/test_first.py",
        preamble + "def test_pass():\n    ran()\n\n"
        "def test_fail():\n    ran()\n    assert 1 == 2, 'failure sentinel'\n\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_param(value):\n    ran()\n    assert value\n",
    )
    project.write(
        "tests/test_second.py",
        preamble + "def test_skip():\n    ran()\n    pytest.skip('skip sentinel')\n\n"
        "@pytest.mark.xfail(reason='expected', strict=False)\n"
        "def test_xfail():\n    ran()\n    assert False\n\n"
        "def test_last():\n    ran()\n",
    )


def _records(path: Path) -> list[tuple[str | None, str, tuple[tuple[str, str | None], ...]]]:
    return sorted(
        (
            case.attrib.get("classname"),
            case.attrib["name"],
            tuple(
                (entry.tag, entry.attrib.get("message"))
                for entry in case
                if entry.tag in {"failure", "error", "skipped"}
            ),
        )
        for case in ET.parse(path).getroot().iter("testcase")
    )


def _pids(path: Path) -> set[str]:
    return set(path.read_text().split()) if path.exists() else set()


def _run_native(
    project: VfyProject, arguments: tuple[str, ...], env: Mapping[str, str | None]
) -> CommandResult:
    return project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--history=history.json",
        "--junitxml=native.xml",
        "-q",
        *arguments,
        env={**_UNSET_ROUTE_ENV, **env},
    )


@pytest.mark.parametrize("name", list(_ROUTES))
def test_explicit_workers_import_once_only_when_the_run_can_fork(tmp_path: Path, name: str) -> None:
    route = _ROUTES[name]
    native = VfyProject(tmp_path / "native")
    reference = VfyProject(tmp_path / "reference")
    for project in (native, reference):
        _write_project(project, route.hazard, coverage_plugin=route.coverage_plugin)

    stock = reference.run_pytest("-q", "-p", "no:cacheprovider", "--junitxml=stock.xml")
    completed = _run_native(native, route.arguments, route.env)

    stock.expect(1)
    assert completed.returncode == stock.returncode, completed.stdout + completed.stderr
    assert _records(native.root / "native.xml") == _records(reference.root / "stock.xml")
    imports = _pids(native.root / "imports.log")
    ran = _pids(native.root / "ran.log")
    assert len(ran) == 2, "the suite must run in two worker processes"
    assert len(imports) == route.importing_processes, (imports, ran)
    assert imports.isdisjoint(ran) is not route.workers_import, (imports, ran)


@pytest.mark.parametrize(
    ("hazard", "diagnostic"),
    [
        (_ImportHazard.THREAD, "collected fork workers reject threads, tracing and profiling"),
        (_ImportHazard.DESCRIPTOR, "collected fork workers reject unowned open descriptor"),
    ],
)
def test_explicit_fork_request_rejects_the_run_the_default_route_falls_back_from(
    tmp_path: Path, hazard: _ImportHazard, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    _write_project(project, hazard)

    completed = _run_native(
        project,
        ("--no-cov", "--capture=sys"),
        {"VFY_WORKER_START": "fork"},
    )

    completed.expect(4)
    assert diagnostic in completed.stderr
    assert not (project.root / "ran.log").exists()


def test_explicit_fork_request_rejects_a_coverage_plugin(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _write_project(project, _ImportHazard.NONE, coverage_plugin=True)

    completed = _run_native(project, ("--cov-config=.coveragerc",), {"VFY_WORKER_START": "fork"})

    completed.expect(4)
    assert "without plug-ins or an enclosing session" in completed.stderr
    assert not (project.root / "ran.log").exists()


def test_automatic_fork_shuts_down_retained_thread_pools_like_fresh_workers(tmp_path: Path) -> None:
    for route in ("pytest", "exec", "automatic"):
        project = VfyProject(tmp_path / route, timeout=30)
        for name in ("first", "second"):
            project.write(
                f"test_{name}.py",
                "import os\n"
                "import threading\n"
                "from concurrent.futures import ThreadPoolExecutor\n"
                "from pathlib import Path\n"
                "executors = []\n"
                "with Path('imports.log').open('a') as stream:\n"
                "    stream.write(f'{os.getpid()}\\n')\n"
                "def test_pool():\n"
                "    threading.Thread(target=threading.Event().wait, daemon=True).start()\n"
                "    executor = ThreadPoolExecutor(1)\n"
                "    executors.append(executor)\n"
                "    assert executor.submit(lambda: 42).result() == 42\n"
                f"    Path('ran-{name}').write_text(str(os.getpid()))\n",
            )
        completed = (
            project.run_pytest("-n", "2", "-q")
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env={
                    **_UNSET_ROUTE_ENV,
                    "VFY_WORKER_START": "exec" if route == "exec" else None,
                },
            )
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "2 passed" in completed.stdout
        if route != "pytest":
            workers = {(project.root / f"ran-{name}").read_text() for name in ("first", "second")}
            assert len(workers) == 2
            imported = _pids(project.root / "imports.log")
            if route == "automatic":
                assert len(imported) == 1
                assert imported.isdisjoint(workers)
            else:
                assert imported == workers
