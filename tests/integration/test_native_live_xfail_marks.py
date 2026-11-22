"""Xfail outcomes follow runtime marker and condition changes."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="native xfail oracle requires Unix and pinned pytest",
)

_SOURCE = """\
import json
from pathlib import Path
import pytest

class ChangingCondition:
    active = False
    def __bool__(self):
        with Path('condition.jsonl').open('a') as output:
            output.write(json.dumps(self.active) + '\\n')
        return self.active

condition = ChangingCondition()

@pytest.mark.probe
def test_same_length_replacement(request):
    original = request.node.own_markers
    assert len(original) == 1
    original[:] = [pytest.mark.xfail(reason='replacement', raises=ValueError).mark]
    assert request.node.own_markers is original and len(original) == 1
    raise ValueError('expected replacement failure')

@pytest.mark.probe
def test_frozen_marker_name_mutation(request):
    marker = request.node.own_markers[0]
    assert marker.name == 'probe'
    object.__setattr__(marker, 'name', 'xfail')
    marker.kwargs.update(reason='renamed', raises=ValueError)
    assert request.node.own_markers[0] is marker
    raise ValueError('expected renamed failure')

@pytest.mark.xfail(condition, reason='condition changed', raises=ValueError)
def test_condition_changes_without_marker_changes():
    condition.active = True
    raise ValueError('expected conditional failure')
"""


def test_native_xfail_refresh_retains_live_marker_and_condition_changes(
    tmp_path: Path,
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nmarkers =\n    probe: ordinary marker\n")
        project.write("test_mutations.py", _SOURCE)
        environment = {}
        arguments = ("-q", "--junitxml=results.xml")
        result = (
            project.run_pytest(*arguments, load_xdist=False, env=environment)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--native-scheduling=cases",
                "--no-replay",
                "--no-cov",
                *arguments,
                env=environment,
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "3 xfailed" in output, output
        conditions = [
            json.loads(line) for line in (project.root / "condition.jsonl").read_text().splitlines()
        ]
        assert conditions == [False, False, True]
        reports = []
        for case in ET.parse(project.root / "results.xml").iter("testcase"):
            skipped = case.find("skipped")
            assert skipped is not None
            reports.append([case.attrib["name"], skipped.attrib["type"], skipped.attrib["message"]])
        assert len(reports) == 3
        observations[route] = [conditions, sorted(reports)]
    assert observations["native"] == observations["pytest"]
