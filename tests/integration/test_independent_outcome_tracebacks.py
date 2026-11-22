"""Actual outcome helper locals hide helper frames from public crash locations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_BODY = """import pytest
from pathlib import Path
MODE = CONTROL
HELPER = pytest.xfail if MODE == 'xfail' else pytest.fail

def test_body():
    Path('body-ran').touch()
    if MODE == 'fail_value':
        HELPER('actual outcome', pytrace=False)
    else:
        HELPER('actual outcome')
"""
_OBSERVER = """import json,pytest
from pathlib import Path
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when != 'call':
        return report
    import test_body
    helper = test_body.HELPER
    code = getattr(helper, '__code__', None)
    if code is None:
        code = helper.__call__.__func__.__code__
    frames = []
    raw = []
    traceback = call.excinfo.tb
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code is code:
            fields = {key: frame.f_locals.get(key, 'missing')
                      for key in ['__tracebackhide__', 'reason']}
            if test_body.MODE != 'xfail':
                fields['pytrace'] = frame.f_locals.get('pytrace', 'missing')
            frames.append(fields)
            raw.append({'filename': frame.f_code.co_filename,
                        'name': frame.f_code.co_name,
                        'line': traceback.tb_lineno, 'locals': fields})
        traceback = traceback.tb_next
    graph = item._repr_failure_py(call.excinfo, style='long')
    crash = graph.reprcrash
    Path('observations.json').write_text(json.dumps({
        'actual_helper_frames': frames,
        'crash': [crash.path, crash.lineno, crash.message],
    }))
    Path('helper-frames.json').write_text(json.dumps(raw))
    return report
"""


def _run(project: VfyProject, engine: str, mode: str) -> dict[str, object]:
    for name in ["body-ran", "observations.json", "helper-frames.json"]:
        (project.root / name).unlink(missing_ok=True)
    result = project.run_engine(engine, "-q", "--tb=long")
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == (0 if mode == "xfail" else 1), result.stdout + result.stderr
    assert (project.root / "body-ran").is_file()
    raw = (project.root / "helper-frames.json").read_text()
    project.write(f"{engine}-helper-frames.json", raw)
    observed = json.loads((project.root / "observations.json").read_text())
    assert isinstance(observed, dict)
    checked: dict[str, object] = {}
    for key, value in observed.items():
        assert isinstance(key, str)
        checked[key] = value
    return checked


@pytest.mark.parametrize("mode", ["fail", "fail_value", "xfail"])
def test_outcome_helpers_hide_actual_frames_from_public_crash_locations(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", _BODY.replace("CONTROL", repr(mode)))
    project.write("conftest.py", _OBSERVER)
    expected = _run(project, "stock", mode)
    actual = _run(project, "independent", mode)
    assert actual == expected
    fields: dict[str, object] = {"__tracebackhide__": True, "reason": "actual outcome"}
    if mode != "xfail":
        fields["pytrace"] = mode != "fail_value"
    assert expected["actual_helper_frames"] == [fields]
    crash = expected["crash"]
    assert isinstance(crash, list)
    assert crash[0] == str(project.root / "test_body.py")
