"""Group crash messages use the actual singleton leaf without changing native lines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_GROUP_BODIES = {
    "single": "raise ExceptionGroup('group', [ValueError('inner')])",
    "nested": "raise ExceptionGroup('outer', [ExceptionGroup('inner', [ValueError('leaf')])])",
    "branching": "raise ExceptionGroup('many', [ValueError('first'), TypeError('second')])",
    "inner_branch": (
        "raise ExceptionGroup('outer', [ExceptionGroup('many', "
        "[ValueError('first'), TypeError('second')])])"
    ),
    "empty_view": """class EmptyView(ExceptionGroup):
        @property
        def exceptions(self):
            return ()
    raise EmptyView('empty', [ValueError('inner')])""",
    "mixed": """try:
        raise ExceptionGroup('group', [ValueError('inner')])
    except ExceptionGroup as error:
        raise RuntimeError('outer') from error""",
    "value": """try:
        raise ExceptionGroup('group', [ValueError('inner')])
    except ExceptionGroup:
        pytest.fail('outer', pytrace=False)""",
}
_OBSERVER = """import json,pytest
from pathlib import Path
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'call':
        graph = report.longrepr
        Path('observations.json').write_text(json.dumps({
            'kind': type(graph).__name__,
            'aliases': [graph.chain[-1][0] is graph.reprtraceback,
                        graph.chain[-1][1] is graph.reprcrash],
            'crashes': [None if crash is None else [Path(crash.path).name,
                        crash.lineno, crash.message] for _, crash, _ in graph.chain],
        }))
    return report
"""


def _run(project: VfyProject, engine: str) -> dict[str, object]:
    (project.root / "observations.json").unlink(missing_ok=True)
    (project.root / "body-ran").unlink(missing_ok=True)
    result = (
        project.run_python("-m", "pytest", "-q", "--tb=long")
        if engine == "stock"
        else project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-q",
            "--tb=long",
            env={"GROUP_NATIVE": "1"},
        )
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == 1, result.stdout + result.stderr
    assert (project.root / "body-ran").is_file()
    observed = json.loads((project.root / "observations.json").read_text())
    assert isinstance(observed, dict)
    checked: dict[str, object] = {}
    for key, value in observed.items():
        assert isinstance(key, str)
        checked[key] = value
    return checked


@pytest.mark.parametrize("mode", list(_GROUP_BODIES))
def test_group_crash_messages_share_the_actual_report_and_value_routes(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER)
    body = _GROUP_BODIES[mode]
    project.write(
        "test_body.py",
        "import pytest\nfrom pathlib import Path\n\ndef test_body():\n"
        "    Path('body-ran').touch()\n    " + body + "\n",
    )
    expected = _run(project, "stock")
    actual = _run(project, "independent")
    assert actual == expected
    assert expected["kind"] == "ExceptionChainRepr"
    assert expected["aliases"] == [True, True]
