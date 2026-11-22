"""Requested short summaries retain test identities and outcome reasons."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("reportchars", ["a", "f", "s", "xX", "p", "N"])
def test_native_short_summary_retains_requested_outcomes(tmp_path: Path, reportchars: str) -> None:
    summaries = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nxfail_strict = false\n")
        project.write(
            "test_summary.py",
            """import pytest

def test_pass():
    pass

def test_fail():
    assert False, 'failure-token'

@pytest.mark.skip(reason='skip-token')
def test_skip():
    pass

@pytest.mark.xfail(reason='xfail-token')
def test_xfail():
    assert False

@pytest.mark.xfail(reason='xpass-token')
def test_xpass():
    pass
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--tb=no",
            f"-r{reportchars}",
        )
        result.expect(1)
        summaries[engine] = [
            line
            for line in result.stdout.splitlines()
            if line.startswith(("FAILED ", "SKIPPED ", "XFAIL ", "XPASS ", "PASSED "))
        ]
    # Native skips include their node IDs; pytest folds skips by source location.
    for engine, lines in summaries.items():
        summaries[engine] = [
            "SKIPPED skip-token" if line.startswith("SKIPPED ") and "skip-token" in line else line
            for line in lines
        ]
    assert summaries["pytest-native"] == summaries["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or int(pytest.version_tuple[0]) < 9,
    reason="native worker subtest summaries require POSIX and pytest 9",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("traceback", ["no", "auto"])
def test_subtest_failure_summary_matches_stock_without_duplicates(
    tmp_path: Path, workers: int, traceback: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_subtests.py",
            "from unittest import TestCase\n"
            "class TestSubtests(TestCase):\n"
            "    def test_failure(self):\n"
            "        with self.subTest(value=1):\n"
            "            self.assertEqual(1, 2)\n",
        )
        project.write("test_other.py", "def test_other():\n    pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "-rf",
            f"--tb={traceback}",
        )
        result.expect(1)
        observed[engine] = [
            line for line in result.stdout.splitlines() if line.startswith("SUBFAILED")
        ]
        expected_tracebacks = int(traceback != "no")
        failure_details = result.stdout.split("short test summary info", maxsplit=1)[0]
        assert failure_details.count("AssertionError: 1 != 2") == expected_tracebacks, (
            result.stdout + result.stderr
        )
    assert len(observed["pytest"]) == 1
    assert "(value=1) test_subtests.py::TestSubtests::test_failure" in observed["pytest"][0]
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or int(pytest.version_tuple[0]) < 9,
    reason="native worker subtest summaries require POSIX and pytest 9",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("subtest_verbosity", [0, 1])
def test_quiet_subtest_summary_preserves_execution_evidence(
    tmp_path: Path, workers: int, subtest_verbosity: int
) -> None:
    summaries = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_subtests.py",
            "from unittest import TestCase\n"
            "class TestSubtests(TestCase):\n"
            "    def test_mixed(self):\n"
            "        with self.subTest(value='passing'):\n"
            "            pass\n"
            "        with self.subTest(value='skipped'):\n"
            "            self.skipTest('deferred')\n"
            "        with self.subTest(value='failing'):\n"
            "            self.assertEqual(1, 2)\n",
        )
        project.write("test_other.py", "def test_other():\n    pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--tb=no",
            "-o",
            f"verbosity_subtests={subtest_verbosity}",
            env={"PYTEST_NATIVE_RESULT": str(project.root / "result.json")}
            if engine == "pytest-native"
            else None,
        )
        result.expect(1)
        summaries[engine] = result.stdout.splitlines()[-1].rsplit(" in ", maxsplit=1)[0]
        if engine == "pytest-native":
            record = json.loads((project.root / "result.json").read_text())
            assert record["counts"] == {
                "passed": 2,
                "failed": 1,
                "errors": 0,
                "skipped": 1,
                "xfailed": 0,
                "xpassed": 0,
                "subtests_passed": 1,
            }
    assert summaries["pytest-native"] == summaries["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or tuple(map(int, pytest.version_tuple[:2])) < (9, 1),
    reason="expected traceback differential requires POSIX and pytest 9.1",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("xfail_traceback", [False, True])
@pytest.mark.parametrize(
    ("kind", "subtest_verbosity"), [("case", 1), ("subtest", 0), ("subtest", 1)]
)
def test_expected_failure_traceback_matches_stock_exactly_once(
    tmp_path: Path,
    workers: int,
    xfail_traceback: bool,
    kind: str,
    subtest_verbosity: int,
) -> None:
    sources = {
        "case": "import pytest\n"
        "@pytest.mark.xfail(reason='expected', strict=False)\n"
        "def test_expected():\n"
        "    print('expected-output-token')\n"
        "    raise ValueError('expected-token')\n",
        "subtest": "import pytest\n"
        "from unittest import TestCase\n"
        "@pytest.mark.xfail(reason='expected', strict=False)\n"
        "class TestSubtests(TestCase):\n"
        "    def test_expected(self):\n"
        "        print('expected-output-token')\n"
        "        with self.subTest(value=1):\n"
        "            raise ValueError('expected-token')\n",
    }
    output_counts = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_expected.py", sources[kind])
        project.write("test_other.py", "def test_other():\n    pass\n")
        options = ["--xfail-tb"] if xfail_traceback else []
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "-rxX",
            "-o",
            f"verbosity_subtests={subtest_verbosity}",
            *options,
        )
        result.expect(0)
        expected_tracebacks = int(xfail_traceback and (kind == "case" or subtest_verbosity != 0))
        assert result.stdout.count("ValueError: expected-token") == expected_tracebacks, (
            result.stdout + result.stderr
        )
        assert result.stdout.count("XFAILURES") == expected_tracebacks
        output_counts[engine] = result.stdout.count("expected-output-token")
    assert output_counts["pytest-native"] == output_counts["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or tuple(map(int, pytest.version_tuple[:2])) < (9, 1),
    reason="native worker subtest capture requires POSIX and pytest 9.1",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    ("kind", "reportchars", "subtest_verbosity", "options"),
    [
        ("case", "X", 1, []),
        ("case", "PX", 1, []),
        ("case", "pX", 1, []),
        ("subtest-xfail", "X", 1, []),
        ("subtest-xfail", "PX", 1, []),
        ("subtest-xpass", "X", 1, []),
        ("subtest-xpass", "PX", 1, []),
        ("subtest-xpass", "X", 0, []),
        ("subtest-xfail", "X", 1, ["-s"]),
        ("subtest-xpass", "X", 1, ["-s"]),
        ("subtest-xpass-parent-failure", "X", 1, []),
        ("case", "X", 1, ["--show-capture=stderr"]),
        ("case", "X", 1, ["--show-capture=no"]),
        ("case", "X", 1, ["--tb=no"]),
        ("case", "X", 1, ["--no-summary"]),
    ],
)
def test_xpass_capture_matches_stock_without_duplicate_reports(
    tmp_path: Path,
    workers: int,
    kind: str,
    reportchars: str,
    subtest_verbosity: int,
    options: list[str],
) -> None:
    captured = {}
    short_lines = {}
    body = (
        "print('call-stdout')\nprint('call-stderr', file=sys.stderr)\nlogging.warning('call-log')\n"
    )
    if kind == "case":
        source = "@pytest.mark.xfail(reason='expected', strict=False)\ndef test_expected():\n"
        source += "".join(f"    {line}\n" for line in body.splitlines())
    else:
        raises_option = ", raises=ValueError" if kind == "subtest-xpass-parent-failure" else ""
        source = (
            "from unittest import TestCase\n"
            f"@pytest.mark.xfail(reason='expected', strict=False{raises_option})\n"
            "class TestExpected(TestCase):\n"
            "    def test_expected(self):\n"
        )
        source += "".join(f"        {line}\n" for line in body.splitlines())
        source += "        with self.subTest(value=1):\n"
        source += (
            "            raise ValueError('expected-subtest')\n"
            if kind == "subtest-xfail"
            else "            pass\n"
        )
        if kind == "subtest-xpass-parent-failure":
            source += "        raise RuntimeError('unexpected-parent')\n"
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n")
        project.write(
            "conftest.py",
            "import logging, sys, pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def output():\n"
            "    print('setup-stdout')\n"
            "    print('setup-stderr', file=sys.stderr)\n"
            "    logging.warning('setup-log')\n"
            "    yield\n"
            "    print('teardown-stdout')\n"
            "    print('teardown-stderr', file=sys.stderr)\n"
            "    logging.warning('teardown-log')\n",
        )
        project.write("test_expected.py", "import logging, sys, pytest\n" + source)
        project.write(
            "test_other.py",
            (
                "import pytest\n@pytest.mark.xfail(reason='other', strict=False)\n"
                if kind == "subtest-xpass-parent-failure"
                else ""
            )
            + "def test_other():\n    pass\n",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            f"-r{reportchars}",
            "-o",
            f"verbosity_subtests={subtest_verbosity}",
            *options,
        )
        assert result.returncode == int(kind == "subtest-xpass-parent-failure"), (
            result.stdout + result.stderr
        )
        lines = result.stdout.splitlines()
        start = next((index for index, line in enumerate(lines) if " XPASSES " in line), None)
        end = next(
            (index for index, line in enumerate(lines) if " short test summary info " in line),
            len(lines) - 1,
        )
        captured[engine] = [] if start is None else lines[start:end]
        short_lines[engine] = sorted(
            line for line in lines if line.startswith(("XPASS ", "PASSED "))
        )
    assert captured["pytest-native"] == captured["pytest"]
    assert short_lines["pytest-native"] == short_lines["pytest"]


@pytest.mark.parametrize("xpass", [False, True])
@pytest.mark.parametrize("options", [[], ["--tb=no"], ["--no-summary"]])
def test_completed_success_capture_survives_keyboard_interrupt(
    tmp_path: Path, xpass: bool, options: list[str]
) -> None:
    capture_lines = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            "import pytest\n"
            + ("@pytest.mark.xfail(reason='expected', strict=False)\n" if xpass else "")
            + "def test_a_completed():\n"
            "    print('completed-output')\n"
            "def test_z_interrupt():\n"
            "    raise KeyboardInterrupt('stop')\n",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-rX" if xpass else "-rP",
            *options,
        )
        result.expect(2)
        assert "KeyboardInterrupt: stop" in result.stdout
        assert result.stdout.count("completed-output") == int(not options), result.stdout
        capture_lines[engine] = [
            line
            for line in result.stdout.splitlines()
            if "PASSES " in line
            or "Captured stdout call" in line
            or line == "completed-output"
            or line.startswith("XPASS ")
        ]
    assert capture_lines["pytest-native"] == capture_lines["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or int(pytest.version_tuple[0]) < 9,
    reason="native worker subtest summaries require POSIX and pytest 9",
)
@pytest.mark.parametrize("workers", [1, 2])
def test_subtest_summary_accepts_control_characters_in_context(
    tmp_path: Path, workers: int
) -> None:
    short_lines = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            "from unittest import TestCase\n"
            "class TestControl(TestCase):\n"
            "    def test_control(self):\n"
            "        with self.subTest(msg='control\\x1b'):\n"
            "            self.assertEqual(1, 2)\n",
        )
        project.write("test_other.py", "def test_other():\n    pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "--tb=no",
            "-rf",
        )
        result.expect(1)
        short_lines[engine] = [
            line for line in result.stdout.splitlines() if line.startswith("SUBFAILED")
        ]
        assert len(short_lines[engine]) == 1, result.stdout
    assert short_lines["pytest-native"] == short_lines["pytest"]


@pytest.mark.skipif(
    os.name != "posix" or tuple(map(int, pytest.version_tuple[:2])) < (9, 1),
    reason="native worker expected-failure capture requires POSIX and pytest 9.1",
)
@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    "kind",
    ["subtest-failed", "subtest-xfailed", "subtest-and-parent-xfailed", "multiple-xfailed-phases"],
)
@pytest.mark.parametrize("show_capture", ["stdout", "no"])
def test_failure_capture_matches_stock_for_each_report(
    tmp_path: Path, workers: int, kind: str, show_capture: str
) -> None:
    captures = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def output():\n"
            "    print('capture-setup')\n"
            "    yield\n"
            "    print('capture-teardown')\n"
            + ("    pytest.xfail('cleanup')\n" if kind == "multiple-xfailed-phases" else ""),
        )
        source = "import pytest\n"
        if kind == "multiple-xfailed-phases":
            source += "def test_expected():\n    print('capture-call')\n    pytest.xfail('body')\n"
        else:
            source += "from unittest import TestCase\n"
            if kind in {"subtest-xfailed", "subtest-and-parent-xfailed"}:
                source += "@pytest.mark.xfail(reason='expected', strict=False)\n"
            source += (
                "class TestExpected(TestCase):\n"
                "    def test_expected(self):\n"
                "        print('capture-call')\n"
                "        with self.subTest(value=1):\n"
                "            raise ValueError('expected-subtest')\n"
            )
            if kind == "subtest-and-parent-xfailed":
                source += "        raise ValueError('expected-parent')\n"
        project.write("test_expected.py", source)
        project.write("test_other.py", "def test_other():\n    pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            f"--workers={workers if engine == 'pytest-native' else 1}",
            "--no-cov",
            "-q",
            "-rfx",
            "--xfail-tb",
            "-o",
            "verbosity_subtests=1",
            f"--show-capture={show_capture}",
        )
        assert result.returncode == int(kind == "subtest-failed"), result.stdout + result.stderr
        captures[engine] = [
            line
            for line in result.stdout.splitlines()
            if line in {"capture-setup", "capture-call", "capture-teardown"}
        ]
    assert captures["pytest-native"] == captures["pytest"]


@pytest.mark.parametrize("reportchars", ["a", "A"])
def test_success_capture_headlines_match_for_parametrized_families(
    tmp_path: Path, reportchars: str
) -> None:
    """Serial runs resolve summary locations for rendered compact family cases."""
    sections = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nxfail_strict = false\n")
        project.write(
            "test_family.py",
            """import pytest

@pytest.mark.parametrize("case", ["quiet", "loud"])
def test_function(case):
    print(f"function-{case}")

@pytest.mark.xfail(reason="static-xpass")
@pytest.mark.parametrize("case", [1, 2])
def test_marked(case):
    print(f"marked-{case}")
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            f"-r{reportchars}",
        )
        result.expect(0)
        lines = result.stdout.splitlines()
        start = next(
            index for index, line in enumerate(lines) if " XPASSES " in line or " PASSES " in line
        )
        end = next(index for index, line in enumerate(lines) if " short test summary info " in line)
        sections[engine] = lines[start:end]
    assert sections["pytest-native"] == sections["pytest"]
    assert any("test_marked[2]" in line for line in sections["pytest-native"])
