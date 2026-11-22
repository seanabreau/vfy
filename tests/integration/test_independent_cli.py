"""The default independent route owns its child without entering pytest."""

from __future__ import annotations

import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
@pytest.mark.parametrize("collection_error", [False, True])
def test_collect_only_preserves_passing_history_coverage_and_reports(
    tmp_path: Path, collection_error: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("product.py", "def value():\n    return 7\n")
    project.write(
        "test_product.py", "from product import value\ndef test_value():\n    assert value() == 7\n"
    )
    baseline = project.run_vfy(
        "test", "--engine=native", "--cov=product", "--cov-report=json:coverage.json", "-q"
    )
    baseline.expect(0)
    assert (
        "test_product.py::test_value"
        in read_history(tmp_path / ".vfy/test-history.sqlite3")["tests"]
    )
    artifacts = [
        tmp_path / name
        for name in (
            ".vfy/test-history.sqlite3",
            ".coverage",
            "coverage.json",
            ".vfy/replay.coverage",
            ".vfy/test-run.json",
        )
    ]
    before = {path.name: (path.read_bytes(), path.stat().st_ino) for path in artifacts}
    if collection_error:
        project.write("test_broken.py", "def test_broken(\n")
    project.write("collect.txt", "--collect-only\n")
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", env={"PYTEST_ADDOPTS": "@collect.txt"}
    )
    assert result.returncode == (2 if collection_error else 0), result.stdout + result.stderr
    after = {path.name: (path.read_bytes(), path.stat().st_ino) for path in artifacts}
    assert after == before


@requires_native_pytest
def test_independent_cli_publishes_native_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_example.py", "def test_example():\n    assert 2 + 2 == 4\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

reports = []

def pytest_collectreport(report):
    reports.append([report.nodeid, report.outcome])

def pytest_sessionfinish():
    Path('collection.json').write_text(json.dumps(reports))
""",
    )

    stock = project.run_pytest(load_xdist=False)
    stock.expect(0)
    expected_reports = json.loads((project.root / "collection.json").read_text())
    assert ["test_example.py", "passed"] in expected_reports
    (project.root / "collection.json").rename(project.root / "stock-collection.json")

    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--native-result-json=results.json"
    )

    result.expect(0)
    record = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert record["engine"] == "vfy-independent-v1"
    assert [
        [report["nodeid"], report["outcome"]] for report in record["collection_reports"]
    ] == expected_reports
    assert json.loads((project.root / "collection.json").read_text()) == expected_reports
    assert record["deselected_nodeids"] == []
    assert [case["nodeid"] for case in record["cases"]] == ["test_example.py::test_example"]


@pytest.mark.parametrize("option", ["--plan-only"])
def test_independent_cli_plan_only_collects_without_executing(tmp_path: Path, option: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_example.py",
        "from pathlib import Path\nPath('imported').touch()\n"
        "def test_example():\n    Path('executed').touch()\n",
    )

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "--no-cov",
        option,
        "--plan-output=plan.json",
    )

    result.expect(0)
    assert (tmp_path / "imported").is_file()
    assert not (tmp_path / "executed").exists()
    plan = json.loads((tmp_path / "plan.json").read_text())
    assert plan["collected"] == plan["selection"]["selected"] == ["test_example.py::test_example"]


@requires_native_pytest
def test_independent_cli_keeps_the_callers_pytest_module(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_example.py",
        "from pathlib import Path\nimport pytest\n"
        "def test_example():\n    Path('facade-origin').write_text(pytest.__file__)\n",
    )

    result = project.run_python(
        "-c",
        "import sys, pytest\nfrom vfy.cli import main\n"
        "original = sys.modules['pytest']\n"
        "status = main(['test', '--engine=native', '--no-cov'])\n"
        "assert sys.modules['pytest'] is original\nraise SystemExit(status)\n",
    )

    result.expect(0)
    origin = (tmp_path / "facade-origin").read_text(encoding="utf-8")
    assert Path(origin).is_file()
    assert (Path(origin).name, Path(origin).parent.name) == ("__init__.py", "pytest")


@requires_native_pytest
@pytest.mark.parametrize("block_upstream", [False, True], ids=["installed-pytest", "no-upstream"])
def test_default_engine_preserves_pytest_outcomes_and_coverage_without_upstream_imports(
    tmp_path: Path, block_upstream: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(".coveragerc", "[run]\ncore = ctrace\n")
    project.write(
        "product.py", "def choose(value):\n    if value:\n        return 1\n    return 0\n"
    )
    project.write(
        "test_product.py",
        "import pytest\nfrom product import choose\n"
        "@pytest.mark.parametrize('value', [True, False])\n"
        "def test_choose(value):\n    assert choose(value) == int(value)\n"
        "@pytest.mark.skip(reason='example')\n"
        "def test_skip():\n    raise AssertionError('skipped body ran')\n",
    )
    arguments = ("--cov=product", "--cov-branch", "--cov-report=", "-q")
    oracle = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_cov.plugin",
        "--junitxml=oracle.xml",
        *arguments,
        env={"COVERAGE_FILE": str(tmp_path / ".coverage.oracle")},
    )
    oracle.expect(0)
    expected_lines = project.lines("product.py", relative=".coverage.oracle")
    expected_arcs = set(project.arcs("product.py", relative=".coverage.oracle") or ())
    project.write(
        "sitecustomize.py",
        "import os\nimport sys\nfrom pathlib import Path\n"
        "if os.environ.get('VFY_BLOCK_UPSTREAM'):\n"
        "    class NoUpstream:\n"
        "        def find_spec(self, fullname, path=None, target=None):\n"
        "            if fullname.split('.')[0] in "
        "{'pytest', '_pytest', 'pluggy', 'coverage', 'packaging'}:\n"
        "                Path('upstream-import-attempt').write_text(fullname)\n"
        "                raise ModuleNotFoundError('upstream import blocked: ' + fullname)\n"
        "    sys.meta_path.insert(0, NoUpstream())\n"
        "    Path('upstream-guard-active').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--junitxml=actual.xml",
        "--native-result-json=results.json",
        *arguments,
        env={"PYTHONPATH": str(tmp_path), "VFY_BLOCK_UPSTREAM": "1" if block_upstream else ""},
    )
    result.expect(0)

    def outcomes(filename: str) -> list[tuple[str | None, str | None, list[str]]]:
        return [
            (case.get("classname"), case.get("name"), [child.tag for child in case])
            for case in ET.parse(tmp_path / filename).iter("testcase")
        ]

    assert outcomes("actual.xml") == outcomes("oracle.xml")
    assert project.lines("product.py") == expected_lines
    assert set(project.arcs("product.py") or ()) == expected_arcs
    record = json.loads((tmp_path / "results.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    assert set(history["tests"]) == {
        "test_product.py::test_choose[True]",
        "test_product.py::test_choose[False]",
        "test_product.py::test_skip",
    }
    assert (tmp_path / "upstream-guard-active").exists() is block_upstream
    assert not (tmp_path / "upstream-import-attempt").exists()
