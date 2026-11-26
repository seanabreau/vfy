from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from coverage import CoverageData as ReferenceData
from coverage.files import canonical_filename

from tests.support.vfy_project import CommandResult, VfyProject


def run_pytest(directory: Path, *arguments: str) -> CommandResult:
    """Run the real pytest boundary with vfy's plug-ins loaded."""

    return VfyProject(directory).run_pytest(
        *arguments,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )


def write_project(directory: Path) -> Path:
    project = VfyProject(directory)
    product = project.write(
        "product.py",
        "def covered():\n    return 42\n\ndef missed():\n    return 0\n",
    )
    project.write(
        "test_product.py",
        "from product import covered\n\ndef test_covered():\n    assert covered() == 42\n",
    )
    return product


def read_lines(data_file: Path, source: Path) -> list[int] | None:
    return VfyProject(data_file.parent).lines(source, data_file.name)


def test_pytest_plugin_collects_and_reports_by_default(tmp_path: Path) -> None:
    product = write_project(tmp_path)

    completed = run_pytest(tmp_path, "--cov=product", "-q")

    completed.expect(0)
    assert "tests coverage" in completed.stdout
    assert "product.py" in completed.stdout
    assert "TOTAL" in completed.stdout
    assert read_lines(tmp_path / ".coverage", product) == [1, 2, 4]
    assert not list(tmp_path.glob(".coverage.*"))


def test_stock_coverage_heading_without_native_runner_matches_pytest_cov(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    write_project(tmp_path)
    project.write(
        "conftest.py",
        "import vfy.native\nvars(vfy.native).pop('_pytest', None)\n",
    )
    headings = []
    for plugin in ("pytest_cov.plugin", "vfy.pytest_plugin"):
        result = project.run_pytest(
            "-p",
            plugin,
            "-q",
            "--cov=product",
            load_xdist=False,
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "COLUMNS": "91"},
        )
        result.expect(0)
        headings.append(
            next(line for line in result.stdout.splitlines() if "coverage: platform" in line)
        )
    assert headings[1] == headings[0]


@pytest.mark.parametrize("activation", ["command", "environment"])
def test_stock_pytest_activation_preserves_runtime_hooks_and_coverage(
    tmp_path: Path, activation: str
) -> None:
    project = VfyProject(tmp_path)
    product = write_project(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_runtest_call(item):\n"
        "    item.user_properties.append(('runtime_hook', 'called'))\n",
    )
    project.write(
        "test_product.py",
        "import os\n"
        "from product import covered\n\n"
        "def test_covered(cov, request):\n"
        "    assert type(cov).__module__ == os.environ['EXPECTED_COVERAGE_MODULE']\n"
        "    assert ('runtime_hook', 'called') in request.node.user_properties\n"
        "    assert covered() == 42\n",
    )
    flags = ["-p", "no:pytest_cov", "-p", "vfy.pytest_plugin"]
    arguments = ["--cov=product", "--cov-branch", "--cov-context=test", "--cov-report="]
    result = project.run_pytest(
        *(flags if activation == "command" else []),
        *arguments,
        load_xdist=False,
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
            "PYTEST_ADDOPTS": "-q --strict-markers"
            + (" " + " ".join(flags) if activation == "environment" else ""),
            "EXPECTED_COVERAGE_MODULE": "vfy.api",
        },
    )
    result.expect(0)
    oracle = project.run_pytest(
        "-p",
        "pytest_cov.plugin",
        *arguments,
        "-q",
        load_xdist=False,
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "COVERAGE_FILE": str(tmp_path / ".coverage.oracle"),
            "EXPECTED_COVERAGE_MODULE": "coverage.control",
        },
    )
    oracle.expect(0)
    actual, expected = project.data(), project.data(".coverage.oracle")
    filename = str(product)
    assert actual.has_arcs()
    assert expected.has_arcs()
    assert actual.measured_files() == expected.measured_files() == {filename}
    assert actual.lines(filename) == expected.lines(filename) == [1, 2, 4]
    assert actual.arcs(filename) == expected.arcs(filename)
    assert actual.contexts_by_lineno(filename) == expected.contexts_by_lineno(filename)
    assert "test_product.py::test_covered|run" in actual.measured_contexts()


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(
            "pytest-native",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="native test execution requires Linux or macOS"
            ),
        ),
        "pytest",
    ],
)
@pytest.mark.parametrize("coverage_enabled", [False, True])
def test_test_command_loads_report_implementations_only_when_needed(
    tmp_path: Path, engine: str, coverage_enabled: bool
) -> None:
    project = VfyProject(tmp_path)
    product = project.write("product.py", "def covered():\n    return 42\n")
    project.write(
        "test_product.py",
        "import atexit\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from product import covered\n\n"
        "REPORT_MODULES = {'vfy.reports', 'vfy.pytest_reports', 'vfy.semantic_reports'}\n"
        "def record_imports():\n"
        "    Path('report-imports.json').write_text(\n"
        "        json.dumps(sorted(REPORT_MODULES.intersection(sys.modules))))\n"
        "atexit.register(record_imports)\n\n"
        "def test_covered():\n"
        "    assert not REPORT_MODULES.intersection(sys.modules)\n"
        "    assert covered() == 42\n",
    )

    arguments = (
        ["--cov=product", "--cov-report=json:coverage.json"] if coverage_enabled else ["--no-cov"]
    )
    completed = project.run_vfy(
        "test", f"--engine={engine}", "--workers=1", "--no-replay", "-q", *arguments
    )

    completed.expect(0)
    assert "1 passed" in completed.stdout
    loaded = json.loads((tmp_path / "report-imports.json").read_text())
    if coverage_enabled:
        assert "vfy.semantic_reports" not in loaded
        report = json.loads((tmp_path / "coverage.json").read_text())
        assert report["totals"]["percent_covered"] == 100.0
        assert project.lines(product) == [1, 2]
    else:
        assert loaded == []
        assert not (tmp_path / ".coverage").exists()


def test_pytest_explicit_zero_threshold_overrides_configured_failure(tmp_path: Path) -> None:
    write_project(tmp_path)
    VfyProject(tmp_path).write(".coveragerc", "[report]\nfail_under = 100\nprecision = 2\n")
    configured = run_pytest(tmp_path, "--cov=product", "-q")
    configured.expect(1)
    overridden = run_pytest(
        tmp_path, "--cov=product", "--cov-fail-under=0", "--cov-precision=0", "-q"
    )
    overridden.expect(0)
    assert "75%" in overridden.stdout


@pytest.mark.parametrize("branch", [False, True])
def test_pytest_collects_modules_imported_inside_a_test(tmp_path: Path, branch: bool) -> None:
    product = tmp_path / "delayed.py"
    product.write_text("answer = 40\nanswer += 2\n", encoding="utf-8")
    (tmp_path / "test_delayed.py").write_text(
        "import importlib\n\n"
        "def test_delayed_import():\n"
        "    module = importlib.import_module('delayed')\n"
        "    assert module.answer == 42\n",
        encoding="utf-8",
    )

    arguments = ["--cov=delayed", "--cov-report=", "-q"]
    if branch:
        arguments.insert(1, "--cov-branch")
    completed = run_pytest(tmp_path, *arguments)

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", product) == [1, 2]


def test_pytest_reports_destinations_and_suppression(tmp_path: Path) -> None:
    write_project(tmp_path)

    completed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-report=term-missing:skip-covered",
        "--cov-report=json:artifacts/coverage.json",
        "--cov-report=xml:artifacts/coverage.xml",
        "--cov-report=markdown:artifacts/coverage.md",
        "--cov-report=lcov:artifacts/coverage.lcov",
        "--cov-report=html:artifacts/html",
        "-q",
    )

    completed.expect(0)
    assert (tmp_path / "artifacts/coverage.json").is_file()
    assert (tmp_path / "artifacts/coverage.xml").is_file()
    assert (tmp_path / "artifacts/coverage.md").is_file()
    assert (tmp_path / "artifacts/coverage.lcov").is_file()
    assert (tmp_path / "artifacts/html/index.html").is_file()

    suppressed = run_pytest(tmp_path, "--cov=product", "--cov-report=", "-q")
    suppressed.expect(0)
    assert "tests coverage" not in suppressed.stdout
    assert (tmp_path / ".coverage").is_file()


def test_pytest_threshold_uses_pytest_failure_status(tmp_path: Path) -> None:
    write_project(tmp_path)

    completed = run_pytest(tmp_path, "--cov=product", "--cov-fail-under=100", "-q")

    assert completed.returncode == 1
    assert "FAIL Required test coverage of 100% not reached" in completed.stdout


def test_pytest_decision_report_and_semantic_thresholds(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text(
        "def choose(first, second):\n"
        "    if first and second:\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    (tmp_path / "test_product.py").write_text(
        "from product import choose\n\n"
        "def test_false():\n"
        "    assert not choose(False, False)\n\n"
        "def test_true():\n"
        "    assert choose(True, True)\n",
        encoding="utf-8",
    )

    completed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-decisions",
        "--cov-report=",
        "--cov-decision-report=json:decisions.json",
        "--cov-fail-under-decisions=100",
        "-q",
    )

    completed.expect(0)
    assert (tmp_path / "decisions.json").is_file(), completed.stdout + completed.stderr
    payload = json.loads((tmp_path / "decisions.json").read_text())
    assert payload["schema"] == "vfy.semantic"
    assert payload["totals"]["decisions"] == {"covered": 2, "total": 2}
    assert payload["totals"]["conditions"] == {"covered": 3, "total": 4}

    failed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-decisions",
        "--cov-report=",
        "--cov-fail-under-conditions=100",
        "-q",
    )
    assert failed.returncode == 1
    assert "Required semantic conditions coverage of 100%" in failed.stdout


def test_pytest_semantic_json_report_creates_parent_directories(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text(
        "def choose(value):\n    if value:\n        return True\n    return False\n",
        encoding="utf-8",
    )
    (tmp_path / "test_product.py").write_text(
        "from product import choose\n\ndef test_choose():\n    assert choose(True)\n",
        encoding="utf-8",
    )

    completed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-decisions",
        "--cov-report=",
        "--cov-decision-report=json:artifacts/decisions/report.json",
        "-q",
    )

    completed.expect(0)
    report = tmp_path / "artifacts/decisions/report.json"
    assert report.is_file()
    assert json.loads(report.read_text())["schema"] == "vfy.semantic"


@pytest.mark.parametrize("report", ["xml", "term:artifacts/decisions.txt"])
def test_pytest_rejects_invalid_semantic_report_before_collection(
    tmp_path: Path, report: str
) -> None:
    write_project(tmp_path)

    completed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-decisions",
        f"--cov-decision-report={report}",
        "-q",
    )

    assert completed.returncode == 4
    assert "semantic report" in completed.stderr
    assert "collected" not in completed.stdout


def test_pytest_context_fixture_and_no_cover_marker(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text(
        "def included():\n    return 1\n\ndef excluded():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "test_product.py").write_text(
        "import vfy\n"
        "from product import excluded, included\n\n"
        "def test_cov_fixture(cov):\n"
        "    assert isinstance(cov, vfy.Coverage)\n"
        "    assert included() == 1\n\n"
        "@pytest.mark.no_cover\n"
        "def test_excluded():\n"
        "    assert excluded() == 2\n",
        encoding="utf-8",
    )
    # Keep the fixture source representative while avoiding an implicit global import.
    test_file = tmp_path / "test_product.py"
    test_file.write_text(
        "import pytest\n" + test_file.read_text(encoding="utf-8"), encoding="utf-8"
    )

    completed = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-context=test",
        "--cov-report=json:coverage.json",
        "-q",
    )

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", product) == [1, 2, 4]
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.read()
    assert any("test_cov_fixture|run" in context for context in data.measured_contexts())


def test_pytest_no_cover_fixture_disables_the_call_phase(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text("def excluded():\n    return 2\n", encoding="utf-8")
    (tmp_path / "test_product.py").write_text(
        "from product import excluded\n\n"
        "def test_excluded(no_cover):\n"
        "    assert excluded() == 2\n",
        encoding="utf-8",
    )

    completed = run_pytest(tmp_path, "--cov=product", "--cov-report=", "-q")

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", product) == [1]


def test_pytest_no_cov_and_no_cov_on_fail(tmp_path: Path) -> None:
    write_project(tmp_path)
    test_file = tmp_path / "test_product.py"
    test_file.write_text("def test_failure():\n    assert False\n", encoding="utf-8")

    disabled = run_pytest(tmp_path, "--cov=product", "--no-cov", "-q")
    assert disabled.returncode == 1
    assert not (tmp_path / ".coverage").exists()

    failed = run_pytest(
        tmp_path,
        "--cov=product",
        "--no-cov-on-fail",
        "--cov-report=json:coverage.json",
        "-q",
    )
    assert failed.returncode == 1
    assert (tmp_path / ".coverage").is_file()
    assert not (tmp_path / "coverage.json").exists()


def test_pytest_append_preserves_prior_data(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text(
        "def first():\n    return 1\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_product.py"
    test_file.write_text(
        "from product import first\n\ndef test_first():\n    assert first() == 1\n",
        encoding="utf-8",
    )
    first = run_pytest(tmp_path, "--cov=product", "--cov-report=", "-q")
    first.expect(0)

    test_file.write_text(
        "from product import second\n\ndef test_second():\n    assert second() == 2\n",
        encoding="utf-8",
    )
    second = run_pytest(
        tmp_path,
        "--cov=product",
        "--cov-append",
        "--cov-report=",
        "-q",
    )

    second.expect(0)
    assert read_lines(tmp_path / ".coverage", product) == [1, 2, 4, 5]


def test_pytest_xdist_combines_worker_data(tmp_path: Path) -> None:
    product = tmp_path / "product.py"
    product.write_text(
        "def first():\n    return 1\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "test_product.py").write_text(
        "from product import first, second\n\n"
        "def test_first():\n"
        "    assert first() == 1\n\n"
        "def test_second():\n"
        "    assert second() == 2\n",
        encoding="utf-8",
    )

    completed = run_pytest(
        tmp_path,
        "-n",
        "2",
        "--cov=product",
        "--cov-context=test",
        "--cov-report=",
        "-q",
    )

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", product) == [1, 2, 4, 5]
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.read()
    assert {context for context in data.measured_contexts() if "test_" in context}


def test_xdist_diagnostic_profiles_retain_each_process(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    write_project(tmp_path)
    profiles = tmp_path / "profiles"
    completed = project.run_pytest(
        "-n",
        "2",
        "--cov=product",
        "--cov-report=",
        "-q",
        env={"VFY_PROFILE_DIR": str(profiles), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    records = [json.loads(path.read_text(encoding="utf-8")) for path in profiles.glob("*.json")]
    assert len(records) == 3, records
    assert len({record["pid"] for record in records}) == 3, records
    for record in records:
        phases = {phase["name"]: phase["elapsed_ms"] for phase in record["phases"]}
        assert phases["execution"] > 0, record


def test_pytest_collects_python_subprocesses(tmp_path: Path) -> None:
    child = tmp_path / "child.py"
    child.write_text("answer = 40\nanswer += 2\n", encoding="utf-8")
    (tmp_path / "test_child.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def test_child():\n"
        "    subprocess.run([sys.executable, 'child.py'], check=True)\n",
        encoding="utf-8",
    )

    completed = run_pytest(tmp_path, "--cov=.", "--cov-report=", "-q")

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", child) == [1, 2]


def test_pytest_accepts_pytest_cov_subprocess_configuration(tmp_path: Path) -> None:
    child = tmp_path / "child.py"
    child.write_text("answer = 42\n", encoding="utf-8")
    (tmp_path / "test_child.py").write_text(
        "import subprocess\n"
        "import sys\n\n"
        "def test_child():\n"
        "    subprocess.run([sys.executable, 'child.py'], check=True)\n",
        encoding="utf-8",
    )
    (tmp_path / ".coveragerc").write_text(
        "[run]\npatch = subprocess\nbranch = true\n",
        encoding="utf-8",
    )

    completed = run_pytest(tmp_path, "--cov=.", "--cov-report=", "-q")

    completed.expect(0)
    assert read_lines(tmp_path / ".coverage", child) == [1]


def test_recorded_paths_use_the_same_spelling_coverage_py_records(tmp_path: Path) -> None:
    product = write_project(tmp_path)

    completed = run_pytest(tmp_path, "--cov=product", "--cov-report=", "-q")

    completed.expect(0)
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.read()
    assert data.measured_files() == {canonical_filename(str(product))}


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege")
def test_recorded_paths_resolve_a_symlinked_project_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    product = write_project(project)
    link = tmp_path / "link"
    link.symlink_to(project)

    completed = run_pytest(link, "--cov=product", "--cov-report=", "-q")

    completed.expect(0)
    assert read_lines(link / ".coverage", product) == [1, 2, 4]


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege")
def test_recorded_paths_match_coverage_py_for_a_symlinked_source_file(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "product.py").write_text(
        "def covered():\n    return 42\n\ndef missed():\n    return 0\n",
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "product.py").symlink_to(real / "product.py")
    (work / "test_product.py").write_text(
        "from product import covered\n\ndef test_covered():\n    assert covered() == 42\n",
        encoding="utf-8",
    )

    completed = run_pytest(work, "--cov=product", "--cov-report=", "-q")

    completed.expect(0)
    data = ReferenceData(str(work / ".coverage"))
    data.read()
    assert data.measured_files() == {canonical_filename(str(work / "product.py"))}


def _terminal_table(result: CommandResult) -> list[str]:
    lines = result.stdout.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("Name "))
    end = next(index for index, line in enumerate(lines) if line.startswith("TOTAL"))
    skipped = [line for line in lines if "skipped due to complete coverage" in line]
    return [*lines[start : end + 1], *skipped]


@pytest.mark.parametrize(
    "report", ["term", "term-missing", "term:skip-covered", "term-missing:skip-covered"]
)
@pytest.mark.parametrize("skip_covered", [None, "true", "false"])
@pytest.mark.parametrize("show_missing", [None, "true", "false"])
def test_terminal_report_settings_combine_with_report_options_like_pytest_cov(
    tmp_path: Path, report: str, show_missing: str | None, skip_covered: str | None
) -> None:
    project = VfyProject(tmp_path)
    write_project(tmp_path)
    project.write("complete.py", "VALUE = 1\n")
    project.write(
        "test_product.py",
        "import complete\n"
        "from product import covered\n\n"
        "def test_covered():\n"
        "    assert covered() == complete.VALUE * 42\n",
    )
    settings = "".join(
        f"{name} = {value}\n"
        for name, value in (("show_missing", show_missing), ("skip_covered", skip_covered))
        if value is not None
    )
    project.write(".coveragerc", f"[report]\n{settings}")
    arguments = ["--cov=.", "--cov-report", report, "-q"]

    expected = project.run_pytest(
        "-p",
        "pytest_cov.plugin",
        *arguments,
        load_xdist=False,
        env={
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "COVERAGE_FILE": str(tmp_path / ".coverage.oracle"),
        },
    )
    expected.expect(0)
    stock = project.run_pytest(
        "-p",
        "vfy.pytest_plugin",
        *arguments,
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    stock.expect(0)
    independent = project.run_vfy("test", "--engine=native", "--no-replay", *arguments)
    independent.expect(0)

    table = _terminal_table(expected)
    assert any("Missing" in line for line in table) == (
        report.startswith("term-missing") or show_missing == "true"
    )
    assert _terminal_table(stock) == table
    assert _terminal_table(independent) == table
