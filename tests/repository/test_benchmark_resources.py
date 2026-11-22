"""Process-tree evidence must describe the command whose result passes parity."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from benchmarks import benchmark_resources
from benchmarks.benchmark_resources import tree_resource_process
from scripts.process_tree import run_captured


@pytest.mark.skipif(sys.platform != "linux", reason="process-tree PSS requires Linux")
def test_tree_resources_preserve_child_status_streams_and_live_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "descendant-ready"
    release = tmp_path / "sampled.pipe"
    os.mkfifo(release)
    collector = (
        Path(__file__).resolve().parents[2]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    wrapper = tmp_path / "sample-live-descendants.py"
    wrapper.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        "sample_tree = collector._sample_tree\n"
        "released = False\n"
        "def sample_and_release(*args):\n"
        "    global released\n"
        "    reading = sample_tree(*args)\n"
        f"    if reading[2] >= 2 and Path({str(ready)!r}).exists() and not released:\n"
        f"        with open({str(release)!r}, 'wb') as stream:\n"
        "            stream.write(b'x')\n"
        "        released = True\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )

    def sample_live_descendants(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return run_captured(
            [command[0], str(wrapper), *command[2:]], cwd=cwd, env=env, timeout=timeout
        )

    monkeypatch.setattr(benchmark_resources, "run_captured", sample_live_descendants)
    descendant = (
        "import sys\n"
        "from pathlib import Path\n"
        "data = bytearray(8*1024*1024)\n"
        "data[::4096] = b'x' * (len(data)//4096)\n"
        "Path(sys.argv[1]).touch()\n"
        "with open(sys.argv[2], 'rb') as stream:\n"
        "    assert stream.read(1) == b'x'\n"
    )
    command = [
        sys.executable,
        "-c",
        "import subprocess,sys; "
        f"subprocess.run([sys.executable,'-c',{descendant!r},*sys.argv[1:]],check=True); "
        "print('public stdout'); print('public stderr',file=sys.stderr); sys.exit(7)",
        str(ready),
        str(release),
    ]
    path = tmp_path / "resources/child.json"
    elapsed, completed, record = tree_resource_process(
        command, cwd=tmp_path, env=os.environ, record_path=path, timeout=15
    )
    assert (completed.args, completed.returncode, completed.stdout, completed.stderr) == (
        command,
        7,
        "public stdout\n",
        "public stderr\n",
    )
    assert elapsed == record["child_wall_ms"]
    assert record["child_command"] == command
    assert record["child_exit_status"] == 7
    assert cast("int", record["peak_process_count"]) >= 2
    assert (
        cast("int", record["peak_tree_rss_bytes"])
        >= cast("int", record["peak_tree_pss_bytes"])
        > 8 * 1024 * 1024
    )
    assert (
        record["collector_sha256"]
        == hashlib.sha256(
            (
                Path(__file__).resolve().parents[2]
                / "validation/pytest_runner/benchmark_child_resources.py"
            ).read_bytes()
        ).hexdigest()
    )
    assert json.loads(path.read_text()) == record
    assert path.with_suffix(".stdout").read_text() == completed.stdout
    assert path.with_suffix(".stderr").read_text() == completed.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="process-tree PSS requires Linux")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("child_command", ["another", "command"]),
        ("collector_sha256", "0" * 64),
        ("child_exit_status", 1),
        ("peak_tree_pss_bytes", 0),
        ("snapshot_count", 0),
        ("child_wall_ms", float("nan")),
        ("total_cpu_ms", -1),
    ],
)
def test_tree_resources_reject_mismatched_or_incomplete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    path = tmp_path / "child.json"
    collector = (
        Path(__file__).resolve().parents[2]
        / "validation/pytest_runner/benchmark_child_resources.py"
    )
    release = tmp_path / "sampled.pipe"
    os.mkfifo(release)
    wrapper = tmp_path / "sample-before-exit.py"
    wrapper.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        "sample_tree = collector._sample_tree\n"
        "released = False\n"
        "def sample_and_release(*args):\n"
        "    global released\n"
        "    reading = sample_tree(*args)\n"
        "    if reading[2] and not released:\n"
        f"        with open({str(release)!r}, 'wb') as stream:\n"
        "            stream.write(b'x')\n"
        "        released = True\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )

    def corrupt(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = run_captured(
            [command[0], str(wrapper), *command[2:]], cwd=cwd, env=env, timeout=timeout
        )
        assert completed.returncode == 0, completed.stderr
        assert path.is_file(), "collector must produce valid evidence before corruption"
        record = json.loads(path.read_text())
        record[field] = value
        path.write_text(json.dumps(record))
        return completed

    monkeypatch.setattr(benchmark_resources, "run_captured", corrupt)
    with pytest.raises(RuntimeError, match=r"resource|wall time|CPU"):
        tree_resource_process(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "with open(sys.argv[1], 'rb') as stream:\n"
                "    assert stream.read(1) == b'x'\n"
                "print('done')\n",
                str(release),
            ],
            cwd=tmp_path,
            env=os.environ,
            record_path=path,
            timeout=15,
        )


@pytest.mark.skipif(sys.platform != "linux", reason="process-tree PSS requires Linux")
def test_tree_resources_do_not_overwrite_prior_evidence(tmp_path: Path) -> None:
    path = tmp_path / "child.json"
    path.write_text("retained evidence")
    with pytest.raises(FileExistsError, match="resource artifact already exists"):
        tree_resource_process(
            [sys.executable, "-c", "raise AssertionError('must not run')"],
            cwd=tmp_path,
            env=os.environ,
            record_path=path,
        )
    assert path.read_text() == "retained evidence"
