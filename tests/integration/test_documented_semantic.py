"""Execute the semantic guide's missing-input example through stock pytest."""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration.test_documentation import block, commands
from tests.support.vfy_project import VfyProject

PAGE = "guides/semantic.md"


def test_semantic_tutorial_closes_the_missing_independent_effect(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("bounds.py", block(PAGE, "semantic-source"))
    project.write("test_bounds.py", block(PAGE, "semantic-tests"))
    initial = commands(project, PAGE, "semantic-collect")[0]
    assert "2 passed" in initial.stdout
    coverage = project.run_vfy("coverage", "json", "-o", "coverage.json")
    assert coverage.returncode == 0, coverage
    totals = json.loads((tmp_path / "coverage.json").read_text())["totals"]
    assert (totals["covered_lines"], totals["num_statements"]) == (4, 4)
    assert (totals["covered_branches"], totals["num_branches"]) == (2, 2)
    missing = commands(project, PAGE, "semantic-fail", status=2)[0]

    report = project.run_vfy("coverage", "check", "--format", "json")
    assert report.returncode == 0, report
    payload = json.loads(report.stdout)
    assert payload["totals"] == {
        "decisions": {"covered": 2, "total": 2},
        "conditions": {"covered": 3, "total": 4},
        "mcdc": {"covered": 1, "total": 2},
        "percent": {"decisions": 100.0, "conditions": 75.0, "mcdc": 50.0},
    }
    assert len(payload["files"]) == 1
    row = payload["files"][0]
    assert row["expression"] == "x >= 0 and x < 10"
    assert {(v["vector"], v["outcome"]) for v in row["vectors"]} == {
        ("0-", False),
        ("11", True),
    }
    assert row["missing"] == ["condition 2=false", "condition 2 lacks independent effect"]
    assert "missing: 'x < 10' must be false (2:19)" in missing.stdout
    assert "missing: 'x < 10' needs an independent effect pair (2:19)" in missing.stdout

    project.write(
        "test_bounds.py",
        block(PAGE, "semantic-tests") + block(PAGE, "semantic-missing-test"),
    )
    collection, gate, explanation = commands(project, PAGE, "semantic-complete")
    assert "3 passed" in collection.stdout
    assert "MC/DC: 100.0%" in gate.stdout
    assert "test_bounds.py::test_at_upper_bound" in explanation.stdout
    report = project.run_vfy("coverage", "check", "--format", "json")
    assert report.returncode == 0, report
    payload = json.loads(report.stdout)
    assert payload["totals"] == {
        "decisions": {"covered": 2, "total": 2},
        "conditions": {"covered": 4, "total": 4},
        "mcdc": {"covered": 2, "total": 2},
        "percent": {"decisions": 100.0, "conditions": 100.0, "mcdc": 100.0},
    }
    assert {(v["vector"], v["outcome"]) for v in payload["files"][0]["vectors"]} == {
        ("0-", False),
        ("10", False),
        ("11", True),
    }
    assert payload["files"][0]["missing"] == []
    pairs = project.run_vfy("explain", "bounds.py:2", "--json")
    assert pairs.returncode == 0, pairs
    assert {
        (pair["condition"], frozenset((pair["left_vector"], pair["right_vector"])))
        for pair in json.loads(pairs.stdout)["semantic_evidence"]
    } == {(1, frozenset(("0-", "11"))), (2, frozenset(("10", "11")))}

    # Complete condition coverage still leaves the lower equality untested.
    project.write("bounds.py", block(PAGE, "semantic-source").replace(">=", ">"))
    mutation = project.run_pytest("test_bounds.py", load_xdist=False)
    assert mutation.returncode == 0, mutation
    assert "3 passed" in mutation.stdout
    project.write(
        "test_bounds.py",
        block(PAGE, "semantic-tests")
        + block(PAGE, "semantic-missing-test")
        + block(PAGE, "semantic-boundary-test"),
    )
    boundary = project.run_pytest("test_bounds.py", load_xdist=False)
    assert boundary.returncode == 1, boundary
    assert "1 failed, 3 passed" in boundary.stdout
    project.write("bounds.py", block(PAGE, "semantic-source"))
    original = project.run_pytest("test_bounds.py", load_xdist=False)
    assert original.returncode == 0, original
    assert "4 passed" in original.stdout
