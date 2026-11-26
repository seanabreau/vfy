"""Report destinations get the file effects of Coverage.py's ``open(path, "w")``."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="POSIX file modes and FIFOs"
)

_UMASK_RUNNER = (
    "import os, subprocess, sys\n"
    "os.umask(int(sys.argv[1], 8))\n"
    "raise SystemExit(subprocess.call(sys.argv[2:]))\n"
)


def _observe(root: Path, name: str) -> dict[str, str]:
    """Describe every path the output created or reached, relative to ``root``."""

    observed = {}
    for path in sorted(root.glob(f"{name}*")):
        if path.is_symlink():
            observed[path.name] = f"link to {os.readlink(path)}"
        else:
            mode = path.stat().st_mode
            kind = "fifo" if stat.S_ISFIFO(mode) else "file"
            observed[path.name] = f"{kind} {stat.S_IMODE(mode):o}"
    return observed


def _prepare(root: Path, name: str, case: str) -> int | None:
    """Create the destination state for ``case``; return a FIFO reader if any."""

    path = root / name
    if case == "existing":
        path.write_text("old\n")
        path.chmod(0o640)
    elif case == "symlink-chain":
        target = root / f"{name}.target"
        target.write_text("old\n")
        target.chmod(0o604)
        (root / f"{name}.link").symlink_to(target.name)
        path.symlink_to(f"{name}.link")
    elif case == "dangling-symlink":
        path.symlink_to(f"{name}.target")
    elif case == "dangling-symlink-without-directory":
        path.symlink_to("missing/target")
    elif case == "fifo":
        os.mkfifo(path)
        # A nonblocking reader lets the writer open the FIFO without a thread.
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    return None


@pytest.mark.parametrize("report", ["json", "xml"])
@pytest.mark.parametrize(
    ("case", "umask"),
    [
        ("new", "022"),
        ("new", "077"),
        ("existing", "022"),
        ("symlink-chain", "022"),
        ("dangling-symlink", "022"),
        ("dangling-symlink-without-directory", "022"),
        ("fifo", "022"),
    ],
)
def test_report_output_matches_coverage_file_effects(
    tmp_path: Path, report: str, case: str, umask: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("module.py", "VALUE = 1\n")
    project.run_coverage_py("run", "module.py").expect(0)
    observed = {}
    for tool in ("coverage", "vfy"):
        name = f"{tool}-report"
        reader = _prepare(tmp_path, name, case)
        command = [project.interpreter, "-m", tool]
        if tool == "vfy":
            command.append("coverage")
        result = project.run_python(
            "-c", _UMASK_RUNNER, umask, *command, report, "-o", name, "--data-file=.coverage"
        )
        content = b""
        if reader is not None:
            try:
                content = os.read(reader, 1 << 20)
            finally:
                os.close(reader)
        files = {
            key.replace(name, "OUTPUT"): value.replace(name, "OUTPUT")
            for key, value in _observe(tmp_path, name).items()
        }
        observed[tool] = (result.returncode == 0, files, bool(content))

    assert observed["vfy"] == observed["coverage"]
    succeeded, files, streamed = observed["vfy"]
    assert succeeded is (case != "dangling-symlink-without-directory")
    assert streamed is (case == "fifo")
    if case == "symlink-chain":
        assert files["OUTPUT"] == "link to OUTPUT.link"
        assert (tmp_path / "vfy-report.target").read_text() != "old\n"


@pytest.mark.parametrize(("existing", "umask"), [(False, "022"), (False, "077"), (True, "022")])
def test_data_file_mode_matches_coverage_run(tmp_path: Path, existing: bool, umask: str) -> None:
    modes = {}
    for tool in ("coverage", "vfy"):
        project = VfyProject(tmp_path / tool)
        project.write("module.py", "VALUE = 1\n")
        data = project.root / ".coverage"
        if existing:
            data.write_bytes(b"")
            data.chmod(0o640)
        command = [project.interpreter, "-m", tool]
        if tool == "vfy":
            command.append("coverage")
        project.run_python("-c", _UMASK_RUNNER, umask, *command, "run", "module.py").expect(0)
        modes[tool] = stat.S_IMODE(data.stat().st_mode)

    assert modes["vfy"] == modes["coverage"] == 0o644 & ~int(umask, 8)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs descriptor paths")
def test_report_reaches_an_open_regular_file_descriptor(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("module.py", "VALUE = 1\n")
    project.run_coverage_py("run", "module.py").expect(0)
    outputs = {}
    for tool, args in (("coverage", ()), ("vfy", ("coverage",))):
        output_directory = tmp_path / tool
        output_directory.mkdir()
        result = project.run_python(
            "-c",
            """import json
import os
import subprocess
import sys
from pathlib import Path

output = Path(sys.argv[1]) / "report"
with output.open("w+b") as handle:
    handle.write(b"old" * 10_000)
    handle.flush()
    fd = handle.fileno()
    target = output.with_name("report-link")
    target.symlink_to(f"/dev/fd/{fd}")
    subprocess.run(
        [sys.executable, "-m", *sys.argv[2:], "json", "-q", "-o", str(target)],
        check=True, pass_fds=(fd,),
    )
    handle.seek(0)
    report = json.load(handle)
    assert output.stat().st_ino == os.fstat(fd).st_ino
    assert target.is_symlink()
    print(json.dumps(report["files"]))
""",
            str(output_directory),
            tool,
            *args,
        )
        result.expect(0)
        outputs[tool] = json.loads(result.stdout)
    assert outputs["vfy"] == outputs["coverage"]
