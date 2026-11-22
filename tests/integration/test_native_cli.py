"""The native engine through the vfy CLI: result records, worker configuration, pytest hooks."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


def _native_engine_supported() -> bool:
    if not sys.platform.startswith(("linux", "darwin")):
        return False
    try:
        return importlib.util.find_spec(
            "vfy._pytest.native_pytest"
        ) is not None and importlib.metadata.version("pytest").split(".", maxsplit=1)[0] in {
            "8",
            "9",
        }
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _native_engine_supported(),
    reason="native pytest requires Linux or macOS and a supported pytest version",
)


def test_explicit_native_test_command_uses_serial_runner_and_records_result(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_engine.py",
        "import os\n"
        "def test_native_default():\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n",
    )
    record = tmp_path / "native-result.json"

    completed = project.run_vfy(
        "-v", "test", "--engine=pytest-native", env={"PYTEST_NATIVE_RESULT": str(record)}
    )

    completed.expect(0)
    assert "Backend: pytest-native " in completed.stderr
    assert "1 passed" in completed.stdout
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["cases"][0]["nodeid"] == "tests/test_engine.py::test_native_default"
    assert any(
        phase["phase"] == "call" and phase["outcome"] == "passed"
        for phase in payload["cases"][0]["phases"]
    )


@pytest.mark.parametrize("coverage", [(), ("--no-cov",)], ids=["cov", "no-cov"])
def test_collection_error_reports_pytest_status_without_missing_result_noise(
    tmp_path: Path, coverage: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write("tests/test_broken.py", "raise RuntimeError('broken module')\n")
    project.write("tests/test_ok.py", "def test_ok():\n    pass\n")

    completed = project.run_vfy("test", "--engine=pytest-native", "-q", *coverage)

    completed.expect(2)
    assert "1 error during collection" in completed.stdout
    assert "could not be recorded" not in completed.stderr


def test_failed_test_reports_missing_result_when_result_storage_is_unwritable(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_broken.py",
        "import os\n"
        "from pathlib import Path\n"
        "def test_failure():\n"
        "    Path(os.environ['VFY_TEST_RESULTS']).mkdir()\n"
        "    Path('blocked-result-destination').touch()\n"
        "    assert False\n",
    )

    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q")

    completed.expect(1)
    assert (tmp_path / "blocked-result-destination").is_file()
    assert "1 failed" in completed.stdout
    assert "Test result evidence could not be recorded" in completed.stderr


def test_native_result_record_counts_mixed_outcomes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_outcomes.py",
        "import pytest\n"
        "def test_passed():\n    assert True\n"
        "def test_failed():\n    assert False\n"
        "@pytest.fixture\n"
        "def broken():\n    raise RuntimeError('setup failed')\n"
        "def test_error(broken):\n    pass\n"
        "@pytest.mark.skip(reason='skip')\n"
        "def test_skipped():\n    pass\n"
        "@pytest.mark.xfail(reason='expected')\n"
        "def test_xfailed():\n    assert False\n"
        "@pytest.mark.xfail(reason='unexpected pass')\n"
        "def test_xpassed():\n    pass\n",
    )
    record = tmp_path / "native-result.json"

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"PYTEST_NATIVE_RESULT": str(record)},
    )

    completed.expect(1)
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["collected"] == 6
    assert payload["counts"] == {
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "skipped": 1,
        "xfailed": 1,
        "xpassed": 1,
        "subtests_passed": 0,
    }


_SESSION_RECORDER = (
    "import os\n"
    "from pathlib import Path\n"
    "import pytest\n\n"
    "@pytest.fixture(scope='session', autouse=True)\n"
    "def record_session_process():\n"
    "    root = Path('sessions')\n"
    "    root.mkdir(exist_ok=True)\n"
    "    (root / str(os.getpid())).touch()\n"
)


def _two_file_project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", "[tool.vfy.test]\nworkers = '2'\n")
    project.write("tests/conftest.py", _SESSION_RECORDER)
    project.write("tests/test_a.py", "def test_a():\n    assert True\n")
    project.write("tests/test_b.py", "def test_b():\n    assert True\n")
    return project


def test_configured_parallel_workers_use_native_execution(tmp_path: Path) -> None:
    project = _two_file_project(tmp_path)

    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "-q")

    completed.expect(0)
    assert "2 passed" in completed.stdout
    assert len(list((tmp_path / "sessions").iterdir())) == 2


def test_explicit_serial_cli_option_overrides_parallel_configuration(tmp_path: Path) -> None:
    project = _two_file_project(tmp_path)

    completed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-replay", "-q"
    )

    completed.expect(0)
    assert "2 passed" in completed.stdout
    assert len(list((tmp_path / "sessions").iterdir())) == 1


@pytest.mark.parametrize("workers", [1, 2])
def test_conftest_report_header_matches_pytest_once(tmp_path: Path, workers: int) -> None:
    marker = "custom project report header"
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", f"def pytest_report_header(config):\n    return {marker!r}\n")
        for name in ("a", "b"):
            project.write(f"test_{name}.py", f"def test_{name}():\n    pass\n")
        # Select each engine while retaining the serial worker default.
        options = ["--engine=pytest"] if engine == "pytest" else ["--engine=pytest-native"]
        if workers > 1:
            options.append(f"--workers={workers}")
        result = project.run_vfy("test", *options, "--no-cov", "-v", "--color=no")
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in result.stdout, output
        observed[engine] = [line for line in result.stdout.splitlines() if line == marker]
    assert observed["pytest-native"] == observed["pytest"] == [marker]


def test_conftest_sessionfinish_matches_pytest_shell_lifecycle(tmp_path: Path) -> None:
    marker = "conftest-sessionfinish-output-sentinel"
    observations: dict[str, tuple[int, int]] = {}

    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "from pathlib import Path\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    Path('sessionfinish-calls.txt').write_text('called\\n', encoding='utf-8')\n"
            "    reporter = session.config.pluginmanager.getplugin('terminalreporter')\n"
            f"    reporter.write_line({marker!r})\n"
            "    session.exitstatus = 1\n",
        )
        project.write("tests/test_engine.py", "def test_body_passes():\n    assert True\n")

        completed = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-replay",
            "--no-cov",
            "-q",
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == 1, output
        assert "1 passed" in output
        assert output.count(marker) == 1, output
        assert (project.root / "sessionfinish-calls.txt").read_text(encoding="utf-8") == (
            "called\n"
        )
        observations[engine] = (completed.returncode, output.count(marker))

    assert observations["pytest-native"] == observations["pytest"] == (1, 1)
