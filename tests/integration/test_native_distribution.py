"""The vfy wheel owns native execution without replacing stock pytest."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native execution requires Unix",
)


def test_native_command_keeps_stock_pytest_usable(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(
        "test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_value(value):\n"
        "    assert value >= 0\n",
    )
    native = project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
    native.expect(0)
    assert "4 passed" in native.stdout
    stock = project.run_pytest(
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        load_xdist=False,
    )
    stock.expect(0)
    assert "4 passed" in stock.stdout
    pytest_version = version("pytest")
    identity = project.run_python(
        "-c",
        f"from importlib.metadata import version; import pytest; "
        f"assert version('pytest') == {pytest_version!r}; "
        f"assert pytest.__version__ == {pytest_version!r}",
    )
    identity.expect(0)


@pytest.mark.parametrize("declaration", ["addopts", "conftest", "conftest-string"])
@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_explicit_builtin_pytester_plugins_run_nested_command(
    tmp_path: Path, engine: str, declaration: str
) -> None:
    project = VfyProject(tmp_path / engine)
    if declaration == "addopts":
        project.write(
            "pytest.ini",
            "[pytest]\naddopts = -p pytester -p pytester_assertions\n",
        )
    elif declaration == "conftest":
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", "pytest_plugins = ['pytester', 'pytester_assertions']\n")
    else:
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", "pytest_plugins = 'pytester,pytester_assertions'\n")
    project.write(
        "test_outer.py",
        "def test_pytester_runs_nested_command(pytester):\n"
        "    pytester.makepyfile('def test_inner():\\n    assert True\\n')\n"
        "    result = pytester.runpytest('-q')\n"
        "    result.assert_outcomes(passed=1)\n",
    )

    result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")

    result.expect(0)
    assert "1 passed" in result.stdout


def test_explicit_stock_engine_rejects_conflicting_native_addopts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = -p vfy._pytest.native_pytest\n",
    )
    project.write(
        "test_case.py",
        "from pathlib import Path\ndef test_stock():\n    Path('body-ran').touch()\n",
    )
    result = project.run_vfy("test", "--engine=pytest", "--workers=1", "--no-cov", "-q")
    result.expect(1)
    assert "vfy manages its native pytest adapter" in result.stderr
    assert "already added" not in result.stderr
    assert not (project.root / "body-ran").exists()


def test_native_collection_restores_stock_after_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("test_case.py", "def test_case():\n    assert True\n")
    project.write(
        "check.py",
        "import pytest\n"
        "from _pytest import fixtures, cacheprovider\n"
        "from _pytest.python import PyCollector\n"
        "original_order = fixtures.reorder_items\n"
        "original_collect = PyCollector._genfunctions\n"
        "original_cache = (pytest.Cache, cacheprovider.Cache)\n"
        "from vfy._pytest import native_pytest\n"
        "assert fixtures.reorder_items is original_order\n"
        "assert PyCollector._genfunctions is original_collect\n"
        "assert (pytest.Cache, cacheprovider.Cache) == original_cache\n"
        "failed = pytest.main(['-p', 'no:cacheprovider', '-p', "
        "'vfy._pytest.native_pytest', '--capture=tee-sys', '-q'])\n"
        "assert failed != 0\n"
        "assert fixtures.reorder_items is original_order\n"
        "assert PyCollector._genfunctions is original_collect\n"
        "assert (pytest.Cache, cacheprovider.Cache) == original_cache\n"
        "assert pytest.main(['-p', 'no:cacheprovider', '-p', "
        "'vfy._pytest.native_pytest', '-q']) == 0\n"
        "assert fixtures.reorder_items is original_order\n"
        "assert PyCollector._genfunctions is original_collect\n"
        "assert (pytest.Cache, cacheprovider.Cache) == original_cache\n"
        "assert pytest.main(['-q']) == 0\n",
    )
    result = project.run_python("check.py", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    result.expect(0)


def test_nested_stock_pytest_keeps_parameter_and_cache_semantics(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(
        "inner/test_parameters.py",
        "import pytest\n"
        "@pytest.fixture(params=['first', 'second'])\n"
        "def resource(request):\n    return request.param\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_parameter(value, resource, cache):\n"
        "    assert resource in ('first', 'second')\n"
        "    cache.set('nested/value', value)\n"
        "    assert cache.get('nested/value', None) == value\n",
    )
    project.write(
        "test_outer.py",
        "import pytest\n"
        "@pytest.mark.parametrize('iteration', [0, 1])\n"
        "def test_nested(iteration):\n"
        "    observed = []\n"
        "    class Recorder:\n"
        "        def pytest_runtest_logreport(self, report):\n"
        "            if report.when == 'call':\n"
        "                observed.append((report.nodeid, report.outcome))\n"
        "    result = pytest.main(['inner', '-q'], plugins=[Recorder()])\n"
        "    assert result == 0\n"
        "    assert len(observed) == 4\n"
        "    assert len({nodeid for nodeid, _ in observed}) == 4\n"
        "    assert all(outcome == 'passed' for _, outcome in observed)\n",
    )
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", "test_outer.py", "-q"
    )
    result.expect(0)
    assert "2 passed" in result.stdout


def test_nested_stock_collection_has_no_native_frames(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(
        "inner/test_parameters.py",
        "import pytest\n"
        "@pytest.fixture(params=['first', 'second'])\n"
        "def resource(request):\n    return request.param\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_matrix(value, resource):\n    assert value in (1, 2)\n",
    )
    project.write(
        "inner/test_collection_error.py",
        "def pytest_generate_tests(metafunc):\n"
        "    raise RuntimeError('nested collection sentinel')\n"
        "def test_collect():\n    pass\n",
    )
    project.write(
        "test_outer.py",
        "import io\n"
        "import json\n"
        "from contextlib import redirect_stderr, redirect_stdout\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "class Recorder:\n"
        "    def __init__(self):\n        self.nodeids = []\n"
        "    def pytest_collection_finish(self, session):\n"
        "        self.nodeids = [item.nodeid for item in session.items]\n"
        "def run_inner(args, plugins=()):\n"
        "    stdout, stderr = io.StringIO(), io.StringIO()\n"
        "    with redirect_stdout(stdout), redirect_stderr(stderr):\n"
        "        result = pytest.main(args, plugins=list(plugins))\n"
        "    return int(result), stdout.getvalue(), stderr.getvalue()\n"
        "def test_nested_stock_collection():\n"
        "    recorder = Recorder()\n"
        "    parameter_status, _, _ = run_inner([\n"
        "        '-p', 'no:cacheprovider', 'inner/test_parameters.py', '-q'\n"
        "    ], [recorder])\n"
        "    error_status, error_stdout, error_stderr = run_inner([\n"
        "        '-p', 'no:cacheprovider', '--fulltrace',\n"
        "        'inner/test_collection_error.py', '-q'\n"
        "    ])\n"
        "    Path('nested-observation.json').write_text(json.dumps({\n"
        "        'parameter_status': parameter_status,\n"
        "        'nodeids': recorder.nodeids,\n"
        "        'error_status': error_status,\n"
        "        'error_stdout': error_stdout,\n"
        "        'error_stderr': error_stderr,\n"
        "    }))\n"
        "    assert parameter_status == 0\n"
        "    assert error_status == 2\n",
    )

    observations: dict[str, dict[str, object]] = {}
    for engine in ("stock", "pytest-native"):
        if engine == "stock":
            result = project.run_pytest("test_outer.py", "-q", load_xdist=False)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                "test_outer.py",
                "-q",
            )
        result.expect(0)
        observations[engine] = json.loads((project.root / "nested-observation.json").read_text())

    stock = observations["stock"]
    native = observations["pytest-native"]
    assert native["parameter_status"] == stock["parameter_status"] == 0
    assert (
        native["nodeids"]
        == stock["nodeids"]
        == [
            "inner/test_parameters.py::test_matrix[first-1]",
            "inner/test_parameters.py::test_matrix[first-2]",
            "inner/test_parameters.py::test_matrix[second-1]",
            "inner/test_parameters.py::test_matrix[second-2]",
        ]
    )
    assert native["error_status"] == stock["error_status"] == 2
    for engine in (stock, native):
        fulltrace = f"{engine['error_stdout']}\n{engine['error_stderr']}"
        assert "compact_collection.py" not in fulltrace
        assert "collection_order.py" not in fulltrace
