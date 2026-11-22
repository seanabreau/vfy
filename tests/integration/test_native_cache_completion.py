"""Native cache completion preserves pytest outcomes and vfy history."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_native_cache_completion_matches_stock_outcomes_and_recovery(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_cases.py",
            """from pathlib import Path
import unittest
import pytest

def recovering():
    return Path('recovered.txt').exists()

@pytest.fixture
def setup_skip():
    pytest.skip('setup skip')

@pytest.fixture
def setup_error():
    if not recovering(): raise ValueError('setup error')

@pytest.fixture
def teardown_error():
    yield
    if not recovering(): raise ValueError('teardown error')

def test_pass(): pass
def test_setup_skip(setup_skip): pass
def test_call_skip(): pytest.skip('call skip')

@pytest.mark.xfail(reason='expected', strict=False)
def test_xfail(): assert False

@pytest.mark.xfail(reason='unexpected', strict=False)
def test_xpass(): pass

def test_failure(): assert recovering()
def test_setup_error(setup_error): pass
def test_teardown_error(teardown_error): pass

class TestSubtest(unittest.TestCase):
    def test_subtest(self):
        with self.subTest(value=1):
            self.assertTrue(recovering())
""",
        )
        project.write(
            ".pytest_cache/v/cache/lastfailed",
            json.dumps(
                {
                    f"test_cases.py::{name}": True
                    for name in (
                        "test_pass",
                        "test_setup_skip",
                        "test_call_skip",
                        "test_xfail",
                        "test_xpass",
                    )
                }
            ),
        )
        runs = []
        for recovering in (False, True):
            if recovering:
                project.write("recovered.txt", "recover")
            result = project.run_vfy(
                "test",
                f"--engine={engine}",
                "--workers=1",
                "--no-cov",
                "-q",
                *(("--lf",) if recovering else ()),
            )
            assert result.returncode == (0 if recovering else 1), result.stdout + result.stderr
            cache = project.root / ".pytest_cache/v/cache"
            history = read_history(project.root / ".vfy/test-history.sqlite3")
            runs.append(
                (
                    json.loads((cache / "nodeids").read_text()),
                    json.loads((cache / "lastfailed").read_text()),
                    {
                        nodeid: [
                            (entry["outcome"], entry.get("failed_phase"))
                            for entry in record["observations"]
                        ]
                        for nodeid, record in history["tests"].items()
                    },
                )
            )
        assert runs[0][1] == {
            "test_cases.py::test_failure": True,
            "test_cases.py::test_setup_error": True,
            "test_cases.py::test_teardown_error": True,
        }
        assert runs[1][1] == {}
        for run in runs:
            assert run[2]["test_cases.py::TestSubtest::test_subtest"] == [("failed", "call")]
        observed[engine] = runs
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize(
    ("option", "failures", "expected_events", "resume"),
    [
        ("--sw", 1, ["subtest", "parent completed"], None),
        (
            "--sw-skip",
            1,
            ["subtest", "parent completed", "ordinary failure"],
            "test_steps.py::test_b_failure",
        ),
        ("--sw-skip", 2, ["subtest", "parent completed"], None),
    ],
)
def test_subtest_cache_completion_preserves_stepwise_report_order(
    tmp_path: Path, option: str, failures: int, expected_events: list[str], resume: str | None
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_steps.py",
            f"""import unittest
from pathlib import Path

def record(name):
    with Path('events.txt').open('a') as stream:
        stream.write(name + '\\n')

class TestSubtest(unittest.TestCase):
    def test_a_subtest(self):
        record('subtest')
        for value in range({failures}):
            with self.subTest(value=value):
                self.assertTrue(False)
        record('parent completed')

def test_b_failure():
    record('ordinary failure')
    assert False

def test_c_later():
    record('later')
""",
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", option
        )
        result.expect(2)
        assert (project.root / "events.txt").read_text().splitlines() == expected_events
        cache = project.root / ".pytest_cache/v/cache"
        last_failed = (
            json.loads((cache / "lastfailed").read_text())
            if (cache / "lastfailed").exists()
            else {}
        )
        stepwise = json.loads((cache / "stepwise").read_text())
        datetime.fromisoformat(stepwise["last_cache_date_str"])
        assert stepwise["last_failed"] == resume
        assert stepwise["last_test_count"] == 3
        assert last_failed == ({resume: True} if resume is not None else {})
        history = read_history(project.root / ".vfy/test-history.sqlite3")
        observed[engine] = {
            nodeid: [
                (entry["outcome"], entry.get("failed_phase")) for entry in record["observations"]
            ]
            for nodeid, record in history["tests"].items()
        }
        assert observed[engine]["test_steps.py::TestSubtest::test_a_subtest"] == [
            ("failed", "call")
        ]
    assert observed["pytest-native"] == observed["pytest"]
