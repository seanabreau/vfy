"""Public test commands keep ownership of vfy options and pass other arguments to pytest."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_global_color_abbreviation_reaches_the_independent_session(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_color.py",
        "import json\nfrom pathlib import Path\n"
        "def test_color(pytestconfig):\n"
        "    Path('color.json').write_text(json.dumps(pytestconfig.getoption('color')))\n",
    )
    result = project.run_vfy(
        "--col=always", "test", "--engine=native", "--workers=1", "--no-cov", "-q"
    )
    result.expect(0)
    assert json.loads((tmp_path / "color.json").read_text()) == "yes"
    assert "\x1b[" in result.stdout


@pytest.mark.parametrize("selector", ["keyword", "path"])
def test_plugin_option_after_pytest_selector_is_not_a_vfy_worker_abbreviation(
    tmp_path: Path, selector: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_addoption(parser):\n    parser.addoption('--work', type=int, default=0)\n",
    )
    project.write(
        "test_options.py",
        "from pathlib import Path\n"
        "def test_chosen(pytestconfig):\n"
        "    assert pytestconfig.getoption('work') == 2\n"
        "    Path('chosen-ran').touch()\n",
    )
    project.write(
        "test_other.py",
        "def test_other(): raise AssertionError('selector was not forwarded')\n",
    )
    selection = ("-k", "chosen") if selector == "keyword" else ("test_options.py",)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        *selection,
        "--work",
        "2",
        "--no-cov",
    )
    result.expect(0)
    assert (tmp_path / "chosen-ran").is_file()
    assert "1 passed" in result.stdout
    if selector == "keyword":
        assert "1 deselected" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        (
            ("test", "--engine=native", "--no-cov", "-k", "chosen", "--workers", "-1"),
            "argument -j/-n/--workers: workers must be auto, logical, or an integer from 1 to 256",
        ),
        (
            ("test", "--engine=native", "--color", "-1", "--no-cov"),
            "argument --color: invalid choice: '-1'",
        ),
        (
            ("--color", "test", "--engine=native", "--workers", "-1"),
            "argument -j/-n/--workers: workers must be auto, logical, or an integer from 1 to 256",
        ),
        (
            ("test", "--engine=native", "--no-cov", "--workers", "257"),
            "argument -j/-n/--workers: workers must be auto, logical, or an integer from 1 to 256",
        ),
    ],
    ids=["worker-after-selector", "negative-color", "bare-global-color", "worker-above-limit"],
)
def test_invalid_test_options_preserve_argument_diagnostics(
    tmp_path: Path, arguments: tuple[str, ...], diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_chosen(): Path('body-ran').touch()\n",
    )
    result = project.run_vfy(*arguments)
    result.expect(2)
    assert result.stderr.startswith("usage: vfy test ")
    assert f"vfy test: error: {diagnostic}" in result.stderr
    assert not (tmp_path / "body-ran").exists()


def test_negative_exponent_values_follow_the_interpreters_argument_parser(tmp_path: Path) -> None:
    reference = argparse.ArgumentParser(exit_on_error=False)
    reference.add_argument("--source")
    try:
        reference.parse_args(["--source", "-1e3"])
    except argparse.ArgumentError:
        accepted = False
    else:
        accepted = True
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    result = project.run_vfy("test", "--engine=native", "--source", "-1e3", "--no-cov")
    assert result.returncode == (0 if accepted else 2), result.stdout + result.stderr
    assert (tmp_path / "body-ran").is_file() is accepted
    if not accepted:
        assert result.stderr.startswith("usage: vfy test ")
        assert "vfy test: error: argument --source: expected one argument" in result.stderr


def test_unknown_negative_spaced_value_remains_an_option_argument(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    result = project.run_vfy("test", "--engine=native", "--source", "-z spaced", "--no-cov")
    result.expect(0)
    assert (tmp_path / "body-ran").is_file()


@pytest.mark.parametrize("digit", [chr(0x10D40), chr(0x11DE0)], ids=["unicode16", "unicode17"])
def test_negative_option_values_follow_the_interpreters_decimal_character_rules(
    tmp_path: Path, digit: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    decimal = re.fullmatch(r"\d", digit) is not None
    result = project.run_vfy(
        "test", "--engine=native", "--workers=1", "--source", "-" + digit, "--no-cov"
    )
    assert result.returncode == (0 if decimal else 2), result.stdout + result.stderr
    assert (tmp_path / "body-ran").is_file() is decimal
    if not decimal:
        assert "argument --source: expected one argument" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="undecodable filesystem argv requires POSIX")
@pytest.mark.parametrize("prefix", ["", chr(0x10FFFF)], ids=["surrogate", "literal-marker"])
@pytest.mark.parametrize("help_requested", [False, True], ids=["config-error", "help"])
def test_undecodable_argv_preserves_configuration_errors_and_help(
    tmp_path: Path, prefix: str, help_requested: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        *(("--help",) if help_requested else ()),
        prefix + os.fsdecode(b"\xff"),
    )
    assert result.returncode == (0 if help_requested else 1), result.stdout + result.stderr
    if help_requested:
        assert result.stdout.startswith("usage: vfy test ")
        assert not result.stderr
    else:
        assert result.stderr.startswith("vfy: error: lone leading surrogate in hex escape")
    assert "Traceback" not in result.stdout + result.stderr
    assert not (tmp_path / "body-ran").exists()
