"""Value-style reports expose live representation objects and writer calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, *, expected_status: int = 0) -> dict[str, object]:
    stock = project.run_python("-m", "pytest", "-q", "-p", "pytest_cov")
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    path = project.root / "observations.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    project.write("stock-observations.json", json.dumps(expected, indent=2))
    path.unlink(missing_ok=True)
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-p",
        "vfy.independent.outcomes",
        env={"VALUE_NATIVE": "1"},
    )
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    actual = json.loads(path.read_text()) if path.exists() else None
    project.write("candidate-observations.json", json.dumps(actual, indent=2))
    project.write("statuses.json", json.dumps([stock.returncode, candidate.returncode]))
    assert stock.returncode == candidate.returncode == expected_status
    assert isinstance(expected, dict), stock.stdout + stock.stderr
    assert expected
    assert actual == expected, candidate.stdout + candidate.stderr
    observation: dict[str, object] = {}
    for key, value in expected.items():
        assert isinstance(key, str)
        observation[key] = value
    return observation


@pytest.mark.parametrize("relation", ["single", "cause", "context", "missing_cause"])
def test_actual_message_only_report_retains_a_real_value_graph(
    tmp_path: Path, relation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    source = "import pytest\ndef test_body():\n"
    if relation == "single":
        source += "    pytest.fail('VALUE-CAPTURE', pytrace=False)\n"
    elif relation == "missing_cause":
        source += "    raise pytest.fail.Exception('VALUE-CAPTURE', pytrace=False)"
        source += " from ValueError('INNER-CAPTURE')\n"
    else:
        source += "    try:\n        raise ValueError('INNER-CAPTURE')\n"
        source += "    except ValueError as error:\n"
        source += "        raise pytest.fail.Exception('VALUE-CAPTURE', pytrace=False)"
        source += " from error\n" if relation == "cause" else "\n"
    project.write("test_value.py", source)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'call':
        representation = report.longrepr
        traceback = representation.reprtraceback
        crash = representation.reprcrash
        actual = []
        raw = call.excinfo.tb
        while raw is not None:
            hidden = raw.tb_frame.f_locals.get('__tracebackhide__',
                raw.tb_frame.f_globals.get('__tracebackhide__', False))
            if callable(hidden): hidden = hidden(call.excinfo)
            if not hidden: actual.append([raw.tb_frame.f_code.co_filename, raw.tb_lineno])
            raw = raw.tb_next
        recorded = None if crash is None else [crash.path, crash.lineno]
        Path('actual-crash.json').write_text(json.dumps({'traceback':actual,'crash':recorded}))
        observation = {
            'type': type(representation).__name__, 'style': traceback.style,
            'traceback_alias': representation.chain[-1][0] is traceback,
            'crash_alias': representation.chain[-1][1] is crash,
            'crash_matches_actual_traceback': recorded == (actual[-1] if actual else None),
            'lines': list(traceback.reprentries[0].lines), 'text': str(representation),
        }
        traceback.reprentries[0].lines[:] = ['REPORT-MUTATION']
        representation.addsection('report section', 'report body', '~')
        observation['mutated_text'] = str(representation)
        Path('observations.json').write_text(json.dumps(observation, indent=2))
    return report
""",
    )
    observed = _run(project, expected_status=1)
    assert observed["type"] == "ExceptionChainRepr"
    assert observed["style"] == "value"
    assert observed["traceback_alias"] is True
    assert observed["crash_alias"] is True
    assert observed["crash_matches_actual_traceback"] is True
    assert observed["lines"] == ["VALUE-CAPTURE"]


def test_fixture_scope_failure_retains_actual_value_graph(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_value.py",
        """import pytest
@pytest.fixture
def narrow(): return 1
@pytest.fixture(scope='session')
def broad(narrow): return narrow
def test_body(broad): pass
""",
    )
    project.write(
        "conftest.py",
        """import json, os
from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'setup':
        value = call.excinfo.value
        details = {'traceback_missing': call.excinfo.tb is None,
                   'representation': type(report.longrepr).__name__,
                   'text': str(report.longrepr)}
        native = bool(os.environ.get('VALUE_NATIVE'))
        Path(('candidate' if native else 'stock') + '-details.json').write_text(json.dumps(details))
        representation = report.longrepr
        assert call.excinfo.tb is not None
        assert type(representation).__name__ == 'ExceptionChainRepr'
        assert representation.chain[-1][0] is representation.reprtraceback
        assert representation.chain[-1][1] is representation.reprcrash
        Path('observations.json').write_text(json.dumps({
            'when': report.when, 'outcome': report.outcome,
            'failed': isinstance(value, pytest.fail.Exception),
            'lookup': isinstance(value, pytest.FixtureLookupError),
            'pytrace': value.pytrace,
            'scope_message': str(value).startswith('ScopeMismatch:'),
            'traceback_present': call.excinfo.tb is not None,
            'graph': [type(representation).__name__,
                      type(representation.reprtraceback).__name__],
        }))
    return report
""",
    )
    observed = _run(project, expected_status=1)
    assert observed == {
        "when": "setup",
        "outcome": "failed",
        "failed": True,
        "lookup": False,
        "pytrace": False,
        "scope_message": True,
        "traceback_present": True,
        "graph": ["ExceptionChainRepr", "ReprTraceback"],
    }


def test_value_chain_group_node_uses_its_actual_native_traceback(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_value.py",
        """import pytest

def test_body():
    try:
        raise ExceptionGroup('actual group', [ValueError('INNER-GROUP')])
    except ExceptionGroup as error:
        raise pytest.fail.Exception('VALUE-CAPTURE', pytrace=False) from error
""",
    )
    project.write(
        "conftest.py",
        """import json, os, traceback
from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'call':
        representation = report.longrepr
        native = representation.chain[0][0]
        raw = call.excinfo.tb
        while raw is not None and raw.tb_frame.f_code is not item.obj.__code__:
            raw = raw.tb_next
        expected = traceback.format_exception(call.excinfo.type, call.excinfo.value, raw)
        actual = native.reprentries[0].lines
        details = {'expected': expected, 'actual': list(actual), 'text': str(representation)}
        name = 'candidate' if os.environ.get('VALUE_NATIVE') else 'stock'
        Path(name + '-native-details.json').write_text(json.dumps(details, indent=2))
        Path('observations.json').write_text(json.dumps({
            'type': type(representation).__name__,
            'native_type': type(native).__name__, 'native_style': native.style,
            'entry_type': type(native.reprentries[0]).__name__,
            'native_matches_actual_formatter': list(actual) == expected,
            'outer_style': representation.reprtraceback.style,
            'outer_lines': list(representation.reprtraceback.reprentries[0].lines),
        }))
    return report
""",
    )
    observed = _run(project, expected_status=1)
    assert observed == {
        "type": "ExceptionChainRepr",
        "native_type": "ReprTracebackNative",
        "native_style": "native",
        "entry_type": "ReprEntryNative",
        "native_matches_actual_formatter": True,
        "outer_style": "value",
        "outer_lines": ["VALUE-CAPTURE"],
    }
