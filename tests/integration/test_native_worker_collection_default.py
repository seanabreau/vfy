"""Compare automatic worker collection with pytest's process lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


def test_explicit_workers_collect_in_fresh_processes_without_parent_test_imports(
    tmp_path: Path,
) -> None:
    projects = {
        name: VfyProject(tmp_path / name)
        for name in ("stock_serial", "native_serial", "stock_workers", "native_workers")
    }
    for project in projects.values():
        project.write(
            "conftest.py",
            "import json, os\nfrom pathlib import Path\nimport pytest\n"
            "def role():\n"
            "    return os.environ.get('VFY_NATIVE_PARALLEL_INDEX', "
            "os.environ.get('PYTEST_XDIST_WORKER', 'controller'))\n"
            "def save(stage, value):\n"
            "    Path(f'{stage}-{role()}.json').write_text(json.dumps(value))\n"
            "def pytest_collection_modifyitems(session, config, items):\n"
            "    save('modify', [item.nodeid for item in items])\n"
            "def pytest_collection_finish(session):\n"
            "    save('collect', [item.nodeid for item in session.items])\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    save('finish', {'items': [item.nodeid for item in session.items], "
            "'collected': session.testscollected, 'status': int(exitstatus)})\n"
            "@pytest.fixture(scope='session')\n"
            "def resource():\n"
            "    yield os.getpid()\n"
            "    save('finalizer', {'pid': os.getpid()})\n",
        )
        for name in ("a", "b"):
            project.write(
                f"test_{name}.py",
                "import os\nfrom pathlib import Path\n"
                "import_pid = os.getpid()\n"
                "role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', "
                "os.environ.get('PYTEST_XDIST_WORKER', 'controller'))\n"
                f"Path(f'import-{name}-{{role}}').write_text(str(import_pid))\n"
                "def test_process(resource):\n"
                "    assert import_pid == resource == os.getpid()\n",
            )
    environment = {"VFY_WORKER_COLLECTION": None, "VFY_WORKER_START": "exec"}
    results = {
        "stock_serial": projects["stock_serial"].run_pytest("-q", env=environment),
        "stock_workers": projects["stock_workers"].run_pytest(
            "-n", "2", "--dist=loadfile", "-q", env=environment
        ),
        "native_serial": projects["native_serial"].run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=1",
            "--no-replay",
            "--no-cov",
            "-q",
            env=environment,
        ),
        "native_workers": projects["native_workers"].run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=2",
            "--no-replay",
            "--no-cov",
            "-q",
            env=environment,
        ),
    }
    inventory = ["test_a.py::test_process", "test_b.py::test_process"]
    for name, result in results.items():
        assert result.returncode == 0, result.stdout + result.stderr
        project = projects[name]
        distributed = name.endswith("workers")
        parent = json.loads((project.root / "finish-controller.json").read_text())
        assert parent == {"items": [] if distributed else inventory, "collected": 2, "status": 0}
        for stage in ("modify", "collect"):
            records = sorted(project.root.glob(f"{stage}-*.json"))
            assert len(records) == (2 if distributed else 1)
            assert all(json.loads(path.read_text()) == inventory for path in records)
            assert (project.root / f"{stage}-controller.json").exists() is not distributed
        imports = sorted(project.root.glob("import-*"))
        assert len(imports) == (4 if distributed else 2)
        assert all(("controller" in path.name) is not distributed for path in imports)
        finalizers = sorted(project.root.glob("finalizer-*.json"))
        assert len(finalizers) == (2 if distributed else 1)
        assert all(("controller" in path.name) is not distributed for path in finalizers)


@pytest.mark.parametrize("option", ["collectonly", "continue_on_collection_errors"])
@pytest.mark.parametrize("enabled", [True, False])
def test_workers_honor_options_changed_during_configure(
    tmp_path: Path, option: str, enabled: bool
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    for project in projects.values():
        project.write(
            "conftest.py",
            "import json, os\nfrom pathlib import Path\n"
            "def pytest_configure(config):\n"
            f"    config.option.{option} = {enabled!r}\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')\n"
            "    Path(f'finish-{role}.json').write_text(json.dumps({\n"
            "        'items': [item.nodeid for item in session.items],\n"
            "        'collected': session.testscollected, 'status': int(exitstatus)}))\n",
        )
        project.write(
            "test_good.py",
            "import os\nfrom pathlib import Path\n"
            "role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')\n"
            "Path(f'import-{role}').touch()\n"
            "def test_body():\n    Path('body-ran').touch()\n",
        )
        if option == "continue_on_collection_errors":
            project.write("test_bad.py", "raise RuntimeError('collection-anchor')\n")
    arguments = ["-q"]
    if not enabled:
        arguments.append(
            "--collect-only" if option == "collectonly" else "--continue-on-collection-errors"
        )
    environment = {"VFY_WORKER_COLLECTION": None, "VFY_WORKER_START": "exec"}
    stock = projects["stock"].run_pytest(*arguments, env=environment)
    native = projects["native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        *arguments,
        env=environment,
    )
    expected_status = (1 if enabled else 2) if option == "continue_on_collection_errors" else 0
    for name, result in (("stock", stock), ("native", native)):
        assert result.returncode == expected_status, result.stdout + result.stderr
        body_runs = (option == "continue_on_collection_errors") == enabled
        assert (projects[name].root / "body-ran").exists() is body_runs
        finish = json.loads((projects[name].root / "finish-controller.json").read_text())
        assert finish["collected"] == 1
        assert finish["status"] == expected_status
        if option == "continue_on_collection_errors":
            assert "collection-anchor" in result.stdout + result.stderr
    controller = projects["native"].root
    assert (controller / "import-controller").exists() is enabled
    finish = json.loads((controller / "finish-controller.json").read_text())
    assert finish["items"] == (["test_good.py::test_body"] if enabled else [])
    if not enabled:
        assert list(controller.glob("import-[0-9]*"))


@pytest.mark.parametrize(
    ("scheduling", "files", "cases", "width"),
    [("file", 1, 1, 1), ("file", 2, 1, 2), ("cases", 1, 1, 1), ("cases", 1, 2, 2)],
)
def test_explicit_capacity_starts_only_workers_with_assigned_tests(
    tmp_path: Path, scheduling: str, files: int, cases: int, width: int
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("stock", "native")}
    for project in projects.values():
        project.write("src/app.py", "def answer():\n    return 42\n")
        project.write(
            "conftest.py",
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "@pytest.fixture(scope='session')\n"
            "def resource():\n"
            "    role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')\n"
            "    yield os.getpid()\n"
            "    Path(f'finalizer-{role}').touch()\n",
        )
        for index in range(files):
            project.write(
                f"test_{index}.py",
                "import os\nfrom pathlib import Path\nimport pytest\nfrom src.app import answer\n"
                "import_pid = os.getpid()\n"
                "role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')\n"
                f"Path(f'import-{index}-{{role}}').touch()\n"
                f"@pytest.mark.parametrize('value', range({cases}))\n"
                "def test_answer(value, resource):\n"
                "    assert import_pid == resource == os.getpid()\n"
                "    assert answer() == 42\n",
            )
    environment = {"VFY_WORKER_COLLECTION": None, "VFY_WORKER_START": "exec"}
    arguments = ("--cov=src", "--cov-report=", "-q")
    stock = projects["stock"].run_pytest(*arguments, env=environment)
    native = projects["native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=3",
        f"--native-scheduling={scheduling}",
        "--no-replay",
        *arguments,
        env=environment,
    )
    for project, result in ((projects["stock"], stock), (projects["native"], native)):
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"{files * cases} passed" in result.stdout
        assert project.lines("src/app.py") == [1, 2]
    candidate = projects["native"].root
    assert {path.name for path in candidate.glob("import-*")} == {
        f"import-{file}-{worker}" for file in range(files) for worker in range(width)
    }
    assert {path.name for path in candidate.glob("finalizer-*")} == {
        f"finalizer-{worker}" for worker in range(width)
    }
