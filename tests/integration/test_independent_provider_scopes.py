"""Fixture lookup diagnostics retain the final visible provider scopes."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import gc
import json
from pathlib import Path
import pytest
from shared import released, saved

@pytest.fixture
def root_fixture(): pass

def pytest_sessionfinish():
    result = {}
    for label, error, cycle in saved:
        representation = error.formatrepr()
        line = next(line for line in representation.errorstring.splitlines()
                    if 'available fixtures:' in line)
        names = set(line.split('available fixtures:', 1)[1].strip().split(', '))
        interesting = {'root_fixture', 'a_fixture', 'module_fixture',
                       'class_fixture', 'late_fixture', 'plugin_fixture'}
        result[label] = sorted(names & interesting)
    gc.collect()
    definition = saved[0][1].request._fixture_defs['root_fixture']
    cls = type(definition)
    original = cls.__dict__.get('argname')
    def renamed(current):
        name = current.__dict__['argname'] if original is None else original.__get__(current, cls)
        return 'renamed_root' if name == 'root_fixture' else name
    cls.argname = property(renamed)
    try:
        line = next(line for line in saved[0][1].formatrepr().errorstring.splitlines()
                    if 'available fixtures:' in line)
        result['mutated_name'] = 'renamed_root' in line and 'root_fixture' not in line
    finally:
        if original is None:
            del cls.argname
        else:
            cls.argname = original
    del error, cycle, definition
    saved.clear()
    gc.collect()
    result['released'] = sorted(released)
    Path('provider-scopes.json').write_text(json.dumps(result, sort_keys=True))
"""


_SHARED = """\
from pathlib import Path
import pytest

saved = []
released = []

class Sentinel:
    def __init__(self, label): self.label = label
    def __del__(self): released.append(self.label)

def save_missing(request, label):
    try:
        request.getfixturevalue('missing_fixture')
    except pytest.FixtureLookupError as error:
        cycle = [error.request, Sentinel(label)]
        cycle.append(cycle)
        saved.append((label, error, cycle))
    else:
        raise AssertionError('missing fixture unexpectedly resolved')
"""


def test_provider_scopes_match_pytest_for_retained_errors(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write("shared.py", _SHARED)
        project.write(
            "fixture_plugin.py", "import pytest\n@pytest.fixture\ndef plugin_fixture(): pass\n"
        )
        project.write("a/conftest.py", "import pytest\n@pytest.fixture\ndef a_fixture(): pass\n")
        project.write(
            "a/test_first.py",
            "import pytest\nfrom shared import save_missing\n"
            "@pytest.fixture\ndef module_fixture(): pass\n"
            "class TestFixture:\n"
            "    @pytest.fixture\n    def class_fixture(self): pass\n"
            "    def test_first(self, request, root_fixture): save_missing(request, 'class')\n",
        )
        project.write(
            "ab/test_register.py",
            "from shared import save_missing\n"
            "def test_register(request): save_missing(request, 'sibling')\n",
        )
        project.write(
            "z/test_after.py",
            "import pytest\nfrom shared import save_missing\n"
            "@pytest.fixture\ndef late_fixture(): pass\n"
            "def test_after(request): save_missing(request, 'after')\n",
        )
        result = (
            project.run_pytest("-p", "fixture_plugin", "-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-p",
                "fixture_plugin",
                "-q",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
        observations[engine] = json.loads((project.root / "provider-scopes.json").read_text())
    # pytest reads its fixture-manager name keys; the independent engine's
    # existing diagnostic reads each binding's live argname descriptor.
    assert observations["pytest"].pop("mutated_name") is False
    assert observations["native"].pop("mutated_name") is True
    assert (
        observations["native"]
        == observations["pytest"]
        == {
            "class": [
                "a_fixture",
                "class_fixture",
                "module_fixture",
                "plugin_fixture",
                "root_fixture",
            ],
            "sibling": ["plugin_fixture", "root_fixture"],
            "after": ["late_fixture", "plugin_fixture", "root_fixture"],
            "released": ["after", "class", "sibling"],
        }
    )


def test_late_registered_provider_is_visible_to_earlier_requests(tmp_path: Path) -> None:
    late_registration = """\
class LateProvider:
    @pytest.fixture
    def late_fixture(self): pass

provider = LateProvider()

def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ == 'test_register':
        metafunc.config.pluginmanager.register(provider, 'late-provider')
"""
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST + late_registration)
        project.write("shared.py", _SHARED)
        project.write(
            "fixture_plugin.py", "import pytest\n@pytest.fixture\ndef plugin_fixture(): pass\n"
        )
        project.write("a/conftest.py", "import pytest\n@pytest.fixture\ndef a_fixture(): pass\n")
        project.write(
            "a/test_first.py",
            "import pytest\nfrom shared import save_missing\n"
            "@pytest.fixture\ndef module_fixture(): pass\n"
            "class TestFixture:\n"
            "    @pytest.fixture\n    def class_fixture(self): pass\n"
            "    def test_first(self, request, root_fixture): save_missing(request, 'class')\n",
        )
        project.write(
            "ab/test_register.py",
            "from shared import save_missing\n"
            "def test_register(request): save_missing(request, 'sibling')\n",
        )
        project.write(
            "z/test_after.py",
            "from shared import save_missing\n"
            "def test_after(request): save_missing(request, 'after')\n",
        )
        result = (
            project.run_pytest("-p", "fixture_plugin", "-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-p",
                "fixture_plugin",
                "-q",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout
        observations[engine] = json.loads((project.root / "provider-scopes.json").read_text())
    assert observations["pytest"].pop("mutated_name") is False
    assert observations["native"].pop("mutated_name") is True
    assert (
        observations["native"]
        == observations["pytest"]
        == {
            "class": [
                "a_fixture",
                "class_fixture",
                "late_fixture",
                "module_fixture",
                "plugin_fixture",
                "root_fixture",
            ],
            "sibling": ["late_fixture", "plugin_fixture", "root_fixture"],
            "after": ["late_fixture", "plugin_fixture", "root_fixture"],
            "released": ["after", "class", "sibling"],
        }
    )
