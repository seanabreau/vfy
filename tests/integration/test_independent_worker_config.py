"""Independent workers expose pytest-xdist's worker identity like stock xdist."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """import json
import os
from pathlib import Path


def pytest_configure(config):
    workerinput = getattr(config, "workerinput", None)
    record = {
        "has_workerinput": workerinput is not None,
        "has_workeroutput": hasattr(config, "workeroutput"),
        "environment": [
            os.environ.get("PYTEST_XDIST_WORKER"),
            os.environ.get("PYTEST_XDIST_WORKER_COUNT"),
            bool(os.environ.get("PYTEST_XDIST_TESTRUNUID")),
        ],
    }
    if workerinput is not None:
        record["workerid"] = workerinput["workerid"]
        record["workercount"] = workerinput["workercount"]
        record["testrunuid"] = workerinput["testrunuid"]
        record["testrunuid_environment"] = os.environ.get("PYTEST_XDIST_TESTRUNUID")
    name = record.get("workerid", "controller")
    directory = Path("records")
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(record))
"""

_TESTS = "\n".join(f"def test_{index}():\n    pass\n" for index in range(8))


def _run(project: VfyProject, engine: str) -> dict[str, dict[str, Any]]:
    records = project.root / "records"
    if records.exists():
        for path in records.iterdir():
            path.unlink()
    result = project.run_engine(
        engine, "-n", "2", "-q", "-p", "no:cacheprovider", stock=["-p", "xdist.plugin"]
    )
    result.expect(0)
    return {path.stem: json.loads(path.read_text()) for path in sorted(records.iterdir())}


def test_independent_workers_publish_xdist_worker_attributes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _CONFTEST)
    project.write("test_workers.py", _TESTS)
    stock = _run(project, "stock")
    independent = _run(project, "independent")

    for records in (stock, independent):
        uids = {records[name]["testrunuid"] for name in ("gw0", "gw1")}
        assert len(uids) == 1
        assert re.fullmatch(r"[0-9a-f]{32}", uids.pop())
        for name in ("gw0", "gw1"):
            assert records[name]["testrunuid"] == records[name].pop("testrunuid_environment")
            records[name]["testrunuid"] = "<uid>"
    assert independent == stock
    assert independent["controller"]["has_workerinput"] is False
    assert independent["gw1"]["environment"] == ["gw1", "2", True]
