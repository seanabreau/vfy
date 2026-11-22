"""Per-hook invocation observations follow real hook dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_alias_counts_survive_unregister_and_reregistration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

class Specifications:
    @pytest.hookspec
    def pytest_count_probe(value): pass
    @pytest.hookspec(firstresult=True)
    def pytest_count_first(value): pass

def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(Specifications)

def pytest_sessionfinish():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_counts.py",
        """import pytest
import conftest

class Aliases:
    def __init__(self, manager): self.manager = manager

    @pytest.hookimpl(specname='pytest_count_probe', tryfirst=True)
    def pytest_alias_first(self, value):
        conftest.events.append(['first', value])
        self.manager.unregister(self)

    @pytest.hookimpl(specname='pytest_count_probe')
    def pytest_alias_second(self, value):
        conftest.events.append(['second', value])

class Faulty:
    def pytest_count_probe(self, value):
        conftest.events.append(['raise', value])
        raise ValueError('hook failed')

class FirstResult:
    @pytest.hookimpl(specname='pytest_count_first', tryfirst=True)
    def pytest_first(self, value):
        conftest.events.append(['firstresult', value])
        return 'stop'

    @pytest.hookimpl(specname='pytest_count_first')
    def pytest_unreached(self, value):
        raise AssertionError('firstresult did not stop')

def test_counts(pytestconfig):
    manager = pytestconfig.pluginmanager
    plugin = Aliases(manager)
    manager.register(plugin, 'counter-one')
    manager.hook.pytest_count_probe(value=1)
    manager.hook.pytest_count_probe(value=2)
    manager.register(plugin, 'counter-two')
    manager.hook.pytest_count_probe(value=3)
    faulty = Faulty()
    manager.register(faulty, 'counter-failing')
    with pytest.raises(ValueError, match='hook failed'):
        manager.hook.pytest_count_probe(value=4)
    manager.unregister(faulty)
    firstresult = FirstResult()
    manager.register(firstresult, 'counter-firstresult')
    assert manager.hook.pytest_count_first(value=5) == 'stop'
    manager.unregister(firstresult)
""",
    )
    oracle = project.run_pytest("-q", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((project.root / "events.json").read_text())
    assert expected == [
        ["first", 1],
        ["second", 1],
        ["first", 3],
        ["second", 3],
        ["raise", 4],
        ["firstresult", 5],
    ]

    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--native-result-json=record.json", "-q"
    )
    result.expect(0)
    assert json.loads((project.root / "events.json").read_text()) == expected
    record = json.loads((project.root / "record.json").read_text())
    rows = {row["name"]: row for row in record["plugins"] if row["name"].startswith("counter-")}
    assert set(rows) == {"counter-one", "counter-two", "counter-failing", "counter-firstresult"}
    assert rows["counter-one"]["generation"] != rows["counter-two"]["generation"]
    assert {name: row["hook_calls"] for name, row in rows.items()} == {
        "counter-one": {"pytest_count_probe": 2},
        "counter-two": {"pytest_count_probe": 2},
        "counter-failing": {"pytest_count_probe": 1},
        "counter-firstresult": {"pytest_count_first": 1},
    }
