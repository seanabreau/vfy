"""Keep public cache access live after publishing worker collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest

_HOOKS = """
import json, os
from pathlib import Path
import pytest

def role():
    return os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')

def observe(config, stage):
    cache = config.cache
    current = role()
    if current != 'controller':
        experimental = os.environ.get('VFY_WORKER_COLLECTION') == '1'
        assert cache._session.has_selection is not experimental
    directory = cache.mkdir('worker-' + current)
    key = 'proof/' + stage + '-' + current
    cache.set(key, {'stage': stage, 'role': current})
    assert cache.get(key, None) == {'stage': stage, 'role': current}
    values = {
        'nodeids': cache.get('cache/nodeids', []),
        'lastfailed': cache.get('cache/lastfailed', {}),
        'payload': cache.get('proof/seed', None),
        'directory_exists': directory.is_dir(),
        'roundtrip': cache.get(key, None),
    }
    Path(stage + '-' + current + '.json').write_text(json.dumps(values))
    return values

@pytest.fixture
def live_cache(request):
    return observe(request.config, 'fixture')

def pytest_sessionfinish(session, exitstatus):
    observe(session.config, 'finish')
"""

_CASES = """
def test_first(live_cache):
    assert live_cache['nodeids'] == ['historical.py::test_old']
    assert live_cache['payload'] == {'saved': [1, 'cache-value']}

def test_second(live_cache):
    assert live_cache['lastfailed'] == {'test_cases.py::test_second': True}
    assert live_cache['directory_exists']
"""


def _cache(project: VfyProject, key: str) -> object:
    return json.loads((project.root / ".pytest_cache" / "v" / key).read_text())


@pytest.mark.parametrize("selection", [(), ("--nf",), ("--lf",)])
def test_published_workers_keep_fixture_and_finish_cache_access(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for name, project in projects.items():
        project.write("conftest.py", _HOOKS)
        project.write("test_cases.py", _CASES)
        project.write(".pytest_cache/v/cache/nodeids", '["historical.py::test_old"]')
        project.write(".pytest_cache/v/cache/lastfailed", '{"test_cases.py::test_second":true}')
        project.write(".pytest_cache/v/proof/seed", '{"saved":[1,"cache-value"]}')
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=2",
            "--no-replay",
            "--no-cov",
            *selection,
            "-q",
            env={
                "VFY_WORKER_COLLECTION": "1" if name == "candidate" else "0",
                "VFY_NATIVE_SCHEDULING": "cases",
            },
        )
        result.expect(0)
        count = 1 if selection == ("--lf",) else 2
        assert f"{count} passed" in result.stdout
        selected_nodeids = (
            ["test_cases.py::test_second"]
            if selection == ("--lf",)
            else ["test_cases.py::test_first", "test_cases.py::test_second"]
        )
        assert _cache(project, "cache/nodeids") == ["historical.py::test_old", *selected_nodeids]
        assert _cache(project, "cache/lastfailed") == {}
        assert _cache(project, "proof/seed") == {"saved": [1, "cache-value"]}
        worker_roles = {str(index) for index in range(count)}
        assert {path.name for path in project.root.glob("fixture-*.json")} == {
            f"fixture-{role}.json" for role in worker_roles
        }
        assert {path.name for path in project.root.glob("finish-*.json")} == {
            f"finish-{role}.json" for role in {"controller", *worker_roles}
        }
    observations = {
        name: {path.name: json.loads(path.read_text()) for path in project.root.glob("*-*.json")}
        for name, project in projects.items()
    }
    assert observations["candidate"] == observations["baseline"]
