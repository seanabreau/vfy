"""Public call and collection reports retain live Python protocol values."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = Path(__file__).parents[1] / "fixtures" / "independent_call_report_api.py"


def test_call_and_collection_report_api_matches_pytest(tmp_path: Path) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_subject.py", "def test_subject():\n    pass\n")
        project.write("conftest.py", _OBSERVER.read_text())
        result = project.run_engine(engine, "--collect-only", "-q", "test_subject.py")
        result.expect(0)
        observed[engine] = json.loads((project.root / "api.json").read_text())
    expected = observed["stock"]
    assert expected["actual_item"]
    assert expected["call_success"] == {
        "result_identity": True,
        "excinfo_none": True,
        "when": "collect",
        "timing": True,
    }
    assert expected["default_control_capture"] == [True, True, True]
    assert expected["explicit_reraise_identity"] == [True, True]
    assert expected["call_cycle_collected"]
    assert expected["report_cycle_collected"]
    assert expected["none_sections_rejected"]
    assert observed["native"] == expected
