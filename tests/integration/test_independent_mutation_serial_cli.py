"""Public fresh serial mutations run without upstream pytest distributions."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_independent_mutation
from tests.support.vfy_project import CommandResult

pytestmark = requires_independent_mutation

_SOURCE = isolated_vfy_path()

_GUARD = """import importlib.metadata
import os
from pathlib import Path
import pytest
from vfy.independent import facade as independent_facade

def pytest_sessionstart(session):
    names = {d.metadata['Name'].lower() for d in importlib.metadata.distributions()}
    assert not names.intersection({'pytest', 'pluggy', 'packaging'})
    assert pytest.fixture is independent_facade.fixture
    assert Path(pytest.__file__).is_file()
    source_root = Path(independent_facade.__file__).resolve().parent
    assert Path(pytest.__file__).resolve().is_relative_to(source_root)
    with Path('invocations').open('a') as stream:
        stream.write(str(os.getpid()) + '\\n')
"""


def _project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path, timeout=90)
    environment = tmp_path / ".independent-python"
    created = project.run_python("-m", "venv", "--symlinks", "--without-pip", str(environment))
    created.expect(0)
    project = VfyProject(tmp_path, timeout=90, interpreter=environment / "bin" / "python")
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _GUARD)
    return project


def _mutate(project: VfyProject, *options: str) -> CommandResult:
    return project.run_vfy(
        "mutate",
        "--workers=1",
        "--all",
        "--source=subject.py",
        "--operator=integer_plus_one",
        "--format=json",
        *options,
        env={"PYTHONPATH": _SOURCE},
    )


def test_public_mutant_changes_default_before_collection_and_uses_fresh_interpreter(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    subject = project.write("subject.py", "def value(default=1):\n    return default\n")
    project.write(
        "test_subject.py",
        """import os
from pathlib import Path
from subject import value

def test_value():
    actual = value()
    with Path('bodies').open('a') as stream:
        stream.write(f'{os.getpid()}:{actual}\\n')
    assert actual == 1

def test_after_first():
    with Path('after-first').open('a') as stream:
        stream.write(str(os.getpid()) + '\\n')
""",
    )
    before = subject.read_bytes()
    result = _mutate(project, "--max-mutations=1")
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    row = report["mutations"][0]
    assert row["owner"]["phase"] == "definition"
    assert row["original"] == "1"
    assert row["replacement"] == "2"
    assert row["killer"] == "test_subject.py::test_value"
    assert row["phase"] == "call"
    assert row["tests"] == []
    observations = (tmp_path / "bodies").read_text().splitlines()
    assert [line.split(":")[1] for line in observations] == ["1", "2"]
    assert len({line.split(":")[0] for line in observations}) == 2
    assert (tmp_path / "after-first").read_text().splitlines() == [observations[0].split(":")[0]]
    assert subject.read_bytes() == before
    assert (tmp_path / ".coverage").is_file()
    assert (tmp_path / ".vfy/mutations.json").is_file()


def test_failed_baseline_then_repair_does_not_cache_a_mutation_kill(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 1\n")
    project.write(
        "test_subject.py",
        """from pathlib import Path
from subject import value

def test_value():
    Path('body-reached').touch()
    assert Path('repaired').exists()
    assert value() == 1
""",
    )
    failed = _mutate(project, "--max-mutations=1")
    assert failed.returncode != 0
    assert (tmp_path / "body-reached").exists()
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy/mutations.json").exists()
    assert "baseline did not pass" in failed.stderr
    (tmp_path / "repaired").touch()
    repaired = _mutate(project, "--max-mutations=1")
    repaired.expect(0)
    assert json.loads(repaired.stdout)["summary"] == {"protected": 1}


def test_body_mutation_uses_affected_cases_and_preserves_cached_plan(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 1\n")
    project.write(
        "test_subject.py",
        "from pathlib import Path\nfrom subject import value\n"
        "def test_a_unrelated():\n"
        "    with Path('unrelated').open('a') as stream: stream.write('ran\\n')\n"
        "def test_value(request):\n"
        "    with Path('counts').open('a') as stream:\n"
        "        stream.write(str(request.session.testscollected))\n"
        "    assert value() == 1\n",
    )
    for cached in (False, True):
        result = _mutate(project, "--engine=native", "--max-mutations=1")
        result.expect(0)
        report = json.loads(result.stdout)
        assert report["mutations"][0]["tests"] == ["test_subject.py::test_value"]
        assert report["summary"] == {"protected": 1}
        assert report["metrics"]["cache_hits"] == int(cached)
    assert (tmp_path / "unrelated").read_text().splitlines() == ["ran"]
    assert (tmp_path / "counts").read_text() == "21"


def test_declared_test_assets_allow_retention_and_invalidate_cached_results(tmp_path: Path) -> None:
    stock = VfyProject(tmp_path)
    asset = stock.write("tests/certs/value.pem", "1")
    stock.write("subject.py", "def value():\n    return 2\n")
    stock.write(
        "tests/test_subject.py",
        "from pathlib import Path\nfrom subject import value\n"
        "def test_value():\n    assert value() == int(Path('tests/certs/value.pem').read_text())\n",
    )
    reference = stock.run_python("-B", "-m", "pytest", "-q", "tests/test_subject.py")
    assert reference.returncode == 1, reference
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 1\n")
    for cached in (False, True, False):
        result = _mutate(project, "--engine=native", "--max-mutations=1")
        assert result.returncode == 0, result
        report = json.loads(result.stdout)
        assert report["summary"] == {"protected": 1}
        assert report["mutations"][0]["tests"] == ["tests/test_subject.py::test_value"]
        assert report["metrics"]["cache_hits"] == int(cached)
        assert report["metrics"]["fast_results"] == int(not cached)
        if cached:
            asset.write_text("01")


@pytest.mark.parametrize("called_during_collection", [False, True])
def test_multiline_signature_distinguishes_definition_from_collection_body_calls(
    tmp_path: Path, called_during_collection: bool
) -> None:
    stock = VfyProject(tmp_path)
    subject = "def value(\n    argument: str | None = None,\n) -> int:\n    return {}\n"
    stock.write("subject.py", subject.format(2))
    observation = (
        "from subject import value\nOBSERVED = "
        + ("value()" if called_during_collection else "1")
        + "\n"
    )
    stock.write("conftest.py", observation)
    stock.write(
        "test_subject.py",
        "from subject import value\nfrom conftest import OBSERVED\n"
        "def test_value():\n    assert value() == OBSERVED\n",
    )
    reference = stock.run_python("-B", "-m", "pytest", "-q")
    assert reference.returncode == (0 if called_during_collection else 1), reference
    project = _project(tmp_path)
    project.write("conftest.py", (tmp_path / "conftest.py").read_text() + observation)
    project.write("subject.py", subject.format(1))
    result = _mutate(project, "--engine=native", "--max-mutations=1", "--no-cache")
    assert result.returncode == 0, result
    report = json.loads(result.stdout)
    assert report["summary"] == {"survived" if called_during_collection else "protected": 1}
    assert report["metrics"]["fast_results"] == int(not called_during_collection)
    assert report["mutations"][0]["tests"] == (
        [] if called_during_collection else ["test_subject.py::test_value"]
    )


def test_import_replaced_callable_code_preserves_fresh_mutation_outcome(tmp_path: Path) -> None:
    stock = VfyProject(tmp_path)
    replacement = (
        "value.__code__ = value.__code__.replace(co_consts=tuple(\n"
        "    1001 if isinstance(constant, int) else constant\n"
        "    for constant in value.__code__.co_consts))\n"
    )
    stock.write("subject.py", "def value():\n    return 1000\n" + replacement)
    stock.write(
        "test_subject.py",
        "from subject import value\ndef test_value():\n    assert value() == 1001\n",
    )
    reference = stock.run_python("-B", "-m", "pytest", "-q")
    assert reference.returncode == 0, reference
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 999\n" + replacement)
    result = _mutate(project, "--max-mutations=1", "--no-cache")
    assert result.returncode == 0, result
    report = json.loads(result.stdout)
    assert report["mutations"][0]["line"] == 2
    assert report["summary"] == {"survived": 1}
    assert report["metrics"]["fast_results"] == 0
    assert report["metrics"]["portable_fallbacks"] == 1


@pytest.mark.parametrize("read", ["value.__code__", "getattr(value, '__code__')"])
def test_collection_code_observation_preserves_fresh_mutation_outcome(
    tmp_path: Path, read: str
) -> None:
    stock = VfyProject(tmp_path)
    observation = (
        f"EXPECTED = next(value for value in {read}.co_consts if isinstance(value, int))\n"
    )
    stock.write("subject.py", "def value():\n    return 1000\n" + observation)
    stock.write(
        "test_subject.py",
        "from subject import value, EXPECTED\ndef test_value():\n    assert value() == EXPECTED\n",
    )
    reference = stock.run_python("-B", "-m", "pytest", "-q")
    assert reference.returncode == 0, reference
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 999\n" + observation)
    result = _mutate(project, "--max-mutations=1", "--no-cache")
    assert result.returncode == 0, result
    report = json.loads(result.stdout)
    assert report["mutations"][0]["line"] == 2
    assert report["summary"] == {"survived": 1}
    assert report["metrics"]["fast_results"] == 0
    assert report["metrics"]["portable_fallbacks"] == 1


def test_blocked_collection_guard_registration_uses_fresh_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 1\n")
    project.write(
        "test_subject.py", "from subject import value\ndef test_value():\n    assert value() == 1\n"
    )
    project.write(
        "sitecustomize.py",
        "import sys\ndef reject(event, args):\n"
        "    if event == 'sys.addaudithook':\n"
        "        raise RuntimeError('project rejects added audit hooks')\n"
        "sys.addaudithook(reject)\n",
    )
    result = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source=subject.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--format=json",
        "--no-cache",
        env={"PYTHONPATH": os.pathsep.join((_SOURCE, str(tmp_path)))},
    )
    assert result.returncode == 0, result
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["fast_results"] == 0
    assert report["metrics"]["portable_fallbacks"] == 1


def test_timeout_reaps_descendant_before_next_scheduled_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def first():\n    return 1\n\ndef second():\n    return 2\n")
    project.write(
        "test_subject.py",
        """import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from subject import first, second

def test_values():
    if first() == 2:
        child = subprocess.Popen([sys.executable, '-c',
            "import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c',"
            "\\\"import fcntl,os,signal; from pathlib import Path; "
            "f=open('descendant.lock','a'); fcntl.flock(f,fcntl.LOCK_EX); "
            "Path('grandchild.pid').write_text(str(os.getpid())); signal.pause()\\\"]); "
            "Path('child.pid').write_text(str(__import__('os').getpid())); time.sleep(60)"])
        deadline = time.monotonic() + 5
        while not Path('grandchild.pid').exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        Path('timed-body').write_text(str(os.getpid()))
        time.sleep(60)
    if Path('timed-body').exists():
        pids = [int(Path(name).read_text()) for name in ('child.pid','grandchild.pid','timed-body')]
        assert all(not Path(f'/proc/{pid}').exists() for pid in pids), pids
        with Path('descendant.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Path('continuation').write_text(json.dumps({'pid': os.getpid(), 'reaped': pids}))
    assert first() == 1
    assert second() == 2
""",
    )
    result = _mutate(project, "--max-mutations=2", "--timeout=3")
    result.expect(1)
    rows = json.loads(result.stdout)["mutations"]
    assert [row["status"] for row in rows] == ["timeout", "protected"]
    assert [row["replacement"] for row in rows] == ["2", "3"]
    assert [row["tests"] for row in rows] == [["test_subject.py::test_values"]] * 2
    assert json.loads(result.stdout)["metrics"]["fast_results"] == 2
    observed = json.loads((tmp_path / "continuation").read_text())
    assert observed["pid"] not in observed["reaped"]
    assert len(observed["reaped"]) == 3


def test_authenticated_worker_interrupt_stops_before_the_next_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def first():\n    return 1\n\ndef second():\n    return 2\n")
    project.write(
        "test_subject.py",
        """from pathlib import Path
from subject import first, second

def test_values():
    if first() == 2:
        Path('interrupt-reached').touch()
        raise KeyboardInterrupt('actual mutant interrupted')
    if Path('interrupt-reached').exists():
        Path('unexpected-continuation').touch()
    assert first() == 1
    assert second() == 2
""",
    )
    result = _mutate(project, "--max-mutations=2")
    assert result.returncode != 0
    assert (tmp_path / "interrupt-reached").is_file()
    assert not (tmp_path / "unexpected-continuation").exists()
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy/mutations.json").exists()


def test_parent_interrupt_runs_fixture_teardown_before_aborting_mutations(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def first():\n    return 1\n\ndef second():\n    return 2\n")
    project.write(
        "test_subject.py",
        """import signal
from pathlib import Path
import pytest
from subject import first, second

@pytest.fixture
def resource():
    yield
    if Path('parent-interrupt-reached').exists():
        Path('fixture-cleaned').touch()

def test_values(resource):
    if first() == 2:
        Path('parent-interrupt-reached').touch()
        signal.pause()
    if Path('parent-interrupt-reached').exists():
        Path('unexpected-continuation').touch()
    assert first() == 1
    assert second() == 2
""",
    )
    command = project.start_vfy(
        "mutate",
        "--engine=native",
        "--workers=1",
        "--all",
        "--source=subject.py",
        "--operator=integer_plus_one",
        "--format=json",
        "--max-mutations=2",
        "--timeout=30",
        env={"PYTHONPATH": _SOURCE},
    )
    try:
        project.wait_for_path("parent-interrupt-reached", timeout=30)
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=15)
        assert result.returncode != 0, result
        assert (tmp_path / "fixture-cleaned").exists(), result
        assert not (tmp_path / "unexpected-continuation").exists()
        assert not (tmp_path / ".coverage").exists()
        assert not (tmp_path / ".vfy/mutations.json").exists()
    finally:
        if command.poll() is None:
            command.kill()
            command.wait(timeout=5)


_COLLECTION_LOG = """
import json
def pytest_collection_finish(session):
    with Path('manifests').open('a') as stream:
        stream.write(json.dumps({'pid':os.getpid(),
            'nodeids':[item.nodeid for item in session.items]}) + '\\n')
"""

_SURVIVING_BODY = """import os
from pathlib import Path
from subject import value

def test_value():
    actual = value()
    with Path('bodies').open('a') as stream:
        stream.write(str(os.getpid()) + ':' + str(actual) + '\\n')
    assert actual in (1, 2)
"""


def _cache_project(path: Path) -> VfyProject:
    project = _project(path)
    project.write("subject.py", "def value():\n    return 1\n")
    project.write("test_subject.py", _SURVIVING_BODY)
    project.write("conftest.py", _GUARD + _COLLECTION_LOG)
    return project


@pytest.mark.parametrize("definition", [False, True])
def test_collected_body_abrupt_exit_reports_error_and_runs_next_isolated_mutation(
    tmp_path: Path,
    definition: bool,
) -> None:
    project = _cache_project(tmp_path)
    first = (
        "def first(default=1):\n    return default\n"
        if definition
        else "def first():\n    return 1\n"
    )
    original = first + "def second():\n    return 2\n"
    source = project.write("subject.py", original)
    project.write(
        "test_subject.py",
        "import os\nfrom pathlib import Path\nfrom subject import first, second\n"
        "def test_first():\n"
        "    if first() == 2:\n"
        "        Path('abrupt-pid').write_text(str(os.getpid()))\n        os._exit(7)\n"
        "    assert first() == 1\n"
        "def test_second():\n"
        "    Path('second-value').write_text(str(second()))\n    assert second() == 2\n",
    )
    result = _mutate(project, "--engine=native", "--max-mutations=2")
    result.expect(1)
    report = json.loads(result.stdout)
    assert report["summary"] == {"error": 1, "protected": 1}
    assert report["metrics"]["fast_results"] == (1 if definition else 2)
    assert report["mutations"][0]["returncode"] == 7
    assert (tmp_path / "second-value").read_text() == "3"
    collections = [
        json.loads(row)["pid"] for row in (tmp_path / "manifests").read_text().splitlines()
    ]
    assert (int((tmp_path / "abrupt-pid").read_text()) in collections) is definition
    assert source.read_text() == original


def test_fresh_full_suite_cache_hit_recollects_without_executing_and_no_cache_skips_probe(
    tmp_path: Path,
) -> None:
    project = _cache_project(tmp_path)
    project.write("subject.py", "def value(default=1):\n    return default\n")
    first = _mutate(project, "--max-mutations=1")
    first.expect(0)
    initial = json.loads(first.stdout)
    assert initial["summary"] == {"survived": 1}
    assert initial["metrics"]["executed"] == 1
    assert initial["metrics"]["cache_hits"] == 0
    assert initial["mutations"][0]["tests"] == []
    bodies = (tmp_path / "bodies").read_bytes()
    body_rows = bodies.decode().splitlines()
    assert [row.split(":")[1] for row in body_rows] == ["1", "2"]
    manifests = [json.loads(row) for row in (tmp_path / "manifests").read_text().splitlines()]
    assert len(manifests) == 3
    pids = {row["pid"] for row in manifests}
    assert len(pids) == 3
    assert len(pids - {int(row.split(":")[0]) for row in body_rows}) == 1
    assert all(row["nodeids"] == ["test_subject.py::test_value"] for row in manifests)
    cache = (tmp_path / ".vfy/mutations.json").read_bytes()
    assert json.loads(cache)["entries"][initial["mutations"][0]["id"]]["result"]["tests"] == []
    baseline = (tmp_path / ".vfy/mutation-baseline.json").read_bytes()
    coverage = (tmp_path / ".coverage").read_bytes()
    coverage_hash = hashlib.sha256(coverage).hexdigest()
    manifest = json.loads(baseline)
    assert manifest["data_file"] == str(tmp_path / ".coverage")
    assert manifest["data_hash"] == coverage_hash
    assert dict(manifest["suite_state"])[str(tmp_path / ".coverage")] == coverage_hash

    second = _mutate(project, "--max-mutations=1")
    second.expect(0)
    reused = json.loads(second.stdout)
    assert reused["summary"] == {"survived": 1}
    assert reused["metrics"]["executed"] == 0
    assert reused["metrics"]["cache_hits"] == 1
    assert reused["mutations"][0]["tests"] == []
    assert (tmp_path / "bodies").read_bytes() == bodies
    assert (tmp_path / ".vfy/mutations.json").read_bytes() == cache
    assert (tmp_path / ".vfy/mutation-baseline.json").read_bytes() == baseline
    assert (tmp_path / ".coverage").read_bytes() == coverage
    collected = [json.loads(row) for row in (tmp_path / "manifests").read_text().splitlines()]
    assert collected[:-1] == manifests
    assert collected[-1]["nodeids"] == ["test_subject.py::test_value"]
    assert collected[-1]["pid"] not in pids

    uncached = _mutate(project, "--max-mutations=1", "--no-cache")
    uncached.expect(0)
    uncached_report = json.loads(uncached.stdout)
    assert uncached_report["metrics"]["cache_hits"] == 0
    assert uncached_report["metrics"]["executed"] == 1
    after = [json.loads(row) for row in (tmp_path / "manifests").read_text().splitlines()]
    assert len(after) == len(collected) + 1
    assert after[:-1] == collected
    body_after = (tmp_path / "bodies").read_text().splitlines()
    assert body_after[:-1] == body_rows
    assert int(body_after[-1].split(":")[0]) == after[-1]["pid"]
    assert body_after[-1].split(":")[1] == "2"
    assert (tmp_path / ".vfy/mutations.json").read_bytes() == cache


def test_collection_rewriting_loaded_helper_never_publishes_mutation_cache(tmp_path: Path) -> None:
    project = _cache_project(tmp_path)
    project.write("helper.py", "VALUE = 0\n")
    project.write(
        "test_subject.py",
        "from pathlib import Path\nimport helper\nfrom subject import value\n"
        "def test_value():\n"
        "    Path('helper-seen').write_text(str(helper.VALUE))\n"
        "    assert value() in (1, 2)\n",
    )
    project.write(
        "conftest.py",
        _GUARD + "\nimport helper\n"
        "def pytest_collection_finish(session):\n"
        "    Path('helper.py').write_text(f'VALUE = {os.getpid()}\\n')\n",
    )
    for _ in range(2):
        result = _mutate(project, "--engine=native", "--max-mutations=1")
        result.expect(0)
        report = json.loads(result.stdout)
        assert report["summary"] == {"survived": 1}
        assert report["metrics"]["cache_hits"] == 0
        assert not (tmp_path / ".vfy/mutations.json").exists()
        assert (tmp_path / "helper-seen").read_text() != (
            tmp_path / "helper.py"
        ).read_text().split()[2]


@pytest.mark.parametrize("changed", ["source", "config", "arguments", "environment"])
def test_fresh_cache_invalidates_stable_nodeids_when_declared_inputs_change(
    tmp_path: Path,
    changed: str,
) -> None:
    project = _cache_project(tmp_path)
    arguments = project.write("arguments.txt", "--quiet\n")
    extra = ["-q", "@arguments.txt"] if changed == "arguments" else []
    first = _mutate(project, "--max-mutations=1", *extra)
    first.expect(0)
    initial = json.loads(first.stdout)
    if changed == "arguments":
        baseline = json.loads((tmp_path / ".vfy/mutation-baseline.json").read_text())
        assert baseline["pytest_args"] == extra
    previous_bodies = (tmp_path / "bodies").read_text().splitlines()
    if changed == "source":
        project.write("test_subject.py", _SURVIVING_BODY + "\n# actual source edit\n")
    elif changed == "config":
        project.write("pytest.ini", "[pytest]\nmarkers=changed: actual config edit\n")
    elif changed == "arguments":
        arguments.write_text("--verbose\n")
    if changed == "environment":
        second = project.run_vfy(
            "mutate",
            "--engine=native",
            "--workers=1",
            "--all",
            "--source=subject.py",
            "--operator=integer_plus_one",
            "--format=json",
            "--max-mutations=1",
            env={"PYTHONPATH": _SOURCE, "PYTEST_ADDOPTS": "-q"},
        )
    else:
        second = _mutate(project, "--max-mutations=1", *extra)
    second.expect(0)
    current = json.loads(second.stdout)
    assert current["summary"] == {"survived": 1}
    assert current["metrics"]["cache_hits"] == 0
    assert current["metrics"]["executed"] == 1
    assert current["mutations"][0]["id"] == initial["mutations"][0]["id"]
    assert current["fingerprint"] != initial["fingerprint"]
    assert len((tmp_path / "bodies").read_text().splitlines()) > len(previous_bodies)
    assert (tmp_path / "subject.py").read_text() == "def value():\n    return 1\n"


@pytest.mark.parametrize("dynamic", ["process", "declared-mutation"])
def test_fresh_mutant_collection_changes_execute_fully_without_cache_eligibility(
    tmp_path: Path,
    dynamic: str,
) -> None:
    project = _cache_project(tmp_path)
    prefix = "import os, pytest\nfrom pathlib import Path\nfrom subject import value\n\n"
    label = "str(os.getpid())" if dynamic == "process" else "str(value())"
    project.write(
        "test_subject.py",
        prefix
        + f"@pytest.mark.parametrize('entry', [None], ids=lambda _: {label})\n"
        + _SURVIVING_BODY.partition("\n\n")[2].replace(
            "def test_value():", "def test_value(entry):"
        ),
    )
    before = (tmp_path / "subject.py").read_bytes()
    for _ in range(2):
        result = _mutate(project, "--max-mutations=1")
        result.expect(0)
        report = json.loads(result.stdout)
        assert report["summary"] == {"survived": 1}
        assert report["metrics"]["executed"] == 1
        assert report["metrics"]["cache_hits"] == 0
        cache_path = tmp_path / ".vfy/mutations.json"
        if cache_path.exists():
            assert report["mutations"][0]["id"] not in json.loads(cache_path.read_text()).get(
                "entries", {}
            )
        assert (tmp_path / "subject.py").read_bytes() == before
    manifests = [json.loads(row) for row in (tmp_path / "manifests").read_text().splitlines()]
    assert len(manifests) >= 5
    if dynamic == "process":
        assert all(
            row["nodeids"] == [f"test_subject.py::test_value[{row['pid']}]"] for row in manifests
        )
    else:
        assert [row["nodeids"] for row in manifests].count(["test_subject.py::test_value[2]"]) == 2


def test_duplicate_display_ids_execute_both_real_origins_without_cache_reuse(
    tmp_path: Path,
) -> None:
    project = _cache_project(tmp_path)
    project.write(
        "test_subject.py",
        """import os
from pathlib import Path
from subject import value
def record(name):
    with Path('bodies').open('a') as stream:
        stream.write(str(os.getpid()) + ':' + name + ':' + str(value()) + '\\n')
    assert value() in (1, 2)
def test_first(): record('first')
def test_second(): record('second')
""",
    )
    initial = _mutate(project, "--max-mutations=1")
    initial.expect(0)
    cached = (tmp_path / ".vfy/mutations.json").read_bytes()
    project.write(
        "conftest.py",
        _GUARD
        + _COLLECTION_LOG
        + """
def pytest_collection_modifyitems(items):
    assert len(items) == 2 and items[0] is not items[1]
    for item in items:
        item.nodeid = 'shared.py::test_same'
""",
    )
    for _ in range(2):
        before = (tmp_path / "bodies").read_text().splitlines()
        actual = _mutate(project, "--max-mutations=1")
        actual.expect(0)
        report = json.loads(actual.stdout)
        assert report["summary"] == {"survived": 1}
        assert report["metrics"]["cache_hits"] == 0
        assert report["metrics"]["executed"] == 1
        assert (tmp_path / ".vfy/mutations.json").read_bytes() == cached
        rows = (tmp_path / "bodies").read_text().splitlines()[len(before) :]
        assert [row.split(":")[1:] for row in rows[-2:]] == [["first", "2"], ["second", "2"]]
        assert rows[-2].split(":")[0] == rows[-1].split(":")[0]
    manifests = [json.loads(row) for row in (tmp_path / "manifests").read_text().splitlines()]
    assert manifests[-1]["nodeids"] == ["shared.py::test_same", "shared.py::test_same"]


def test_open_entropy_descriptor_keeps_the_collected_mutation_path(tmp_path: Path) -> None:
    # Python 3.14 keeps a descriptor for /dev/urandom open; a forked worker can share it.
    project = _cache_project(tmp_path)
    project.write("subject.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "test_subject.py",
        "from subject import first, second\n"
        "def test_first():\n    assert first() == 1\n"
        "def test_second():\n    assert second() == 2\n",
    )
    project.write(
        "conftest.py",
        _GUARD + _COLLECTION_LOG + "\nENTROPY = os.open('/dev/urandom', os.O_RDONLY)\n",
    )
    result = _mutate(project, "--engine=native", "--max-mutations=2")
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 2}
    assert report["metrics"]["fast_results"] == 2
