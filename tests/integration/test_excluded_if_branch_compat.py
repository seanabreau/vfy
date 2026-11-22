from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest
from coverage import Coverage as CoveragePy

from tests.support.reference import reference_coverage
from vfy import Coverage


class BranchSummary(TypedDict):
    num_branches: int
    covered_branches: int
    num_partial_branches: int


class BranchReport(TypedDict):
    executed_branches: list[list[int]]
    missing_branches: list[list[int]]
    summary: BranchSummary


def _json_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    result: dict[str, object] = {}
    for key, item in value.items():
        assert isinstance(key, str)
        result[key] = item
    return result


def _write_source(path: Path, calls: str = "") -> str:
    source = (
        "def noop():\n"
        "    pass\n"
        "\n"
        "def choose(platform, flag):\n"
        "    if platform == 'jupyter':  # pragma: no cover\n"
        "        noop()\n"
        "    elif platform == 'terminal':\n"
        "        if flag:\n"
        "            noop()\n"
        "        else:\n"
        "            noop()\n"
        "    else:\n"
        "        if flag:\n"
        "            noop()\n"
        "        else:\n"
        "            noop()\n"
        "\n"
        f"{calls}"
    )
    path.write_text(source, encoding="utf-8")
    return source


def _branch_arcs(value: object) -> list[list[int]]:
    assert isinstance(value, list)
    arcs: list[list[int]] = []
    for arc in value:
        assert isinstance(arc, list)
        assert len(arc) == 2
        start, end = arc
        assert isinstance(start, int)
        assert not isinstance(start, bool)
        assert isinstance(end, int)
        assert not isinstance(end, bool)
        arcs.append([start, end])
    return arcs


def _branch_count(summary: dict[str, object], name: str) -> int:
    value = summary.get(name)
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    return value


def _json_report(
    measured: Coverage | CoveragePy, source: str, filename: str, output: Path
) -> BranchReport:
    measured.start()
    try:
        exec(compile(source, filename, "exec"), {})
    finally:
        measured.stop()
    measured.json_report(morfs=[filename], outfile=str(output))
    payload: object = json.loads(output.read_text(encoding="utf-8"))
    payload_object = _json_object(payload)
    files = _json_object(payload_object.get("files"))
    assert len(files) == 1
    report = _json_object(next(iter(files.values())))
    summary = _json_object(report.get("summary"))
    return {
        "executed_branches": _branch_arcs(report.get("executed_branches")),
        "missing_branches": _branch_arcs(report.get("missing_branches")),
        "summary": {
            "num_branches": _branch_count(summary, "num_branches"),
            "covered_branches": _branch_count(summary, "covered_branches"),
            "num_partial_branches": _branch_count(summary, "num_partial_branches"),
        },
    }


@pytest.mark.parametrize(
    ("calls", "covered_branches", "partial_branches"),
    [
        (
            "choose('terminal', True)\n"
            "choose('terminal', False)\n"
            "choose('other', True)\n"
            "choose('other', False)\n",
            6,
            0,
        ),
        ("choose('terminal', True)\n", 2, 2),
        ("", 0, 0),
    ],
    ids=["all-alternates-covered", "partial-alternate-coverage", "unexecuted-alternates"],
)
def test_excluded_if_alternate_branch_reports_match_coverage_py(
    tmp_path: Path, calls: str, covered_branches: int, partial_branches: int
) -> None:
    source = tmp_path / "excluded_if_report.py"
    source_text = _write_source(source, calls)
    filename = str(source.resolve())

    with reference_coverage(branch=True, config_file=False) as reference:
        expected = _json_report(reference, source_text, filename, tmp_path / "reference.json")

    actual_coverage = Coverage(data_file=None, branch=True, config_file=False)
    try:
        actual = _json_report(actual_coverage, source_text, filename, tmp_path / "vfy.json")
    finally:
        actual_coverage.get_data().close(force=True)

    assert actual["executed_branches"] == expected["executed_branches"]
    assert actual["missing_branches"] == expected["missing_branches"]
    for field in ("num_branches", "covered_branches", "num_partial_branches"):
        assert actual["summary"][field] == expected["summary"][field], field
    assert actual["summary"]["num_branches"] == 6
    assert actual["summary"]["covered_branches"] == covered_branches
    assert actual["summary"]["num_partial_branches"] == partial_branches
