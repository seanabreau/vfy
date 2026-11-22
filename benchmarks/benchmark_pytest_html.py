"""Compare native CLI and direct-pytest HTML reports after validating their contents."""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
import tempfile
from html.parser import HTMLParser
from importlib.metadata import version
from pathlib import Path
from typing import override

from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    rotated_orders,
    sampling_config,
    summarize,
    timed_process,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    hardware_details,
    require_clean,
    require_revision,
    require_unchanged_artifact,
    source_state,
)
from vfy.native import build_profile

TOOLS = ("plain", "pytest-html", "vfy-plugin", "vfy-cli")
WORKLOADS = ("self-contained", "assets", "streaming")
EVIDENCE = re.compile(r"evidence-\d+")


class Report(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], list[str]]] = []
        self.blob: str | None = None
        self._row_links: list[str] = []
        self._row: list[str] = []
        self._cell: list[str] | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("data-jsonblob") is not None:
            self.blob = attributes["data-jsonblob"]
        if tag == "a" and (href := attributes.get("href")):
            self._row_links.append(href)
        if tag == "tr":
            self._row = []
            self._row_links = []
        elif tag in {"td", "th"}:
            self._cell = []

    @override
    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr":
            self.rows.append((self._row, self._row_links))


def report_contents(path: Path) -> dict[str, tuple[str, list[str]]]:
    html = path.read_text(encoding="utf-8")
    report = Report()
    report.feed(html)
    outcomes: dict[str, tuple[str, list[str]]] = {}
    if report.blob is not None:
        data = json.loads(report.blob)
        if data["environment"].get("Region") != "public-region":
            raise ValueError("report lost configured metadata")
        for nodeid, rows in data["tests"].items():
            if len(rows) != 1:
                raise ValueError(f"unexpected result rows for {nodeid}")
            evidence: list[str] = []
            for extra in rows[0]["extras"]:
                content = extra["content"]
                if content.startswith("data:"):
                    content = base64.b64decode(content.partition(",")[2]).decode("utf-8")
                else:
                    content = (path.parent / content).read_text(encoding="utf-8")
                evidence.extend(EVIDENCE.findall(content))
            outcomes[nodeid] = (rows[0]["result"], evidence)
    else:
        if not any(row == ["Region", "public-region"] for row, _ in report.rows):
            raise ValueError("report lost configured metadata")
        for row, links in report.rows:
            if len(row) >= 4 and row[0] in {"Passed", "Failed", "Skipped", "XFailed"}:
                if row[1] in outcomes:
                    raise ValueError(f"duplicate result row: {row[1]}")
                evidence = EVIDENCE.findall(row[3])
                for link in links:
                    if link.endswith(".txt"):
                        evidence.extend(
                            EVIDENCE.findall((path.parent / link).read_text(encoding="utf-8"))
                        )
                outcomes[row[1]] = (row[0], evidence)
    if "secret-value" in html:
        raise ValueError("report leaked redacted metadata")
    if not outcomes:
        raise ValueError("report contains no test results")
    return outcomes


def create_fixture(root: Path, tool: str, count: int) -> None:
    root.mkdir(parents=True)
    helper = "pytest_html" if tool == "pytest-html" else "vfy.pytest_html"
    root.joinpath("conftest.py").write_text(
        "import pytest\n"
        + (f"from {helper} import extras as html_extras\n" if tool != "plain" else "")
        + "@pytest.fixture\ndef evidence(request):\n"
        + (
            "    extras = request.getfixturevalue('extras')\n"
            "    return lambda value: extras.append(html_extras.text(f'evidence-{value}'))\n"
            if tool != "plain"
            else "    return lambda value: None\n"
        ),
        encoding="utf-8",
    )
    root.joinpath("pytest.ini").write_text(
        "[pytest]\nenvironment_table_redact_list = ^TOKEN$\n", encoding="utf-8"
    )
    root.joinpath("test_cases.py").write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('value', range({count}))\n"
        "def test_value(value, evidence):\n"
        "    evidence(value)\n"
        "    assert value >= 0\n"
        "def test_fail(): assert False, 'expected failure'\n"
        "@pytest.mark.skip(reason='expected skip')\ndef test_skip(): pass\n"
        "@pytest.mark.xfail(reason='expected xfail')\ndef test_xfail(): assert False\n",
        encoding="utf-8",
    )


def command(tool: str, workload: str) -> list[str]:
    args = [sys.executable, "-m"]
    if tool == "vfy-cli":
        args += ["vfy", "test", "--engine=pytest-native", "--all", "-j", "1", "--no-cov"]
    else:
        args += ["pytest"]
        plugins = {
            "plain": (),
            "pytest-html": ("pytest_html.plugin", "pytest_html.fixtures", "pytest_metadata.plugin"),
            "vfy-plugin": ("vfy.pytest_plugin", "vfy.pytest_reporting"),
        }[tool]
        for plugin in plugins:
            args += ["-p", plugin]
    args += ["-q", "--tb=auto"]
    if tool != "plain":
        args += [
            "--html=reports/results.html",
            "--metadata",
            "Region",
            "public-region",
            "--metadata",
            "TOKEN",
            "secret-value",
        ]
        if workload != "assets":
            args += ["--self-contained-html"]
        if workload == "streaming":
            args += ["-o", "generate_report_on_test=true"]
    return args


def environment(root: Path) -> dict[str, str]:
    return benchmark_environment({"HOME": str(root / ".home")})


def run(args: list[str], root: Path, count: int) -> float:
    for name in ("reports", ".vfy"):
        if (root / name).exists():
            shutil.rmtree(root / name)
    elapsed, result = timed_process(args, cwd=root, env=environment(root), timeout=120)
    expected = (f"{count} passed", "1 failed", "1 skipped", "1 xfailed")
    if result.returncode != 1 or any(value not in result.stdout for value in expected):
        raise ValueError(
            f"unexpected test result: {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
    return elapsed


def validate_report(root: Path, count: int) -> None:
    expected: dict[str, tuple[str, list[str]]] = {
        f"test_cases.py::test_value[{index}]": ("Passed", [f"evidence-{index}"])
        for index in range(count)
    }
    expected.update(
        {
            "test_cases.py::test_fail": ("Failed", []),
            "test_cases.py::test_skip": ("Skipped", []),
            "test_cases.py::test_xfail": ("XFailed", []),
        }
    )
    actual = report_contents(root / "reports/results.html")
    if actual != expected:
        raise ValueError(f"HTML result or attachment parity failed in {root}")


def main() -> None:  # noqa: C901 - one linear parity and sampling protocol
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=int, default=1000)
    parser.add_argument("--streaming-tests", type=int, default=100)
    add_sampling_arguments(parser)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--diagnostic", action="store_true", help="allow dirty source and shorter samples"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.tests, args.streaming_tests) < 1:
        parser.error("test counts and runs must be positive; warmups must be nonnegative")
    if args.diagnostic:
        args.mode = "diagnostic"
    sampling = sampling_config(args)
    repository = Path(__file__).resolve().parents[1]
    publication = sampling.publishable and not args.validate_only
    artifact_before = artifact_provenance(strict=True) if publication else None
    before = require_clean(repository) if publication else source_state(repository)
    if not args.validate_only and publication and build_profile() != "release":
        parser.error("measurements require clean release source, three warmups, and fifteen runs")
    for package, pinned in (("pytest-html", "4.2.0"), ("pytest-metadata", "3.1.1")):
        if version(package) != pinned:
            parser.error(f"expected {package} {pinned}")
    records: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="vfy-html-benchmark-") as temporary:
        root = Path(temporary)
        for workload in WORKLOADS:
            count = args.streaming_tests if workload == "streaming" else args.tests
            roots = {tool: root / workload / tool for tool in TOOLS}
            commands = {tool: command(tool, workload) for tool in TOOLS}
            print(
                f"{workload}: checking {count + 3} outcomes, metadata, and attachments",
                file=sys.stderr,
                flush=True,
            )
            for tool in TOOLS:
                create_fixture(roots[tool], tool, count)
                run(commands[tool], roots[tool], count)
                if tool != "plain":
                    validate_report(roots[tool], count)
            samples: dict[str, list[float]] = {tool: [] for tool in TOOLS}
            if not args.validate_only:
                for iteration, order in enumerate(
                    rotated_orders(TOOLS, sampling.warmups + sampling.runs)
                ):
                    phase = "warmup" if iteration < sampling.warmups else "sample"
                    print(
                        f"{workload}: {phase} {iteration + 1}",
                        file=sys.stderr,
                        flush=True,
                    )
                    for tool in order:
                        elapsed = run(commands[tool], roots[tool], count)
                        if tool != "plain":
                            validate_report(roots[tool], count)
                        if iteration >= sampling.warmups:
                            samples[tool].append(elapsed)
                for tool in TOOLS[1:]:
                    validate_report(roots[tool], count)
            timings = {tool: summarize(values) for tool, values in samples.items() if values}
            records[workload] = {
                "test_count": count + 3,
                "commands": commands,
                "parity": "passed",
                "timings": timings,
                "speedups": {
                    tool: timings["pytest-html"]["median_ms"] / timings[tool]["median_ms"]
                    for tool in ("vfy-plugin", "vfy-cli")
                    if timings
                },
            }
    after = source_state(repository)
    if publication:
        require_revision(repository, before.revision)
    if publication and before != after:
        raise ValueError("source changed during measurement")
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    record = {
        "benchmark": "pytest-html",
        "revision": before.revision,
        "source_dirty": before.dirty,
        "revision_after": after.revision,
        **sampling.as_dict(),
        "environment": {
            "hardware": hardware_details(),
            "build_profile": build_profile(),
            "versions": {
                package: version(package)
                for package in ("vfy", "pytest", "pytest-html", "pytest-metadata")
            },
        },
        "methodology": (
            "Rotated fresh-report process wall times; warm filesystem caches. No coverage. "
            "All tools use --tb=auto. "
            "The vfy CLI uses native execution; direct-pytest workloads use pytest. "
            "vfy emits static HTML; pytest-html also embeds its interactive JavaScript UI. "
            "Outcome IDs, statuses, configured metadata, redaction, and each test's text "
            "attachments are parity-gated."
        ),
        "workloads": records,
    }
    record["provenance"] = {
        **build_provenance(
            repository,
            workload={"tests": args.tests, "streaming_tests": args.streaming_tests},
            workers=1,
            cache="warm-filesystem; fresh reports per sample",
            packages=("pytest", "pytest-html", "pytest-metadata"),
            publication=publication,
        ),
        "starting_source": before.as_dict(),
        "ending_source": after.as_dict(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
