"""Controller history consumes published phases while standalone timing stays intact."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)


@requires_native_pytest
def test_xdist_history_preserves_published_phase_timings_and_failures(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(): return 1\n")
    project.write(
        "conftest.py",
        dedent("""\
            import os
            from pathlib import Path
            import pytest

            def pytest_configure(config):
                if os.environ.get('NATIVE_PROOF') == '1':
                    import _pytest.runner
                    def forbidden(*args, **kwargs):
                        raise AssertionError('stock runtestprotocol executed')
                    _pytest.runner.runtestprotocol = forbidden
                    if hasattr(config, 'workerinput'):
                        Path('native-' + config.workerinput['workerid']).touch()

            @pytest.hookimpl(tryfirst=True)
            def pytest_runtest_logreport(report):
                report.duration = {'setup': .001, 'call': .002, 'teardown': .004}[report.when]

            @pytest.fixture
            def bad_setup():
                raise RuntimeError('setup failed')

            @pytest.fixture
            def bad_teardown():
                yield
                raise RuntimeError('teardown failed')
            """),
    )
    project.write(
        "test_app.py",
        dedent("""\
            import pytest
            from app import value

            def test_pass():
                assert value() == 1

            def test_call_and_teardown(bad_teardown):
                assert value() == 2

            def test_setup(bad_setup):
                pass

            def test_skip():
                pytest.skip('call skipped')
            """),
    )
    histories: list[str] = []
    for engine in ("pytest", "pytest-native"):
        history_path = project.write("history.json", '{"custom":{"z":2,"a":1},"tests":{}}')
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--no-replay",
            "--history=history.json",
            "--cov=app",
            "--cov-report=",
            "--numprocesses=2",
            "-q",
            env={"NATIVE_PROOF": "1" if engine == "pytest-native" else None},
        )
        result.expect(1)
        text = history_path.read_text(encoding="utf-8")
        history = json.loads(text)
        run_ids = {
            observation["run_id"]
            for entry in history["tests"].values()
            for observation in entry["observations"]
        }
        assert len(run_ids) == 1
        histories.append(text.replace(run_ids.pop(), "run"))
    assert len(list(tmp_path.glob("native-gw*"))) == 2
    assert histories[0] == histories[1]
    entries = {
        name: {
            **({"duration_ms": 2.0} if name != "test_setup" else {}),
            "observations": [
                {
                    "attempt": "primary",
                    "duration_ms": 5.0 if name == "test_setup" else 7.0,
                    **({"failed_phase": phase} if phase else {}),
                    "outcome": outcome,
                    "run_id": "run",
                }
            ],
        }
        for name, outcome, phase in (
            ("test_pass", "passed", None),
            ("test_call_and_teardown", "failed", "call"),
            ("test_setup", "failed", "setup"),
            ("test_skip", "skipped", None),
        )
    }
    expected = {
        "custom": {"a": 1, "z": 2},
        "tests": {f"test_app.py::{name}": entry for name, entry in entries.items()},
    }
    assert histories[0] == json.dumps(expected, indent=2, sort_keys=True)

    result = project.run_pytest(
        "--cov=app",
        "--cov-report=",
        "-n2",
        "-q",
        env={"VFY_TEST_HISTORY": "standalone.json", "VFY_TEST_RESULTS": None},
    )
    result.expect(1)
    standalone = json.loads((tmp_path / "standalone.json").read_text(encoding="utf-8"))
    assert standalone == {
        "tests": {
            f"test_app.py::{name}": {"duration_ms": 2.0}
            for name in ("test_pass", "test_call_and_teardown", "test_skip")
        }
    }


def test_failed_result_publication_preserves_prior_standalone_history(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(): return 1\n")
    project.write("test_app.py", "from app import value\ndef test_value(): assert value() == 1\n")
    history = project.write("history.json", '{"tests":{"prior":{"duration_ms":42}}}')
    prior = history.read_bytes()
    (tmp_path / "blocked").mkdir()
    result = project.run_pytest(
        "--cov=app",
        "--cov-report=",
        "-n2",
        "-q",
        env={"VFY_TEST_HISTORY": "history.json", "VFY_TEST_RESULTS": "blocked"},
    )
    result.expect(0)
    assert history.read_bytes() == prior
