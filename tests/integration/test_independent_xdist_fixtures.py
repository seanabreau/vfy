"""Independent runs provide pytest-xdist's worker fixtures with or without pytest-xdist."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import venv
from pathlib import Path
from typing import Any

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_TESTS = """import json
from pathlib import Path


def record(request, worker_id, testrun_uid, name):
    workerinput = getattr(request.config, "workerinput", None)
    Path("records").mkdir(exist_ok=True)
    Path("records", name + ".json").write_text(json.dumps({
        "worker_id": worker_id,
        "testrun_uid": testrun_uid,
        "workerid": None if workerinput is None else workerinput["workerid"],
        "mainargv": None if workerinput is None else workerinput["mainargv"],
    }))

""" + "\n".join(
    f"def test_{index}(request, worker_id, testrun_uid):\n"
    f"    record(request, worker_id, testrun_uid, 'test_{index}')\n"
    for index in range(8)
)


def _bare_interpreter(tmp_path: Path) -> Path:
    """Create an environment that has the installed vfy wheel and no pytest-xdist."""
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    spec = importlib.util.find_spec("vfy")
    assert spec is not None
    assert spec.origin is not None
    (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "vfy"
    ).symlink_to(Path(spec.origin).parent, target_is_directory=True)
    return environment / "bin" / "python"


def _run(
    project: VfyProject, engine: str, *arguments: str, autoload: bool = True
) -> list[dict[str, Any]]:
    records = project.root / "records"
    if records.exists():
        for path in records.iterdir():
            path.unlink()
    # Entry-point autoloading is what loads pytest-xdist, or vfy's stand-in when it is absent.
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None} if autoload else None
    result = project.run_engine(engine, *arguments, "-q", "-p", "no:cacheprovider", env=environment)
    result.expect(0)
    return [json.loads(path.read_text()) for path in sorted(records.iterdir())]


def _assert_parallel(records: list[dict[str, Any]]) -> None:
    assert len(records) == 8
    uids = {record["testrun_uid"] for record in records}
    assert len(uids) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", uids.pop())
    for record in records:
        assert record["worker_id"] == record["workerid"]
        assert record["worker_id"] in {"gw0", "gw1"}
        # Like pytest-xdist, mainargv is the command the user ran, with its worker option.
        argv = record["mainargv"]
        assert argv[-3:] == ["-q", "-p", "no:cacheprovider"]
        assert any(argv[index : index + 2] == ["-n", "2"] for index in range(len(argv)))
        assert not any("vfy/independent" in argument for argument in record["mainargv"])


def _assert_serial(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> None:
    for records in (first, second):
        assert {record["worker_id"] for record in records} == {"master"}
        assert {record["workerid"] for record in records} == {None}
        assert len({record["testrun_uid"] for record in records}) == 1
        assert re.fullmatch(r"[0-9a-f]{32}", records[0]["testrun_uid"])
    assert first[0]["testrun_uid"] != second[0]["testrun_uid"]


def test_stock_and_independent_workers_share_one_uid_with_xdist(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_workers.py", _TESTS)
    _assert_parallel(_run(project, "stock", "-n", "2"))
    _assert_parallel(_run(project, "independent", "-n", "2"))
    _assert_serial(_run(project, "stock"), _run(project, "stock"))
    _assert_serial(_run(project, "independent"), _run(project, "independent"))


def test_explicitly_loaded_xdist_provides_the_fixtures_once(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_workers.py", _TESTS)
    for engine in ("stock", "native"):
        _assert_parallel(_run(project, engine, "-n", "2", "-p", "xdist.plugin", autoload=False))


def test_independent_workers_provide_xdist_fixtures_without_xdist(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project", interpreter=_bare_interpreter(tmp_path))
    project.write("test_workers.py", _TESTS)
    probe = project.run_python("-c", "import xdist")
    assert probe.returncode != 0
    _assert_parallel(_run(project, "independent", "-n", "2"))
    _assert_serial(_run(project, "independent"), _run(project, "independent"))
