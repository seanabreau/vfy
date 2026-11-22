"""Native workers preserve distinct warnings outside test execution."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="native workers require POSIX"
)


@pytest.mark.parametrize("terminal", [True, False])
@pytest.mark.parametrize("observer", [False, True])
def test_parallel_session_warnings_preserve_order_and_multiplicity(
    tmp_path: Path, terminal: bool, observer: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nfilterwarnings = always\n")
    project.write(
        "conftest.py",
        "import os, warnings\n"
        "def emit(phase):\n"
        "    for _ in range(2):\n"
        "        warnings.warn('shared ' + phase + ' sentinel', UserWarning)\n"
        "    worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')\n"
        "    if worker is not None:\n"
        "        warnings.warn('worker ' + worker + ' ' + phase + ' sentinel', UserWarning)\n"
        "def pytest_configure(config):\n"
        "    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:\n"
        "        probe = os.environ.get('VFY_PYTEST_RECORD_PROBE')\n"
        "        if probe is not None:\n"
        "            import sys\n"
        "            from pathlib import Path\n"
        "            sys.path.insert(0, str(Path(probe).parent))\n"
        "            import pytest_execution_probe\n"
        "            pytest_execution_probe.pytest_configure(config)\n"
        "def pytest_collection_modifyitems(config):\n    emit('collection')\n"
        + (
            ""
            if terminal
            else "    reporter = config.pluginmanager.getplugin('terminalreporter')\n"
            "    if reporter is not None:\n"
            "        config.pluginmanager.unregister(reporter)\n"
        )
        + "def pytest_sessionfinish():\n    emit('finish')\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import warnings\ndef test_case():\n    warnings.warn('call sentinel', UserWarning)\n",
        )
    destination = tmp_path / "result.json"
    environment = {"PYTEST_NATIVE_RESULT": str(destination)}
    if observer:
        probe = Path(__file__).resolve().parents[2] / "scripts" / "pytest_execution_probe.py"
        inventory = project.write(
            "inventory.json", json.dumps([f"test_{name}.py::test_case" for name in ("a", "b")])
        )
        environment.update(
            VFY_PYTEST_RECORD_MODE="native",
            VFY_PYTEST_RECORD_SESSION="a" * 64,
            VFY_PYTEST_RECORD_PROBE=str(probe),
            VFY_PYTEST_RECORD_ROOT=str(tmp_path),
            VFY_PYTEST_RECORD_TEMP=str(tmp_path),
            VFY_PYTEST_RECORD_PREFIX=sys.prefix,
            VFY_PYTEST_OBSERVER_RESULT=str(tmp_path / "observer.json"),
            VFY_PYTEST_RECORD_RAW=str(destination),
            VFY_PYTEST_RECORD_INVENTORY_NODEIDS=str(inventory),
            VFY_PYTEST_RECORD_INVENTORY_NODEIDS_SHA256=hashlib.sha256(
                inventory.read_bytes()
            ).hexdigest(),
        )
    result = project.run_vfy(
        "test",
        # The observer variant binds an installed-pytest probe to its native transport.
        *(("--engine=pytest-native",) if observer else ()),
        "--workers=2",
        "--no-replay",
        "--no-cov",
        *((f"--native-result-json={destination}",) if not observer else ()),
        "-q",
        env=environment,
    )
    transcript = result.stdout + result.stderr
    assert result.returncode == 0, transcript
    record = json.loads(destination.read_text())
    if observer:
        assert record["session_token"] == "a" * 64
        observed = json.loads((tmp_path / "observer.json").read_text())
        assert record["observer_nonce"] == observed["observer_nonce"]
    records = record["warnings"]
    messages = [record["message"] for record in records]
    for phase in ("collection", "finish"):
        assert sum(f"UserWarning: shared {phase} sentinel" in message for message in messages) == 2
        for worker in range(2):
            sentinel = f"UserWarning: worker {worker} {phase} sentinel"
            assert sum(sentinel in message for message in messages) == 1
            if terminal:
                assert transcript.count(sentinel) == 1
    assert sum("UserWarning: call sentinel" in message for message in messages) == 2
    groups = [
        "collection"
        if "collection sentinel" in message
        else "finish"
        if "finish sentinel" in message
        else "call"
        for message in messages
    ]
    assert groups == ["collection"] * 4 + ["call"] * 2 + ["finish"] * 4
    if terminal:
        assert "2 passed, 10 warnings" in transcript


@pytest.mark.parametrize("workers", [0, 2])
def test_unregistered_terminal_reporter_skips_its_session_summary(
    tmp_path: Path, workers: int
) -> None:
    observations: list[str] = []
    for engine in ("pytest", "default"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "from pathlib import Path\n"
            "def pytest_collection_modifyitems(config):\n"
            "    reporter = config.pluginmanager.getplugin('terminalreporter')\n"
            "    if reporter is not None:\n"
            "        config.pluginmanager.unregister(reporter)\n"
            "def pytest_sessionfinish():\n"
            "    Path('finished').write_text('finished')\n"
            "def pytest_terminal_summary(config):\n"
            "    assert config.pluginmanager.getplugin('terminalreporter') is not None\n",
        )
        for name in ("a", "b"):
            project.write(f"test_{name}.py", "def test_case():\n    pass\n")
        result = (
            project.run_python("-m", "pytest", "-q")
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--no-replay",
                *((f"--workers={workers}",) if workers else ()),
                "-q",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append((project.root / "finished").read_text())
    assert observations == ["finished", "finished"]
