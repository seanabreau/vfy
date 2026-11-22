"""Click regressions reduced to public pytest collection and fixture contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _in_worker_order[T](rows: list[T], workers: int) -> list[T]:
    """Workers interleave their appends, so only one worker preserves serial order."""
    return rows if workers == 1 else sorted(rows, key=json.dumps)


def _compare_project(project: VfyProject, workers: int) -> int:
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    observed = project.root / "observed.jsonl"
    expected = [json.loads(line) for line in observed.read_text().splitlines()]
    observed.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
    )
    candidate.expect(0)
    actual = [json.loads(line) for line in observed.read_text().splitlines()]
    assert _in_worker_order(actual, workers) == _in_worker_order(expected, workers)
    result = json.loads((project.root / "result.json").read_text())
    assert result["counts"]["passed"] == len(expected)
    return len(expected)


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("disable_escaping", [False, True])
def test_enum_regex_and_scalar_subclass_parameter_ids_match_pytest(
    tmp_path: Path, workers: int, disable_escaping: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = "
        f"{disable_escaping}\n",
    )
    project.write(
        "test_ids.py",
        r"""import enum, json, re
from pathlib import Path
import pytest

class Sentinel(enum.Enum):
    UNSET = 0
class Number(enum.IntEnum):
    TWO = 2
    def __str__(self):
        return 'custom-number'
class Letter(enum.StrEnum):
    A = "nonascii\u00e9"
class Text(str):
    pass
class Count(int):
    pass

@pytest.fixture(params=[Letter.A])
def letter(request):
    return request.param

@pytest.mark.parametrize('value', [
    Sentinel.UNSET, Number.TWO, Letter.A, re.compile(r'error\d+\n'),
    re.compile(b'bytes\\d+'), Text('nonascii\u00e9'), Count(7),
    re.compile('nonascii\u00e9'), re.compile(b'nonascii\xe9'),
])
def test_ids(value, letter, request):
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({'nodeid': request.node.nodeid}) + '\n')
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 9


@pytest.mark.parametrize("workers", [1, 2])
def test_only_method_receivers_are_removed_from_required_arguments(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_receivers.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture
def self():
    return 'self fixture'
@pytest.fixture
def cls():
    return 'cls fixture'
@pytest.fixture
def combined(self, cls):
    return [self, cls]

def observe(request, value):
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({'nodeid': request.node.nodeid, 'value': value}) + '\\n')

def test_module(self, cls, combined, request):
    observe(request, [self, cls, combined])

@pytest.mark.parametrize('cls', [None, str, bytes, Path])
def test_parameter(cls, request):
    observe(request, None if cls is None else cls.__name__)

class TestBound:
    @pytest.fixture
    def value(receiver, cls):
        return [type(receiver).__name__, cls]

    def test_method(receiver, value, request):
        observe(request, value)

    @staticmethod
    def test_static(self, cls, request):
        observe(request, [self, cls])
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 7


@pytest.mark.parametrize("workers", [1, 2])
def test_legacy_paths_share_modern_fixture_resources_and_file_protocol(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_paths.py",
        """import json, os
from pathlib import Path
import pytest

def test_paths(tmpdir, tmp_path, tmpdir_factory, tmp_path_factory, request):
    assert os.fspath(tmpdir) == str(tmp_path)
    assert os.fspath(tmpdir_factory.getbasetemp()) == str(tmp_path_factory.getbasetemp())
    path = tmpdir.mkdir('testdir').join('testfile')
    path.write('first\\nsecond')
    with path.open() as source:
        lines = list(source)
    binary = tmpdir.join('binary')
    binary.write(b'bytes', mode='wb')
    shared = tmpdir_factory.mktemp('shared', numbered=False)
    assert Path(os.fspath(shared)).parent == tmp_path_factory.getbasetemp()
    with pytest.raises(FileExistsError):
        tmpdir_factory.mktemp('shared', numbered=False)
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({
            'nodeid': request.node.nodeid,
            'name': path.basename,
            'text': path.read(),
            'lines': lines,
            'bytes': binary.read_binary().decode(),
            'directory': path.dirpath() == tmpdir.join('testdir'),
            'strpath': path.strpath == str(path),
        }) + '\\n')
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 1


@pytest.mark.parametrize("workers", [1, 2])
def test_legacy_factory_bypasses_overridden_factory_parameters(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_factory.py",
        """import json, os
from pathlib import Path
import pytest

@pytest.fixture(scope='session', params=['first', 'second'])
def tmp_path_factory(tmp_path_factory, request):
    raise AssertionError('legacy factory used the fixture override')

def test_factory(tmpdir_factory, request):
    assert request.getfixturevalue('tmpdir_factory') is tmpdir_factory
    assert Path(os.fspath(tmpdir_factory.getbasetemp())).is_dir()
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({
            'nodeid': request.node.nodeid,
        }) + '\\n')
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 1


@pytest.mark.parametrize("workers", [1, 2])
def test_legacy_path_tracks_overridden_path_parameters(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_path_override.py",
        """import json, os
from pathlib import Path
import pytest

@pytest.fixture(params=['first', 'second'])
def tmp_path(tmp_path, request):
    path = tmp_path / request.param
    path.mkdir()
    return path

def test_path(tmpdir, request):
    assert request.getfixturevalue('tmpdir') is tmpdir
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({
            'nodeid': request.node.nodeid,
            'base': Path(os.fspath(tmpdir)).name,
        }) + '\\n')
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 2


@pytest.mark.parametrize("workers", [1, 2])
def test_legacy_path_normalizes_relative_override_without_resolving_it(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_relative_path.py",
        """import json, os
from pathlib import Path
import pytest

@pytest.fixture(params=['relative', 'folder/../data'])
def tmp_path(request):
    return Path(request.param)

def test_relative_path(tmpdir, request):
    assert os.path.isabs(tmpdir.strpath)
    assert tmpdir.strpath == os.path.abspath(request.getfixturevalue('tmp_path'))
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps({
            'nodeid': request.node.nodeid,
            'relative': os.path.relpath(tmpdir.strpath),
        }) + '\\n')
""",
    )
    observed = _compare_project(project, workers)
    assert observed == 2
