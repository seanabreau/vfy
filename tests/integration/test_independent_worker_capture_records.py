"""Successful worker capture remains available to execution-record consumers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native test workers require POSIX")
@pytest.mark.parametrize("capture_mode", ["fd", "sys"])
@pytest.mark.parametrize("workers", [1, 2])
def test_execution_record_retains_passing_worker_output(
    tmp_path: Path, workers: int, capture_mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import sys\nimport pytest\n\n"
        "@pytest.fixture\n"
        "def phases(request):\n"
        "    name = request.node.name\n"
        "    print(name + '-setup-stdout')\n"
        "    print(name + '-setup-stderr', file=sys.stderr)\n"
        "    yield\n"
        "    print(name + '-teardown-stdout')\n"
        "    print(name + '-teardown-stderr', file=sys.stderr)\n",
    )
    for name in ("one", "two"):
        project.write(
            f"tests/test_{name}.py",
            "import sys\n\n"
            f"def test_{name}(phases):\n"
            f"    print('test_{name}-call-stdout')\n"
            f"    print('test_{name}-call-stderr', file=sys.stderr)\n",
        )

    stock = project.run_pytest("-q", "-rP", f"--capture={capture_mode}", load_xdist=False)
    record = project.root / "native.json"
    native = project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        f"--capture={capture_mode}",
        "--no-cov",
        f"--native-result-json={record}",
        "-q",
    )

    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    cases = json.loads(record.read_text(encoding="utf-8"))["cases"]
    assert [case["nodeid"] for case in cases] == [
        "tests/test_one.py::test_one",
        "tests/test_two.py::test_two",
    ]
    for case in cases:
        name = case["nodeid"].split("::")[-1]
        assert [phase["phase"] for phase in case["phases"]] == ["setup", "call", "teardown"]
        for phase in case["phases"]:
            assert phase["outcome"] == "passed"
            for stream in ("stdout", "stderr"):
                expected = f"{name}-{phase['phase']}-{stream}\n"
                assert expected in stock.stdout
                assert phase[stream] == expected
