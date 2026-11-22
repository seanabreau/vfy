"""Native option objects preserve caller values and configured arguments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("trylast", [False, True])
def test_raw_plugins_preserve_sessionfinish_errors_and_cleanup_priority(
    tmp_path: Path, trylast: bool
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "report_plugin.py",
            f"""import pytest
from pathlib import Path
def event(value):
    with Path('events').open('a') as stream: stream.write(value+'\\n')
def pytest_configure(config): config.add_cleanup(lambda:event('cleanup'))
def pytest_runtest_logreport(report):
    if report.when=='call': raise RuntimeError('report hook sentinel')
@pytest.hookimpl(trylast={trylast!r})
def pytest_sessionfinish():
    event('sessionfinish')
    raise ValueError('finish hook sentinel')
def pytest_unconfigure(): event('unconfigure')
""",
        )
        project.write(
            "test_body.py",
            """import pytest
from report_plugin import event
@pytest.fixture
def resource():
    yield
    event('fixture-finalized')
def test_body(resource): pass
""",
        )
        result = project.run_engine(route, "-q", "-p", "report_plugin")
        output = result.stdout + result.stderr
        observations.append(
            (
                result.returncode,
                (project.root / "events").read_text(),
                "ValueError: finish hook sentinel" in output,
            )
        )
    assert observations[0] == observations[1]
    assert observations[1][0] != 0
    assert observations[1][2]
    expected = "sessionfinish\nunconfigure\ncleanup\n"
    if trylast:
        expected = "fixture-finalized\n" + expected
    assert observations[1][1] == expected


def _option_project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = -q -k selected -p second_plugin "
        "--custom-count=7 --opaque=from-config\n",
    )
    project.write(
        "bridge.py",
        """str_calls=0
class Opaque:
    def __str__(self):
        global str_calls
        str_calls+=1
        raise AssertionError('opaque option was stringified')
raw=Opaque()
configured=Opaque()
cli=Opaque()
plugins=['report_plugin']
def decode(value):
    return {'from-config':configured,'from-cli':cli}[value]
""",
    )
    project.write(
        "report_plugin.py",
        """import bridge
from pathlib import Path
def pytest_addoption(parser): parser.addoption('--opaque',type=bridge.decode,default=None)
def pytest_configure(config): Path('raw-plugin-active').touch()
""",
    )
    project.write(
        "second_plugin.py",
        """import bridge,json
from pathlib import Path
def pytest_addoption(parser): parser.addoption('--custom-count',type=int,default=0)
def pytest_unconfigure(config):
    Path('bridge-str-calls').write_text(str(bridge.str_calls))
def pytest_configure(config):
    Path('options.json').write_text(json.dumps({
        'count':config.getoption('custom_count'),
        'verbose':config.getoption('verbose'),
        'opaque_cli':config.option.opaque is bridge.cli,
        'raw_plugin':config.pluginmanager.hasplugin('report_plugin'),
        'configured_plugin':config.pluginmanager.hasplugin('second_plugin'),
    }))
""",
    )
    project.write(
        "test_body.py",
        "from pathlib import Path\n"
        "def test_selected(): Path('selected.body').touch()\n"
        "def test_other(): Path('other.body').touch()\n",
    )
    return project


@pytest.mark.parametrize("mode", ["config", "cli"])
def test_explicit_arguments_preserve_configuration_and_selection(tmp_path: Path, mode: str) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = _option_project(tmp_path / route)
        arguments = ["-p", "report_plugin", "--opaque=from-config"]
        if mode == "cli":
            arguments += ["--custom-count=9", "--opaque=from-cli", "-k", "other"]
        result = project.run_engine(route, *arguments)
        result.expect(0)
        assert (project.root / "raw-plugin-active").exists()
        assert (project.root / "bridge-str-calls").read_text() == "0"
        observations.append(
            (
                json.loads((project.root / "options.json").read_text()),
                sorted(path.name for path in project.root.glob("*.body")),
            )
        )
    assert observations[0] == observations[1]
    options, bodies = observations[1]
    assert options == {
        "count": 9 if mode == "cli" else 7,
        "verbose": -1,
        "opaque_cli": mode == "cli",
        "raw_plugin": True,
        "configured_plugin": True,
    }
    assert bodies == ["selected.body" if mode == "config" else "other.body"]
