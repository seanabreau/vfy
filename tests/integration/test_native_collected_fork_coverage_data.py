"""Forked workers publish coverage that equals fresh workers and a serial run."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)

_UNSET_ROUTE_ENV: dict[str, str | None] = {
    "VFY_WORKER_START": None,
    "VFY_WORKER_COLLECTION": None,
    "VFY_NATIVE_SCHEDULING": None,
}

_APP = """\
def classify(value):
    if value > 1:
        return "big"
    return "small"


def twice(value):
    return classify(value) + classify(value - 5)


def from_subprocess():
    return classify(0)
"""

# Seven tests across two modules give each of two workers an assignment. Import
# time (module bodies, decorators) belongs to collection. Only one test imports the
# lazy module: when several workers import it, each credits its own first test, so
# the context of its body would depend on the assignment.
_TESTS = """\
import os
import subprocess
import sys

import pytest

import app

with open("imports.log", "a") as stream:
    stream.write(f"{os.getpid()}\\n")


def ran():
    with open("ran.log", "a") as stream:
        stream.write(f"{os.getpid()}\\n")


def test_big():
    ran()
    assert app.classify(9) == "big"


def test_twice():
    ran()
    assert app.twice(3) == "bigsmall"


def test_descendant_process():
    ran()
    subprocess.run(
        [sys.executable, "-c", "import app; assert app.from_subprocess() == 'small'"],
        check=True,
    )
"""

_LAZY_TEST = """

def test_lazy_import():
    ran()
    import lazy

    assert lazy.VALUE == 1
"""


@dataclass(frozen=True)
class _Measured:
    lines: dict[str, list[int]]
    arcs: dict[str, list[tuple[int, int]]]
    contexts: dict[str, dict[int, list[str]]]
    report: dict[str, object]


def _write_project(project: VfyProject) -> None:
    project.write("app.py", _APP)
    project.write("lazy.py", "VALUE = 1\n\n\ndef unused():\n    return VALUE\n")
    project.write("tests/test_first.py", _TESTS + _LAZY_TEST)
    project.write("tests/test_second.py", _TESTS.replace("def test_", "def test_second_"))


def _measure(project: VfyProject, route: str, env: Mapping[str, str | None]) -> _Measured:
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={1 if route == 'serial' else 2}",
        "--no-replay",
        "--cov=app",
        "--cov=lazy",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:coverage.json",
        "-q",
        env={**_UNSET_ROUTE_ENV, **env},
    )
    completed.expect(0)
    assert "7 passed" in completed.stdout, completed.stdout
    data = project.data()
    root = str(project.root) + "/"
    files = sorted(data.measured_files())
    report = json.loads((project.root / "coverage.json").read_text())
    return _Measured(
        lines={path.removeprefix(root): sorted(data.lines(path) or []) for path in files},
        arcs={path.removeprefix(root): sorted(data.arcs(path) or []) for path in files},
        contexts={
            path.removeprefix(root): {
                line: sorted(contexts) for line, contexts in data.contexts_by_lineno(path).items()
            }
            for path in files
        },
        report={
            "totals": report["totals"],
            "files": {
                name: file["summary"] | {"missing": file["missing_lines"]}
                for name, file in report["files"].items()
            },
        },
    )


def test_forked_workers_publish_the_coverage_of_fresh_workers_and_a_serial_run(
    tmp_path: Path,
) -> None:
    routes: dict[str, tuple[str, Mapping[str, str | None]]] = {
        "serial": ("serial", {}),
        "exec": ("parallel", {"VFY_WORKER_START": "exec"}),
        "fork": ("parallel", {}),
    }
    measured: dict[str, _Measured] = {}
    imports: dict[str, set[str]] = {}
    ran: dict[str, set[str]] = {}
    for name, (route, env) in routes.items():
        project = VfyProject(tmp_path / name)
        _write_project(project)
        measured[name] = _measure(project, route, env)
        imports[name] = set((project.root / "imports.log").read_text().split())
        ran[name] = set((project.root / "ran.log").read_text().split())
        assert not list(project.root.glob(".vfy-native-child-*")), "staging must be removed"
        assert not list(project.root.glob(".coverage.*")), "no shard may remain"

    # Prove the routes differ in process shape before comparing their data.
    assert len(ran["serial"]) == 1
    assert len(ran["exec"]) == len(ran["fork"]) == 2
    assert len(imports["fork"]) == 1, "the fork route imports once in the controller"
    assert imports["fork"].isdisjoint(ran["fork"])
    assert len(imports["exec"]) == 2, "fresh workers import the project themselves"

    expected = measured["serial"]
    assert set(expected.lines) == {"app.py", "lazy.py"}
    assert measured["exec"] == expected
    assert measured["fork"] == expected
    # The subprocess and the lazy import are exercised in every route.
    assert 11 in expected.lines["app.py"]
    assert any(
        context.endswith("test_lazy_import|run") for context in expected.contexts["lazy.py"][1]
    )
