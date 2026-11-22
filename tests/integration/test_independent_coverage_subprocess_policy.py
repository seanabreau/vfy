"""pytest-cov child collection policy is separate from owned worker startup."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROTOCOL = """import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent

def run(stage):
    depth = os.environ.get('STARTUP_DEPTH', '0')
    if depth != '1':
        return
    environment = os.environ.copy()
    environment['STARTUP_DEPTH'] = '2'
    environment['STARTUP_ROLE'] = stage
    subprocess.run([sys.executable, '-m', 'subject.child'], cwd=ROOT / 'elsewhere',
                   env=environment, check=True, capture_output=True)
"""

_SITE = """import site
import startup_protocol
site.ENABLE_USER_SITE = True
startup_protocol.run('site')
"""

_CHILD = """import json
import os
from pathlib import Path
from subject import product

role = os.environ.get('STARTUP_ROLE', 'direct')
value = product.child(role)
with (Path(__file__).parents[1] / 'children.jsonl').open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'ppid': os.getppid(),
                             'role': role, 'cwd': os.getcwd(),
                            'start': os.environ.get('VFY_PROCESS_START')}) + '\\n')
print(value)
"""

_PRODUCT = """def parent(value):
    return value + 1

def child(role):
    if role == 'direct':
        return 'direct'
    if role == 'pth':
        return 'pth'
    if role == 'site':
        return 'site'
    return 'user'
"""

_TEST = """import json
import os
from pathlib import Path
import subprocess
import sys
from subject import product

ROOT = Path(__file__).parent

def test_value(cov):
    assert cov is not None
    assert product.parent(1) == 2
    environment = os.environ.copy()
    environment['STARTUP_DEPTH'] = '1'
    environment['STARTUP_ROLE'] = 'direct'
    result = subprocess.run([sys.executable, '-m', 'subject.child'],
                            cwd=ROOT / 'elsewhere', env=environment,
                            text=True, capture_output=True, check=True)
    assert result.stdout == 'direct\\n'
    with (ROOT / 'collectors.jsonl').open('a') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'collector': id(cov)}) + '\\n')
"""


def _project(root: Path, patch: bool) -> VfyProject:
    environment = root.parent / f"{root.name}-environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    site_packages = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    # The fixture owns these files; the active installation is read only.
    (site_packages / "a0-installation.pth").write_text(
        f"import site; site.addsitedir({sysconfig.get_path('purelib')!r})\n"
    )
    (site_packages / "b0-application.pth").write_text(
        "import startup_protocol; startup_protocol.run('pth')\n"
    )
    project = VfyProject(root, interpreter=environment / "bin/python")
    project.write("startup_protocol.py", _PROTOCOL)
    project.write("sitecustomize.py", _SITE)
    project.write("usercustomize.py", "import startup_protocol\nstartup_protocol.run('user')\n")
    project.write("subject/__init__.py", "")
    project.write("subject/product.py", _PRODUCT)
    project.write("subject/child.py", _CHILD)
    project.write(
        "subject/startup.py",
        "def pth():\n    return 'pth'\n\n"
        "def site():\n    return 'site'\n\n"
        "def user():\n    return 'user'\n",
    )
    project.write("test_a.py", _TEST)
    project.write("test_b.py", _TEST)
    (root / "elsewhere").mkdir()
    # Coverage.py's default sysmon core cannot preserve dynamic contexts on 3.14+.
    project.write(
        "coverage.ini",
        "[run]\ncore = ctrace\ncontext = baseline\n" + ("patch = subprocess\n" if patch else ""),
    )
    return project


def _environment(project: VfyProject, route: str) -> dict[str, str | None]:
    return {
        "COVERAGE_FILE": str(project.root / f".coverage.{route}"),
        "PYTHONPATH": str(project.root),
        "VFY_PROCESS_START": None,
        "VFY_PROCESS_OPTIONS": None,
    }


def _run(project: VfyProject, route: str, workers: int) -> CommandResult:
    arguments = [
        "--cov=subject",
        f"--cov-config={project.root / 'coverage.ini'}",
        "--cov-context=test",
        "--cov-branch",
        "--cov-report=",
        "-q",
    ]
    environment = _environment(project, route)
    if route == "oracle":
        return project.run_python(
            "-m", "pytest", "-p", "pytest_cov.plugin", *arguments, env=environment
        )
    return project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--native-result-json=result.json",
        *arguments,
        env=environment,
    )


def _snapshot(project: VfyProject, route: str) -> dict[str, object]:
    data = project.data(f".coverage.{route}")
    try:
        return {
            str(Path(filename).relative_to(project.root)): {
                "lines": sorted(data.lines(filename) or []),
                "arcs": sorted(data.arcs(filename) or []),
                "contexts": {
                    line: sorted(contexts)
                    for line, contexts in data.contexts_by_lineno(filename).items()
                },
                "tracer": data.file_tracer(filename),
            }
            for filename in sorted(data.measured_files())
        }
    finally:
        data.close(force=True)


@pytest.mark.parametrize("patch", [False, True])
@pytest.mark.parametrize("workers", [1, 2])
def test_only_configured_subprocess_patch_measures_ordinary_children(
    tmp_path: Path, patch: bool, workers: int
) -> None:
    project = _project(tmp_path / "project", patch)
    observations = []
    for route in ("oracle", "candidate"):
        result = _run(project, route, workers)
        result.expect(0)
        children = [
            json.loads(line) for line in (project.root / "children.jsonl").read_text().splitlines()
        ]
        assert {row["role"] for row in children} == {"direct", "pth", "site", "user"}
        assert all(Path(row["cwd"]) == project.root / "elsewhere" for row in children)
        assert len({row["pid"] for row in children}) == len(children)
        observations.append(_snapshot(project, route))
        (project.root / "children.jsonl").unlink()
    assert observations[0] == observations[1]
    data = project.data(".coverage.candidate")
    try:
        assert set(data.contexts_by_lineno(str(project.root / "subject/product.py"))[2]) == {
            "baseline|test_a.py::test_value|run",
            "baseline|test_b.py::test_value|run",
        }
        child = str(project.root / "subject/child.py")
        assert bool(data.lines(child)) is patch
        if patch:
            assert set(data.contexts_by_lineno(child)[1]) == {"baseline"}
    finally:
        data.close(force=True)
    candidate = json.loads((project.root / "result.json").read_text())
    assert candidate["engine"] == "vfy-independent-v1"
    topology = candidate["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == workers
    assert len(set(topology["worker_pids"])) == workers
