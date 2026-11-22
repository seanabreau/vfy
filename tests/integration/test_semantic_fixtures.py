"""Compare Markdown fixtures with collected semantic decisions and observations."""

from __future__ import annotations

import json
import re
import runpy
import sys
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import pytest

from vfy import Coverage


class Definition(TypedDict):
    kind: str
    line: int
    expression: str
    logic: str
    condition_ranges: list[list[int]]


class Observation(TypedDict):
    kind: str
    line: int
    expression: str
    vector: str
    outcome: bool


class PathOutcome(TypedDict):
    label: str
    range: list[int]


class PathDefinition(TypedDict):
    kind: str
    line: int
    expression: str
    outcomes: list[PathOutcome]


class PathObservation(TypedDict):
    kind: str
    line: int
    outcome: int


class SemanticExpectation(TypedDict):
    definitions: list[Definition]
    observations: list[Observation]
    path_definitions: NotRequired[list[PathDefinition]]
    path_observations: NotRequired[list[PathObservation]]


pytestmark = pytest.mark.skipif(
    not hasattr(sys, "monitoring"),
    reason="semantic coverage requires CPython 3.12+ monitoring",
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "semantic"


def _write_text(path: Path, text: str) -> None:
    """Write fixture source with stable LF bytes on every platform."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _block(markdown: str, language: str) -> str:
    blocks = re.findall(rf"```{language}\n(.*?)```", markdown, flags=re.DOTALL)
    if len(blocks) != 1:
        raise AssertionError(
            f"fixture must contain exactly one {language!r} block (found {len(blocks)})"
        )
    block = blocks[0]
    assert isinstance(block, str)
    return block


def _expected(path: Path) -> SemanticExpectation:
    # Reviewed fixture JSON is checked against the independently collected model below.
    return cast(SemanticExpectation, json.loads(_block(path.read_text(encoding="utf-8"), "json")))


def _collect(tmp_path: Path, source: str) -> Coverage:
    filename = tmp_path / "fixture.py"
    _write_text(filename, source)
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"),
        source=[str(tmp_path)],
        decisions=True,
    )
    coverage.start()
    try:
        runpy.run_path(str(filename), run_name="__main__")
    finally:
        coverage.stop()
        coverage.save()
    reader = Coverage(data_file=str(tmp_path / ".coverage"), decisions=True, config_file=False)
    reader.load()
    return reader


def _normalized(store: Coverage) -> SemanticExpectation:
    data = store.get_decision_data()
    by_key = {definition.key: definition for definition in data.definitions}
    path_by_key = {definition.key: definition for definition in data.path_definitions}
    definitions: list[Definition] = [
        {
            "kind": definition.kind,
            "line": definition.line,
            "expression": definition.expression,
            "logic": definition.logic,
            "condition_ranges": [
                [item.start_line, item.start_column, item.end_line, item.end_column]
                for item in definition.condition_ranges
            ],
        }
        for definition in data.definitions
    ]
    observations: list[Observation] = [
        {
            "kind": by_key[observation.decision].kind,
            "line": by_key[observation.decision].line,
            "expression": by_key[observation.decision].expression,
            "vector": observation.vector,
            "outcome": observation.outcome,
        }
        for observation in data.observations
    ]
    definitions.sort(key=lambda value: (value["line"], value["kind"], value["expression"]))
    observations.sort(
        key=lambda value: (
            value["line"],
            value["kind"],
            value["expression"],
            value["vector"],
            value["outcome"],
        )
    )
    path_definitions: list[PathDefinition] = [
        {
            "kind": definition.kind,
            "line": definition.line,
            "expression": definition.expression,
            "outcomes": [
                {
                    "label": outcome.label,
                    "range": [
                        outcome.start_line,
                        outcome.start_column,
                        outcome.end_line,
                        outcome.end_column,
                    ],
                }
                for outcome in definition.outcomes
            ],
        }
        for definition in data.path_definitions
    ]
    path_observations: list[PathObservation] = [
        {
            "kind": path_by_key[observation.decision].kind,
            "line": path_by_key[observation.decision].line,
            "outcome": observation.outcome,
        }
        for observation in data.path_observations
    ]
    path_definitions.sort(key=lambda value: (value["line"], value["kind"]))
    path_observations.sort(key=lambda value: (value["line"], value["kind"], value["outcome"]))
    return {
        "definitions": definitions,
        "observations": observations,
        "path_definitions": path_definitions,
        "path_observations": path_observations,
    }


def _canonical(payload: SemanticExpectation) -> SemanticExpectation:
    definitions = sorted(
        payload["definitions"],
        key=lambda value: (value["line"], value["kind"], value["expression"]),
    )
    observations = sorted(
        payload["observations"],
        key=lambda value: (
            value["line"],
            value["kind"],
            value["expression"],
            value["vector"],
            value["outcome"],
        ),
    )
    path_definitions = sorted(
        payload.get("path_definitions", []),
        key=lambda value: (value["line"], value["kind"]),
    )
    path_observations = sorted(
        payload.get("path_observations", []),
        key=lambda value: (value["line"], value["kind"], value["outcome"]),
    )
    return {
        "definitions": definitions,
        "observations": observations,
        "path_definitions": path_definitions,
        "path_observations": path_observations,
    }


@pytest.mark.parametrize(
    "fixture",
    sorted(FIXTURES.glob("*.md")),
    ids=lambda path: path.stem,
)
def test_literate_fixture_matches_reviewed_truth_table(fixture: Path, tmp_path: Path) -> None:
    source = _block(fixture.read_text(encoding="utf-8"), "python")
    assert _normalized(_collect(tmp_path, source)) == _canonical(_expected(fixture))
