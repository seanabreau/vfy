"""Native xdist result publication preserves upstream pytest report outcomes."""

from __future__ import annotations

import json
import os
import re
from html import unescape
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="native execution requires Unix"),
    pytest.mark.skipif(int(pytest.version_tuple[0]) < 9, reason="pytest 9 provides subtests"),
]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_failed_builtin_subtest_retains_history_and_html_outcome(
    tmp_path: Path, workers: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("reference", "native")}
    for project in projects.values():
        project.write(
            "test_subtests.py",
            "def test_values(subtests):\n"
            "    with subtests.test(value='broken'):\n"
            "        assert False, 'subtest failure sentinel'\n",
        )
    arguments = (f"--numprocesses={workers}", "--html=results.html", "-q")
    reference = projects["reference"].run_pytest(
        "-p",
        "vfy.pytest_reporting",
        *arguments,
        env={"VFY_TEST_RESULTS": str(projects["reference"].root / "results.json")},
    )
    native = projects["native"].run_vfy(
        "test", "--engine=pytest-native", "--no-replay", "--no-cov", *arguments
    )
    assert reference.returncode == pytest.ExitCode.TESTS_FAILED, reference.stdout + reference.stderr
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    assert "subtest failure sentinel" in native.stdout
    reference_summary = re.findall(r"^(.+) in [\d.]+s$", reference.stdout, re.MULTILINE)
    native_summary = re.findall(r"^(.+) in [\d.]+s$", native.stdout, re.MULTILINE)
    assert reference_summary == ["2 failed"], reference.stdout
    assert native_summary == reference_summary, native.stdout
    reference_result = json.loads(
        (projects["reference"].root / "results.json").read_text(encoding="utf-8")
    )
    assert not reference_result["infrastructure_failed"]
    assert [(test["nodeid"], test["outcome"]) for test in reference_result["tests"]] == [
        ("test_subtests.py::test_values", "failed")
    ]
    history = read_history(projects["native"].root / ".vfy/test-history.sqlite3")
    assert (
        history["tests"]["test_subtests.py::test_values"]["observations"][-1]["outcome"] == "failed"
    )
    outcomes: dict[str, list[tuple[str, str]]] = {}
    for route, project in projects.items():
        document = (project.root / "results.html").read_text(encoding="utf-8")
        outcomes[route] = [
            (outcome, unescape(nodeid))
            for outcome, nodeid in re.findall(
                r'<tr><td class="(\w+)">\w+</td><td>(.*?)</td>', document
            )
        ]
    assert outcomes["reference"] == [("Failed", "test_subtests.py::test_values")]
    assert outcomes["native"] == outcomes["reference"]
