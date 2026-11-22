"""Read persisted history independently of the production storage reader."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import NotRequired, TypedDict, cast


class Observation(TypedDict):
    run_id: str
    attempt: str
    outcome: str
    duration_ms: float
    failed_phase: NotRequired[str]


class Entry(TypedDict):
    duration_ms: float
    observations: list[Observation]


class History(TypedDict):
    tests: dict[str, Entry]


def read_history(path: Path) -> History:
    """Inspect ordinary test histories without creating a missing database."""
    if path.suffix != ".sqlite3":
        return cast(History, json.loads(path.read_text(encoding="utf-8")))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        tests: dict[str, Entry] = {}
        for test_id, nodeid, duration, present, negative_zero in database.execute(
            "SELECT id,nodeid,duration_ms,observations_present,duration_negative_zero "
            "FROM tests ORDER BY nodeid"
        ):
            entry = cast(Entry, {})
            if duration is not None:
                entry["duration_ms"] = -0.0 if negative_zero else duration
            if present:
                observations = []
                for (
                    run_id,
                    attempt,
                    outcome,
                    phase,
                    elapsed,
                    observation_negative_zero,
                ) in database.execute(
                    "SELECT runs.run_id,attempt,outcome,failed_phase,duration_ms, "
                    "duration_negative_zero "
                    "FROM observations LEFT JOIN runs ON runs.id=observations.run_id "
                    "WHERE test_id=? ORDER BY position",
                    (test_id,),
                ):
                    observation: Observation = {
                        "run_id": run_id,
                        "attempt": ("primary", "confirmation")[attempt],
                        "outcome": ("passed", "failed", "skipped")[outcome],
                        "duration_ms": -0.0 if observation_negative_zero else elapsed,
                    }
                    if phase is not None:
                        observation["failed_phase"] = ("setup", "call", "teardown")[phase]
                    observations.append(observation)
                entry["observations"] = observations
            tests[nodeid] = entry
        return {"tests": tests}
