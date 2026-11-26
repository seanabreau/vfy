"""Execute the documented workflows against the installed vfy boundary."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import TypedDict
from xml.etree import ElementTree as ET

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
TUTORIAL = "getting-started/quickstart.md"
SELECTION = "getting-started/selection.md"
API = "reference/python-api.md"
MUTATION = "guides/mutation.md"
requires_native_test_engine = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="documented vfy test examples require the Linux/macOS native engine",
)


def block(page: str, name: str) -> str:
    """Read one explicitly executable block; ordinary snippets remain prose."""
    contents = (DOCS / page).read_text(encoding="utf-8")
    pattern = rf"<!-- doc-test: {re.escape(name)} -->\n```\w+\n(.*?)\n```"
    matches = re.findall(pattern, contents, re.DOTALL)
    assert len(matches) == 1, f"{page}: expected one doc-test block named {name}"
    match = matches[0]
    assert isinstance(match, str)
    return match + "\n"


@pytest.fixture
def project(tmp_path: Path) -> VfyProject:
    example = ROOT / "examples" / "coverage"
    for directory in ("src", "tests"):
        shutil.copytree(
            example / directory,
            tmp_path / directory,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    for filename in ("pyproject.toml", "uv.lock", ".gitignore"):
        shutil.copyfile(example / filename, tmp_path / filename)
    return VfyProject(tmp_path)


def commands(project: VfyProject, page: str, name: str, *, status: int = 0) -> list[CommandResult]:
    """Use the installed interpreter in place of uv's environment dispatch."""
    results = []
    for line in block(page, name).splitlines():
        args = shlex.split(line)
        assert args, f"{page}/{name}: expected a command"
        if (
            page == MUTATION
            and sys.platform == "darwin"
            and args[:4] == ["uv", "run", "vfy", "mutate"]
        ):
            # Follow the guide's macOS instructions for each default-engine example.
            if not any(arg.startswith("--engine") for arg in args):
                args.insert(4, "--engine=pytest-native")
        if args[:3] == ["uv", "run", "vfy"]:
            result = project.run_vfy(*args[3:])
        elif args[:3] == ["uv", "run", "pytest"]:
            result = project.run_pytest(*args[3:], load_xdist=False)
        elif args[0] == "vfy":
            result = project.run_vfy(*args[1:])
        else:
            assert args[0] == "git", f"unsupported tutorial command: {line}"
            result = project.run(*args)
        assert result.returncode == status, (page, name, line, result)
        results.append(result)
    return results


def passed_nodes(result: CommandResult) -> set[str]:
    return set(re.findall(r"^(tests/\S+) PASSED", result.stdout, re.MULTILINE))


class FileEvidence(TypedDict):
    lines: list[int]
    arcs: list[tuple[int, int]]
    contexts: dict[int, list[str]]


def evidence(project: VfyProject, filename: str = ".coverage") -> dict[str, FileEvidence]:
    data = project.data(filename)
    return {
        Path(source).relative_to(project.root.resolve()).as_posix(): {
            "lines": sorted(data.lines(source) or []),
            "arcs": sorted(data.arcs(source) or []),
            "contexts": data.contexts_by_lineno(source),
        }
        for source in sorted(data.measured_files())
    }


def assert_missing_report(project: VfyProject, result: CommandResult) -> None:
    assert result.stdout.replace("\\", "/") == block(TUTORIAL, "missing-output")


@requires_native_test_engine
def test_stable_tutorial_closes_the_documented_coverage_gap(project: VfyProject) -> None:
    initial = commands(project, TUTORIAL, "collect")[0]
    assert passed_nodes(initial) == {
        "tests/test_bounds.py::test_positive_value",
        "tests/test_formatting.py::test_format_count",
    }
    assert_missing_report(project, commands(project, TUTORIAL, "report")[0])
    bounds = evidence(project)["src/bounds.py"]
    assert bounds["lines"] == [1, 4, 5, 7]
    assert (5, 7) in bounds["arcs"]
    assert (5, 6) not in bounds["arcs"]
    # Entry/self arcs differ across CPython versions. Compare those with the
    # pinned reference instead of baking one interpreter's event shape in.
    oracle = project.run_coverage_py(
        "run", "--branch", "--source=src", "--data-file=.coverage.oracle", "-m", "pytest"
    )
    assert oracle.returncode == 0, oracle
    assert bounds["arcs"] == evidence(project, ".coverage.oracle")["src/bounds.py"]["arcs"]
    commands(project, TUTORIAL, "fail-gate", status=2)

    project.write("tests/test_bounds.py", block(TUTORIAL, "complete-tests"))
    rerun, report = commands(project, TUTORIAL, "recollect")
    assert passed_nodes(rerun) == {
        "tests/test_bounds.py::test_positive_value",
        "tests/test_bounds.py::test_negative_value",
        "tests/test_formatting.py::test_format_count",
    }
    assert "100%" in report.stdout
    commands(project, TUTORIAL, "export")
    payload = json.loads((project.root / "coverage.json").read_text())
    bounds_report = next(v for k, v in payload["files"].items() if k.endswith("bounds.py"))
    assert bounds_report["executed_lines"] == [1, 4, 5, 6, 7]
    assert bounds_report["missing_lines"] == []
    assert bounds_report["missing_branches"] == []
    assert payload["totals"]["percent_covered"] == 100
    xml = ET.parse(project.root / "coverage.xml").getroot()
    assert float(xml.attrib["line-rate"]) == 1
    assert float(xml.attrib["branch-rate"]) == 1


def test_collection_guide_keeps_pytest_and_records_branches(project: VfyProject) -> None:
    result = commands(project, "guides/collection.md", "stock-coverage")[0]
    assert "2 passed" in result.stdout
    bounds = evidence(project)["src/bounds.py"]
    assert bounds["lines"] == [1, 4, 5, 7]
    assert (5, 7) in bounds["arcs"]
    assert (5, 6) not in bounds["arcs"]


@requires_native_test_engine
def test_adoption_comparison_runs_the_requested_scope(project: VfyProject) -> None:
    stock, native = commands(project, "guides/native-adoption.md", "compare-execution")
    assert "2 passed" in stock.stdout
    assert "2 passed" in native.stdout
    assert not (project.root / ".coverage").exists()


@requires_native_test_engine
def test_selection_tutorial_reconstructs_the_full_result(project: VfyProject) -> None:
    # Supply test-only identity without requiring the user's global Git config.
    project.run("git", "init", "-q")
    project.run("git", "config", "user.email", "vfy-tests@example.invalid")
    project.run("git", "config", "user.name", "vfy Tests")
    commands(project, SELECTION, "git-baseline")
    baseline = commands(project, SELECTION, "baseline")[0]
    assert len(passed_nodes(baseline)) == 2
    project.write("src/bounds.py", block(SELECTION, "edit-bounds"))
    selected = commands(project, SELECTION, "select")[0]
    assert passed_nodes(selected) == {"tests/test_bounds.py::test_positive_value"}
    explanation = json.loads(commands(project, SELECTION, "explain")[0].stdout)
    assert [test["nodeid"] for test in explanation["tests"]] == [
        "tests/test_bounds.py::test_positive_value"
    ]
    assert_missing_report(project, commands(project, SELECTION, "selected-report")[0])
    selected_data = evidence(project)

    # An independent full run through Coverage.py supplies the schema-7 oracle.
    oracle = project.run_coverage_py(
        "run", "--branch", "--source=src", "--data-file=.coverage.oracle", "-m", "pytest", "-v"
    )
    assert oracle.returncode == 0, oracle
    oracle_data = evidence(project, ".coverage.oracle")
    assert {k: (v["lines"], v["arcs"]) for k, v in selected_data.items()} == {
        k: (v["lines"], v["arcs"]) for k, v in oracle_data.items()
    }
    # vfy's full run additionally proves per-test contexts survive replay.
    commands(project, SELECTION, "baseline")
    assert (project.root / ".coverage.oracle").is_file(), (
        "the native run must not consume an independent Coverage.py data file"
    )
    assert evidence(project) == selected_data
    _, fallback = commands(project, SELECTION, "fallback")
    assert passed_nodes(fallback) == {
        "tests/test_bounds.py::test_positive_value",
        "tests/test_formatting.py::test_format_count",
    }
    assert "no passing baseline" in fallback.stdout + fallback.stderr
    commands(project, SELECTION, "refresh")


def test_documented_api_imports_and_collection_round_trip(project: VfyProject) -> None:
    imports = project.run_python("-c", block(API, "public-imports"))
    assert imports.returncode == 0, imports
    project.write("api_example.py", block(API, "api-collection"))
    result = project.run_python("api_example.py")
    assert result.returncode == 0, result
    assert result.stdout.strip() == "[1, 4, 5, 6, 7]"
    # Read the documented vfy file with the pinned Coverage.py reader too.
    assert project.lines("src/bounds.py", ".coverage.api") == [1, 4, 5, 6, 7]


@requires_native_test_engine
def test_mutation_guide_turns_a_survivor_into_a_protected_boundary(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", block(MUTATION, "mutation-config"))
    source = block(MUTATION, "mutation-source")
    project.write("src/sign.py", source)
    project.write("tests/test_sign.py", block(MUTATION, "mutation-weak-tests"))
    baseline = commands(project, MUTATION, "mutation-baseline")[0]
    assert "2 passed" in baseline.stdout
    assert "100%" in commands(project, MUTATION, "mutation-coverage")[0].stdout
    before = evidence(project)["src/sign.py"]
    weak_node = "tests/test_sign.py::test_zero_has_unit_magnitude"
    assert f"{weak_node}|run" in before["contexts"][6]

    commands(project, MUTATION, "mutation-focused", status=2)
    report = json.loads((project.root / "mutations.json").read_text())
    assert report["schema"] == "vfy.mutation"
    assert report["summary"] == {"survived": 1}
    assert len(report["mutations"]) == 1
    survivor = report["mutations"][0]
    assert Path(survivor["path"]) == project.root / "src/sign.py"
    assert (
        survivor["line"],
        survivor["operator"],
        survivor["original"],
        survivor["replacement"],
    ) == (5, "comparison_boundary", ">=", ">")
    assert survivor["status"] == "survived"
    assert survivor["killer"] is None
    selected = ["tests/test_sign.py::test_negative", weak_node]
    if sys.platform == "darwin":
        assert sorted(survivor["tests"]) == sorted(selected)
    else:
        assert survivor["tests"] == selected
    assert report["metrics"]["fast_results"] == 1
    assert (project.root / "src/sign.py").read_text() == source

    project.write("tests/test_sign.py", block(MUTATION, "mutation-strong-tests"))
    refreshed = commands(project, MUTATION, "mutation-refresh")[0]
    assert "2 passed" in refreshed.stdout
    after = evidence(project)["src/sign.py"]
    boundary_node = "tests/test_sign.py::test_zero_is_nonnegative"
    assert f"{boundary_node}|run" in after["contexts"][6]
    assert all(f"{weak_node}|run" not in contexts for contexts in after["contexts"].values())
    assert (after["lines"], after["arcs"]) == (before["lines"], before["arcs"])
    assert "100%" in commands(project, MUTATION, "mutation-coverage")[0].stdout

    commands(project, MUTATION, "mutation-protected")
    protected_report = json.loads((project.root / "mutations.json").read_text())
    assert protected_report["summary"] == {"protected": 1}
    assert len(protected_report["mutations"]) == 1
    protected = protected_report["mutations"][0]
    assert protected["id"] == survivor["id"]
    assert protected["status"] == "protected"
    selected = ["tests/test_sign.py::test_negative", boundary_node]
    if sys.platform == "darwin":
        assert sorted(protected["tests"]) == sorted(selected)
    else:
        assert protected["tests"] == selected
    assert protected_report["metrics"]["fast_results"] == 1
    assert protected["killer"] == boundary_node
    assert protected["returncode"] == 1
    assert (project.root / "src/sign.py").read_text() == source


def workflow_steps(name: str) -> list[list[str]]:
    """Read the recipes' inline commands, rejecting unsupported run syntax."""
    source = block("guides/ci.md", name)
    runs = re.findall(r"^\s+(?:- )?run: (.+)$", source, re.MULTILINE)
    steps = []
    for command in runs:
        if command in {
            "uv sync --locked",
            "rustup default 1.95.0",
        }:
            continue  # The test environment already contains the build under review.
        args = shlex.split(command)
        assert args[:2] == ["uv", "run"], (name, command)
        assert args[2] in {"vfy", "pytest"}, (name, command)
        steps.append(args[2:])
    return steps


def run_step(project: VfyProject, arguments: list[str], *, status: int = 0) -> None:
    if arguments[0] == "pytest":
        result = project.run_pytest(*arguments[1:], load_xdist=False)
    else:
        assert arguments[0] == "vfy"
        result = project.run_vfy(*arguments[1:])
    assert result.returncode == status, (arguments, result)


@requires_native_test_engine
def test_ci_recipes_report_the_same_complete_dataset(project: VfyProject) -> None:
    (test,) = workflow_steps("ci-full")
    run_step(project, test, status=1)
    assert (project.root / "coverage.xml").is_file()

    project.write("tests/test_bounds.py", block(TUTORIAL, "complete-tests"))
    run_step(project, test)
    full = evidence(project)
    collect_shard, combine, export, gate = workflow_steps("ci-shards")
    for test_file in ("tests/test_bounds.py", "tests/test_formatting.py"):
        variables = {
            "$TEST_FILE": test_file,
            "$COVERAGE_SHARD": f".coverage.{Path(test_file).stem}",
        }
        run_step(project, [variables.get(arg, arg) for arg in collect_shard])
    shards = project.root / "shards"
    shards.mkdir()
    raw_shards = list(project.root.glob(".coverage.*"))
    assert len(raw_shards) == 2
    for shard in raw_shards:
        shutil.move(shard, shards / shard.name)
    # Avoid letting the full-run database hide missing combination output.
    (project.root / ".coverage").unlink()
    for step in (combine, export, gate):
        run_step(project, step)
    combined = evidence(project)
    assert {k: (v["lines"], v["arcs"]) for k, v in combined.items()} == {
        k: (v["lines"], v["arcs"]) for k, v in full.items()
    }
    assert len(list(shards.glob(".coverage.*"))) == 2
    xml = ET.parse(project.root / "coverage.xml").getroot()
    assert float(xml.attrib["line-rate"]) == 1
    assert float(xml.attrib["branch-rate"]) == 1


@requires_native_test_engine
def test_ci_recipe_preserves_reports_after_a_test_failure(project: VfyProject) -> None:
    source = block(TUTORIAL, "complete-tests").replace("clamp_low(-1) == 0", "clamp_low(-1) == 99")
    project.write("tests/test_bounds.py", source)
    (test,) = workflow_steps("ci-full")
    run_step(project, test, status=1)
    assert project.data().has_arcs()
    xml = ET.parse(project.root / "coverage.xml").getroot()
    assert float(xml.attrib["line-rate"]) == 1
    assert float(xml.attrib["branch-rate"]) == 1


def test_ci_pytest_recipe_gates_and_preserves_reports(project: VfyProject) -> None:
    collect, export, gate = workflow_steps("ci-pytest")
    run_step(project, collect)
    run_step(project, gate, status=2)

    source = block(TUTORIAL, "complete-tests").replace("clamp_low(-1) == 0", "clamp_low(-1) == 99")
    project.write("tests/test_bounds.py", source)
    run_step(project, collect, status=1)
    for step in (export, gate):
        run_step(project, step)
    assert project.data().has_arcs()
    xml = ET.parse(project.root / "coverage.xml").getroot()
    assert float(xml.attrib["line-rate"]) == 1
    assert float(xml.attrib["branch-rate"]) == 1


@requires_native_test_engine
def test_pr_mutation_recipe_uses_fetched_committed_changes(project: VfyProject) -> None:
    project.git_init()
    remote = project.root.parent / "mutation-origin.git"
    clone = project.run("git", "clone", "--bare", str(project.root), str(remote))
    assert clone.returncode == 0, clone
    branch = project.run("git", "--git-dir", str(remote), "branch", "-f", "main", "HEAD")
    assert branch.returncode == 0, branch
    project.run("git", "remote", "add", "origin", str(remote))
    bounds = project.root / "src/bounds.py"
    project.write("src/bounds.py", bounds.read_text() + "\n# Review of lower bound.\n")
    project.git_commit("Review lower bound")
    commands(project, MUTATION, "mutation-pr-fetch")
    commands(project, MUTATION, "mutation-baseline")
    commands(project, MUTATION, "mutation-pr-run", status=2)
    report = json.loads((project.root / "build/mutations.json").read_text())
    assert report["scope"]["requested_base"] == "origin/main"
    assert report["scope"]["selected_files"] == [str(bounds.resolve())]
    assert report["summary"]["survived"] > 0
