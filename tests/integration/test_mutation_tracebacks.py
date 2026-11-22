"""Mutation diagnostics honor pytest configuration and reporting callbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_native_pytest
@pytest.mark.parametrize(
    ("setting", "style"),
    [
        ("default", "native"),
        ("cli", "short"),
        ("ini", "long"),
        ("environment", "short"),
        ("argument-file", "auto"),
        ("locals", "auto"),
        ("grouped-locals", "auto"),
        ("full-trace", "auto"),
        ("xfail-trace", "auto"),
    ],
)
def test_mutations_keep_reporting_hooks_and_explicit_traceback_styles(
    tmp_path: Path, setting: str, style: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "import app\n\n"
        "original = pytest.Function.repr_failure\n"
        "def render(self, error):\n"
        "    Path('rendered-style').write_text(self.config.option.tbstyle)\n"
        "    return original(self, error)\n"
        "pytest.Function.repr_failure = render\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def verify_reporting_before_teardown(pytestconfig):\n"
        "    if app.value() == 1:\n"
        "        Path('baseline-style').write_text(pytestconfig.option.tbstyle)\n"
        "    yield\n"
        "    if app.value() != 1:\n"
        "        Path('teardown-style').write_text(Path('rendered-style').read_text())\n",
    )
    project.write(
        "tests/test_app.py", "from app import value\ndef test_value():\n    assert value() == 1\n"
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    arguments: tuple[str, ...] = ()
    if setting == "cli":
        arguments = ("--tb=short",)
    elif setting == "ini":
        project.write("pytest.ini", "[pytest]\naddopts = --tb=long\n")
    elif setting == "environment":
        environment["PYTEST_ADDOPTS"] = "--tb=short"
    elif setting == "argument-file":
        project.write("pytest-args.txt", "--tb=auto\n")
        arguments = ("@pytest-args.txt",)
    elif setting == "locals":
        arguments = ("--showlocals",)
    elif setting == "grouped-locals":
        arguments = ("-ql",)
    elif setting == "full-trace":
        arguments = ("--full-trace",)
    elif setting == "xfail-trace":
        arguments = ("--xfail-tb",)
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--no-cache",
        "--format=json",
        "-q",
        *arguments,
        "tests",
        env=environment,
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["phase"] == "call"
    assert (tmp_path / "teardown-style").read_text() == style
    assert (tmp_path / "baseline-style").read_text() == style
