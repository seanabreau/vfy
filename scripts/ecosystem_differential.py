"""Compare Rust collection with Coverage.py on pinned ecosystem smoke cases.

The scheduled smoke check uses pinned cases. It supplements the repository's
boundary tests and checks against newer upstream revisions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from coverage import CoverageData


@dataclass(frozen=True)
class EcosystemCase:
    name: str
    requirement: str
    source: str


CASES = (
    EcosystemCase(
        "attrs",
        "attrs==25.4.0",
        """\
import attrs

@attrs.define
class Settings:
    name = attrs.field()
    retries = attrs.field(default=2)

def exercise() -> dict[str, object]:
    return attrs.asdict(Settings("demo"))
""",
    ),
    EcosystemCase(
        "click",
        "click==8.3.1",
        """\
import click
from click.testing import CliRunner

@click.command()
@click.option("--count", default=1, type=int)
def greet(count: int) -> None:
    for _ in range(count):
        click.echo("hello")

def exercise() -> str:
    result = CliRunner().invoke(greet, ["--count", "2"])
    assert result.exit_code == 0
    return result.output
""",
    ),
    EcosystemCase(
        "httpx",
        "httpx==0.28.1",
        """\
import httpx

def exercise() -> tuple[str, str]:
    request = httpx.Request("GET", "https://example.test/items?page=1")
    return request.method, request.url.host
""",
    ),
)


def run_checked(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False, timeout=120
    )
    if completed.returncode:
        details = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(f"{subprocess.list2cmdline(command)} failed:\n{details}")


class FileEvidence(TypedDict):
    lines: list[int]
    arcs: list[tuple[int, int]]
    contexts: dict[str, list[str]]


class NormalizedData(TypedDict):
    has_arcs: bool
    files: dict[str, FileEvidence]
    contexts: list[str]


def normalized_data(path: Path) -> NormalizedData:

    data = CoverageData(str(path))
    data.read()
    files: dict[str, FileEvidence] = {}
    for filename in sorted(data.measured_files()):
        lines = sorted(data.lines(filename) or ())
        arcs = sorted(tuple(arc) for arc in (data.arcs(filename) or ()))
        by_line = {
            str(line): sorted(contexts)
            for line, contexts in sorted(data.contexts_by_lineno(filename).items())
        }
        files[filename.replace("\\", "/")] = {
            "lines": lines,
            "arcs": arcs,
            "contexts": by_line,
        }
    return {
        "has_arcs": data.has_arcs(),
        "files": files,
        "contexts": sorted(data.measured_contexts()),
    }


def normalized_totals(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise RuntimeError(f"report {path} has no totals object")
    return {str(key): value for key, value in totals.items()}


def compare_case(case: EcosystemCase) -> None:
    with tempfile.TemporaryDirectory(prefix=f"vfy-ecosystem-{case.name}-") as temporary:
        root = Path(temporary)
        source = root / "app.py"
        runner = root / "run_case.py"
        source.write_text(case.source, encoding="utf-8")
        runner.write_text("from app import exercise\nexercise()\n", encoding="utf-8")
        reference_data = root / "coverage.py.sqlite"
        vfy_data = root / "vfy.sqlite"
        context = f"ecosystem:{case.name}"
        common = [
            "--branch",
            "--context",
            context,
            "--source",
            str(root),
        ]
        run_checked(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                *common,
                "--data-file",
                str(reference_data),
                str(runner),
            ],
            cwd=root,
        )
        run_checked(
            [
                sys.executable,
                "-m",
                "vfy",
                "coverage",
                "run",
                *common,
                "--data-file",
                str(vfy_data),
                str(runner),
            ],
            cwd=root,
        )

        reference = normalized_data(reference_data)
        vfy = normalized_data(vfy_data)
        if reference != vfy:
            raise AssertionError(
                f"{case.name}: normalized data differs\n"
                f"Coverage.py: {json.dumps(reference, indent=2, sort_keys=True)}\n"
                f"vfy: {json.dumps(vfy, indent=2, sort_keys=True)}"
            )

        reference_report = root / "coverage-report.json"
        vfy_report = root / "vfy-report.json"
        run_checked(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                "--data-file",
                str(reference_data),
                "-o",
                str(reference_report),
            ],
            cwd=root,
        )
        run_checked(
            [
                sys.executable,
                "-m",
                "vfy",
                "coverage",
                "json",
                "--data-file",
                str(vfy_data),
                "--output",
                str(vfy_report),
            ],
            cwd=root,
        )
        if normalized_totals(reference_report) != normalized_totals(vfy_report):
            raise AssertionError(
                f"{case.name}: report totals differ\n"
                f"Coverage.py: {normalized_totals(reference_report)}\n"
                f"vfy: {normalized_totals(vfy_report)}"
            )
        print(f"ok: {case.name} ({case.requirement})")


def main() -> int:
    failures: list[str] = []
    for case in CASES:
        try:
            compare_case(case)
        except (AssertionError, RuntimeError) as error:
            failures.append(f"{case.name}: {error}")
            print(f"FAIL: {case.name}: {error}", file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
