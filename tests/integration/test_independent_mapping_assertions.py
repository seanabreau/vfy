"""Mapping comparison diagnostics match stock pytest through the public CLI."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_TESTS = """from collections import OrderedDict
from collections.abc import MutableMapping
import json
import os
from pathlib import Path
import pytest

class Environment(MutableMapping):
    def __init__(self, values): self.data = dict(values)
    def __getitem__(self, key): return self.data[key]
    def __setitem__(self, key, value): self.data[key] = value
    def __delitem__(self, key): del self.data[key]
    def __iter__(self): return iter(self.data)
    def __len__(self): return len(self.data)
    def __repr__(self): return 'environ(' + repr(self.data) + ')'

class BrokenIteration(Environment):
    def __eq__(self, other): return False
    def __iter__(self):
        raise RuntimeError('mapping iteration failed')

@pytest.mark.parametrize('kind', ['dict', 'ordered', 'environment'])
@pytest.mark.parametrize('difference', ['different', 'left-extra', 'right-extra'])
def test_comparison(kind, difference):
    factory = {'dict': dict, 'ordered': OrderedDict, 'environment': Environment}[kind]
    if difference == 'different':
        left, right = factory({'a':1, 'same':3}), {'a':2, 'same':3}
    elif difference == 'left-extra':
        left, right = factory({'a':1}), {}
    else:
        left, right = factory({}), {'a':1}
    assert left == right

def test_equal_comparison_does_not_describe():
    left = Environment({'a':1})
    assert left == {'a':1}

def test_ordered_mapping_equality_is_order_sensitive():
    left = OrderedDict([('a',1), ('b',2)])
    right = OrderedDict([('b',2), ('a',1)])
    assert left == right

def test_mapping_iteration_failure_preserves_assertion():
    left = BrokenIteration({'a':1})
    assert left == {'a':2}

class BrokenLookup(Environment):
    def __eq__(self, other): return False
    def __getitem__(self, key):
        raise RuntimeError('mapping lookup failed')

def test_mapping_lookup_failure_keeps_the_extra_heading():
    left = BrokenLookup({'a':1})
    assert left == {}
"""

_OBSERVER = """import json
import os
from pathlib import Path
import pytest

reports = []
def pytest_runtest_logreport(report):
    crash = getattr(report.longrepr, 'reprcrash', None)
    reports.append({
        'nodeid': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
        'exception': getattr(crash, 'message', None),
        'diagnostic': str(report.longrepr) if report.longrepr is not None else None,
        'stdout': report.capstdout, 'stderr': report.capstderr, 'log': report.caplog,
    })

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    Path(os.environ['MAPPING_REPORT']).write_text(json.dumps(reports, sort_keys=True))
"""


def test_mapping_explanations_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_mapping.py", _TESTS)
    project.write("conftest.py", _OBSERVER)
    records = {}
    for engine in ("stock", "native"):
        output = tmp_path / f"{engine}.json"
        environment = {
            "MAPPING_REPORT": str(output),
            "PYTHONHASHSEED": "0",
            # This fixture protects the renderer's default, non-CI verbosity.
            "CI": None,
            "BUILD_NUMBER": None,
        }
        result = (
            project.run_pytest("--tb=long", "test_mapping.py", load_xdist=False, env=environment)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--tb=long",
                "test_mapping.py",
                env=environment,
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        records[engine] = json.loads(output.read_text())
    assert records["native"] == records["stock"]
