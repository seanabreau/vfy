"""Header hooks retain their startup position, results, and verbosity policy."""

from __future__ import annotations

import platform
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

VFY_VERSION = version("vfy")

_ARGUMENTS = [(), ("-q",), ("-qq",), ("-qv",), ("-vvq",), ("--no-header",), ("-p", "no:terminal")]


def _write_project(project: VfyProject) -> None:
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """from pathlib import Path
import os
import pytest

START = Path.cwd()

def event(value):
    with (START / 'events').open('a') as output:
        output.write(value + '\\n')

@pytest.hookimpl(wrapper=True)
def pytest_sessionstart(session):
    event('start before')
    os.chdir(START.parent)
    try:
        result = yield
    finally:
        os.chdir(START)
    event('start after')
    return result

@pytest.hookimpl(specname='pytest_sessionstart')
def pytest_sessionstart_normal(session):
    event('normal ' + str(getattr(session.config.option, 'verbose', 0)))

@pytest.hookimpl(tryfirst=True)
def pytest_report_header(config, start_path):
    event('header ' + str(config.option.verbose) + ' ' + str(start_path == START))
    return ['CUSTOM_first_a', 'CUSTOM_first_b']

@pytest.hookimpl(trylast=True, specname='pytest_report_header')
def pytest_report_header_last():
    return 'CUSTOM_last'

@pytest.hookimpl(wrapper=True, specname='pytest_report_header')
def pytest_report_header_wrapper():
    result = yield
    result.append(['CUSTOM_wrapper'])
    return result
""",
    )


def _observe(
    project: VfyProject, arguments: tuple[str, ...], *, candidate: bool
) -> tuple[str, list[str]]:
    command = (
        project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
        if candidate
        else project.run_python("-m", "pytest", *arguments)
    )
    assert command.returncode == 0, command.stdout + command.stderr
    if candidate:
        verbose = sum(
            argument.count("v") - argument.count("q")
            for argument in arguments
            if argument.startswith("-") and not argument.startswith("--")
        )
        active = verbose >= 0 and "--no-header" not in arguments and "no:terminal" not in arguments
        identity = (
            f"platform {sys.platform} -- Python {platform.python_version()}, vfy-{VFY_VERSION}"
        )
        if verbose > 0:
            identity += f" -- {project.interpreter}"
        platform_lines = [
            line for line in command.stdout.splitlines() if line.startswith("platform ")
        ]
        assert platform_lines == ([identity] if active else [])
    events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    lines = [line for line in command.stdout.splitlines() if line.startswith("CUSTOM_")]
    return events, lines


@pytest.mark.parametrize("arguments", _ARGUMENTS)
def test_report_header_preserves_results_and_startup_policy(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    _write_project(project)
    expected = _observe(project, arguments, candidate=False)
    assert _observe(project, arguments, candidate=True) == expected
    verbose = sum(
        argument.count("v") - argument.count("q")
        for argument in arguments
        if argument.startswith("-") and not argument.startswith("--")
    )
    assert f"normal {verbose}\n" in expected[0]
    active = verbose >= 0 and "--no-header" not in arguments and "no:terminal" not in arguments
    assert bool(expected[1]) is active
    if active:
        assert expected[1] == ["CUSTOM_wrapper", "CUSTOM_last", "CUSTOM_first_a", "CUSTOM_first_b"]
        assert f"header {verbose} True\n" in expected[0]
        assert (
            expected[0].index("normal")
            < expected[0].index("header")
            < expected[0].index("start after")
        )
