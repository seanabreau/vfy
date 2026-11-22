from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _job_properties(text: str) -> dict[str, set[str]]:
    """Return the top-level property names of every job in a workflow file."""
    jobs: dict[str, set[str]] = {}
    in_jobs = False
    current: str | None = None
    for line in text.splitlines():
        if re.match(r"^\S", line):
            in_jobs = line.startswith("jobs:")
            current = None
        elif in_jobs and (job := re.match(r"^  ([\w-]+):\s*(?:#.*)?$", line)):
            current = str(job.group(1))
            jobs[current] = set()
        elif in_jobs and current and (prop := re.match(r"^    ([\w-]+):", line)):
            jobs[current].add(prop.group(1))
    return jobs


def test_every_workflow_job_has_a_timeout() -> None:
    missing = [
        f"{path.name}:{job}"
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job, properties in _job_properties(path.read_text(encoding="utf-8")).items()
        # A job that calls a reusable workflow cannot set timeout-minutes.
        if "uses" not in properties and "timeout-minutes" not in properties
    ]
    assert missing == []


def test_job_scanner_reads_top_level_job_properties() -> None:
    text = (
        "name: x\njobs:\n  a:\n    runs-on: u\n    timeout-minutes: 5\n"
        "    steps:\n      - run: y\n  b:\n    uses: ./w.yml\n"
    )
    assert _job_properties(text) == {
        "a": {"runs-on", "timeout-minutes", "steps"},
        "b": {"uses"},
    }


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_workflow_files_define_jobs(path: Path) -> None:
    assert _job_properties(path.read_text(encoding="utf-8"))
