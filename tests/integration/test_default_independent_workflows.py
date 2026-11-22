"""The independent engine runs vfy execution even when pytest is installed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution is unavailable on Windows"
)
@pytest.mark.parametrize("fails", [False, True])
def test_independent_engine_records_actual_outcome_and_finalizer(
    tmp_path: Path, fails: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_independent.py",
        "from pathlib import Path\nimport pytest\n"
        "@pytest.fixture\ndef actual():\n    yield 7\n    Path('finalized').touch()\n"
        f"def test_independent(actual):\n    assert actual == {8 if fails else 7}\n",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--native-result-json=actual.json"
    )
    assert result.returncode == int(fails), result.stdout + result.stderr
    payload = json.loads((tmp_path / "actual.json").read_text())
    assert payload["engine"] == "vfy-independent-v1"
    assert [case["nodeid"] for case in payload["cases"]] == [
        "test_independent.py::test_independent"
    ]
    assert payload["counts"]["failed" if fails else "passed"] == 1
    assert (tmp_path / "finalized").is_file()


@pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution is unavailable on Windows"
)
@pytest.mark.parametrize("worker_source", ["default", "cli", "config"])
def test_independent_engine_preserves_rust_worker_resolution(
    tmp_path: Path, worker_source: str
) -> None:
    project = VfyProject(tmp_path)
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_actual():\n    assert True\n")
    if worker_source == "config":
        project.write("pyproject.toml", "[tool.vfy.test]\nworkers = 2\n")
    arguments = ("--workers=2",) if worker_source == "cli" else ()
    result = project.run_vfy(
        "test", "--engine=native", *arguments, "--no-cov", "--native-result-json=actual.json"
    )
    result.expect(0)
    payload = json.loads((tmp_path / "actual.json").read_text())
    assert payload["engine"] == "vfy-independent-v1"
    assert payload["execution_topology"]["requested_workers"] == (
        1 if worker_source == "default" else 2
    )
    assert [case["nodeid"] for case in payload["cases"]] == [
        "test_a.py::test_actual",
        "test_b.py::test_actual",
    ]
    assert payload["counts"]["passed"] == 2


@requires_native_pytest
@pytest.mark.parametrize(
    "option",
    [
        "--audit-selection",
        "--confirm-failures",
        "--baseline=baseline.json",
    ],
)
def test_independent_engine_preserves_unimplemented_option_errors_before_collection(
    tmp_path: Path, option: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_uncollected.py",
        "from pathlib import Path\nPath('imported').touch()\n"
        "def test_uncollected():\n    assert True\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", option)
    result.expect(4)
    assert f"independent execution does not support {option.split('=')[0]}" in result.stderr
    assert not (tmp_path / "imported").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="serial descendant reaping is Linux-only")
def test_default_mutation_uses_serial_independent_baseline_and_mutant(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def value(default=1):\n    return default\n")
    project.write(
        "test_subject.py",
        "from pathlib import Path\nfrom subject import value\n"
        "def test_value():\n    actual = value()\n"
        "    with Path('bodies').open('a') as stream:\n        stream.write(str(actual)+'\\n')\n"
        "    assert actual == 1\n",
    )
    result = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source=subject.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--format=json",
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["killer"] == "test_subject.py::test_value"
    assert (tmp_path / "bodies").read_text().splitlines() == ["1", "2"]


def test_explicit_pytest_route_uses_the_actual_optional_distribution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_optional.py",
        "from pathlib import Path\nimport pytest\n"
        "def test_optional():\n    Path('pytest-origin').write_text(pytest.__file__)\n",
    )
    result = project.run_vfy(
        "test", "--engine=pytest", "--no-cov", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    result.expect(0)
    assert (
        Path((tmp_path / "pytest-origin").read_text()).resolve() == Path(pytest.__file__).resolve()
    )
