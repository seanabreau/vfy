"""Upstream xdist XML serialization with native atomic filesystem contracts."""

from __future__ import annotations

import errno
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from stat import S_IMODE
from textwrap import dedent
from typing import Literal

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest
type Route = Literal["upstream", "native"]


def _run(project: VfyProject, route: Route, workers: str, destination: Path) -> CommandResult:
    launcher = project.write(
        "launch.py",
        "import os, sys\n"
        "os.umask(0o022)\n"
        "os.execv(sys.executable, [sys.executable, '-m', *sys.argv[1:]])\n",
    )
    command = (
        ("vfy", "test", "--engine=pytest-native", "--no-replay", "--no-cov")
        if route == "native"
        else ("pytest", "-p", "xdist.plugin")
    )
    return project.run_python(
        str(launcher), *command, "--numprocesses", workers, f"--junitxml={destination}"
    )


def _xml(path: Path, project_root: Path) -> str:
    root = ET.parse(path).getroot()
    for element in root.iter():
        element.attrib.pop("time", None)
        element.attrib.pop("timestamp", None)
    for suite in root.findall("testsuite"):
        # Distributed report arrival order is outside the XML value contract.
        suite[:] = sorted(suite, key=lambda element: (element.tag, element.get("name", "")))
    return ET.tostring(root, encoding="unicode").replace(str(project_root), "PROJECT")


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("mode", [0o640, 0o600])
def test_xdist_junit_preserves_existing_permissions_and_upstream_xml(
    tmp_path: Path, workers: str, mode: int
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_report.py",
            "import pytest\n"
            "def test_property(record_property):\n"
            "    record_property('text', 'line1\\nline2\\ttab\\rend')\n"
            "@pytest.mark.skip(reason='skipped-report-sentinel')\ndef test_skip(): pass\n",
        )
        project.write("pytest.ini", "[pytest]\njunit_family = xunit1\n")
        output = project.root / "report.xml"
        created = _run(project, route, workers, output)
        created.expect(0)
        assert S_IMODE(output.stat().st_mode) == 0o644
        output.chmod(mode)
        replaced = _run(project, route, workers, output)
        replaced.expect(0)
        assert S_IMODE(output.stat().st_mode) == mode
        assert replaced.stdout.count(f"generated xml file: {output}") == 1, replaced.stdout
        observations.append(_xml(output, project.root))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize(
    "kind",
    ["existing", "chain", "dangling", "missing-parent", "parent-link", "directory", "loop"],
)
def test_xdist_junit_follows_symlink_destinations_without_replacing_links(
    tmp_path: Path, workers: str, kind: str
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_report.py", "def test_pass(): pass\n")
        output = project.root / "report.xml"
        target = project.root / "target.xml"
        chain = project.root / "chain.xml"
        if kind == "parent-link":
            directory = project.root / "actual"
            directory.mkdir()
            link = project.root / "reports"
            link.symlink_to(directory.name, target_is_directory=True)
            output = link / output.name
            target = directory / output.name
        elif kind == "loop":
            chain.symlink_to(output.name)
            output.symlink_to(chain.name)
        elif kind == "directory":
            target.mkdir()
            output.symlink_to(target.name, target_is_directory=True)
        elif kind == "missing-parent":
            target = project.root / "absent" / target.name
            output.symlink_to(Path("absent") / target.name)
        else:
            if kind != "dangling":
                target.write_text("previous-report\n")
                target.chmod(0o640)
            if kind == "chain":
                chain.symlink_to(target.name)
                output.symlink_to(chain.name)
            else:
                output.symlink_to(target.name)
        result = _run(project, route, workers, output)
        successful = kind in {"existing", "chain", "dangling", "parent-link"}
        assert (result.returncode == 0) is successful, result.stdout + result.stderr
        if kind == "parent-link":
            assert output.parent.is_symlink()
        else:
            assert output.is_symlink()
        if kind == "chain":
            assert chain.is_symlink()
        if kind == "missing-parent":
            assert not target.parent.exists()
        observations.append(
            (
                os.readlink(output) if output.is_symlink() else None,
                S_IMODE(target.stat().st_mode) if target.is_file() else None,
                _xml(target, project.root) if successful else None,
            )
        )
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_xdist_junit_blocked_parent_preserves_the_existing_file(
    tmp_path: Path, workers: str
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_report.py",
            "from pathlib import Path\ndef test_pass(): Path('executed').touch()\n",
        )
        blocker = project.write("blocked", "preserved-parent\n")
        result = _run(project, route, workers, blocker / "report.xml")
        assert result.returncode != 0, result.stdout + result.stderr
        assert "File exists" in result.stdout + result.stderr
        assert (project.root / "executed").exists()
        assert blocker.read_text() == "preserved-parent\n"


@pytest.mark.parametrize("workers", ["0", "2"])
def test_xdist_junit_readonly_destination_preserves_the_previous_report(
    tmp_path: Path, workers: str
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_report.py", "def test_pass(): pass\n")
        output = project.write("report.xml", "previous-report\n")
        output.chmod(0o400)
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_APPEND)
        except PermissionError:
            pass
        else:
            os.close(descriptor)
            pytest.skip("the current user can write a mode-0400 report")
        result = _run(project, route, workers, output)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "Permission denied" in result.stdout + result.stderr
        assert "generated xml file" not in result.stdout
        assert output.read_text() == "previous-report\n"
        assert S_IMODE(output.stat().st_mode) == 0o400


@pytest.mark.parametrize("workers", ["0", "2"])
def test_native_xdist_junit_serialization_failure_preserves_old_xml_and_restores_open(
    tmp_path: Path, workers: str
) -> None:
    previous = '<?xml version="1.0"?><testsuites name="previous-report" />'
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            dedent("""\
                import builtins
                from pathlib import Path
                from _pytest import junitxml
                def pytest_configure(config):
                    if hasattr(config, 'workerinput'): return
                    junitxml.open = builtins.open
                    def interrupted_xml(*args, **kwargs):
                        raise OSError('serialization-sentinel')
                    junitxml.ET.tostring = interrupted_xml
                def pytest_unconfigure(config):
                    if not hasattr(config, 'workerinput'):
                        Path('restored').write_text(str(junitxml.open is builtins.open))
                """),
        )
        project.write(
            "test_report.py",
            "from pathlib import Path\ndef test_pass(): Path('executed').touch()\n",
        )
        output = project.write("report.xml", previous)
        output.chmod(0o640)
        result = _run(project, route, workers, output)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "serialization-sentinel" in result.stdout + result.stderr
        assert (project.root / "executed").exists()
        assert (project.root / "restored").read_text() == "True"
        if route == "native":
            assert output.read_text() == previous
            assert ET.parse(output).getroot().attrib == {"name": "previous-report"}
        else:
            assert output.read_text() == '<?xml version="1.0" encoding="utf-8"?>'
        assert S_IMODE(output.stat().st_mode) == 0o640


def test_xdist_junit_write_failure_raises_the_upstream_os_error(tmp_path: Path) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_report.py", "def test_passes():\n    pass\n")
        # pytest accepts the name; opening it fails with ENAMETOOLONG for every user.
        destination = project.root / f"{'r' * 300}.xml"
        result = _run(project, route, "2", destination)
        output = result.stdout + result.stderr
        observations.append(
            (
                result.returncode != 0,
                f"OSError: [Errno {errno.ENAMETOOLONG}]" in output,
                "RuntimeError" in output,
            )
        )
    assert observations[0] == (True, True, False), observations
    assert observations[1] == observations[0], observations
