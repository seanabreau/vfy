"""Canonical reason text preserves Python surrogate code points through publication."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_PLUGIN = r"""import json
from pathlib import Path
import pytest
from settings import CHILD,OUTCOME
reason='reason-\udcff'

@pytest.hookimpl(wrapper=True)
def pytest_runtest_logreport(report):
    selected=(hasattr(report,'context') if CHILD else not hasattr(report,'context'))
    if selected and report.when=='call' and report.skipped:
        if OUTCOME=='skip':
            path,line,_=report.longrepr
            report.longrepr=(path,line,'Skipped: '+reason)
            message=report.longrepr[2]
        else:
            report.wasxfail='reason: '+reason
            message=report.wasxfail
        assert ord(message[-1])==0xdcff
        Path('live.json').write_text(json.dumps({'message':message,'when':report.when}))
    return (yield)
"""


def _inputs(project: VfyProject, *, engine: str, child: bool, outcome: str) -> None:
    project.write("pytest.ini", "[pytest]\n")
    project.write("settings.py", f"CHILD={child}\nOUTCOME={outcome!r}\n")
    project.write("conftest.py", _PLUGIN)
    call = f"pytest.{outcome}('original reason')\n"
    body = "    with subtests.test('child'):\n        " + call if child else "    " + call
    project.write("test_body.py", "import pytest\ndef test_value(subtests):\n" + body)


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
@pytest.mark.parametrize("outcome", ["skip", "xfail"])
@pytest.mark.parametrize("child", [False, True])
def test_reason_message_surrogate_preserves_live_string_and_reference_xml(
    tmp_path: Path, engine: str, outcome: str, child: bool
) -> None:
    project = VfyProject(tmp_path)
    _inputs(project, engine=engine, child=child, outcome=outcome)
    project.write("results.xml", "previous complete document\n")
    arguments = ["-q", "--junitxml=results.xml", "--color=no"]
    environment = {"PYTHONIOENCODING": "utf-8:backslashreplace"}
    if engine == "stock":
        result = project.run_python("-B", "-m", "pytest", *arguments, env=environment)
    else:
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            *arguments,
            "--native-result-json=record.json",
            env=environment,
        )
    project.write("run.stdout", result.stdout)
    project.write("run.stderr", result.stderr)
    project.write("run.status", str(result.returncode))
    result.expect(0)
    live = json.loads((project.root / "live.json").read_text())
    assert live == {
        "message": ("Skipped: " if outcome == "skip" else "reason: ") + "reason-\udcff",
        "when": "call",
    }
    skipped = ET.parse(project.root / "results.xml").findall(".//skipped")
    assert len(skipped) == 1
    assert skipped[0].attrib["type"] == "pytest." + outcome
    assert skipped[0].attrib["message"] == "reason-#xDCFF"
    if engine == "native":
        record = json.loads((project.root / "record.json").read_text())
        case = record["cases"][0]
        report = (
            case["subtests"][0]
            if child
            else next(p for p in case["phases"] if p["phase"] == "call")
        )
        expected = [ord(character) for character in live["message"]]
        assert report["exception"] == {"codepoints": expected}
