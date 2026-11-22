"""Forked workers capture each test's output exactly as stock pytest does."""

from __future__ import annotations

import os
import signal
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from contextlib import suppress
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

_JUNIT = (
    "--junitxml=results.xml",
    "-o",
    "junit_logging=all",
    "-o",
    "junit_log_passing_tests=true",
    "-o",
    "junit_family=xunit1",
)

# Each test writes through a different layer: sys streams, a stream retained from
# collection, raw descriptors, and a descendant process. Output at import time
# belongs to collection and must not reach any test.
_CAPTURE_MODULE = """\
import os
import subprocess
import sys

import pytest

with open("imports.log", "a") as stream:
    stream.write(f"{os.getpid()}\\n")

retained = sys.stderr
print("import-time stdout")
os.write(1, b"import-time fd\\n")


def ran():
    with open("ran.log", "a") as stream:
        stream.write(f"{os.getpid()}\\n")


def test_passing_layers():
    ran()
    print("sys stdout")
    print("sys stderr", file=sys.stderr)
    retained.write("retained stderr\\n")
    os.write(1, b"fd stdout\\n")
    os.write(2, b"fd stderr\\n")
    subprocess.run([sys.executable, "-c", "print('descendant stdout')"], check=True)


def test_failing_layers():
    ran()
    print("failing stdout")
    os.write(2, b"failing fd stderr\\n")
    assert False, "failure sentinel"


def test_skipping_layers():
    ran()
    print("skipping stdout")
    os.write(1, b"skipping fd stdout\\n")
    pytest.skip("skip sentinel")


@pytest.fixture
def noisy():
    print("setup stdout")
    os.write(2, b"setup fd stderr\\n")
    yield
    print("teardown stdout")
    os.write(1, b"teardown fd stdout\\n")


@pytest.mark.parametrize("value", range(4))
def test_fixture_phases(noisy, value):
    ran()
    os.write(1, f"body {value}\\n".encode() * 200)
"""


def _write_project(project: VfyProject) -> None:
    project.write("tests/test_capture.py", _CAPTURE_MODULE)
    project.write("tests/test_other.py", _CAPTURE_MODULE)


def _junit_records(project: VfyProject) -> list[tuple[object, ...]]:
    """Keep every testcase child, including captured text, and drop only the checkout path."""

    def normalize(value: str | None) -> str:
        return (value or "").replace(str(project.root), "<PROJECT>")

    return sorted(
        (
            case.attrib.get("classname"),
            case.attrib["name"],
            tuple(
                (
                    child.tag,
                    tuple(sorted((key, normalize(value)) for key, value in child.attrib.items())),
                    normalize(child.text),
                )
                for child in case
            ),
        )
        for case in ET.parse(project.root / "results.xml").getroot().iter("testcase")
    )


def _pids(path: Path) -> set[str]:
    return set(path.read_text().split()) if path.exists() else set()


def _run_vfy(
    project: VfyProject, arguments: tuple[str, ...], env: Mapping[str, str | None]
) -> CommandResult:
    return project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--history=history.json",
        "-q",
        *_JUNIT,
        *arguments,
        env={**_UNSET_ROUTE_ENV, **env},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param((), id="default-capture-with-coverage"),
        pytest.param(("--no-cov",), id="fd"),
        pytest.param(("--no-cov", "--capture=sys"), id="sys"),
        pytest.param(("--no-cov", "-s"), id="s"),
        pytest.param(("--no-cov", "--capture=no"), id="no"),
    ],
)
def test_forked_workers_capture_output_per_test_like_stock_pytest(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    native = VfyProject(tmp_path / "native")
    reference = VfyProject(tmp_path / "reference")
    for project in (native, reference):
        _write_project(project)
    stock_capture = tuple(argument for argument in arguments if argument != "--no-cov")

    stock = reference.run_pytest("-q", "-p", "no:cacheprovider", *_JUNIT, *stock_capture)
    completed = _run_vfy(native, arguments, {})

    stock.expect(1)
    assert completed.returncode == stock.returncode, completed.stdout + completed.stderr
    imports = _pids(native.root / "imports.log")
    ran = _pids(native.root / "ran.log")
    assert len(ran) == 2, "the suite must run in two worker processes"
    assert len(imports) == 1, "the controller imports once and the workers inherit it"
    assert imports.isdisjoint(ran)
    assert _junit_records(native) == _junit_records(reference)


def test_forked_workers_render_passing_captures_like_stock_pytest(tmp_path: Path) -> None:
    native = VfyProject(tmp_path / "native")
    reference = VfyProject(tmp_path / "reference")
    for project in (native, reference):
        _write_project(project)

    stock = reference.run_pytest("-p", "no:cacheprovider", "-rP", "-q")
    completed = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-rP",
        "-q",
        env=_UNSET_ROUTE_ENV,
    )

    def passes(output: str) -> list[str]:
        lines = output.splitlines()
        start = next(index for index, line in enumerate(lines) if " PASSES " in line) + 1
        # The last line is the result summary, which carries timing.
        end = next(
            (index for index in range(start, len(lines)) if lines[index].startswith("==")),
            len(lines) - 1,
        )
        return lines[start:end]

    assert completed.returncode == stock.returncode == 1, completed.stdout + completed.stderr
    assert passes(completed.stdout) == passes(stock.stdout)
    assert len(_pids(native.root / "imports.log")) == 1


@pytest.mark.parametrize("start", ["fork", "exec"])
def test_interrupt_leaves_no_workers_and_exits_alike_on_both_routes(
    tmp_path: Path, start: str
) -> None:
    project = VfyProject(tmp_path)
    for module in range(2):
        project.write(
            f"tests/test_blocked_{module}.py",
            f"""\
import os
import signal
from pathlib import Path

with open("imports.log", "a") as stream:
    stream.write(f"{{os.getpid()}}\\n")


def test_blocked():
    Path("worker.{module}").write_text(str(os.getpid()))
    os.write(2, b"blocked in a worker\\n")
    signal.pause()
""",
        )
    command = project.start_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "-q",
        env={**_UNSET_ROUTE_ENV, "VFY_WORKER_START": None if start == "fork" else "exec"},
    )
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 20
        while len({path.read_text() for path in tmp_path.glob("worker.*")}) < 2:
            assert time.monotonic() < deadline, "two workers must block in a test"
            time.sleep(0.05)
        pids = sorted(int(path.read_text()) for path in tmp_path.glob("worker.*"))
        os.kill(command.process.pid, signal.SIGINT)
        completed = command.wait(timeout=20)
        completed.expect(2)
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert len(_pids(tmp_path / "imports.log")) == (1 if start == "fork" else 2)
    finally:
        if command.poll() is None:
            command.terminate()
        for pid in pids:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
