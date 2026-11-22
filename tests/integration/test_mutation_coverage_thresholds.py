"""Internal mutation coverage preserves report configuration and explicit gates."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def _project(tmp_path: Path, addopts: str = "", fail_under: int = 100) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        "[tool.pytest.ini_options]\n"
        f"addopts = {addopts!r}\n"
        "[tool.coverage.report]\n"
        f"fail_under = {fail_under}\n",
    )
    project.write(
        "app.py",
        "def value():\n    return 1\n\ndef untested():\n    return 3\n",
    )
    project.write(
        "test_app.py",
        "import os\nfrom app import value\n\ndef test_value():\n"
        "    os.environ.pop('VFY_MUTATION_BASELINE_COVERAGE', None)\n"
        "    assert value() == 1\n",
    )
    project.write(
        "conftest.py",
        "import json\nfrom pathlib import Path\nfrom vfy import Coverage\n\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    options = {'parsed': config.option.cov_fail_under,\n"
        "               'disabled': config.option.no_cov}\n"
        "    Path('pytest-options.json').write_text(json.dumps(options))\n"
        "    coverage = Coverage.current()\n"
        "    if coverage is not None:\n"
        "        Path('baseline-options.json').write_text(json.dumps({\n"
        "            'configured': coverage.get_option('report:fail_under'),\n"
        "            **options,\n"
        "        }))\n",
    )
    return project


_MUTATE = (
    "mutate",
    "--all",
    "--source=app.py",
    "--operator=integer_plus_one",
    "--max-mutations=1",
    "--no-cache",
    "--format=json",
)


@pytest.mark.parametrize("threshold", [None, 0], ids=["implicit", "explicit-zero"])
@pytest.mark.parametrize(
    "route",
    [
        pytest.param(
            "independent",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("auto", marks=requires_native_pytest),
        pytest.param("portable", marks=requires_native_pytest),
    ],
)
def test_internal_mutation_collection_does_not_activate_configured_report_gate(
    tmp_path: Path, route: str, threshold: int | None
) -> None:
    options = "" if threshold is None else f"--cov-fail-under={threshold}"
    project = _project(tmp_path, options)
    stock = project.run_pytest("-q", "-p", "pytest_cov", load_xdist=False)
    stock.expect(0)
    stock_options = json.loads((tmp_path / "pytest-options.json").read_text())

    engine = "native" if route == "independent" else "pytest-native"
    result = project.run_vfy(
        *_MUTATE,
        f"--engine={engine}",
        env={"VFY_MUTATION_ENGINE": "auto" if route == "independent" else route},
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}, report
    assert report["mutations"][0]["killer"] == "test_app.py::test_value", report
    assert json.loads((tmp_path / "baseline-options.json").read_text()) == {
        "configured": 100,
        **stock_options,
    }
    assert json.loads((tmp_path / "pytest-options.json").read_text()) == stock_options


@pytest.mark.parametrize("location", ["configured", "cli", "environment"])
@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
def test_explicit_mutation_baseline_coverage_keeps_configured_report_gate(
    tmp_path: Path, location: str, engine: str
) -> None:
    project = _project(tmp_path, "--cov=app" if location == "configured" else "")
    arguments = ["--cov=app"] if location == "cli" else []
    environment = {"PYTEST_ADDOPTS": "--cov=app"} if location == "environment" else {}
    stock = project.run_pytest(
        "-q", "-p", "pytest_cov", *arguments, env=environment, load_xdist=False
    )
    stock.expect(1)
    assert "Coverage failure:" in stock.stdout
    assert "fail-under=100" in stock.stdout
    stock_options = json.loads((tmp_path / "pytest-options.json").read_text())
    result = project.run_vfy(*_MUTATE, f"--engine={engine}", *arguments, env=environment)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "baseline" in result.stderr
    assert json.loads((tmp_path / "baseline-options.json").read_text()) == {
        "configured": 100,
        **stock_options,
    }


@pytest.mark.parametrize(("primary", "selected"), [(100, 0), (0, 100)])
@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
        pytest.param("pytest-native", marks=requires_native_pytest),
    ],
)
def test_mutation_baseline_uses_selected_coverage_configuration(
    tmp_path: Path, engine: str, primary: int, selected: int
) -> None:
    project = _project(tmp_path, "--cov=app --cov-config=alternate.ini", primary)
    project.write("alternate.ini", f"[report]\nfail_under = {selected}\n")
    stock = project.run_pytest("-q", "-p", "pytest_cov", load_xdist=False)
    assert stock.returncode == int(selected > 0), stock.stdout + stock.stderr
    stock_options = json.loads((tmp_path / "pytest-options.json").read_text())

    result = project.run_vfy(*_MUTATE, f"--engine={engine}")
    if stock.returncode == 0:
        result.expect(0)
        report = json.loads(result.stdout)
        assert report["summary"] == {"protected": 1}, report
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "baseline" in result.stderr
    assert json.loads((tmp_path / "baseline-options.json").read_text()) == {
        "configured": selected,
        **stock_options,
    }
