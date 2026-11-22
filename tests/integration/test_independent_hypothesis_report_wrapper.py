"""The admitted Hypothesis report wrapper remains callable by plug-ins."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_manual_hypothesis_report_wrapper_preserves_result_and_error(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_wrapper.py",
            """\
import inspect
import json
from pathlib import Path
from types import SimpleNamespace


def test_manual(request):
    implementations = request.config.hook.pytest_runtest_makereport.get_hookimpls()
    implementation = next(
        entry for entry in implementations if entry.plugin_name == 'hypothesispytest'
    )
    item = request.node
    item.hypothesis_report_information = 'manual report information'
    report = SimpleNamespace(when='call', sections=[])
    class Outcome:
        def get_result(self): return report
    wrapper = implementation.function(item, None)
    assert next(wrapper) is None
    try:
        wrapper.send(Outcome())
    except StopIteration:
        pass
    else:
        raise AssertionError('wrapper did not finish')
    del item.hypothesis_report_information
    class Failure:
        def get_result(self): raise ValueError('legacy outcome sentinel')
    wrapper = implementation.function(item, None)
    assert next(wrapper) is None
    try:
        wrapper.send(Failure())
    except ValueError as error:
        failure = str(error)
    else:
        raise AssertionError('wrapper suppressed the outcome error')
    Path('manual.json').write_text(json.dumps({
        'sections': report.sections,
        'failure': failure,
        'native': inspect.isbuiltin(implementation.function),
    }))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-p", "hypothesispytest", "-q", load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-p",
                "hypothesispytest",
                "-q",
                env=env,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads((project.root / "manual.json").read_text())
        assert data.pop("native") is (engine == "native")
        observations[engine] = data
    assert (
        observations["native"]
        == observations["pytest"]
        == {
            "sections": [["Hypothesis", "manual report information"]],
            "failure": "legacy outcome sentinel",
        }
    )
