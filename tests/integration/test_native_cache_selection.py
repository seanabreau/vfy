"""Differential proof for native pytest's persisted cache selectors."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native cache selection requires Unix",
)


def _run(project: VfyProject, native: bool, *arguments: str) -> CommandResult:
    command = ["-q"]
    if native:
        command.extend(["-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"])
    command.extend(arguments)
    return project.run_pytest(*command, load_xdist=False)


def _run_public_cache(project: VfyProject, native: bool, *arguments: str) -> CommandResult:
    if native:
        return project.run_vfy(
            "test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q", *arguments
        )
    return _run(project, False, *arguments)


def _events(project: VfyProject) -> list[str]:
    path = project.root / "events.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _clear_events(project: VfyProject) -> None:
    project.write("events.txt", "")


def _last_failed(project: VfyProject) -> dict[str, bool]:
    path = project.root / ".pytest_cache/v/cache/lastfailed"
    if not path.exists():
        return {}
    value: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    result: dict[str, bool] = {}
    for key, status in value.items():
        assert isinstance(key, str)
        assert isinstance(status, bool)
        result[key] = status
    return result


def _cache_snapshot(project: VfyProject) -> dict[str, bytes]:
    cache = project.root / ".pytest_cache"
    return {
        path.relative_to(cache).as_posix(): path.read_bytes()
        for path in sorted(cache.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("selector", ["--lf", "--ff"])
@pytest.mark.parametrize("cached_failure", [False, True], ids=["empty-cache", "outside-selection"])
def test_failure_selectors_honor_no_failures_policy(
    tmp_path: Path, selector: str, cached_failure: bool
) -> None:
    observations = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "test_selected.py",
            "from pathlib import Path\n"
            "def test_selected(): Path('body-ran.txt').write_text('ran')\n",
        )
        failures = {"test_missing.py::test_missing": True} if cached_failure else {}
        project.write(".pytest_cache/v/cache/lastfailed", json.dumps(failures))
        result = _run_public_cache(project, native, selector, "--lfnf=none")
        observations.append(
            (result.returncode, (project.root / "body-ran.txt").exists(), _last_failed(project))
        )
    expected = (0, True, failures) if cached_failure else (5, False, {})
    assert observations == [expected, expected]


@pytest.mark.parametrize("cached", [False, True], ids=["new-tests", "known-tests"])
@pytest.mark.parametrize("last_failed", [False, True], ids=["all-tests", "last-failed"])
def test_new_first_coalesces_duplicate_nodeids(
    tmp_path: Path, cached: bool, last_failed: bool
) -> None:
    observations = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "conftest.py",
            "from pathlib import Path\n"
            "def pytest_sessionfinish(session):\n"
            "    terminal = session.config.pluginmanager.getplugin('terminalreporter')\n"
            "    count = len(terminal.stats.get('deselected', ()))\n"
            "    Path('deselected.txt').write_text(str(count))\n",
        )
        project.write(
            "test_duplicates.py",
            "from pathlib import Path\n"
            "def record(value):\n"
            "    with Path('events.txt').open('a') as stream: stream.write(value + '\\n')\n"
            "def test_one(): record('one')\n"
            "def test_two(): record('two')\n",
        )
        nodeids = ["test_duplicates.py::test_one", "test_duplicates.py::test_two"]
        project.write(".pytest_cache/v/cache/nodeids", json.dumps(nodeids if cached else []))
        if last_failed:
            project.write(".pytest_cache/v/cache/lastfailed", json.dumps({nodeids[1]: True}))
        result = _run_public_cache(
            project,
            native,
            "--nf",
            *(["--lf"] if last_failed else []),
            "--keep-duplicates",
            "test_duplicates.py",
            "test_duplicates.py",
        )
        result.expect(0)
        observations.append(
            (
                _events(project),
                json.loads((project.root / ".pytest_cache/v/cache/nodeids").read_text()),
                (project.root / "deselected.txt").read_text(),
            )
        )
    expected_nodeids = nodeids if cached or not last_failed else [nodeids[1]]
    expected_events = ["two"] if last_failed else ["one", "two"]
    expected_deselected = "2" if last_failed else "0"
    assert observations == [(expected_events, expected_nodeids, expected_deselected)] * 2


def test_cache_serializes_custom_dictionary_items(tmp_path: Path) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "test_dictionary.py",
            "class Dictionary(dict):\n"
            "    def items(self):\n"
            "        yield ('visible', 2)\n"
            "        yield ('second', 3)\n"
            "def test_cache(cache):\n"
            "    cache.set('review/dictionary', Dictionary(hidden=1))\n"
            "    assert cache.get('review/dictionary', None) == {'visible': 2, 'second': 3}\n"
            "    cache.set('review/empty-dictionary', Dictionary())\n"
            "    assert cache.get('review/empty-dictionary', None) == {}\n",
        )
        result = _run_public_cache(project, native)
        result.expect(0)
        stored = json.loads((project.root / ".pytest_cache/v/review/dictionary").read_text())
        assert stored == {"visible": 2, "second": 3}


@pytest.mark.parametrize(
    "mutation",
    [
        "value['added'] = 2",
        "del value['original']",
        "del value['second']; value['added'] = 2",
        "value.clear(); value['added'] = 2",
    ],
    ids=["append", "delete-current", "replace-later", "clear"],
)
def test_cache_dictionary_mutation_matches_stock_and_keeps_previous_value_on_error(
    tmp_path: Path,
    mutation: str,
) -> None:
    # CPython releases differ on whether encoding detects this mutation.
    outcomes = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "test_dictionary.py",
            "import json\n"
            "from pathlib import Path\n"
            "class MutatingList(list):\n"
            "    def __iter__(self):\n"
            f"        {mutation}\n"
            "        return super().__iter__()\n"
            "value = {'original': MutatingList([1]), 'second': 2, 'third': 3}\n"
            "def test_cache(cache):\n"
            "    cache.set('review/dictionary', {'previous': 1})\n"
            "    path = Path('.pytest_cache/v/review/dictionary')\n"
            "    previous = path.read_bytes()\n"
            "    try:\n"
            "        cache.set('review/dictionary', value)\n"
            "    except RuntimeError as error:\n"
            "        outcome = str(error)\n"
            "        assert path.read_bytes() == previous\n"
            "    else:\n"
            "        outcome = 'stored'\n"
            "    stored = cache.get('review/dictionary', None)\n"
            "    Path('outcome.json').write_text(json.dumps([outcome, stored]))\n",
        )
        result = _run_public_cache(project, native)
        result.expect(0)
        outcomes.append(json.loads((project.root / "outcome.json").read_text()))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(
    "setup",
    [
        "value = {1: 'integer', '1': 'string', 'unicode': 'café'}",
        "value = [float('nan'), float('inf'), -float('inf'), '__vfy_cache_0_nan__']",
        "class Dictionary(dict):\n"
        "    def items(self): return [['visible', 2]]\n"
        "value = Dictionary(hidden=1)",
        "value = []; value.append(value)",
    ],
    ids=["coerced-keys-and-unicode", "nonfinite", "subclass-pairs", "circular"],
)
def test_cache_fixture_encoding_matches_interpreter_and_preserves_prior_data(
    tmp_path: Path, setup: str
) -> None:
    observations = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write(
            "test_encoding.py",
            "import json\nfrom pathlib import Path\n" + setup + "\ndef test_cache(cache):\n"
            "    cache.set('review/encoded', {'previous': 1})\n"
            "    path = Path('.pytest_cache/v/review/encoded')\n"
            "    previous = path.read_bytes()\n"
            "    try:\n"
            "        cache.set('review/encoded', value)\n"
            "    except (TypeError, ValueError) as error:\n"
            "        outcome = [type(error).__name__, str(error)]\n"
            "        assert path.read_bytes() == previous\n"
            "    else:\n"
            "        outcome = ['stored']\n"
            "    Path('outcome.json').write_text(json.dumps(outcome))\n",
        )
        result = _run_public_cache(project, native)
        result.expect(0)
        observations.append(
            (
                (project.root / "outcome.json").read_bytes(),
                (project.root / ".pytest_cache/v/review/encoded").read_bytes(),
            )
        )
    assert observations[0] == observations[1]


def test_last_failed_prioritizes_cached_files_and_skips_unrelated_imports(
    tmp_path: Path,
) -> None:
    observations = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "conftest.py",
            """from pathlib import Path

def pytest_collection_modifyitems(items):
    Path('items.txt').write_text('\\n'.join(item.nodeid for item in items))
""",
        )
        project.write("test_a.py", "raise RuntimeError('unrelated module imported')\n")
        project.write("test_z.py", "def test_passed(): pass\ndef test_failed(): pass\n")
        project.write(".pytest_cache/v/cache/lastfailed", '{"test_z.py::test_failed": true}')
        result = _run_public_cache(project, native, "--lf")
        result.expect(0)
        observations.append(
            (
                (project.root / "items.txt").read_text().splitlines(),
                _last_failed(project),
            )
        )
    assert (
        observations
        == [
            (["test_z.py::test_failed"], {}),
        ]
        * 2
    )


@pytest.mark.parametrize("selection", ["stale-node", "other-file"])
def test_last_failed_collects_normally_until_a_cached_failure_is_found(
    tmp_path: Path, selection: str
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write("test_a.py", "def test_other(): pass\n")
        project.write("test_z.py", "def test_present(): pass\n")
        project.write(".pytest_cache/v/cache/lastfailed", '{"test_z.py::test_missing": true}')
        targets = ("test_a.py",) if selection == "other-file" else ()
        result = _run_public_cache(project, native, "--lf", *targets)
        result.expect(0)
        assert f"{1 if targets else 2} passed" in result.stdout
        assert _last_failed(project) == {"test_z.py::test_missing": True}


@pytest.mark.parametrize(
    ("serialized", "expected"),
    [
        ('"\\udcff"', "\udcff"),
        (
            '{"\\ud800":["left\\udfff-right","\\ud800\\udc00","\\\\udcff",'
            '"__vfy_cache_string_0_0__"]}',
            {
                "\ud800": [
                    "left\udfff-right",
                    "\U00010000",
                    "\\udcff",
                    "__vfy_cache_string_0_0__",
                ]
            },
        ),
        (
            "{" + ",".join(f'"\\udcff":{index}' for index in range(12)) + "}",
            {"\udcff": 11},
        ),
    ],
    ids=["scalar", "nested-keys-and-strings", "duplicate-keys"],
)
def test_cache_reads_external_json_with_escaped_surrogate_strings(
    tmp_path: Path, serialized: str, expected: object
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(".pytest_cache/v/review/surrogates", serialized)
        project.write(
            "test_cache.py",
            "def test_cache(cache):\n"
            f"    assert cache.get('review/surrogates', None) == {expected!r}\n",
        )
        result = _run_public_cache(project, native)
        result.expect(0)


@pytest.mark.parametrize(
    "serialized",
    ['"\\ud800\\uZZZZ"', '"\\ud800\n"', '{"\\udcff":1,}', '"\\udcff" trailing'],
    ids=["invalid-escape", "literal-control", "trailing-comma", "trailing-data"],
)
def test_cache_surrogate_decoding_preserves_invalid_json_default(
    tmp_path: Path, serialized: str
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(".pytest_cache/v/review/surrogates", serialized)
        project.write(
            "test_cache.py",
            "def test_cache(cache):\n"
            "    missing = object()\n"
            "    assert cache.get('review/surrogates', missing) is missing\n",
        )
        result = _run_public_cache(project, native)
        result.expect(0)


@pytest.mark.parametrize("first_native", [False, True], ids=["stock-to-native", "native-to-stock"])
def test_stepwise_cache_round_trips_between_native_and_stock(
    tmp_path: Path, first_native: bool
) -> None:
    source = (
        "from pathlib import Path\n"
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b():\n"
        "    record('b')\n"
        "    assert not Path('fail.flag').exists()\n"
        "def test_c(): record('c')\n"
    )
    oracle = VfyProject(tmp_path / "oracle")
    project = VfyProject(tmp_path / "roundtrip")
    for fixture in (oracle, project):
        fixture.write("test_steps.py", source)
        fixture.write("fail.flag", "")
    reference = _run(oracle, False, "--sw")
    stopped = _run(project, first_native, "--sw")
    assert reference.returncode == stopped.returncode == 2, stopped.stdout + stopped.stderr
    cache_path = ".pytest_cache/v/cache/stepwise"
    expected = json.loads((oracle.root / cache_path).read_text(encoding="utf-8"))
    actual = json.loads((project.root / cache_path).read_text(encoding="utf-8"))
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        assert actual.keys() == expected.keys()
        assert datetime.fromisoformat(actual.pop("last_cache_date_str")).utcoffset() is None
        expected.pop("last_cache_date_str")
    assert actual == expected

    (project.root / "fail.flag").unlink()
    _clear_events(project)
    resumed = _run(project, not first_native, "--sw")
    resumed.expect(0)
    assert _events(project) == ["b", "c"]


@pytest.mark.parametrize(
    ("arguments", "expected_order"),
    [(("--ff",), ["b", "c", "a"]), (("--sw",), ["c", "b"]), (("--sw", "--ff"), ["b", "c"])],
)
@pytest.mark.parametrize("hook_options", ["wrapper=True", "wrapper=True, tryfirst=True", ""])
def test_cache_selection_matches_stock_around_collection_wrappers(
    tmp_path: Path, arguments: tuple[str, ...], expected_order: list[str], hook_options: str
) -> None:
    observations: list[list[str]] = []
    cached_nodeids: list[object] = []
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write(
            "test_steps.py",
            "def record(value):\n"
            "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
            "        stream.write(value + '\\n')\n"
            "def test_a(): record('a')\n"
            "def test_b(): record('b')\n"
            "def test_c(): record('c')\n",
        )
        wrapper = "    result = yield\n" if hook_options else ""
        return_result = "    return result\n" if hook_options else ""
        project.write(
            "conftest.py",
            "import pytest\n"
            f"@pytest.hookimpl({hook_options})\n"
            "def pytest_collection_modifyitems(items):\n"
            f"{wrapper}"
            "    items.reverse()\n"
            f"{return_result}",
        )
        project.write(
            ".pytest_cache/v/cache/lastfailed", json.dumps({"test_steps.py::test_b": True})
        )
        # A real stock run supplies the installed release's stepwise cache schema.
        seed = VfyProject(tmp_path / ("native-seed" if native else "stock-seed"))
        seed.write(
            "test_steps.py", "def test_a(): pass\ndef test_b(): assert False\ndef test_c(): pass\n"
        )
        stopped = _run(seed, False, "--sw")
        stopped.expect(2)
        stepwise_path = ".pytest_cache/v/cache/stepwise"
        project.write(stepwise_path, (seed.root / stepwise_path).read_text(encoding="utf-8"))
        result = _run(project, native, *arguments)
        result.expect(0)
        observations.append(_events(project))
        cached_nodeids.append(
            json.loads((project.root / ".pytest_cache/v/cache/nodeids").read_text(encoding="utf-8"))
        )
    assert observations[0] == observations[1]
    if hook_options:
        assert observations[0] == expected_order
    assert cached_nodeids[0] == cached_nodeids[1]


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_cache_fixture_preserves_pytest_public_type(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_cache_type.py",
        "import pytest\n"
        "def test_cache_is_public_type(cache):\n"
        "    assert isinstance(cache, pytest.Cache)\n",
    )

    result = _run(project, native)
    result.expect(0)


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_last_failed_and_failed_first_match_stock_pytest(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write("fail.flag", "fail\n")
    project.write(
        "test_selection.py",
        "from pathlib import Path\n"
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b():\n"
        "    record('b')\n"
        "    assert not Path('fail.flag').exists()\n"
        "def test_c(): record('c')\n",
    )

    first = _run(project, native)
    first.expect(1)
    assert _events(project) == ["a", "b", "c"]
    assert _last_failed(project) == {"test_selection.py::test_b": True}

    _clear_events(project)
    failed_first = _run(project, native, "--ff")
    failed_first.expect(1)
    assert _events(project) == ["b", "a", "c"]

    (project.root / "fail.flag").unlink()
    _clear_events(project)
    last_failed = _run(project, native, "--lf")
    last_failed.expect(0)
    assert _events(project) == ["b"]
    assert _last_failed(project) == {}


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_new_first_uses_cached_nodeids_and_file_mtime_order(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_a.py",
        "def test_a():\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('a\\n')\n",
    )
    project.write(
        "test_b.py",
        "def test_b():\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('b\\n')\n",
    )
    os.utime(project.root / "test_a.py", (1_600_000_000, 1_600_000_000))
    os.utime(project.root / "test_b.py", (1_700_000_000, 1_700_000_000))

    baseline = _run(project, native)
    baseline.expect(0)
    _clear_events(project)
    project.write(
        "test_c.py",
        "def test_c():\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('c\\n')\n",
    )

    new_first = _run(project, native, "--nf")
    new_first.expect(0)
    assert _events(project) == ["c", "b", "a"]


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_stepwise_resumes_at_failure_and_runs_all_after_recovery(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write("fail.flag", "fail\n")
    project.write(
        "test_steps.py",
        "from pathlib import Path\n"
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b():\n"
        "    record('b')\n"
        "    assert not Path('fail.flag').exists()\n"
        "def test_c(): record('c')\n",
    )

    stopped = _run(project, native, "--sw")
    stopped.expect(2)
    assert _events(project) == ["a", "b"]

    (project.root / "fail.flag").unlink()
    _clear_events(project)
    resumed = _run(project, native, "--sw")
    resumed.expect(0)
    assert _events(project) == ["b", "c"]

    _clear_events(project)
    complete = _run(project, native, "--sw")
    complete.expect(0)
    assert _events(project) == ["a", "b", "c"]


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_stepwise_skip_ignores_first_failure_and_resumes_at_second(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write("fail.flag", "fail\n")
    project.write(
        "test_steps.py",
        "from pathlib import Path\n"
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b():\n"
        "    record('b')\n"
        "    assert not Path('fail.flag').exists()\n"
        "def test_c():\n"
        "    record('c')\n"
        "    assert not Path('fail.flag').exists()\n",
    )

    stopped = _run(project, native, "--sw-skip")
    stopped.expect(2)
    assert _events(project) == ["a", "b", "c"]
    stepwise_path = project.root / ".pytest_cache/v/cache/stepwise"
    stepwise_state: object = json.loads(stepwise_path.read_text(encoding="utf-8"))
    if isinstance(stepwise_state, dict):
        last_failed = stepwise_state.get("last_failed")
    else:
        last_failed = stepwise_state
    assert last_failed == "test_steps.py::test_c"

    (project.root / "fail.flag").unlink()
    _clear_events(project)
    resumed = _run(project, native, "--sw")
    resumed.expect(0)
    assert _events(project) == ["c"]


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_last_failed_cache_tracks_collection_errors_and_recovery(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write("test_broken.py", "def test_broken(:\n    pass\n")
    project.write(
        "test_other.py",
        "def test_other():\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('other\\n')\n",
    )

    broken = _run(project, native)
    broken.expect(2)
    assert _last_failed(project) == {"test_broken.py": True}
    assert _events(project) == []

    project.write(
        "test_broken.py",
        "def test_broken():\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write('broken\\n')\n",
    )
    recovered = _run(project, native, "--lf")
    recovered.expect(0)
    assert _events(project) == ["broken"]
    assert _last_failed(project) == {}


def test_continued_collection_errors_persist_and_select_the_recovered_module(
    tmp_path: Path,
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("test_broken.py", 'raise ValueError("collection failure")\n')
        project.write(
            "test_other.py",
            "def test_other():\n"
            "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
            "        stream.write('other\\n')\n",
        )

        failed = _run_public_cache(project, native, "--continue-on-collection-errors")
        failed.expect(1)
        assert "ValueError: collection failure" in failed.stdout
        assert _events(project) == ["other"]
        assert _last_failed(project) == {"test_broken.py": True}

        project.write(
            "test_broken.py",
            "def test_broken():\n"
            "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
            "        stream.write('broken\\n')\n",
        )
        _clear_events(project)
        recovered = _run_public_cache(project, native, "--lf")
        recovered.expect(0)
        assert _events(project) == ["broken"]
        assert _last_failed(project) == {}


@pytest.mark.parametrize("stop_early", [False, True], ids=["complete", "maxfail"])
def test_compact_parameter_cache_retains_all_selected_cases_and_drives_new_first(
    tmp_path: Path, stop_early: bool
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write(
            "test_old.py",
            "import pytest\n"
            "from pathlib import Path\n"
            "@pytest.mark.parametrize('value', [0, 1, 2])\n"
            "def test_old(value):\n"
            "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
            "        stream.write(f'old{value}\\n')\n"
            "    assert not Path('fail.flag').exists() or value != 0\n",
        )
        if stop_early:
            project.write("fail.flag", "")
        initial = _run_public_cache(project, native, *(["--maxfail=1"] if stop_early else []))
        assert initial.returncode == int(stop_early), initial.stdout + initial.stderr
        assert _events(project) == (["old0"] if stop_early else ["old0", "old1", "old2"])
        assert json.loads(
            (project.root / ".pytest_cache/v/cache/nodeids").read_text(encoding="utf-8")
        ) == ["test_old.py::test_old[0]", "test_old.py::test_old[1]", "test_old.py::test_old[2]"]

        if stop_early:
            (project.root / "fail.flag").unlink()
        _clear_events(project)
        project.write(
            "test_new.py",
            "def test_new():\n"
            "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
            "        stream.write('new\\n')\n",
        )
        os.utime(project.root / "test_old.py", (1_700_000_000, 1_700_000_000))
        os.utime(project.root / "test_new.py", (1_600_000_000, 1_600_000_000))
        reordered = _run_public_cache(project, native, "--nf")
        reordered.expect(0)
        assert _events(project) == ["new", "old0", "old1", "old2"]


@pytest.mark.parametrize(
    "arguments",
    [("-m", "visible"), ("--deselect", "test_cases.py::test_hidden"), ("--collect-only",)],
    ids=["compact-marker-selection", "ordinary-item-deselection", "collection-only"],
)
def test_cache_manifest_preserves_deselection_and_collection_only(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    for native in (False, True):
        project = VfyProject(tmp_path / ("native" if native else "stock"))
        project.write("pytest.ini", "[pytest]\nmarkers = visible\n")
        project.write(
            "test_cases.py",
            "import pytest\n"
            "@pytest.mark.parametrize('value', [0, 1])\n"
            "def test_hidden(value): pass\n"
            "@pytest.mark.visible\n"
            "@pytest.mark.parametrize('value', [0, 1])\n"
            "def test_visible(value): pass\n",
        )
        manifest_path = ".pytest_cache/v/cache/nodeids"
        historical = [
            "prior.py::test_prior",
            "test_cases.py::test_hidden[0]",
            "test_cases.py::test_visible",
            "test_cases.py::test_visible[0]",
        ]
        project.write(manifest_path, json.dumps(historical))
        result = _run_public_cache(project, native, *arguments)
        result.expect(0)
        expected = set(historical)
        if arguments != ("--collect-only",):
            expected.update(["test_cases.py::test_visible[0]", "test_cases.py::test_visible[1]"])
        assert json.loads((project.root / manifest_path).read_text(encoding="utf-8")) == sorted(
            expected
        )


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_last_failed_cache_clears_a_test_skipped_during_setup(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_setup_skip.py",
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def skip_during_setup():\n"
        "    pytest.skip('setup skip')\n"
        "def test_skipped():\n"
        "    raise AssertionError('the skipped test body must not run')\n",
    )
    project.write(
        ".pytest_cache/v/cache/lastfailed",
        json.dumps({"test_setup_skip.py::test_skipped": True}),
    )

    result = _run(project, native, "--lf")
    result.expect(0)
    assert _last_failed(project) == {}


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_stepwise_discards_seeded_failure_when_collected_count_changes(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_steps.py",
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b(): record('b')\n"
        "def test_c(): record('c')\n",
    )
    project.write(
        ".pytest_cache/v/cache/stepwise",
        json.dumps(
            {
                "last_failed": "test_steps.py::test_b",
                "last_test_count": 2,
                "last_cache_date_str": "2026-09-25T12:00:00",
            }
        ),
    )

    result = _run(project, native, "--sw")
    result.expect(0)
    assert _events(project) == ["a", "b", "c"]


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_last_failed_ignores_seeded_nodeid_outside_collection(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_current.py",
        "def record(value):\n"
        "    with open('events.txt', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(value + '\\n')\n"
        "def test_a(): record('a')\n"
        "def test_b(): record('b')\n",
    )
    project.write(
        ".pytest_cache/v/cache/lastfailed",
        json.dumps({"test_removed.py::test_missing": True}),
    )

    result = _run(project, native, "--lf")
    result.expect(0)
    assert _events(project) == ["a", "b"]


def test_keyboard_interrupt_preserves_prior_native_cache_state(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "native")
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_interrupt.py",
        """from pathlib import Path
import pytest

def record(value):
    with Path('lifecycle.txt').open('a', encoding='utf-8') as stream:
        stream.write(value + '\\n')

@pytest.fixture(scope='session', autouse=True)
def session_resource():
    yield
    record('finalizer')

def test_interrupt():
    record('interrupt')
    raise KeyboardInterrupt()

def test_later():
    record('later')
""",
    )
    cache = project.root / ".pytest_cache/v/cache"
    cache.mkdir(parents=True)
    seeded_cache = {
        "lastfailed": '{"prior.py::test_prior": true}\n',
        "nodeids": '["prior.py::test_prior"]\n',
        "stepwise": (
            '{"last_failed":"prior.py::test_prior","last_test_count":1,'
            '"last_cache_date_str":"2026-09-25T12:00:00"}\n'
        ),
    }
    for name, contents in seeded_cache.items():
        (cache / name).write_text(contents, encoding="utf-8")
    previous_cache = _cache_snapshot(project)

    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
    )

    result.expect(2)
    assert (project.root / "lifecycle.txt").read_text(encoding="utf-8").splitlines() == [
        "interrupt",
        "finalizer",
    ]
    assert _cache_snapshot(project) == previous_cache
