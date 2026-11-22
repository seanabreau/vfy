"""Prove the worker-owned collection controller through public commands."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

_EXPERIMENT = {"VFY_WORKER_COLLECTION": "1"}
_BASELINE = {"VFY_WORKER_COLLECTION": "0"}

_LIFECYCLE = """
import json, os
from pathlib import Path
import pytest

def role():
    return os.environ.get('VFY_NATIVE_PARALLEL_INDEX',
        os.environ.get('PYTEST_XDIST_WORKER', 'controller'))

def save(stage, data):
    Path(f'{stage}-{role()}.json').write_text(json.dumps(data))

@pytest.hookimpl(tryfirst=True)
def pytest_collection(session):
    save('outer', [item.nodeid for item in session.items])

def pytest_collection_modifyitems(session, config, items):
    save('modify', [item.nodeid for item in items])

def pytest_collection_finish(session):
    save('collect', [item.nodeid for item in session.items])

def pytest_sessionfinish(session, exitstatus):
    save('finish', {'items': [item.nodeid for item in session.items],
                   'collected': session.testscollected, 'status': int(exitstatus)})

@pytest.fixture(scope='session')
def owned():
    yield role()
    save('finalizer', {'role': role()})
"""


def _native(project: VfyProject, *args: str, experimental: bool = True) -> CommandResult:
    return project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        *args,
        env={
            **(_EXPERIMENT if experimental else _BASELINE),
            "VFY_NATIVE_SCHEDULING": "cases",
        },
    )


def _junit(
    project: VfyProject,
) -> list[tuple[dict[str, str], list[tuple[str, dict[str, str], str]]]]:
    root = ET.parse(project.root / "result.xml").getroot()
    cases = [
        (
            {
                key: value.replace(str(project.root), "<project>")
                for key, value in case.attrib.items()
                if key != "time"
            },
            [
                (
                    child.tag,
                    dict(child.attrib),
                    (child.text or "").replace(str(project.root), "<project>"),
                )
                for child in case
            ],
        )
        for case in root.iter("testcase")
    ]
    return sorted(
        cases,
        key=lambda value: (value[0].get("classname", ""), value[0].get("name", "")),
    )


@pytest.mark.parametrize("count", [0, 1, 5])
def test_worker_controller_has_no_items_and_preserves_junit(tmp_path: Path, count: int) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    source = "import pytest\n" + "\n".join(
        f"def test_case_{index}(owned):\n    assert owned != 'controller'\n"
        for index in range(count)
    )
    for project in projects.values():
        project.write("conftest.py", _LIFECYCLE)
        project.write("test_cases.py", source)
    stock = projects["stock"].run_pytest(
        "-n",
        "2",
        "-q",
        "-W",
        "error::pytest.PytestAssertRewriteWarning",
        "--junitxml=result.xml",
        "-o",
        "junit_family=legacy",
        env=_BASELINE,
    )
    native = _native(
        projects["native"],
        "-q",
        "-W",
        "error::pytest.PytestAssertRewriteWarning",
        "--junitxml=result.xml",
        "-o",
        "junit_family=legacy",
    )
    expected_status = 0 if count else 5
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == expected_status, result.stdout + result.stderr
        assert "Module already imported so cannot be rewritten" not in result.stdout + result.stderr
        finish = json.loads((project.root / "finish-controller.json").read_text())
        assert finish["items"] == []
        assert json.loads((project.root / "outer-controller.json").read_text()) == []
        assert finish["collected"] == count
        assert not (project.root / "modify-controller.json").exists()
        assert not (project.root / "collect-controller.json").exists()
        collections = [json.loads(path.read_text()) for path in project.root.glob("collect-*.json")]
        expected_workers = 2 if project is projects["stock"] else max(1, min(2, count))
        assert len(collections) == expected_workers
        assert all(len(items) == count for items in collections)
        finalizers = [
            json.loads(path.read_text()) for path in project.root.glob("finalizer-*.json")
        ]
        assert all(record["role"] != "controller" for record in finalizers)
        assert bool(finalizers) == bool(count)
    assert _junit(projects["native"]) == _junit(projects["stock"])


def test_worker_collection_failures_preserve_native_preflight(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for project in projects.values():
        project.write("conftest.py", _LIFECYCLE)
        project.write("test_bad.py", "raise RuntimeError('collection-anchor')\n")
        project.write(
            "test_good.py",
            "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
        )
    results = {
        name: _native(project, "-q", "--junitxml=result.xml", experimental=name == "candidate")
        for name, project in projects.items()
    }
    for name, result in results.items():
        assert result.returncode == 2, result.stdout + result.stderr
        assert "collection-anchor" in result.stdout
        assert "during collection" in result.stdout
        assert not (projects[name].root / "body-ran").exists()
        xml = ET.parse(projects[name].root / "result.xml")
        errors = list(xml.iter("error"))
        assert len(errors) == 1
        assert "collection-anchor" in (errors[0].text or "")


def test_worker_collection_skips_reach_controller_junit(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    for project in projects.values():
        project.write("conftest.py", _LIFECYCLE)
        project.write(
            "test_skip.py",
            "import pytest\npytest.skip('collection-skip-anchor', allow_module_level=True)\n",
        )
    stock = projects["stock"].run_pytest("-n", "2", "-q", "--junitxml=result.xml", env=_BASELINE)
    native = _native(projects["native"], "-q", "--junitxml=result.xml")
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == 5, result.stdout + result.stderr
        assert "collection-skip-anchor" in (project.root / "result.xml").read_text()
    assert _junit(projects["native"]) == _junit(projects["stock"])


def _cache(project: VfyProject, name: str) -> object:
    return json.loads((project.root / ".pytest_cache" / "v" / "cache" / name).read_text())


def test_worker_collection_adopts_full_selection_history(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for project in projects.values():
        project.write(
            "test_cases.py",
            "def test_first():\n    assert False\n"
            "def test_second():\n    pass\ndef test_third():\n    pass\n",
        )
    for name, project in projects.items():
        failed = _native(project, "-q", experimental=name == "candidate")
        failed.expect(1)
        assert _cache(project, "lastfailed") == {"test_cases.py::test_first": True}
        project.write(
            "test_cases.py",
            "def test_first():\n    pass\n"
            "def test_second():\n    pass\ndef test_third():\n    pass\n",
        )
        recovered = _native(project, "--lf", "-q", experimental=name == "candidate")
        recovered.expect(0)
        assert "1 passed" in recovered.stdout
        assert _cache(project, "lastfailed") == {}
        # A selected-only inventory would discard the two previously cached siblings.
        assert _cache(project, "nodeids") == [
            "test_cases.py::test_first",
            "test_cases.py::test_second",
            "test_cases.py::test_third",
        ]
        ordered = _native(project, "--nf", "-q", experimental=name == "candidate")
        ordered.expect(0)
        empty = _native(project, "-k", "missing", "-q", experimental=name == "candidate")
        empty.expect(5)
    assert _cache(projects["candidate"], "nodeids") == _cache(projects["baseline"], "nodeids")


def test_worker_collection_continuation_uses_existing_collector(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for project in projects.values():
        project.write("conftest.py", _LIFECYCLE)
        project.write("test_bad.py", "raise RuntimeError('continue-collection-anchor')\n")
        project.write(
            "test_good.py",
            "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
        )
    for name, project in projects.items():
        result = _native(
            project, "--continue-on-collection-errors", "-q", experimental=name == "candidate"
        )
        result.expect(1)
        assert (project.root / "body-ran").exists()
        assert json.loads((project.root / "collect-controller.json").read_text()) == [
            "test_good.py::test_body"
        ]


@pytest.mark.parametrize("returncode", [0, 7])
def test_worker_collection_exit_preserves_explicit_status(tmp_path: Path, returncode: int) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    preserved = {
        "native-result.json": '{"sentinel": true}\n',
        ".pytest_cache/v/cache/lastfailed": '{"unrelated.py::test_previous": true}\n',
        ".pytest_cache/v/cache/nodeids": '["unrelated.py::test_previous"]\n',
    }
    for project in projects.values():
        for path, content in preserved.items():
            project.write(path, content)
        project.write(
            "conftest.py",
            "import pytest\ndef pytest_collection_finish(session):\n"
            f"    pytest.exit('collection-exit-anchor', returncode={returncode})\n",
        )
        project.write(
            "test_exit.py",
            "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
        )
    for name, project in projects.items():
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=2",
            "--no-replay",
            "--no-cov",
            "-q",
            env={
                **(_EXPERIMENT if name == "candidate" else _BASELINE),
                "VFY_NATIVE_SCHEDULING": "cases",
                "PYTEST_NATIVE_RESULT": str(project.root / "native-result.json"),
            },
        )
        assert result.returncode == returncode, result.stdout + result.stderr
        assert not (project.root / "body-ran").exists()
        assert "collection-exit-anchor" in result.stdout + result.stderr
        for path, content in preserved.items():
            assert (project.root / path).read_bytes() == content.encode()


@pytest.mark.parametrize("phase", ["collection", "execution", "finish"])
def test_worker_intentional_exit_cannot_mask_incomplete_shutdown(
    tmp_path: Path, phase: str
) -> None:
    project = VfyProject(tmp_path)
    preserved = {
        "native-result.json": '{"sentinel": true}\n',
        ".pytest_cache/v/cache/lastfailed": '{"unrelated.py::test_previous": true}\n',
        ".pytest_cache/v/cache/nodeids": '["unrelated.py::test_previous"]\n',
    }
    for path, content in preserved.items():
        project.write(path, content)
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "def stop_worker():\n"
        "    if os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        Path('intentional-exit').touch()\n"
        "        pytest.exit('masked exit anchor', returncode=0)\n"
        "def pytest_collection_finish(session):\n"
        "    if os.getenv('EXIT_PHASE') == 'collection':\n        stop_worker()\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if os.getenv('EXIT_PHASE') == 'finish':\n        stop_worker()\n"
        "def pytest_unconfigure(config):\n"
        "    if os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        Path('incomplete-shutdown').touch()\n"
        "        os._exit(0)\n",
    )
    project.write(
        "test_exit.py",
        "import os\nimport pytest\nfrom conftest import stop_worker\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_body(value):\n"
        "    if os.getenv('EXIT_PHASE') == 'execution':\n        stop_worker()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--native-scheduling=cases",
        "--no-replay",
        "--no-cov",
        "-q",
        env={
            **_EXPERIMENT,
            "EXIT_PHASE": phase,
            "PYTEST_NATIVE_RESULT": str(project.root / "native-result.json"),
        },
    )
    output = result.stdout + result.stderr
    assert (project.root / "intentional-exit").exists(), output
    assert (project.root / "incomplete-shutdown").exists(), output
    # os._exit skips pytest.main's return and the required completion envelope.
    assert result.returncode == pytest.ExitCode.INTERNAL_ERROR, output
    assert "without a valid collection completion" in output
    for path, content in preserved.items():
        assert (project.root / path).read_bytes() == content.encode()


@pytest.mark.parametrize("returncode", [0, 7])
def test_worker_sessionfinish_exit_preserves_explicit_status(
    tmp_path: Path, returncode: int
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    for project in projects.values():
        project.write("native-result.json", '{"sentinel": true}\n')
        project.write(
            "conftest.py",
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "def role():\n"
            "    return os.getenv('VFY_NATIVE_PARALLEL_INDEX',\n"
            "        'stock' if os.getenv('FINISH_SERIAL_STOCK') else 'controller')\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    if role() in {'0', 'stock'}:\n"
            "        Path('finish-requested').touch()\n"
            f"        pytest.exit('finish-exit-anchor', returncode={returncode})\n",
        )
        project.write(
            "test_cases.py",
            "from pathlib import Path\nimport pytest\nfrom conftest import role\n"
            "@pytest.mark.parametrize('value', range(4))\n"
            "def test_case(value):\n    Path(f'body-{role()}').touch()\n",
        )
    results = {
        "stock": projects["stock"].run_pytest(
            "-q", load_xdist=False, env={**_BASELINE, "FINISH_SERIAL_STOCK": "1"}
        ),
        "native": projects["native"].run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=2",
            "--native-scheduling=cases",
            "--no-replay",
            "--no-cov",
            "-q",
            env={
                **_EXPERIMENT,
                "PYTEST_NATIVE_RESULT": str(projects["native"].root / "native-result.json"),
            },
        ),
    }
    for name, result in results.items():
        project = projects[name]
        output = result.stdout + result.stderr
        assert result.returncode == returncode, output
        assert "finish-exit-anchor" in output
        assert (project.root / "finish-requested").exists(), output
        assert (project.root / f"body-{'stock' if name == 'stock' else '0'}").exists(), output
        assert (project.root / "native-result.json").read_text() == '{"sentinel": true}\n'


def test_worker_collection_manifest_mismatch_runs_no_bodies(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    source = """
import os
from pathlib import Path

def pytest_generate_tests(metafunc):
    worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX',
        os.environ.get('PYTEST_XDIST_WORKER', 'controller'))
    metafunc.parametrize('value', range(3 if worker in {'1', 'gw1'} else 2))

def test_case(value):
    Path('body-ran').touch()
"""
    for project in projects.values():
        project.write("test_cases.py", source)
    stock = projects["stock"].run_pytest("-n", "2", "-q", env=_BASELINE)
    native = _native(projects["native"], "-q")
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == 1, result.stdout + result.stderr
        assert not (project.root / "body-ran").exists()
        assert "different" in (result.stdout + result.stderr).lower()


def test_worker_collection_waits_for_outer_wrapper_changes(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    conftest = (
        _LIFECYCLE
        + """
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session):
    result = yield
    session.items.reverse()
    save('after-wrapper', [item.nodeid for item in session.items])
    return result
"""
    )
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write(
            "test_cases.py",
            "import pytest\n@pytest.mark.parametrize('value', range(5))\n"
            "def test_case(value, owned):\n    assert owned != 'controller'\n",
        )
    stock = projects["stock"].run_pytest("-n", "2", "-q", env=_BASELINE)
    native = _native(projects["native"], "-q")
    expected = [f"test_cases.py::test_case[{index}]" for index in reversed(range(5))]
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "5 passed" in result.stdout
        assert json.loads((project.root / "after-wrapper-controller.json").read_text()) == []
        finishes = [
            json.loads(path.read_text())
            for path in project.root.glob("finish-*.json")
            if path.name != "finish-controller.json"
        ]
        assert len(finishes) == 2
        assert all(record["items"] == expected for record in finishes)


def test_worker_collection_early_maxfail_preserves_native_status(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for project in projects.values():
        project.write("test_a_bad.py", "raise RuntimeError('early-collection-maxfail-anchor')\n")
        project.write(
            "test_z_good.py",
            "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
        )
    for name, project in projects.items():
        result = _native(project, "-x", "-q", experimental=name == "candidate")
        result.expect(1)
        assert "early-collection-maxfail-anchor" in result.stdout
        assert not (project.root / "body-ran").exists()


def test_worker_collection_error_allows_sessionfinish_status_override(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("baseline", "candidate")}
    for project in projects.values():
        project.write(
            "conftest.py",
            "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n",
        )
        project.write("test_bad.py", "raise RuntimeError('collection-status-override-anchor')\n")
    for name, project in projects.items():
        result = _native(project, "-q", "--junitxml=result.xml", experimental=name == "candidate")
        result.expect(0)
        assert "collection-status-override-anchor" in result.stdout
        assert _cache(project, "lastfailed") == {"test_bad.py": True}
        errors = list(ET.parse(project.root / "result.xml").iter("error"))
        assert len(errors) == 1
        assert "collection-status-override-anchor" in (errors[0].text or "")
    assert _junit(projects["candidate"]) == _junit(projects["baseline"])


def test_worker_collection_error_forwards_collection_and_finish_warnings(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    conftest = """
import os, warnings

def role():
    return os.environ.get('VFY_NATIVE_PARALLEL_INDEX',
        os.environ.get('PYTEST_XDIST_WORKER', 'controller')).replace('gw', '')

def pytest_sessionfinish(session, exitstatus):
    warnings.warn(f'finish-warning-anchor-{role()}', UserWarning)
"""
    source = """
import warnings
import pytest
from pathlib import Path
from conftest import role
warnings.warn(f'collection-warning-anchor-{role()}', UserWarning)
if role() == '1':
    raise RuntimeError('warning-error-anchor')

@pytest.mark.parametrize('value', range(2))
def test_body(value):
    Path('body-ran').touch()
"""
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write("test_bad.py", source)
    stock = projects["stock"].run_pytest("-n", "2", "-q", env=_BASELINE)
    native = _native(projects["native"], "-q")
    stock.expect(1)
    native.expect(2)
    assert all(not (project.root / "body-ran").exists() for project in projects.values())
    for result in (stock, native):
        text = result.stdout + result.stderr
        for worker in ("0", "1"):
            assert text.count(f"collection-warning-anchor-{worker}") == 1, text
            assert text.count(f"finish-warning-anchor-{worker}") == 1, text
        assert text.count("finish-warning-anchor-controller") == 1, text


@pytest.mark.parametrize("stage", ["unconfigure", "cleanup"])
def test_worker_shutdown_failure_rejects_successful_body_and_green_replay(
    tmp_path: Path, stage: str
) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("src/app.py", "def read_value():\n    return 1\n")
        project.write(
            "conftest.py",
            "import os\nfrom pathlib import Path\n"
            "def role():\n"
            "    return os.getenv('VFY_NATIVE_PARALLEL_INDEX',\n"
            "        'stock' if os.getenv('SHUTDOWN_SERIAL_STOCK') else 'controller')\n"
            "def fail_shutdown():\n"
            "    Path(f'shutdown-{role()}').touch()\n"
            "    try:\n        raise OSError('shutdown cause sentinel')\n"
            "    except OSError as cause:\n"
            "        raise ValueError('worker shutdown sentinel') from cause\n"
            "def enabled(stage):\n"
            "    return (os.getenv('SHUTDOWN_FAILURE') == stage\n"
            "        and role() in {'0', 'stock'})\n"
            "def pytest_configure(config):\n"
            "    if enabled('cleanup'):\n        config.add_cleanup(fail_shutdown)\n"
            "def pytest_unconfigure(config):\n"
            "    if enabled('unconfigure'):\n        fail_shutdown()\n",
        )
        project.write(
            "test_cases.py",
            "from pathlib import Path\nimport pytest\n"
            "from conftest import role\nfrom src.app import read_value\n"
            "@pytest.mark.parametrize('value', range(4))\n"
            "def test_case(value):\n"
            "    assert read_value() == 1\n"
            "    Path(f'body-{role()}').touch()\n",
        )
        preserved: dict[str, bytes] = {}
        arguments = ("--all", "--history=history.json", "--source=src", "--cov-report=", "-q")
        if route == "native":
            project.git_init()
            seed = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                *arguments,
                env={**_BASELINE, "SHUTDOWN_FAILURE": None},
            )
            seed.expect(0)
            preserved = {
                name: (project.root / name).read_bytes()
                for name in ("history.json", "test-run.json", "replay.coverage")
            }
            (project.root / "body-controller").unlink()
            project.write("src/app.py", "def read_value():\n    return 1 + 0\n")
            project.git_commit("change covered source before worker shutdown failure")
        environment = {"SHUTDOWN_FAILURE": stage}
        result = (
            # Serial pytest is the exception/status oracle; xdist has its own
            # worker-crash recovery policy after successful test reports.
            project.run_pytest(
                "-q",
                load_xdist=False,
                env={**_BASELINE, **environment, "SHUTDOWN_SERIAL_STOCK": "1"},
            )
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--native-scheduling=cases",
                *arguments,
                env={**_EXPERIMENT, **environment},
            )
        )
        output = result.stdout + result.stderr
        failing_role = "stock" if route == "stock" else "0"
        assert (project.root / f"body-{failing_role}").exists(), output
        assert (project.root / f"shutdown-{failing_role}").exists(), output
        assert result.returncode == 1, output
        assert "worker shutdown sentinel" in output
        assert "shutdown cause sentinel" in output
        assert not (project.root / "shutdown-controller").exists(), output
        assert {name: (project.root / name).read_bytes() for name in preserved} == preserved
