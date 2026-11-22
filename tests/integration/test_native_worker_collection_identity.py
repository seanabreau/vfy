"""Keep saved test evidence separate across controller collection policies."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def _metadata(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        return dict(connection.execute("SELECT key, value FROM meta"))


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("src/app.py", "def read_value(value):\n    return value\n")
    project.write(
        "test_cases.py",
        "from pathlib import Path\nimport pytest\nfrom src.app import read_value\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_case(value):\n"
        "    assert read_value(value) == value\n"
        "    path = Path(f'.vfy/executions-{value}')\n"
        "    path.parent.mkdir(exist_ok=True)\n"
        "    with path.open('a') as stream:\n        stream.write('called\\n')\n",
    )
    project.git_init()
    return project


def _executions(project: VfyProject) -> list[int]:
    return [
        len((project.root / f".vfy/executions-{value}").read_text().splitlines())
        for value in range(4)
    ]


@pytest.mark.parametrize(
    "engine", [pytest.param("pytest-native", marks=requires_native_pytest), "pytest"]
)
def test_worker_collection_policy_separates_invocation_and_green_evidence(
    tmp_path: Path, engine: str
) -> None:
    project = _project(tmp_path)
    identities: dict[bool, tuple[str, str]] = {}
    for index, flag in enumerate(("0", None, "1", "0", "0")):
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=2",
            "--source=src",
            "--cov-report=",
            "-q",
            env={"VFY_WORKER_COLLECTION": flag},
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        native_request = engine == "pytest-native" and flag != "0"
        reused = index in {2, 4} if engine == "pytest-native" else index > 0
        assert ("Reused 4 tests from passing baseline" in result.stderr) is reused, output
        expected = (1, 2, 2, 3, 3)[index] if engine == "pytest-native" else 1
        assert _executions(project) == [expected] * 4
        metadata = _metadata(project.root / ".vfy/replay.coverage")
        identity = (
            metadata["vfy_replay_fingerprint"],
            metadata["vfy_green_test_fingerprint"],
        )
        if native_request in identities:
            # Returning to the stable policy restores its original identity exactly.
            assert identity == identities[native_request]
        else:
            identities[native_request] = identity
    if engine == "pytest-native":
        assert identities[False][0] != identities[True][0]
        assert identities[False][1] != identities[True][1]


@requires_native_pytest
def test_worker_collection_policy_separates_portable_ci_evidence(tmp_path: Path) -> None:
    project = _project(tmp_path)
    exported = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--all",
        "--export-baseline=.vfy/baseline.json",
        "--source=src",
        "--cov-report=",
        "-q",
        env={"VFY_WORKER_COLLECTION": "0"},
    )
    exported.expect(0)
    bundle = (tmp_path / ".vfy/baseline.json").read_bytes()
    imported = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--baseline=.vfy/baseline.json",
        "--plan-output=.vfy/plan.json",
        "--source=src",
        "--cov-report=",
        "-q",
        env={"VFY_WORKER_COLLECTION": None},
    )
    imported.expect(0)
    assert json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]["fallback_full"]
    assert _executions(project) == [2] * 4
    assert (tmp_path / ".vfy/baseline.json").read_bytes() == bundle
