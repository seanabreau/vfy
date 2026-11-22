"""The maintained adapter preserves the active Hypothesis plugin's public effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = {
    "given": """@given(st.integers(min_value=0, max_value=1000))
def test_property(value):
    record(value)
""",
    "shrinking": """@given(st.integers(min_value=0, max_value=10))
def test_property(value):
    record(value)
    assert value < 3
""",
    "healthcheck": """@given(st.integers(min_value=0, max_value=1000))
def test_property(resource, value):
    resource.append(value)
    record(len(resource))
""",
    "fixture_reuse": """@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=0, max_value=1000))
def test_property(resource, value):
    resource.append(value)
    record(len(resource))
""",
    "parametrize": """@pytest.mark.parametrize('label', ['left', 'right'])
@given(st.integers(min_value=0, max_value=1000))
def test_property(label, value):
    record([label, value])
""",
    "fixture_params": """@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.integers(min_value=0, max_value=1000))
def test_property(parameter, value):
    record([parameter, value])
""",
    "settings_without_given": """@settings(max_examples=2)
def test_property():
    record('body must not run')
""",
    "given_then_fixture": """@pytest.fixture
@given(st.integers())
def resource(value):
    return value

def test_property(resource):
    record(resource)
""",
    "fixture_then_given": """@given(st.integers())
@pytest.fixture
def resource(value):
    return value

def test_property(resource):
    record(resource)
""",
}


def _project(root: Path, case: str) -> VfyProject:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\nnorecursedirs =\n")
    project.write(
        ".hypothesis/test_forbidden.py", "raise AssertionError('must remain uncollected')\n"
    )
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
from hypothesis import settings

settings.register_profile('vfy-small', max_examples=3, database=None, deadline=None)
reports, markers, fixture_events = [], [], []

@pytest.fixture
def resource():
    values = []
    fixture_events.append('setup')
    yield values
    fixture_events.append(['teardown', len(values)])

@pytest.fixture(params=['left', 'right'], ids=['left', 'right'])
def parameter(request):
    return request.param

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    markers.extend([item.nodeid, item.get_closest_marker('hypothesis') is not None]
                   for item in items)

def pytest_runtest_logreport(report):
    reports.append([report.nodeid, report.when, report.outcome,
                    any(name == 'Hypothesis' for name, text in report.sections),
                    isinstance(report.__dict__.get('_hypothesis_stats'), str)])

def pytest_unconfigure():
    Path('plugin-effects.json').write_text(json.dumps([reports, markers, fixture_events]))
""",
    )
    project.write(
        "test_property.py",
        """import json, pytest
from pathlib import Path
from hypothesis import given, settings, HealthCheck, strategies as st

def record(value):
    with Path('examples').open('a') as output:
        output.write(json.dumps(value) + '\\n')

"""
        + _CASES[case],
    )
    return project


@pytest.mark.parametrize(
    ("plugin", "case"),
    [("hypothesispytest", case) for case in _CASES]
    + [
        ("hypothesis.extra.pytestplugin", case)
        for case in ("given", "healthcheck", "fixture_params")
    ],
)
def test_active_hypothesis_plugin_preserves_generation_fixtures_and_profiles(
    tmp_path: Path, plugin: str, case: str
) -> None:
    project = _project(tmp_path, case)
    options = (
        "-v",
        "-p",
        plugin,
        "--hypothesis-profile=vfy-small",
        "--hypothesis-seed=17",
        "--hypothesis-show-statistics",
    )
    stock = project.run_python("-m", "pytest", *options)
    status = (
        2
        if case in {"given_then_fixture", "fixture_then_given"}
        else (1 if case in {"shrinking", "healthcheck", "settings_without_given"} else 0)
    )
    assert stock.returncode == status, stock.stdout + stock.stderr
    expected_effects = json.loads((tmp_path / "plugin-effects.json").read_text())
    examples = tmp_path / "examples"
    expected_examples = examples.read_text().splitlines() if examples.exists() else []
    project.write("stock-effects.json", json.dumps(expected_effects))
    project.write("stock-examples.json", json.dumps(expected_examples))
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    if examples.exists():
        examples.unlink()
    (tmp_path / "plugin-effects.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--native-result-json=results.json",
        *options,
    )
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "plugin-effects.json").read_text()) == expected_effects
    assert (examples.read_text().splitlines() if examples.exists() else []) == expected_examples
    for result in (stock, candidate):
        assert "hypothesis profile 'vfy-small'" in result.stdout
        assert "must remain uncollected" not in result.stdout + result.stderr
        if case == "shrinking":
            assert "value=3" in result.stdout + result.stderr
        if case == "healthcheck":
            assert "function-scoped fixture 'resource'" in result.stdout + result.stderr


@pytest.mark.parametrize("workers", [1, 2])
def test_hypothesis_summary_survives_report_copy_and_worker_transport(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = sample: summary witness\n")
    project.write(
        "conftest.py",
        "import json, os, pytest\n"
        "from pathlib import Path\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    return type(report)._from_json(report._to_json())\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'teardown':\n"
        "        with Path('observed-' + str(os.getpid())).open('a') as output:\n"
        "            output.write(json.dumps([report.nodeid, 'sample' in report.keywords, "
        "bool(report.__dict__.get('_hypothesis_stats'))]) + '\\n')\n",
    )
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            "import pytest\n"
            "from hypothesis import given, settings, strategies as st\n"
            "@pytest.mark.sample\n"
            "@settings(max_examples=2, database=None, deadline=None)\n"
            "@given(st.integers())\n"
            "def test_property(value): pass\n",
        )
    args = ("-p", "hypothesispytest", "--hypothesis-show-statistics", "-q")
    stock = project.run_python("-m", "pytest", *args)
    expected = sorted(
        json.loads(line)
        for path in tmp_path.glob("observed-*")
        for line in path.read_text().splitlines()
    )
    for path in tmp_path.glob("observed-*"):
        path.unlink()
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=record.json",
        *args,
        env={"VFY_WORKER_START": "exec"},
    )
    diagnostic = stock.stdout + stock.stderr + result.stdout + result.stderr
    assert stock.returncode == result.returncode == 0, diagnostic
    actual = sorted(
        json.loads(line)
        for path in tmp_path.glob("observed-*")
        for line in path.read_text().splitlines()
    )
    assert len(expected) == 2, diagnostic
    assert all(row[1:] == [True, True] for row in expected), diagnostic
    assert actual == expected, diagnostic
    record = json.loads((tmp_path / "record.json").read_text())
    topology = record["execution_topology"]
    assert topology["active_workers"] == workers, diagnostic
    assert len(set(topology["worker_pids"])) == workers, diagnostic
    teardowns = [
        phase
        for case in record["cases"]
        for phase in case["phases"]
        if phase["phase"] == "teardown"
    ]
    assert len(teardowns) == 2, diagnostic
    for teardown in teardowns:
        summary = teardown["report_summary"]
        assert "sample" in summary["keywords"], diagnostic
        assert summary["hypothesis_statistics"], diagnostic
    assert "Hypothesis Statistics" in stock.stdout, diagnostic
    assert "Hypothesis Statistics" in result.stdout, diagnostic
