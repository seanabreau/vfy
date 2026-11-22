"""Actual failures expose mutable traceback graphs and terminal protocols."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_BODY = """def inner(value):
    raise ValueError('actual crash')

def middle(value):
    inner(value)

def test_body():
    middle(7)
"""

_OBSERVER = """import json, pytest
from pathlib import Path

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when != 'call':
        return report
    value = report.longrepr
    assert type(value).__name__ == 'ExceptionChainRepr'
    assert value.chain[-1][0] is value.reprtraceback
    assert value.chain[-1][1] is value.reprcrash
    entries = value.reprtraceback.reprentries
    graph = {
        'type': type(value).__name__,
        'traceback': type(value.reprtraceback).__name__,
        'style': value.reprtraceback.style,
        'entries': [{'type': type(entry).__name__, 'style': entry.style,
                     'lines': list(entry.lines),
                     'args': None if entry.reprfuncargs is None else list(entry.reprfuncargs.args),
                     'file': [entry.reprfileloc.path,entry.reprfileloc.lineno,
                              entry.reprfileloc.message]}
                    for entry in entries],
        'crash': [type(value.reprcrash).__name__,Path(value.reprcrash.path).name,
                  value.reprcrash.lineno,value.reprcrash.message],
    }
    events = []
    sentinel = RuntimeError('actual terminal error')
    class Writer:
        fullwidth = 40
        def line(self, text='', **markup):
            events.append(['line',text,markup])
            if MODE == 'writer_error': raise sentinel
            if MODE == 'reentry' and len(events)==1:
                entries[-1].lines.append('E   CALLBACK APPENDED')
        def write(self, text, **markup): events.append(['write',text,markup])
        def _write_source(self, lines, indents): events.append(['source',list(lines),list(indents)])
        def sep(self, sep, title=None, **markup): events.append(['sep',sep,title,markup])
    if MODE == 'mutate':
        entries[-1].lines.append('E   MUTATED GRAPH')
        value.reprcrash.message = 'changed crash'
        value.addsection('section title','section body','~')
    error = None
    try: value.toterminal(Writer())
    except RuntimeError as caught: error = [caught is sentinel,str(caught)]
    Path('observations.json').write_text(json.dumps({'graph':graph,'events':events,'error':error,
        'text':str(value),'lines':list(entries[-1].lines)}))
    return report
"""

_ROUNDTRIP = """import json, pytest
from pathlib import Path

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when != 'call': return report
    original = report.longrepr
    report.extra = ['shared']
    original.addsection('original','body')
    calls = []
    original_to_json = report._to_json
    def actual_to_json():
        calls.append('serialize')
        report.extra.append('callback')
        return original_to_json()
    report._to_json = actual_to_json
    wire = report._to_json()
    clone = type(report)._from_json(wire)
    value = clone.longrepr
    assert calls == ['serialize']
    assert value is not original
    assert value.reprtraceback is not original.reprtraceback
    assert value.reprcrash is not original.reprcrash
    assert value.chain[-1][0] is value.reprtraceback
    assert value.chain[-1][1] is value.reprcrash
    assert clone.extra is report.extra
    assert clone.sections is not report.sections
    assert clone.user_properties is not report.user_properties
    value.reprtraceback.reprentries[-1].lines.append('E   CLONE ONLY')
    assert 'E   CLONE ONLY' not in original.reprtraceback.reprentries[-1].lines
    Path('observations.json').write_text(json.dumps({'calls':calls,'extra':clone.extra,
        'type':type(value).__name__,'traceback':type(value.reprtraceback).__name__,
        'crash': [Path(value.reprcrash.path).name,value.reprcrash.lineno,value.reprcrash.message],
        'sections':value.sections,'chain_aliases':True,'distinct':True}))
    return report
"""


def _run(project: VfyProject, engine: str, style: str, *, status: int = 1) -> dict[str, object]:
    arguments = ["-q", f"--tb={style}"]
    result = (
        project.run_python("-m", "pytest", *arguments)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status.json", json.dumps(result.returncode))
    assert result.returncode == status, result.stdout + result.stderr
    observation = json.loads((project.root / "observations.json").read_text())
    assert isinstance(observation, dict)
    result: dict[str, object] = {}
    for key, value in observation.items():
        assert isinstance(key, str)
        result[key] = value
    return result


@pytest.mark.parametrize("style", ["auto", "long", "short"])
@pytest.mark.parametrize("mode", ["plain", "mutate", "writer_error", "reentry"])
def test_failure_graph_styles_aliases_mutations_and_live_writer_calls(
    tmp_path: Path, style: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", _BODY)
    project.write("conftest.py", _OBSERVER.replace("MODE", repr(mode)))
    expected = _run(project, "stock", style)
    actual = _run(project, "independent", style)
    assert actual == expected
    assert expected["error"] == (
        [True, "actual terminal error"] if mode == "writer_error" else None
    )


@pytest.mark.parametrize("style", ["long", "short"])
def test_failed_report_roundtrip_reconstructs_graph_and_preserves_extra_aliases(
    tmp_path: Path, style: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", _BODY)
    project.write("conftest.py", _ROUNDTRIP)
    expected = _run(project, "stock", style)
    actual = _run(project, "independent", style)
    assert actual == expected


_NATIVE_OBSERVER = """import json,pytest
from pathlib import Path
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item,call):
    report=yield
    if report.when!='call': return report
    value=report.longrepr
    assert type(value).__name__=='ExceptionChainRepr'
    assert value.chain[-1][0] is value.reprtraceback
    assert value.chain[-1][1] is value.reprcrash
    nodes=[]
    for traceback,crash,description in value.chain:
        nodes.append({'type':type(traceback).__name__, 'style':traceback.style,
            'entries':[[type(entry).__name__,list(entry.lines)]
                       for entry in traceback.reprentries],
            'extraline':traceback.extraline, 'relation':description,
            'crash':None if crash is None else [Path(crash.path).name,crash.lineno,crash.message]})
    Path('observations.json').write_text(json.dumps({'nodes':nodes}))
    return report
"""


@pytest.mark.parametrize("mode", ["group", "mixed_group", "missing_cause"])
def test_exception_graph_retains_actual_native_fallback_lines(tmp_path: Path, mode: str) -> None:
    bodies = {
        "group": "def test_body():\n    raise ExceptionGroup('group', [ValueError('inner')])\n",
        "mixed_group": """def test_body():
    try:
        raise ExceptionGroup('group', [ValueError('inner')])
    except ExceptionGroup as error:
        raise RuntimeError('outer') from error
""",
        "missing_cause": (
            "def test_body():\n    raise RuntimeError('outer') from ValueError('missing')\n"
        ),
    }
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", bodies[mode])
    project.write("conftest.py", _NATIVE_OBSERVER)
    expected = _run(project, "stock", "long")
    actual = _run(project, "independent", "long")
    assert actual == expected


_DIRECT_STYLE = """from pathlib import Path
import json

def pytest_runtest_makereport(item,call):
    if call.when!='call':return
    kwargs={} if REQUEST is None else {'style':REQUEST}
    graph=item._repr_failure_py(call.excinfo,**kwargs)
    assert graph.chain[-1][0] is graph.reprtraceback
    assert graph.chain[-1][1] is graph.reprcrash
    observed={'style':graph.reprtraceback.style,
              'entries':[entry.style for entry in graph.reprtraceback.reprentries]}
    Path('observations.json').write_text(json.dumps(observed))
"""


@pytest.mark.parametrize("configured", ["auto", "long", "short"])
@pytest.mark.parametrize("requested", [None, "auto", "long", "short"])
def test_direct_item_failure_style_uses_configured_filter_and_omitted_default(
    tmp_path: Path, configured: str, requested: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", _BODY)
    project.write("conftest.py", _DIRECT_STYLE.replace("REQUEST", repr(requested)))
    expected = _run(project, "stock", configured)
    actual = _run(project, "independent", configured)
    assert actual == expected
