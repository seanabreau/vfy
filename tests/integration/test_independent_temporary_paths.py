"""Temporary-directory names and rejection side effects match pytest."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_factory_numbering_normalization_and_collision_side_effects(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_factory.py",
        """import os, shutil, stat
from pathlib import Path
import pytest

def test_factory(tmp_path_factory, tmpdir_factory):
    factory = tmp_path_factory
    base = factory.getbasetemp()
    (base / 'batch0').touch()
    (base / 'BATCH4').mkdir()
    (base / 'batch009').touch()
    (base / 'batch-1').touch()
    (base / 'batch-not-a-number').mkdir()
    first = factory.mktemp('batch')
    assert first.name == 'batch10'
    second = factory.mktemp('batch')
    assert second.name == 'batch11'
    if os.name != 'nt':
        assert (base / 'batchcurrent').is_symlink()
        assert (base / 'batchcurrent').readlink() == second
        assert stat.S_IMODE(second.stat().st_mode) == 0o700
    shutil.rmtree(second)
    assert factory.mktemp('batch').name == 'batch11'
    assert Path(str(tmpdir_factory.mktemp('batch'))).name == 'batch12'

    (base / 'huge999999999999999999999999999999').touch()
    assert factory.mktemp('huge').name == 'huge1000000000000000000000000000000'
    (base / 'digits  +\u0661\u0662 ').touch()
    assert factory.mktemp('digits').name == 'digits13'
    assert factory.mktemp('./normalized').name == 'normalized0'
    assert factory.mktemp('unused/../relative/').name == 'relative0'
    assert factory.mktemp(Path('pathlike')).name == 'pathlike0'

    plain = factory.mktemp('plain', numbered=False)
    assert plain.name == 'plain'
    assert not (base / 'plaincurrent').exists()
    with pytest.raises(FileExistsError) as error:
        factory.mktemp('plain', numbered=False)
    assert error.value.errno == 17
    assert error.value.filename == str(plain)
    plain.rmdir()
    assert factory.mktemp('plain', numbered=False) == plain
    (base / 'blockedcurrent').mkdir()
    assert factory.mktemp('blocked').name == 'blocked0'
    assert (base / 'blockedcurrent').is_dir()

    outside = base.parent / 'outside'
    outside.mkdir()
    rejected = ['', '.', '..', '../escape', 'nested/child', str(outside / 'escape')]
    if os.name != 'nt':
        (base / 'escape-link').symlink_to(outside / 'child', target_is_directory=True)
        rejected.append('escape-link')
    before = sorted(path.name for path in base.iterdir())
    for name in rejected:
        for numbered in (True, False):
            with pytest.raises(ValueError, match='not a normalized and relative path'):
                factory.mktemp(name, numbered=numbered)
            assert sorted(path.name for path in base.iterdir()) == before
            assert list(outside.iterdir()) == []
    assert not (base.parent / 'escape').exists()
""",
    )
    arguments = ("-q", "--basetemp=base")
    result = (
        project.run_pytest(*arguments, load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_tmp_path_fixture_sanitizes_and_truncates_names_before_shared_numbering(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n",
    )
    project.write(
        "test_names.py",
        """import json
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', [0, 1], ids=['punctuation / !', 'punctuation / !'])
def test_name_with_punctuation(value, tmp_path, tmpdir):
    assert Path(str(tmpdir)) == tmp_path
    with Path('names.jsonl').open('a') as output:
        output.write(json.dumps(tmp_path.name) + '\\n')

@pytest.mark.parametrize('value', [0], ids=['\u00e9\u4e2d /!'])
def test_unicode(value, tmp_path):
    with Path('names.jsonl').open('a') as output:
        output.write(json.dumps(tmp_path.name) + '\\n')

def test_abcdefghijklmnopqrstuvwxyz_extra_long_name(tmp_path):
    with Path('names.jsonl').open('a') as output:
        output.write(json.dumps(tmp_path.name) + '\\n')
""",
    )
    arguments = ("-q", "--basetemp=base")
    result = (
        project.run_pytest(*arguments, load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    names = [json.loads(line) for line in (tmp_path / "names.jsonl").read_text().splitlines()]
    assert names == [
        "test_name_with_punctuation_pun0",
        "test_name_with_punctuation_pun1",
        "test_unicode_é中____0",
        "test_abcdefghijklmnopqrstuvwxy0",
    ]


@pytest.mark.skipif(os.name == "nt", reason="independent workers require POSIX")
@pytest.mark.parametrize("engine", ["stock", "native"])
def test_independent_worker_factories_isolate_numbering_and_clear_previous_roots(
    tmp_path: Path, engine: str
) -> None:
    for attempt in range(2):
        project = VfyProject(tmp_path / engine)
        project.write("base/old", "previous run")
        for name in ("alpha", "beta"):
            project.write(
                f"test_{name}.py",
                f"""import json
from pathlib import Path
def test_directory(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    assert not (base / 'old').exists()
    first = tmp_path_factory.mktemp('shared')
    second = tmp_path_factory.mktemp('shared')
    first.joinpath('owner').write_text({name!r})
    Path('{name}.json').write_text(json.dumps([str(base), first.name, second.name]))
""",
            )
        arguments = ("-q", "--basetemp=base")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--no-replay",
                "--workers=2",
                *arguments,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        actual = [
            json.loads((project.root / f"{name}.json").read_text()) for name in ("alpha", "beta")
        ]
        if engine == "stock":
            assert [entry[1:] for entry in actual] == [
                ["shared0", "shared1"],
                ["shared2", "shared3"],
            ]
        else:
            assert [entry[1:] for entry in actual] == [["shared0", "shared1"]] * 2
            assert len({entry[0] for entry in actual}) == 2
        for name, entry in zip(("alpha", "beta"), actual, strict=True):
            assert (Path(entry[0]) / entry[1] / "owner").read_text() == name
            assert not (Path(entry[0]) / "old").exists()
        if attempt == 0:
            for base in {entry[0] for entry in actual}:
                Path(base).joinpath("old").write_text("previous worker root")
