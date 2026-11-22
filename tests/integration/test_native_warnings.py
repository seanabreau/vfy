"""Compare runtime warning-marker changes with pytest execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("append", [True, False], ids=["append", "prepend"])
def test_runtime_warning_marks_preserve_parameter_filter_order(
    tmp_path: Path, append: bool
) -> None:
    source = f"""import pytest
import warnings
def test_prepare(request):
    for item in request.session.items:
        if item.originalname == 'test_case':
            item.add_marker(
                pytest.mark.filterwarnings('ignore:precedence sentinel:UserWarning'),
                append={append},
            )
def pytest_generate_tests(metafunc):
    if metafunc.definition.name == 'test_case':
        error = pytest.mark.filterwarnings('error:precedence sentinel:UserWarning')
        metafunc.parametrize('value', [
            pytest.param(0, marks=error),
            pytest.param(1, marks=error),
        ])
def test_case(value):
    warnings.warn('precedence sentinel', UserWarning)
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == (0 if append else 1), transcript
        assert ("3 passed" if append else "2 failed, 1 passed") in transcript, transcript


@pytest.mark.parametrize("target", ["item", "parent"])
def test_warning_filters_refresh_after_an_earlier_test_changes_markers(
    tmp_path: Path, target: str
) -> None:
    selected = "request.session.items[1]" if target == "item" else "request.node.parent"
    parameters = "@pytest.mark.parametrize('value', [0, 1])" if target == "parent" else ""
    arguments = "value" if target == "parent" else ""
    source = f"""import warnings
import pytest

def test_a_change_marker(request):
    {selected}.add_marker(pytest.mark.filterwarnings('ignore::UserWarning'))

{parameters}
def test_b_warning({arguments}):
    warnings.warn('runtime warning marker sentinel', UserWarning)
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write("test_warnings.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-W",
            "error::UserWarning",
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert f"{3 if target == 'parent' else 2} passed" in output, output
        assert "UserWarning: runtime warning marker sentinel" not in output


def test_compact_cases_keep_their_parameter_warning_filters(tmp_path: Path) -> None:
    source = """import warnings
import pytest

@pytest.mark.parametrize('action', [
    pytest.param('ignore', marks=pytest.mark.filterwarnings('ignore::UserWarning')),
    pytest.param('error', marks=pytest.mark.filterwarnings('error::UserWarning')),
])
def test_parameter_warning(action):
    if action == 'error':
        with pytest.raises(UserWarning, match='parameter warning sentinel'):
            warnings.warn('parameter warning sentinel', UserWarning)
    else:
        warnings.warn('parameter warning sentinel', UserWarning)
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_warnings.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-W",
            "default::UserWarning",
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "2 passed" in output, output
        assert "warnings summary" not in output, output


def test_warning_profiles_survive_through_compact_case_finalizers(tmp_path: Path) -> None:
    source = """import gc
from pathlib import Path
import warnings
import pytest

pytestmark = pytest.mark.filterwarnings('ignore::FutureWarning')

@pytest.fixture
def cleanup(request):
    saved_node = request.node
    def finalize():
        gc.collect()
        assert request.node is saved_node
        warnings.warn('function filter at teardown', UserWarning)
        warnings.warn('inherited filter at teardown', FutureWarning)
        warnings.warn('runtime parent filter at teardown', DeprecationWarning)
        with Path('finalizers.txt').open('a') as stream:
            stream.write(request.node.nodeid + '\\n')
    request.addfinalizer(finalize)

def test_prepare(request):
    request.node.parent.add_marker(
        pytest.mark.filterwarnings('ignore::DeprecationWarning')
    )

@pytest.mark.filterwarnings('ignore::UserWarning')
@pytest.mark.parametrize('action', [
    pytest.param('ignore', marks=pytest.mark.filterwarnings('ignore::RuntimeWarning')),
    pytest.param('error', marks=pytest.mark.filterwarnings('error::RuntimeWarning')),
])
def test_case(action, cleanup):
    gc.collect()
    warnings.warn('function filter during call', UserWarning)
    if action == 'error':
        with pytest.raises(RuntimeWarning, match='parameter filter'):
            warnings.warn('parameter filter', RuntimeWarning)
    else:
        warnings.warn('parameter filter', RuntimeWarning)
"""
    finalizers = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write("test_warnings.py", source)
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "-W", "error"
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "3 passed" in output, output
        assert "warnings summary" not in output, output
        finalizers[engine] = (project.root / "finalizers.txt").read_text().splitlines()
    assert (
        finalizers["pytest-native"]
        == finalizers["pytest"]
        == [
            "test_warnings.py::test_case[ignore]",
            "test_warnings.py::test_case[error]",
        ]
    )


@pytest.mark.parametrize("workers", [1, 2])
def test_native_records_collection_warnings_before_call_warnings(
    tmp_path: Path, workers: int
) -> None:
    source = """import warnings
import pytest

warnings.warn('module collection warning', UserWarning)

@pytest.mark.parametrize('left,right', zip([1, 2], [1, 2]))
def test_render_panel(left, right):
    if left == 1:
        warnings.warn('call warning', UserWarning)
    assert left == right
"""
    stock_observer = """import json
from pathlib import Path
from _pytest.warnings import warning_record_to_str

def pytest_warning_recorded(warning_message, when, nodeid, location):
    record = {
        'message': warning_record_to_str(warning_message),
        'filename': warning_message.filename,
        'lineno': warning_message.lineno,
        'nodeid': nodeid,
    }
    with Path('warnings.jsonl').open('a') as stream:
        stream.write(json.dumps(record) + '\\n')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_panel.py", source)
        project.write("test_extra.py", "def test_extra(): pass\n")
        if engine == "pytest":
            project.write("conftest.py", stock_observer)
            result = project.run_pytest("-q", load_xdist=False)
            records = [
                json.loads(line)
                for line in (project.root / "warnings.jsonl").read_text().splitlines()
            ]
        else:
            record_path = project.root / "native.json"
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--all",
                f"--workers={workers}",
                "--no-cov",
                "-q",
                env={"PYTEST_NATIVE_RESULT": str(record_path)},
            )
            records = json.loads(record_path.read_text())["warnings"]
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "3 passed" in transcript, transcript
        assert f"{len(records)} warnings in" in transcript, transcript
        observed[engine] = [
            {
                key: value.replace(str(project.root), "<PROJECT>")
                if isinstance(value, str)
                else value
                for key, value in record.items()
            }
            for record in records
        ]
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"][0]["nodeid"] == ""
    assert "module collection warning" in observed["pytest-native"][0]["message"]
    assert "call warning" in observed["pytest-native"][-1]["message"]
