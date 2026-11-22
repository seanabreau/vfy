"""Independent execution keeps the shared controller's resources and publications."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution is unavailable on Windows"
)


@pytest.mark.parametrize("verbose", [False, True])
def test_independent_controller_keeps_plan_notices_in_verbose_diagnostics(
    tmp_path: Path, verbose: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
    )

    result = project.run_vfy(
        *(("--verbose",) if verbose else ()), "test", "--engine=native", "--no-cov", "-q"
    )

    result.expect(0)
    assert (tmp_path / "body-ran").is_file()
    assert "1 passed" in result.stdout
    if verbose:
        assert "Running the full suite" in result.stderr
        assert "Backend: native" in result.stderr
    else:
        assert result.stderr == ""


@pytest.mark.parametrize(
    ("worker_source", "expected"),
    [
        ("default", 1),
        ("cli", 2),
        ("vfy-config", 2),
        ("pytest-config", 2),
        ("environment", 2),
        ("argument-file", 2),
        ("cli-over-environment", 1),
        ("vfy-over-environment", 1),
    ],
)
def test_independent_controller_preserves_worker_argument_provenance(
    tmp_path: Path, worker_source: str, expected: int
) -> None:
    project = VfyProject(tmp_path)
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os\nfrom pathlib import Path\n"
            f"def test_{name}():\n    Path('{name}.pid').write_text(str(os.getpid()))\n",
        )
    arguments = []
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    if worker_source == "cli":
        arguments.append("--workers=2")
    elif worker_source == "vfy-config":
        project.write("pyproject.toml", "[tool.vfy.test]\nworkers = 2\n")
    elif worker_source == "pytest-config":
        project.write("pytest.ini", "[pytest]\naddopts = --workers=2\n")
    elif worker_source == "argument-file":
        project.write("workers.txt", "--workers=2\n")
        arguments.append("@workers.txt")
    elif worker_source in {"environment", "cli-over-environment", "vfy-over-environment"}:
        environment["PYTEST_ADDOPTS"] = "--workers=2"
        if worker_source == "cli-over-environment":
            arguments.append("--workers=1")
        elif worker_source == "vfy-over-environment":
            project.write("pyproject.toml", "[tool.vfy.test]\nworkers = 1\n")

    result = project.run_vfy(
        "test",
        "--engine=native",
        *arguments,
        "--no-cov",
        "--native-result-json=actual.json",
        env=environment,
    )

    result.expect(0)
    record = json.loads((tmp_path / "actual.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    assert record["execution_topology"]["requested_workers"] == expected
    assert [case["nodeid"] for case in record["cases"]] == [
        "test_a.py::test_a",
        "test_b.py::test_b",
    ]
    assert record["counts"]["passed"] == 2
    assert len({(tmp_path / f"{name}.pid").read_text() for name in ("a", "b")}) == expected


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("full", [False, True])
def test_default_no_cov_keeps_corrupt_replay_inputs_unread_and_records_real_history(
    tmp_path: Path, workers: int, full: bool
) -> None:
    project = VfyProject(tmp_path)
    previous = {
        path: project.write(path, "corrupt prior coverage\n").read_bytes()
        for path in (".coverage", ".vfy/replay.coverage")
    }
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "from pathlib import Path\nimport pytest\n"
            "@pytest.fixture\ndef actual():\n    yield\n"
            f"    Path('{name}.finalized').touch()\n"
            f"def test_{name}(actual):\n    Path('{name}.ran').touch()\n",
        )
    probe = project.write(
        "probe/sitecustomize.py",
        "from pathlib import Path\nimport vfy.native as native\n"
        "import vfy.dependency_fingerprint as dependencies\n"
        "from vfy.data import CoverageStore\n"
        "def forbidden(*args, **kwargs):\n"
        "    Path('unexpected-replay-read').touch()\n"
        "    raise AssertionError('disabled coverage read a replay input')\n"
        "native.git_replay_state = forbidden\n"
        "native.plan_test_selection = forbidden\n"
        "dependencies._compute = forbidden\n"
        "CoverageStore.read = classmethod(forbidden)\n"
        "Path('probe-installed').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        *(("--all",) if full else ()),
        f"--workers={workers}",
        "--no-cov",
        env={"PYTHONPATH": str(probe.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)
    assert (tmp_path / "probe-installed").is_file()
    assert not (tmp_path / "unexpected-replay-read").exists()
    assert {path: (tmp_path / path).read_bytes() for path in previous} == previous
    assert "Reused" not in result.stdout + result.stderr
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    for name in ("a", "b"):
        assert (tmp_path / f"{name}.ran").is_file()
        assert (tmp_path / f"{name}.finalized").is_file()
        observation = history["tests"][f"test_{name}.py::test_{name}"]["observations"][-1]
        assert observation["outcome"] == "passed"


@pytest.mark.parametrize("scope", [("test_chosen.py",), ("-k", "chosen")])
def test_default_coverage_and_history_preserve_the_requested_scope(
    tmp_path: Path, scope: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value():\n    return 7\n")
    project.write(
        "test_chosen.py",
        "from pathlib import Path\nfrom subject import value\n"
        "def test_chosen():\n    assert value() == 7\n    Path('chosen').touch()\n",
    )
    project.write(
        "test_other.py",
        "from pathlib import Path\ndef test_other():\n"
        "    Path('other').touch()\n    assert False\n",
    )

    result = project.run_vfy("test", "--engine=native", "--source=subject.py", *scope)

    result.expect(0)
    assert (tmp_path / "chosen").is_file()
    assert not (tmp_path / "other").exists()
    assert project.lines("subject.py") == [1, 2]
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    observation = history["tests"]["test_chosen.py::test_chosen"]["observations"][-1]
    assert observation["outcome"] == "passed"


@pytest.mark.parametrize("plugin", ["pytest_asyncio", "pytest_asyncio.plugin"])
def test_independent_controller_keeps_the_asyncio_alias_owner(tmp_path: Path, plugin: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n",
    )
    project.write(
        "test_async.py",
        "import asyncio\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture\nasync def actual():\n"
        "    await asyncio.sleep(0)\n    yield 7\n    Path('finalized').touch()\n"
        "async def test_actual(actual):\n"
        "    await asyncio.sleep(0)\n    assert actual == 7\n    Path('awaited').touch()\n",
    )

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        plugin,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)
    assert (tmp_path / "awaited").is_file()
    assert (tmp_path / "finalized").is_file()


def test_independent_unknown_target_keeps_its_usage_error_without_fake_history(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_actual.py",
        "from pathlib import Path\ndef test_actual():\n    Path('ran').touch()\n",
    )

    result = project.run_vfy("test", "--engine=native", "--no-cov", "missing.py")

    result.expect(4)
    assert "test target does not exist: missing.py" in result.stdout + result.stderr
    assert "Test result evidence could not be recorded" not in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()
    assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
