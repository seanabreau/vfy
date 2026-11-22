"""Traceback arguments reach the configuration and failed report graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import native_engines

_CASES = [
    ("default", "", "", [], "auto"),
    ("long", "", "", ["--tb=long"], "long"),
    ("short", "", "", ["--tb", "short"], "short"),
    ("auto", "", "", ["--tb=auto"], "auto"),
    ("last", "", "", ["--tb=short", "--tb", "long"], "long"),
    ("ini", "--tb=short", "", [], "short"),
    ("env", "", "--tb=long", [], "long"),
    ("precedence", "--tb=long", "--tb=short", ["--tb=auto"], "auto"),
    ("override", "--tb=short", "", ["-o", "addopts=--tb=long"], "long"),
    ("argument_file", "", "", ["@arguments.txt"], "short"),
]
_OBSERVER = """import json
from pathlib import Path
import pytest

def pytest_configure(config):
    Path('options.json').write_text(json.dumps({
        'dest': config.getoption('tbstyle'),
        'alias': config.getoption('--tb'),
        'namespace': config.option.tbstyle,
        'known': config.known_args_namespace.tbstyle,
        'false_long_alias': config.getoption('--tbstyle', default='absent'),
        'false_dest_alias': config.getoption('tb', default='absent'),
    }))

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'call' and report.failed:
        graph = report.longrepr
        Path('graph.json').write_text(json.dumps({
            'kind': type(graph).__name__,
            'style': graph.reprtraceback.style,
            'traceback_alias': graph.chain[-1][0] is graph.reprtraceback,
            'crash_alias': graph.chain[-1][1] is graph.reprcrash,
            'live': item.config.getoption('--tb'),
        }))
    return report
"""


def _run(
    project: VfyProject,
    engine: str,
    arguments: list[str],
    *,
    addopts: str = "",
) -> CommandResult:
    env = {"PYTEST_ADDOPTS": addopts}
    result = project.run_engine(engine, "-q", *arguments, env=env)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    return result


@native_engines
@pytest.mark.parametrize(("case", "ini", "environment", "arguments", "expected"), _CASES)
def test_traceback_option_precedence_reaches_real_item_and_failed_graph(
    tmp_path: Path,
    engine: str,
    case: str,
    ini: str,
    environment: str,
    arguments: list[str],
    expected: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\naddopts = {ini}\n")
    project.write("arguments.txt", "--tb\nshort\n")
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_body.py",
        """from pathlib import Path

def explode(value):
    raise ValueError('actual traceback failure')

def test_body(request):
    assert request.node.config.getoption('--tb') == request.config.option.tbstyle
    Path('body-ran').write_text('reached')
    explode(17)
""",
    )
    result = _run(project, engine, arguments, addopts=environment)
    result.expect(1)
    assert (tmp_path / "body-ran").read_text() == "reached"
    assert json.loads((tmp_path / "options.json").read_text()) == {
        "dest": expected,
        "alias": expected,
        "namespace": expected,
        "known": expected,
        "false_long_alias": "absent",
        "false_dest_alias": "absent",
    }
    assert json.loads((tmp_path / "graph.json").read_text()) == {
        "kind": "ExceptionChainRepr",
        "style": "short" if expected == "short" else "long",
        "traceback_alias": True,
        "crash_alias": True,
        "live": expected,
    }
    assert "actual traceback failure" in result.stdout
    assert result.stderr == ""


@native_engines
@pytest.mark.parametrize(
    "arguments", [["--tb"], ["--tb=unknown"], ["--tbstyle=short"], ["--t=short"]]
)
def test_invalid_traceback_arguments_are_usage_errors_before_body(
    tmp_path: Path, engine: str, arguments: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    result = _run(project, engine, arguments)
    result.expect(4)
    assert not (tmp_path / "body-ran").exists()
    assert "error" in result.stderr.lower()


@native_engines
@pytest.mark.parametrize("style", ["no", "line", "native"])
def test_remaining_traceback_styles_run_the_body(tmp_path: Path, engine: str, style: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    result = _run(project, engine, [f"--tb={style}"])
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()
