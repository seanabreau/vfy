"""Install the reviewed checkout and execute four introductory guides.

Requires uv, Git, and the source-build prerequisites documented in installation.
Run on Linux or macOS; selection and mutation use vfy's native runner.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent


def block(page: str, name: str) -> str:
    source = (ROOT / "docs" / page).read_text()
    matches = list(
        re.finditer(rf"<!-- doc-test: {re.escape(name)} -->\n```\w+\n(.*?)\n```", source, re.DOTALL)
    )
    if len(matches) != 1:
        raise ValueError(f"Expected one {name!r} block in {page}")
    content = matches[0].group(1)
    assert isinstance(content, str)
    return content + "\n"


def run(project: Path, args: list[str], status: int = 0) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("PYTHONPATH", "VIRTUAL_ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    env.update(
        UV_PROJECT_ENVIRONMENT=str(project / ".venv"),
        GIT_AUTHOR_NAME="vfy tutorial check",
        GIT_AUTHOR_EMAIL="vfy-tests@example.invalid",
        GIT_COMMITTER_NAME="vfy tutorial check",
        GIT_COMMITTER_EMAIL="vfy-tests@example.invalid",
    )
    print(f"[{project.name}] $ {shlex.join(args)}", flush=True)
    result = subprocess.run(
        args, cwd=project, env=env, text=True, capture_output=True, timeout=1800, check=False
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    if result.returncode != status:
        raise RuntimeError(f"Expected exit {status}, got {result.returncode}: {args}")
    return result


def commands(
    project: Path, page: str, name: str, status: int = 0
) -> list[subprocess.CompletedProcess[str]]:
    results = []
    for line in block(page, name).splitlines():
        if not line.startswith("$ ") or line.endswith("\\"):
            raise ValueError(f"Expected a single-line command: {line!r}")
        results.append(run(project, shlex.split(line[2:]), status))
    return results


def install(base: Path, name: str) -> Path:
    project = base / name
    shutil.copytree(
        ROOT / "examples/coverage",
        project,
        ignore=shutil.ignore_patterns(
            ".venv", ".git", ".vfy", "__pycache__", ".pytest_cache", ".coverage*"
        ),
    )
    manifest = project / "pyproject.toml"
    original = manifest.read_text()
    config = tomllib.loads(original)
    if config["tool"]["uv"]["sources"]["vfy"] != {"path": "../..", "editable": False}:
        raise ValueError("Expected a noneditable vfy source relative to the reviewed checkout")
    section = re.search(r"(?ms)^\[tool\.uv\.sources\]\n.*?(?=^\[|\Z)", original)
    if section is None:
        raise ValueError("Expected a tool.uv.sources table in the example")
    replacement, count = re.subn(
        r"(?m)^vfy\s*=.*$",
        lambda _: f"vfy = {{ path = {json.dumps(str(ROOT))}, editable = false }}",
        section.group(),
    )
    if count != 1:
        raise ValueError("Expected one vfy source in the example")
    relocated = original[: section.start()] + replacement + original[section.end() :]
    config["tool"]["uv"]["sources"]["vfy"]["path"] = str(ROOT)
    if tomllib.loads(relocated) != config:
        raise ValueError("Relocating the example changed configuration outside its vfy source")
    manifest.write_text(relocated)
    # The copied example needs a lock entry for its absolute source directory.
    run(project, ["uv", "sync"])
    code = (
        "import importlib.metadata,json,vfy,vfy._native; "
        "d=importlib.metadata.distribution('vfy'); "
        "print(json.dumps({'package':vfy.__file__,'native':vfy._native.__file__,"
        "'direct_url':json.loads(d.read_text('direct_url.json'))}))"
    )
    payload = json.loads(run(project, ["uv", "run", "python", "-c", code]).stdout)
    direct_url = payload["direct_url"]
    assert direct_url["url"] == ROOT.as_uri(), payload
    assert "dir_info" in direct_url, payload
    assert not direct_url["dir_info"].get("editable", False), payload
    for module in ("package", "native"):
        assert Path(payload[module]).resolve().is_relative_to(project / ".venv"), payload
    print(f"Verified fresh noneditable installation from {ROOT}", flush=True)
    return project


def nodes(result: subprocess.CompletedProcess[str]) -> set[str]:
    return set(re.findall(r"^(tests/\S+) PASSED", result.stdout, re.MULTILINE))


def check_report(project: Path, result: subprocess.CompletedProcess[str]) -> None:
    actual = result.stdout.replace(str(project) + "/", "")
    expected = block("getting-started/quickstart.md", "missing-output")
    assert [line.split() for line in actual.splitlines() if not line.startswith("---")] == [
        line.split() for line in expected.splitlines() if not line.startswith("---")
    ], result.stdout


def check_coverage(project: Path) -> None:
    page = "getting-started/quickstart.md"
    initial = commands(project, page, "collect")[0]
    assert nodes(initial) == {
        "tests/test_bounds.py::test_positive_value",
        "tests/test_formatting.py::test_format_count",
    }, initial.stdout
    check_report(project, commands(project, page, "report")[0])
    commands(project, page, "fail-gate", 2)
    (project / "tests/test_bounds.py").write_text(block(page, "complete-tests"))
    rerun, report = commands(project, page, "recollect")
    assert len(nodes(rerun)) == 3, rerun.stdout
    assert "100%" in report.stdout, report.stdout
    commands(project, page, "export")
    data = json.loads((project / "coverage.json").read_text())
    assert data["totals"]["percent_covered"] == 100, data["totals"]
    xml = ET.parse(project / "coverage.xml").getroot()
    assert float(xml.attrib["line-rate"]) == float(xml.attrib["branch-rate"]) == 1
    print("Coverage tutorial passed: 80%, gate exit 2, then 100% and JSON/XML reports.")


def check_reporting_coexistence(project: Path) -> None:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    versions = [
        next(item for item in dev if item.startswith(name + "=="))
        for name in ("pytest-html", "pytest-metadata")
    ]
    run(project, ["uv", "pip", "install", "--python", str(project / ".venv/bin/python"), *versions])
    (project / "conftest.py").write_text(
        "import pytest_html.plugin\n"
        "import pytest_metadata.plugin\n\n"
        "def pytest_configure(config):\n"
        "    assert config.pluginmanager.get_plugin('html') is pytest_html.plugin\n"
        "    assert config.pluginmanager.get_plugin('metadata') is pytest_metadata.plugin\n"
        "    assert not config.pluginmanager.hasplugin('vfy.pytest_reporting')\n\n"
        "def pytest_html_report_title(report):\n"
        "    report.title = 'Upstream reporting coexistence'\n"
    )
    # Keep the separately installed reporting plug-ins without changing the example lock.
    result = run(
        project,
        [
            "uv",
            "run",
            "--no-sync",
            "pytest",
            "-p",
            "no:pytest_cov",
            "-p",
            "vfy.pytest_plugin",
            "--cov=src",
            "--html=results.html",
            "--self-contained-html",
            "--metadata",
            "TutorialCheck",
            "upstream-preserved",
        ],
    )
    assert "3 passed" in result.stdout, result.stdout
    html = (project / "results.html").read_text()
    assert "Upstream reporting coexistence" in html
    assert "upstream-preserved" in html
    print("Reporting coexistence passed with " + ", ".join(versions))


def check_selection(project: Path) -> None:
    page = "getting-started/selection.md"
    commands(project, page, "git-baseline")
    assert len(nodes(commands(project, page, "baseline")[0])) == 2
    (project / "src/bounds.py").write_text(block(page, "edit-bounds"))
    selected = commands(project, page, "select")[0]
    assert nodes(selected) == {"tests/test_bounds.py::test_positive_value"}, selected.stdout
    assert "Selected 1 / 2 tests" in selected.stderr, selected.stderr
    assert "Reused 1 tests" in selected.stderr, selected.stderr
    check_report(project, commands(project, page, "selected-report")[0])
    explanation = json.loads(commands(project, page, "explain")[0].stdout)
    assert [test["nodeid"] for test in explanation["tests"]] == [
        "tests/test_bounds.py::test_positive_value"
    ], explanation
    _, fallback = commands(project, page, "fallback")
    assert len(nodes(fallback)) == 2, fallback.stdout
    assert "no passing baseline" in fallback.stdout + fallback.stderr
    commands(project, page, "refresh")
    print("Selection tutorial passed: one selected, one reused, report, explanation, fallback.")


def check_mutation(project: Path) -> None:
    page = "guides/mutation.md"
    for directory in ("src", "tests"):
        shutil.rmtree(project / directory)
        (project / directory).mkdir()
    manifest = project / "pyproject.toml"
    manifest.write_text(
        re.sub(
            r"\[tool\.(?:pytest|coverage|vfy)\.[^\]]+\]\n.*?(?=\n\[|\Z)",
            "",
            manifest.read_text(),
            flags=re.DOTALL,
        )
        + "\n"
        + block(page, "mutation-config")
    )
    source = block(page, "mutation-source")
    (project / "src/sign.py").write_text(source)
    (project / "tests/test_sign.py").write_text(block(page, "mutation-weak-tests"))
    commands(project, page, "mutation-baseline")
    assert "100%" in commands(project, page, "mutation-coverage")[0].stdout
    commands(project, page, "mutation-focused", 2)
    report = json.loads((project / "mutations.json").read_text())
    assert report["summary"] == {"survived": 1}, report
    (survivor,) = report["mutations"]
    assert (survivor["original"], survivor["replacement"], survivor["killer"]) == (
        ">=",
        ">",
        None,
    ), survivor

    (project / "tests/test_sign.py").write_text(block(page, "mutation-strong-tests"))
    commands(project, page, "mutation-refresh")
    commands(project, page, "mutation-protected")
    report = json.loads((project / "mutations.json").read_text())
    assert report["summary"] == {"protected": 1}, report
    (protected,) = report["mutations"]
    assert protected["id"] == survivor["id"], protected
    assert protected["killer"] == "tests/test_sign.py::test_zero_is_nonnegative", protected
    assert (project / "src/sign.py").read_text() == source
    print(
        "Mutation guide passed: boundary mutation survived with exit 2, then protected with exit 0."
    )


def check_semantic(project: Path) -> None:
    # Retain the locked dependencies but remove the coverage example's configuration:
    # the semantic guide starts with two root-level files and no coverage settings.
    shutil.rmtree(project / "src")
    shutil.rmtree(project / "tests")
    manifest = project / "pyproject.toml"
    manifest.write_text(
        re.sub(
            r"\[tool\.(?:pytest|coverage|vfy)\.[^\]]+\]\n.*?(?=\n\[|\Z)",
            "",
            manifest.read_text(),
            flags=re.DOTALL,
        )
    )
    page = "guides/semantic.md"
    (project / "bounds.py").write_text(block(page, "semantic-source"))
    (project / "test_bounds.py").write_text(block(page, "semantic-tests"))
    commands(project, page, "semantic-collect")
    commands(project, page, "semantic-fail", 2)
    report_args = ["uv", "run", "vfy", "coverage", "check", "--format", "json"]
    initial = json.loads(run(project, report_args).stdout)
    assert initial["totals"]["percent"] == {"decisions": 100.0, "conditions": 75.0, "mcdc": 50.0}, (
        initial
    )

    (project / "test_bounds.py").write_text(
        block(page, "semantic-tests") + "\n" + block(page, "semantic-missing-test")
    )
    commands(project, page, "semantic-complete")
    complete = json.loads(run(project, report_args).stdout)
    assert complete["totals"]["percent"] == {
        "decisions": 100.0,
        "conditions": 100.0,
        "mcdc": 100.0,
    }, complete
    print(
        "Semantic guide passed: missing input caused exit 2, then all metrics reached 100%/exit 0."
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="vfy-tutorials-") as directory:
        base = Path(directory).resolve()
        if base.is_relative_to(ROOT):
            raise ValueError("Set TMPDIR outside the repository for an isolated installation")
        coverage_project = install(base, "coverage-example")
        check_coverage(coverage_project)
        check_reporting_coexistence(coverage_project)
        check_selection(install(base, "selection-example"))
        check_mutation(install(base, "mutation-example"))
        check_semantic(install(base, "semantic-example"))


if __name__ == "__main__":
    main()
