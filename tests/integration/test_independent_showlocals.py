"""Show frame locals through the public independent runner and pinned pytest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = """from pathlib import Path
import pytest

class Probe:
    def __init__(self, label):
        self.label = label
    def __repr__(self):
        with Path("repr.txt").open("a") as output:
            output.write(self.label + "\\n")
        return "VALUE(" + self.label + ")"

class BadRepr:
    def __repr__(self):
        with Path("repr.txt").open("a") as output:
            output.write("bad-repr\\n")
        raise ValueError("repr issue")

def helper():
    zulu = Probe("helper")
    alpha = {"z": list(range(50)), "a": "a" * 300}
    __builtins__ = {"actual": "not rendered"}
    locals()["@private_helper"] = Probe("must-not-repr")
    raise ValueError("call failure")

def middle():
    zulu = Probe("middle")
    helper()

def test_call():
    outer = Probe("outer")
    middle()

@pytest.fixture
def setup_value():
    zulu = Probe("setup")
    alpha = {"z": list(range(50)), "a": "a" * 300}
    raise ValueError("setup failure")

def test_setup(setup_value):
    pass

def test_pass():
    value = Probe("passing")
    assert value.label == "passing"

def test_bad_repr():
    bad = BadRepr()
    raise ValueError("body failure")
"""

_CONFTEST = """import json
from pathlib import Path
import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    addresses = {}
    if call.excinfo is not None:
        traceback = call.excinfo.value.__traceback__
        while traceback is not None:
            if "bad" in traceback.tb_frame.f_locals:
                addresses[traceback.tb_frame.f_code.co_name] = id(
                    traceback.tb_frame.f_locals["bad"]
                )
            traceback = traceback.tb_next
    outcome = yield
    report = outcome.get_result()
    row = {"when": report.when, "outcome": report.outcome,
           "showlocals": item.config.getoption("showlocals"), "addresses": addresses}
    if report.failed:
        row["text"] = report.longreprtext
        row["entries"] = [
            {"style": entry.style,
             "locals": None if entry.reprlocals is None else entry.reprlocals.lines}
            for entry in report.longrepr.reprtraceback.reprentries
        ]
    with Path("reports.jsonl").open("a") as output:
        output.write(json.dumps(row) + "\\n")
"""


def _project(tmp_path: Path, *, addopts: str = "") -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("test_locals.py", _SOURCE)
    project.write("conftest.py", _CONFTEST)
    project.write("pytest.ini", f"[pytest]\naddopts = {addopts}\n")
    return project


def _observe(
    project: VfyProject, node: str, options: tuple[str, ...], *, candidate: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    arguments = ("--no-header", *options, f"test_locals.py::{node}")
    command = (
        project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
        if candidate
        else project.run_python("-m", "pytest", *arguments)
    )
    assert command.returncode == (0 if node == "test_pass" else 1), command.stdout + command.stderr
    reports = project.root / "reports.jsonl"
    records = [json.loads(line) for line in reports.read_text().splitlines()]
    reports.unlink()
    rendered = project.root / "repr.txt"
    calls = rendered.read_text().splitlines() if rendered.exists() else []
    rendered.unlink(missing_ok=True)
    return records, calls


@pytest.mark.parametrize("node", ["test_setup", "test_call"])
@pytest.mark.parametrize(
    ("options", "enabled"),
    [
        ((), False),
        (("-l",), True),
        (("--showlocals",), True),
        (("-l", "--no-showlocals"), False),
        (("--no-showlocals", "-l"), True),
        (("-l", "-vv"), True),
        (("-l", "--tb=short"), True),
    ],
)
def test_showlocals_preserves_complete_setup_and_call_diagnostics(
    tmp_path: Path, node: str, options: tuple[str, ...], enabled: bool
) -> None:
    project = _project(tmp_path)
    reference = _observe(project, node, options, candidate=False)
    assert _observe(project, node, options, candidate=True) == reference
    rows, calls = reference
    failed = [row for row in rows if row["outcome"] == "failed"]
    assert len(failed) == 1
    assert all(row["showlocals"] == enabled for row in rows)
    assert bool(calls) == enabled
    assert "must-not-repr" not in calls
    entries = failed[0]["entries"]
    assert all((entry["locals"] is not None) == enabled for entry in entries)
    if enabled:
        for entry in entries:
            names = [line.split(" = ", 1)[0].rstrip() for line in entry["locals"]]
            assert names == sorted(names)
            assert all(not name.startswith("@") for name in names)
        if node == "test_call":
            assert any("__builtins__ = <builtins>" in entry["locals"] for entry in entries)


@pytest.mark.parametrize("options", [(), ("-l",)])
def test_showlocals_does_not_render_passing_body_values(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    project = _project(tmp_path)
    reference = _observe(project, "test_pass", options, candidate=False)
    assert _observe(project, "test_pass", options, candidate=True) == reference
    assert reference[1] == []
    assert [row["outcome"] for row in reference[0]] == ["passed"] * 3


@pytest.mark.parametrize("options", [(), ("--no-showlocals",)])
def test_showlocals_addopts_can_be_negated_at_the_command_line(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    project = _project(tmp_path, addopts="-l")
    reference = _observe(project, "test_call", options, candidate=False)
    assert _observe(project, "test_call", options, candidate=True) == reference
    assert all(row["showlocals"] == (not options) for row in reference[0])
    assert bool(reference[1]) == (not options)
