"""`[run] concurrency = multiprocessing` through vfy's public entry points."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

import vfy
from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

START_METHODS = [
    pytest.param(
        method,
        marks=pytest.mark.skipif(
            method not in multiprocessing.get_all_start_methods(),
            reason=f"{method} is unavailable on this platform",
        ),
    )
    for method in ("fork", "forkserver", "spawn")
]

WORK = """\
def child(value):
    if value % 2:
        return value * value
    return -value


def parent():
    return "parent"
"""

PROGRAM = """\
import multiprocessing
import sys

from pkg.work import child, parent

if __name__ == "__main__":
    context = multiprocessing.get_context(sys.argv[1])
    process = context.Process(target=child, args=(3,))
    process.start()
    process.join()
    assert process.exitcode == 0
    print(parent())
"""

CONFIG = """\
[tool.coverage.run]
branch = true
concurrency = ["multiprocessing"]
source = ["pkg"]
"""


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("pkg/__init__.py", "")
    project.write("pkg/work.py", WORK)
    project.write("program.py", PROGRAM)
    project.write("pyproject.toml", CONFIG)
    return project


def _shards(root: Path) -> list[Path]:
    return sorted(root.glob(".coverage.*"))


@pytest.mark.parametrize("method", START_METHODS)
def test_run_measures_multiprocessing_children_like_coverage_py(
    tmp_path: Path, method: str
) -> None:
    reference = _project(tmp_path / "reference")
    actual = _project(tmp_path / "actual")

    for result in (
        reference.run_coverage_py("run", "program.py", method),
        actual.run_vfy("coverage", "run", "program.py", method),
    ):
        result.expect(0)
        assert result.stdout == "parent\n"

    # The parent and the child each write a process-specific shard.
    assert len(_shards(reference.root)) == len(_shards(actual.root)) == 2
    assert not (actual.root / ".coverage").exists()

    for project, combine in (
        (reference, reference.run_coverage_py),
        (actual, actual.run_vfy),
    ):
        arguments = ("combine",) if project is reference else ("coverage", "combine")
        combined = combine(*arguments)
        combined.expect(0)

    expected_lines = reference.lines("pkg/work.py")
    assert actual.lines("pkg/work.py") == expected_lines
    assert expected_lines is not None
    assert {2, 3} <= set(expected_lines)
    assert sorted(actual.arcs("pkg/work.py") or ()) == sorted(reference.arcs("pkg/work.py") or ())


@pytest.mark.parametrize("method", START_METHODS)
def test_pytest_plugin_combines_multiprocessing_children(tmp_path: Path, method: str) -> None:
    project = _project(tmp_path)
    project.write(
        "tests/test_child.py",
        "import multiprocessing\n"
        "\n"
        "from pkg.work import child, parent\n"
        "\n"
        "\n"
        "def test_child():\n"
        f"    context = multiprocessing.get_context({method!r})\n"
        "    process = context.Process(target=child, args=(3,))\n"
        "    process.start()\n"
        "    process.join()\n"
        "    assert process.exitcode == 0\n"
        "    assert parent() == 'parent'\n",
    )

    result = project.run_pytest("-q", "-p", "no:cacheprovider", "--cov=pkg", "tests")

    result.expect(0)
    # Helper interpreters outlive pytest and must not leave a shard behind.
    assert _shards(project.root) == []
    lines = project.lines("pkg/work.py")
    assert lines is not None
    assert {2, 3, 8} <= set(lines)
    assert 4 not in lines


@requires_native_pytest
@pytest.mark.parametrize("method", START_METHODS)
def test_default_runner_stages_multiprocessing_without_measuring_ordinary_subprocesses(
    tmp_path: Path, method: str
) -> None:
    project = _project(tmp_path)
    project.write("pkg/unrelated.py", "from pathlib import Path\nPath('ordinary-ran').touch()\n")
    project.write(
        "tests/test_child.py",
        "import multiprocessing, subprocess, sys\n"
        "\n"
        "from pkg.work import child\n"
        "\n"
        "\n"
        "def test_child():\n"
        f"    process = multiprocessing.get_context({method!r}).Process(target=child, args=(3,))\n"
        "    process.start()\n"
        "    process.join()\n"
        "    assert process.exitcode == 0\n"
        "    subprocess.run([sys.executable, '-m', 'pkg.unrelated'], check=True)\n",
    )

    result = project.run_vfy("test", "--all", "--cov=pkg", "tests")

    result.expect(0)
    assert (project.root / "ordinary-ran").is_file()
    assert not project.lines("pkg/unrelated.py")
    assert _shards(project.root) == []
    lines = project.lines("pkg/work.py")
    assert lines is not None
    assert {2, 3} <= set(lines)


@pytest.mark.parametrize("method", ["spawn", "forkserver"])
@pytest.mark.parametrize("flags", [(), ("-S",), ("-I",)])
def test_default_runner_preserves_multiprocessing_interpreter_startup(
    tmp_path: Path, method: str, flags: tuple[str, ...]
) -> None:
    if method not in multiprocessing.get_all_start_methods():
        pytest.skip(f"{method} is unavailable on this platform")
    records = {}
    for route in ("stock", "candidate"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "startup/sitecustomize.py",
            "import sys\n"
            "STARTUP = {'warnoptions': sys.warnoptions[:], 'no_site': sys.flags.no_site}\n",
        )
        project.write(
            "child_target.py",
            """import json, sys, warnings
from pathlib import Path

def child():
    try:
        warnings.warn('multiprocessing startup policy', UserWarning)
    except UserWarning:
        warning_is_error = True
    else:
        warning_is_error = False
    startup = sys.modules.get('sitecustomize')
    Path('spawned.json').write_text(json.dumps({
        'startup': getattr(startup, 'STARTUP', None),
        'warnoptions': sys.warnoptions[:],
        'warning_is_error': warning_is_error,
        'flags': [sys.flags.isolated, sys.flags.ignore_environment,
                  sys.flags.no_site, sys.flags.safe_path],
    }))
""",
        )
        project.write(
            "test_child.py",
            "import multiprocessing\nfrom child_target import child\n"
            "def test_child():\n"
            f"    process = multiprocessing.get_context({method!r}).Process(target=child)\n"
            "    process.start()\n"
            "    try:\n        process.join(10)\n        assert process.exitcode == 0\n"
            "    finally:\n        if process.is_alive():\n"
            "            process.kill()\n            process.join(5)\n",
        )
        import_paths = dict.fromkeys(
            (
                str(project.root / "startup"),
                str(Path(vfy.__file__).resolve().parents[1]),
                str(Path(pytest.__file__).resolve().parents[1]),
            )
        )
        environment = {
            "PYTHONPATH": os.pathsep.join(import_paths),
            "PYTHONWARNINGS": "error::UserWarning",
        }
        command = (
            ("pytest", "-q")
            if route == "stock"
            else ("vfy", "test", "--all", "--workers=2", "--no-cov")
        )
        result = project.run_python(*flags, "-m", *command, env=environment)
        result.expect(0)
        records[route] = json.loads((project.root / "spawned.json").read_text())
    assert (records["stock"]["startup"] is not None) is (not flags)
    assert records["candidate"] == records["stock"]


@pytest.mark.parametrize("method", ["forkserver", "spawn"])
def test_pytest_plugin_leaves_no_helper_shards_without_concurrency(
    tmp_path: Path, method: str
) -> None:
    if method not in multiprocessing.get_all_start_methods():
        pytest.skip(f"{method} is unavailable on this platform")
    project = _project(tmp_path)
    project.write("pyproject.toml", '[tool.coverage.run]\nsource = ["pkg"]\n')
    project.write(
        "tests/test_child.py",
        "import multiprocessing\n"
        "\n"
        "from pkg.work import child\n"
        "\n"
        "\n"
        "def test_child():\n"
        f"    process = multiprocessing.get_context({method!r}).Process(target=child, args=(3,))\n"
        "    process.start()\n"
        "    process.join()\n"
        "    assert process.exitcode == 0\n",
    )

    result = project.run_pytest("-q", "-p", "no:cacheprovider", "--cov=pkg", "tests")

    result.expect(0)
    assert _shards(project.root) == []


@pytest.mark.parametrize("method", START_METHODS)
def test_api_without_configuration_measures_nested_multiprocessing_children(
    tmp_path: Path, method: str
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("nested fork is unavailable on this platform")
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write(
        "pkg/work.py",
        "import multiprocessing\n"
        "\n"
        "def leaf():\n"
        "    return 42\n"
        "\n"
        "def child():\n"
        "    process = multiprocessing.get_context('fork').Process(target=leaf)\n"
        "    process.start()\n"
        "    process.join()\n"
        "    assert process.exitcode == 0, process.exitcode\n",
    )
    project.write(
        "program.py",
        "import multiprocessing\n"
        "from vfy import Coverage\n"
        "from pkg.work import child\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    coverage = Coverage(config_file=False, concurrency=['multiprocessing'], "
        "source=['pkg'])\n"
        "    coverage.start()\n"
        f"    process = multiprocessing.get_context({method!r}).Process(target=child)\n"
        "    process.start()\n"
        "    process.join()\n"
        "    assert process.exitcode == 0, process.exitcode\n"
        "    coverage.stop()\n"
        "    coverage.save()\n",
    )

    result = project.run_python("program.py")

    result.expect(0)
    assert len(_shards(project.root)) == 3
    combined = project.run_vfy("coverage", "combine")
    combined.expect(0)
    lines = project.lines("pkg/work.py")
    assert lines is not None
    assert {4, 7, 8, 9, 10} <= set(lines)
