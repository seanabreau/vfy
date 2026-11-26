"""Independent selection binds historical evidence to the final collection."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


def _selected_spare_metadata(root: Path) -> dict[str, object]:
    project = VfyProject(root)
    project.write(
        "test_app.py",
        "def test_first():\n    assert True\ndef test_second():\n    assert True\n",
    )
    project.write(
        "conftest.py",
        "import json\nfrom pathlib import Path\ncalled = []\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'call': called.append(report.nodeid)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if hasattr(session.config, 'workerinput'):\n"
        "        worker = session.config.workerinput['workerid']\n"
        "        Path(f'{worker}.json').write_text(json.dumps({\n"
        "            'collected': session.testscollected,\n"
        "            'items': [item.nodeid for item in session.items],\n"
        "            'called': called, 'status': int(exitstatus)}))\n",
    )
    result = project.run_python(
        "-m", "pytest", "-p", "xdist.plugin", "-n", "2", "--dist=load", "-k", "test_first", "-q"
    )
    result.expect(0)
    workers = [json.loads(path.read_text()) for path in root.glob("gw*.json")]
    assert len(workers) == 2, workers
    unused = [worker for worker in workers if not worker["called"]]
    expected: dict[str, object] = {
        "collected": 1,
        "items": ["test_app.py::test_first"],
        "called": [],
        "status": 0,
    }
    assert unused == [expected]
    return expected


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
@pytest.mark.parametrize(("workers", "rewrite"), [("1", False), ("2", False), ("1", True)])
def test_affected_execution_matches_full_coverage(
    tmp_path: Path, workers: str, rewrite: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "test_app.py",
        "from app import first, second\n"
        "def test_first():\n    assert first() == 1\n"
        "def test_second():\n    assert second() == 2\n",
    )
    project.write(
        "conftest.py",
        "import json, os\nfrom pathlib import Path\ncalled = []\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if Path('.vfy/rewrite').exists():\n"
        "        items[-1]._nodeid += '_renamed'\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'call': called.append(report.nodeid)\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
        "        Path('.vfy/surplus-metadata.json').write_text(json.dumps({\n"
        "            'collected': session.testscollected,\n"
        "            'items': [item.nodeid for item in session.items],\n"
        "            'called': called, 'status': int(exitstatus)}))\n"
        "        Path('.vfy/surplus-finish').write_text(str(exitstatus))\n",
    )
    project.git_init()
    (tmp_path / ".vfy").mkdir(exist_ok=True)
    arguments = (
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--cov=app",
        "--cov-branch",
        "--cov-report=",
        "--native-result-json=.vfy/result.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments, "--all")
    baseline.expect(0)
    project.write("app.py", "def first():\n    return int('1')\ndef second():\n    return 2\n")
    if rewrite:
        project.write(".vfy/rewrite", "")
    expected = ["test_app.py::test_first"]
    if rewrite:
        expected.append("test_app.py::test_second_renamed")
    spare = None
    if workers == "2":
        spare = _selected_spare_metadata(tmp_path / ".vfy/reference")
        (tmp_path / ".vfy/surplus-finish").unlink()
        (tmp_path / ".vfy/surplus-metadata.json").unlink()
    selected = project.run_vfy(*arguments)
    selected.expect(0)
    selected_record = json.loads((tmp_path / ".vfy/result.json").read_text())
    if workers == "2":
        assert selected_record["execution_topology"]["active_workers"] == 1
        assert (tmp_path / ".vfy/surplus-finish").read_text() == "0"
        assert json.loads((tmp_path / ".vfy/surplus-metadata.json").read_text()) == spare
    selected_data = project.data()
    app = str((tmp_path / "app.py").resolve())
    selected_evidence = (
        selected_data.lines(app),
        selected_data.arcs(app),
        selected_data.contexts_by_lineno(app),
    )
    full = project.run_vfy(*arguments, "--all")
    full_data = project.data()
    assert selected.returncode == full.returncode == 0, selected.stdout + selected.stderr
    assert [case["nodeid"] for case in selected_record["cases"]] == expected
    assert selected_evidence == (
        full_data.lines(app),
        full_data.arcs(app),
        full_data.contexts_by_lineno(app),
    )
    project.write("app.py", "def first():\n    return 9\ndef second():\n    return 2\n")
    broken = project.run_vfy(*arguments)
    broken_record = json.loads((tmp_path / ".vfy/result.json").read_text())
    broken_full = project.run_vfy(*arguments, "--all")
    stock = project.run_pytest("-q", load_xdist=False)
    assert broken.returncode == broken_full.returncode == stock.returncode == 1, (
        broken.stdout + broken.stderr + stock.stdout + stock.stderr
    )
    assert [case["nodeid"] for case in broken_record["cases"]] == expected, (
        broken.stdout + broken.stderr
    )


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_affected_coverage_reports_and_threshold_use_reconstructed_full_data(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    source = "def first():\n    return 1\ndef second():\n    return 2\n"
    project.write("app.py", source)
    project.write(
        "test_app.py",
        "from app import first, second\n"
        "def test_first(): assert first() == 1\n"
        "def test_second(): assert second() == 2\n",
    )
    project.git_init()
    stock = project.run_pytest(
        "-p",
        "pytest_cov.plugin",
        "--cov=app",
        "--cov-report=",
        "--cov-fail-under=100",
        load_xdist=False,
        env={"COVERAGE_FILE": str(tmp_path / ".coverage.stock")},
    )
    stock.expect(0)
    arguments = (
        "test",
        "--engine=native",
        "--workers=1",
        "--cov=app",
        "--cov-context=test",
        "--cov-report=json:.vfy/report.json",
        "--cov-fail-under=100",
        "--native-result-json=.vfy/result.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments, "--all")
    baseline.expect(0)
    assert (
        json.loads((tmp_path / ".vfy/report.json").read_text())["totals"]["percent_covered"] == 100
    )
    project.write("app.py", source.replace("return 1", "return int('1')"))
    selected = project.run_vfy(*arguments)
    selected.expect(0)
    assert selected.stdout.count("Coverage JSON written to file") == 1
    assert (
        json.loads((tmp_path / ".vfy/report.json").read_text())["totals"]["percent_covered"] == 100
    )
    record = json.loads((tmp_path / ".vfy/result.json").read_text())
    assert [case["nodeid"] for case in record["cases"]] == ["test_app.py::test_first"]
    project.write("app.py", source.replace("return 1", "return 9"))
    failed = project.run_vfy(*arguments)
    failed.expect(1)
    assert failed.stdout.count("Coverage JSON written to file") == 1
    assert (
        json.loads((tmp_path / ".vfy/report.json").read_text())["totals"]["percent_covered"] < 100
    )
    record = json.loads((tmp_path / ".vfy/result.json").read_text())
    assert [case["nodeid"] for case in record["cases"]] == ["test_app.py::test_first"]
    assert record["counts"]["failed"] == 1


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_plan_only_works_without_upstream_and_preserves_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case():\n    assert True\n")
    baseline = project.run_vfy("test", "--engine=native", "--workers=1", "-q")
    baseline.expect(0)
    project.write("test_case.py", "def test_case():\n    raise AssertionError('body ran')\n")
    project.write(
        "sitecustomize.py",
        "import sys\nfrom pathlib import Path\n"
        "class NoUpstream:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in {'pytest', '_pytest', 'pluggy'}:\n"
        "            Path('upstream-attempt').write_text(fullname)\n"
        "            raise ModuleNotFoundError(fullname)\n"
        "sys.meta_path.insert(0, NoUpstream())\n"
        "Path('guard-active').touch()\n",
    )
    before = {
        name: (tmp_path / name).read_bytes() for name in (".coverage", ".vfy/test-history.sqlite3")
    }
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=1",
        "--no-cov",
        "--plan-only",
        "--plan-output=.vfy/plan.json",
        "-q",
        env={"PYTHONPATH": str(tmp_path)},
    )
    result.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())
    assert plan["collected"] == plan["selection"]["selected"] == ["test_case.py::test_case"]
    assert {name: (tmp_path / name).read_bytes() for name in before} == before
    assert (tmp_path / "guard-active").is_file()
    assert not (tmp_path / "upstream-attempt").exists()


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
@pytest.mark.parametrize("repeated", [False, True])
def test_repeated_display_ids_keep_full_coverage_without_git(
    tmp_path: Path, repeated: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "def value(): return 1\n")
    project.write(
        "test_case.py",
        "from product import value\n"
        "def test_first(): assert value() == 1\n"
        "def test_second(): assert value() == 1\n",
    )
    if repeated:
        project.write(
            "conftest.py",
            "def pytest_collection_modifyitems(items):\n"
            "    for item in items: item._nodeid = 'shared.py::same'\n",
        )
    arguments = ("--cov=product", "--cov-context=test", "--cov-report=", "-q")
    stock = project.run_pytest(
        "-p",
        "pytest_cov.plugin",
        *arguments,
        load_xdist=False,
        env={"COVERAGE_FILE": str(tmp_path / ".coverage.stock")},
    )
    for _ in range(2):
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--workers=1",
            *arguments,
            "--native-result-json=.vfy/result.json",
        )
        assert result.returncode == stock.returncode == 0, result.stdout + result.stderr
        assert result.stderr.splitlines().count("Running the full suite") == 1
        reason = "Note: collected tests have repeated display identities; running the full suite"
        assert (reason in result.stderr.splitlines()) is repeated
        record = json.loads((tmp_path / ".vfy/result.json").read_text())
        assert [case["nodeid"] for case in record["cases"]] == (
            ["shared.py::same"] * 2
            if repeated
            else ["test_case.py::test_first", "test_case.py::test_second"]
        )
        assert project.lines("product.py") == project.lines(
            "product.py", relative=".coverage.stock"
        )


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_append_preserves_previous_execution_coverage(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "test_case.py",
        "import os\nfrom product import first, second\n"
        "def test_value():\n"
        "    assert (first, second)[int(os.environ['CHOICE'])]() in (1, 2)\n",
    )
    for engine in ("stock", "native"):
        for choice in ("0", "1"):
            result = project.run_engine(
                engine,
                "--cov=product",
                "--cov-report=",
                "-q",
                *(["--cov-append"] if choice == "1" else []),
                stock=("-p", "pytest_cov.plugin"),
                independent=("--workers=1",),
                no_cov=False,
                env={"CHOICE": choice, "COVERAGE_FILE": str(tmp_path / f".coverage.{engine}")},
            )
            result.expect(0)
    assert (
        project.lines("product.py", relative=".coverage.native")
        == project.lines("product.py", relative=".coverage.stock")
        == [1, 2, 3, 4]
    )


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
@pytest.mark.parametrize("static_context", [None, "label"])
def test_setup_skips_remain_in_the_green_collection_inventory(
    tmp_path: Path, static_context: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    if static_context is not None:
        project.write(".coveragerc", f"[run]\ncontext = {static_context}\n")
    project.write(
        "test_app.py",
        "import pytest\nfrom app import first, second\n"
        "def test_first(): assert first() == 1\n"
        "def test_second(): assert second() == 2\n"
        "@pytest.mark.skip(reason='unavailable')\n"
        "def test_unavailable(): raise AssertionError('skip body ran')\n",
    )
    project.git_init()
    stock = project.run_pytest("-q", load_xdist=False)
    arguments = (
        "test",
        "--engine=native",
        "--workers=1",
        "--cov=app",
        "--cov-report=",
        "--native-result-json=.vfy/result.json",
        "--plan-output=.vfy/plan.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments, "--all")
    assert stock.returncode == baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert "2 passed, 1 skipped" in stock.stdout, stock.stdout + stock.stderr
    baseline_cases = json.loads((tmp_path / ".vfy/result.json").read_text())["cases"]
    assert [(case["nodeid"], case["phases"][0]["outcome"]) for case in baseline_cases] == [
        ("test_app.py::test_first", "passed"),
        ("test_app.py::test_second", "passed"),
        ("test_app.py::test_unavailable", "skipped"),
    ]
    project.write("app.py", "def first():\n    return int('1')\ndef second():\n    return 2\n")
    selected = project.run_vfy(*arguments)
    selected.expect(0)
    plan = json.loads((tmp_path / ".vfy/plan.json").read_text())
    assert plan["collected"] == [case["nodeid"] for case in baseline_cases]
    assert plan["selection"]["fallback_full"] is (static_context is not None), (
        selected.stdout + selected.stderr
    )
    ran = [
        case["nodeid"] for case in json.loads((tmp_path / ".vfy/result.json").read_text())["cases"]
    ]
    if static_context is not None:
        expected = [case["nodeid"] for case in baseline_cases]
        assert plan["selection"]["selected"] == ran == expected
    else:
        # The skipped test has no coverage context, so its dependencies are unknown.
        expected = ["test_app.py::test_first", "test_app.py::test_unavailable"]
        assert sorted(plan["selection"]["selected"]) == sorted(ran) == expected


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_skipped_tests_rerun_when_changed_code_decides_the_skip(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write(
        "app.py",
        "def first():\n    return 1\ndef enabled():\n    return False\n",
    )
    project.write(
        "test_app.py",
        "from app import first\ndef test_first(): assert first() == 1\n",
    )
    # A dynamic import keeps the skip condition outside the static import graph.
    project.write(
        "test_gated.py",
        "import importlib\nimport pytest\n"
        "@pytest.mark.skipif(not importlib.import_module('app').enabled(), reason='disabled')\n"
        "def test_gated(): assert True\n",
    )
    project.git_init()
    arguments = (
        "test",
        "--engine=native",
        "--workers=1",
        "--cov=app",
        "--cov-context=test",
        "--cov-report=",
        "--native-result-json=.vfy/result.json",
        "-q",
    )
    baseline = project.run_vfy(*arguments, "--all")
    baseline.expect(0)
    project.write(
        "app.py",
        "def first():\n    return int('1')\ndef enabled():\n    return True\n",
    )
    selected = project.run_vfy(*arguments)
    selected.expect(0)
    outcomes = {
        case["nodeid"]: case["phases"][-1]["outcome"]
        for case in json.loads((tmp_path / ".vfy/result.json").read_text())["cases"]
    }
    assert outcomes == {
        "test_app.py::test_first": "passed",
        "test_gated.py::test_gated": "passed",
    }
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    assert "2 passed" in stock.stdout, stock.stdout + stock.stderr


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_selected_run_with_two_workers_matches_one_worker(tmp_path: Path) -> None:
    def run(workers: str) -> tuple[list[str], list[str]]:
        root = tmp_path / f"workers{workers}"
        root.mkdir()
        project = VfyProject(root)
        project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
        project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
        project.write(
            "test_app.py",
            "from app import first, second\n"
            "def test_a(): assert first() == 1\n"
            "def test_b(): assert first() == 1\n"
            "def test_c(): assert first() == 1\n"
            "def test_d(): assert second() == 2\n",
        )
        project.git_init()
        arguments = (
            "test",
            "--engine=native",
            f"--workers={workers}",
            "--cov=app",
            "--cov-context=test",
            "--cov-report=",
            "--native-result-json=.vfy/result.json",
            "--plan-output=.vfy/plan.json",
            "-q",
        )
        baseline = project.run_vfy(*arguments, "--all")
        baseline.expect(0)
        project.write("app.py", "def first():\n    return int('1')\ndef second():\n    return 2\n")
        result = project.run_vfy(*arguments)
        result.expect(0)
        plan = json.loads((root / ".vfy/plan.json").read_text())
        record = json.loads((root / ".vfy/result.json").read_text())
        assert not plan["selection"]["fallback_full"], plan["selection"]["reasons"]
        return (
            sorted(plan["selection"]["selected"]),
            sorted(case["nodeid"] for case in record["cases"]),
        )

    expected = [f"test_app.py::test_{name}" for name in "abc"]
    outcomes = {workers: run(workers) for workers in ("1", "2")}
    assert outcomes["1"] == outcomes["2"] == (expected, expected)


@pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")
def test_selected_run_prints_reports_when_history_cannot_be_recorded(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", "def first():\n    return 1\ndef second():\n    return 2\n")
    project.write(
        "test_app.py",
        "from app import first, second\n"
        "def test_first(): assert first() == 1\n"
        "def test_second(): assert second() == 2\n",
    )
    project.git_init()
    arguments = (
        "test",
        "--engine=native",
        "--workers=1",
        "--cov=app",
        "--cov-context=test",
        "--cov-report=term",
        "-q",
    )
    baseline = project.run_vfy(*arguments, "--all")
    baseline.expect(0)
    history = tmp_path / ".vfy/test-history.sqlite3"
    history.unlink()
    history.mkdir()
    project.write("app.py", "def first():\n    return int('1')\ndef second():\n    return 2\n")
    selected = project.run_vfy(*arguments)
    output = selected.stdout + selected.stderr
    assert "could not be recorded" in output, output
    assert selected.returncode == 1, output
    assert selected.stdout.count("TOTAL") == 1, output
    assert "Complete coverage was not saved" in output, output
    full = project.run_vfy(*arguments, "--all")
    full.expect(0)
    assert "TOTAL" in full.stdout, full.stdout + full.stderr
