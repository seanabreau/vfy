"""Intentional worker exits preserve pytest status without publishing success."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


@pytest.mark.parametrize("returncode", [None, 0, 1, 7, -1, 256])
@pytest.mark.parametrize("scheduling", ["file", "cases"])
def test_intentional_worker_exit_preserves_status_finalizers_and_evidence(
    tmp_path: Path, returncode: int | None, scheduling: str
) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route, timeout=25)
        project.write("src/app.py", "def read_value():\n    return 1\n")
        preserved: dict[str, bytes] = {}
        environment = {"PYTEST_NATIVE_RESULT": str(project.root / "execution.json")}
        arguments = (
            "--all",
            "--no-replay",
            "--history=history.json",
            "--source=src",
            "--cov-report=",
            "-q",
        )
        if route == "native":
            project.write(
                "test_seed.py",
                "from src.app import read_value\ndef test_seed():\n    assert read_value() == 1\n",
            )
            project.git_init()
            seed = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                *(argument for argument in arguments if argument != "--no-replay"),
                env=environment,
            )
            seed.expect(0)
            names = (
                ".coverage",
                "history.json",
                "execution.json",
                "test-run.json",
                "replay.coverage",
            )
            preserved = {name: (project.root / name).read_bytes() for name in names}
            (project.root / "test_seed.py").unlink()
        project.write(
            "test_family.py",
            "import os\n"
            "import time\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "from src.app import read_value\n"
            "@pytest.fixture(scope='session')\n"
            "def lifecycle():\n"
            "    pid = os.getpid()\n"
            "    Path(f'start-{pid}').touch()\n"
            "    yield\n"
            "    Path(f'finish-{pid}').touch()\n"
            "@pytest.mark.parametrize('value', range(8))\n"
            "def test_family(value, lifecycle):\n"
            "    assert read_value() == 1\n"
            "    worker = os.getenv('VFY_NATIVE_PARALLEL_INDEX')\n"
            f"    if {scheduling == 'cases'} and worker is not None:\n"
            "        if worker != '0':\n"
            "            Path(f'blocked-{os.getpid()}').touch()\n"
            "            while True:\n"
            "                time.sleep(0.01)\n"
            "        deadline = time.monotonic() + 15\n"
            "        while len(list(Path('.').glob('blocked-*'))) != 3:\n"
            "            assert time.monotonic() < deadline, 'sibling workers did not start'\n"
            "            time.sleep(0.01)\n"
            "    Path('requester').write_text(str(os.getpid()))\n"
            f"    pytest.exit('requested exit sentinel', returncode={returncode})\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=4",
                *arguments,
                env={**environment, "VFY_NATIVE_SCHEDULING": scheduling},
            )
        )
        output = result.stdout + result.stderr
        expected_status = 2 if returncode is None else returncode % 256
        assert result.returncode == expected_status, output
        assert "requested exit sentinel" in output
        requester = (project.root / "requester").read_text()
        assert (project.root / f"start-{requester}").exists(), output
        assert (project.root / f"finish-{requester}").exists(), output
        peers = list(project.root.glob("blocked-*"))
        assert len(peers) == (3 if route == "native" and scheduling == "cases" else 0), output
        for path in peers:
            with pytest.raises(ProcessLookupError):
                os.kill(int(path.name.removeprefix("blocked-")), 0)
        assert {name: (project.root / name).read_bytes() for name in preserved} == preserved


def test_worker_exiting_zero_without_exit_record_is_infrastructure_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_crash.py", "import os\ndef test_crash():\n    os._exit(0)\n")
    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=2", "--no-replay", "--no-cov", "-q"
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.INTERNAL_ERROR, output
    assert "before publishing a result" in output


@pytest.mark.parametrize("returncode", [None, 0, 1, 7, -1, 256])
def test_worker_exit_during_compilation_preserves_status_and_message(
    tmp_path: Path, returncode: int | None
) -> None:
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            "import os\n"
            "import time\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "def pytest_collection_modifyitems(config, items):\n"
            "    if os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
            "        Path('peer-collected').write_text(str(os.getpid()))\n"
            "    for item in items:\n"
            "        original = item.getparent\n"
            "        def getparent(cls, original=original):\n"
            "            if (os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '0'\n"
            "                    and getattr(config.pluginmanager,\n"
            "                                '_native_execution_started', False)):\n"
            "                deadline = time.monotonic() + 15\n"
            "                while not Path('peer-collected').exists():\n"
            "                    assert time.monotonic() < deadline, 'peer did not collect'\n"
            "                    time.sleep(0.01)\n"
            "                Path('compile-exit').touch()\n"
            f"                pytest.exit('compile exit sentinel', returncode={returncode})\n"
            "            return original(cls)\n"
            "        item.getparent = getparent\n",
        )
        project.write(
            "test_exit.py",
            "from pathlib import Path\n"
            "import pytest\n"
            "def test_exit():\n"
            "    Path('executed').touch()\n"
            f"    pytest.exit('compile exit sentinel', returncode={returncode})\n",
        )
        project.write(
            "test_peer.py",
            "from pathlib import Path\ndef test_peer():\n    Path('peer-executed').touch()\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"VFY_WORKER_START": "exec"},
            )
        )
        output = result.stdout + result.stderr
        expected_status = 2 if returncode is None else returncode % 256
        assert result.returncode == expected_status, output
        assert "compile exit sentinel" in output
        assert (project.root / "compile-exit").exists() is (route == "native"), output
        assert (project.root / "executed").exists() is (route == "stock"), output
        assert not (project.root / "peer-executed").exists(), output
        if route == "native":
            peer = int((project.root / "peer-collected").read_text())
            with pytest.raises(ProcessLookupError):
                os.kill(peer, 0)


@pytest.mark.parametrize("damage", ["malformed", "status-mismatch"])
def test_invalid_worker_exit_record_preserves_previous_evidence(
    tmp_path: Path, damage: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/app.py", "def read_value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import json\nimport os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    damage = os.getenv('VFY_DAMAGE_EXIT')\n"
        "    if damage and os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '0':\n"
        "        directory = Path(os.environ['VFY_NATIVE_PARALLEL_DIR'])\n"
        "        destination = directory / 'exit.0.json'\n"
        "        record = json.loads(destination.read_text())\n"
        "        assert record['returncode'] == 0\n"
        "        assert not (directory / 'result.0.json').exists()\n"
        "        if damage == 'malformed':\n"
        "            destination.write_text('{invalid worker exit')\n"
        "        else:\n"
        "            record['returncode'] = 7\n"
        "            destination.write_text(json.dumps(record))\n"
        "        Path('damaged-exit').touch()\n",
    )
    project.write(
        "test_exit.py",
        "import os\nimport pytest\nfrom src.app import read_value\n"
        "def test_exit():\n"
        "    assert read_value() == 1\n"
        "    if os.getenv('VFY_DAMAGE_EXIT'):\n"
        "        pytest.exit('requested exit sentinel', returncode=0)\n",
    )
    project.git_init()
    arguments = ("--all", "--history=history.json", "--source=src", "--cov-report=", "-q")
    environment = {"PYTEST_NATIVE_RESULT": str(tmp_path / "execution.json")}
    seed = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", *arguments, env=environment
    )
    seed.expect(0)
    preserved = {
        name: (tmp_path / name).read_bytes()
        for name in (
            ".coverage",
            "history.json",
            "execution.json",
            "test-run.json",
            "replay.coverage",
        )
    }

    rejected = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        *arguments,
        env={**environment, "VFY_DAMAGE_EXIT": damage},
    )
    output = rejected.stdout + rejected.stderr
    assert (tmp_path / "damaged-exit").exists(), output
    assert rejected.returncode == pytest.ExitCode.INTERNAL_ERROR, output
    if damage == "malformed":
        assert "native parallel worker 0 published an invalid exit record" in output
    else:
        assert "exit record inconsistent with its process status or result" in output
    assert "requested exit sentinel" in output
    assert {name: (tmp_path / name).read_bytes() for name in preserved} == preserved
