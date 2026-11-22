"""Reviewed coverage aliases activate one owned plugin without upstream imports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_ALIASES = ("vfy.pytest_plugin", "pytest_cov", "pytest_cov.plugin")


@pytest.mark.parametrize("alias", _ALIASES)
@pytest.mark.parametrize("declaration", ["explicit", "conftest"])
def test_coverage_alias_uses_single_owned_plugin(
    tmp_path: Path, alias: str, declaration: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "def value():\n    return 42\n")
    project.write(
        "test_product.py",
        "from product import value\n"
        "def test_value(cov):\n"
        "    assert cov is not None\n"
        "    assert value() == 42\n",
    )
    project.write(
        "conftest.py",
        (f"pytest_plugins = [{alias!r}]\n" if declaration == "conftest" else "")
        + "import json\nfrom pathlib import Path\n"
        + "def pytest_sessionfinish(session):\n"
        + "    manager = session.config.pluginmanager\n"
        + "    plugin = manager.getplugin('vfy.independent.coverage_support')\n"
        + "    if plugin is None:\n        return\n"
        + f"    assert manager.getplugin({alias!r}) is plugin\n"
        + "    names = [name for name, value in manager.list_name_plugin() if value is plugin]\n"
        + "    Path('identities.json').write_text(json.dumps(names))\n",
    )
    arguments = ["--cov=product", "--cov-report=json:report.json", "-q"]
    if declaration == "explicit":
        oracle = project.run_python("-m", "pytest", "-p", "pytest_cov.plugin", *arguments)
        oracle.expect(0)
        oracle_file = json.loads((project.root / "report.json").read_text())["files"]["product.py"]
    else:
        oracle_file = None
    if declaration == "explicit":
        arguments[:0] = ["-p", alias, "-p", "vfy.independent.coverage_support"]
    else:
        # Options need registration before conftest collection; repeat its alias to
        # verify that the conftest does not register a second module identity.
        arguments[:0] = ["-p", "vfy.independent.coverage_support"]
    project.write(
        "bootstrap.py",
        "import importlib.abc\nimport sys\n"
        "attempts = []\n"
        "class Forbidden(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in {'_pytest', 'pluggy', 'packaging', 'pytest_cov'}:\n"
        "            attempts.append(fullname)\n"
        "            raise AssertionError('forbidden upstream import: ' + fullname)\n"
        "        if fullname == 'vfy.pytest_plugin':\n"
        "            attempts.append(fullname)\n"
        "            raise AssertionError('legacy plugin import')\n"
        "sys.meta_path.insert(0, Forbidden())\n"
        "from vfy.independent.entry import main\n"
        "exitstatus = main(sys.argv[1:])\n"
        "assert not attempts, attempts\n"
        "raise SystemExit(exitstatus)\n",
    )
    result = project.run_python("bootstrap.py", *arguments, env={"PYTHONPATH": _SOURCE})
    result.expect(0)
    assert json.loads((project.root / "identities.json").read_text()) == [
        "vfy.independent.coverage_support"
    ]
    report = json.loads((project.root / "report.json").read_text())
    assert report["totals"]["covered_lines"] == 2
    if oracle_file is not None:
        assert report["files"]["product.py"] == oracle_file


@pytest.mark.parametrize("alias", _ALIASES)
def test_blocked_coverage_alias_blocks_canonical_and_can_be_reenabled(
    tmp_path: Path, alias: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    assert True\n")
    project.write(
        "conftest.py",
        "import os\n"
        "def pytest_sessionstart(session):\n"
        "    manager = session.config.pluginmanager\n"
        + f"    assert manager.is_blocked({alias!r}) is (os.environ['BLOCKED'] == '1')\n"
        + "    expected = os.environ['BLOCKED'] == '0'\n"
        + "    assert manager.hasplugin('vfy.independent.coverage_support') is expected\n",
    )
    for blocked in (True, False):
        arguments = ["-p", f"no:{alias}"]
        if not blocked:
            arguments.extend(["-p", alias])
        result = project.run_python(
            "-m",
            "vfy.independent.entry",
            *arguments,
            "-q",
            env={"PYTHONPATH": _SOURCE, "BLOCKED": str(int(blocked))},
        )
        result.expect(0)


@pytest.mark.parametrize("alias", _ALIASES)
def test_conftest_can_activate_coverage_alias_without_early_request(
    tmp_path: Path, alias: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"pytest_plugins = [{alias!r}]\n")
    project.write(
        "test_value.py",
        "def test_value(cov, request):\n"
        "    assert cov is None\n"
        "    manager = request.config.pluginmanager\n"
        "    canonical = manager.getplugin('vfy.independent.coverage_support')\n"
        + f"    assert manager.getplugin({alias!r}) is canonical\n",
    )
    result = project.run_python("-m", "vfy.independent.entry", "-q", env={"PYTHONPATH": _SOURCE})
    result.expect(0)
