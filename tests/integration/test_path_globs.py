from __future__ import annotations

import json
import re
from pathlib import Path

import coverage
import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("option", ["include", "omit"])
@pytest.mark.parametrize("configured", [False, True])
def test_report_literal_braces_match_coverage_py(
    tmp_path: Path, option: str, configured: bool
) -> None:
    project = VfyProject(tmp_path)
    paths = [project.write(name, "x = 1\n") for name in ("a{b,c}.py", "ab.py", "ac.py")]
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(path.resolve()): {1} for path in paths})
    data.write()
    project.write("pyproject.toml", f'[tool.coverage.report]\n{option} = ["*a{{b,c}}.py"]\n')
    project.write(
        "report.py",
        "import importlib, json, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "configured = sys.argv[3] == 'True'\n"
        "cov = Coverage(config_file='pyproject.toml' if configured else False)\n"
        "cov.load()\n"
        "options = {} if configured else {sys.argv[2]: ['*a{b,c}.py']}\n"
        "cov.json_report(outfile='report.json', **options)\n"
        "print(json.dumps(sorted(json.load(open('report.json'))['files'])))\n",
    )
    results = []
    for tool in ("coverage", "vfy"):
        completed = project.run_python("report.py", tool, option, str(configured))
        completed.expect(0)
        results.append([Path(name).name for name in json.loads(completed.stdout)])
    expected = ["a{b,c}.py"] if option == "include" else ["ab.py", "ac.py"]
    assert results == [expected, expected]


@pytest.mark.parametrize("option", ["include", "omit"])
def test_collection_literal_braces_match_coverage_py(tmp_path: Path, option: str) -> None:
    project = VfyProject(tmp_path)
    for name in ("a{b,c}.py", "ab.py", "ac.py"):
        project.write(name, "x = 1\n")
    defaults = 'include = ["*/a*.py"]\n' if option == "omit" else ""
    project.write("pyproject.toml", f'[tool.coverage.run]\n{defaults}{option} = ["*a{{b,c}}.py"]\n')
    project.write(
        "collect.py",
        "import importlib, json, pathlib, runpy, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(config_file='pyproject.toml', data_file=None)\n"
        "cov.start()\n"
        "try:\n"
        "    for name in ('a{b,c}.py', 'ab.py', 'ac.py'):\n"
        "        runpy.run_path(name)\n"
        "finally:\n"
        "    cov.stop()\n"
        "names = sorted(pathlib.Path(p).name for p in cov.get_data().measured_files())\n"
        "print(json.dumps(names))\n",
    )
    results = []
    for tool in ("coverage", "vfy"):
        completed = project.run_python("collect.py", tool)
        completed.expect(0)
        results.append(json.loads(completed.stdout))
    expected = ["a{b,c}.py"] if option == "include" else ["ab.py", "ac.py"]
    assert results == [expected, expected]


def test_collection_unicode_character_class_matches_coverage_py(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("a.py", "é.py"):
        project.write(name, "x = 1\n")
    project.write(
        "collect.py",
        "import importlib, json, pathlib, runpy, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(config_file=False, data_file=None, include=['*/[^a].py'])\n"
        "cov.start()\n"
        "try:\n"
        "    for name in ('a.py', 'é.py'):\n"
        "        runpy.run_path(name)\n"
        "finally:\n"
        "    cov.stop()\n"
        "names = sorted(pathlib.Path(p).name for p in cov.get_data().measured_files())\n"
        "print(json.dumps(names))\n",
    )
    results = []
    for tool in ("coverage", "vfy"):
        completed = project.run_python("collect.py", tool)
        completed.expect(0)
        results.append(json.loads(completed.stdout))
    assert results == [["é.py"], ["é.py"]]


@pytest.mark.parametrize("option", ["include", "omit"])
@pytest.mark.parametrize("boundary", ["start", "json_report"])
@pytest.mark.parametrize(
    ("pattern", "error_type", "same_message"),
    [
        ("*a**b.py", "ConfigError", True),
        ("pkg/***/a.py", "ConfigError", True),
        ("pkg/**/**/a.py", "ConfigError", True),
        ("[z-a].py", re.error.__name__, False),
    ],
)
def test_invalid_file_pattern_errors_match_coverage_py(
    tmp_path: Path,
    option: str,
    boundary: str,
    pattern: str,
    error_type: str,
    same_message: bool,
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("ab.py", "x = 1\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(source.resolve()): {1}})
    data.write()
    section = "run" if boundary == "start" else "report"
    project.write(".coveragerc", f"[{section}]\n{option} = {pattern}\n")
    project.write(
        "invalid.py",
        "import importlib, json, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(config_file='.coveragerc')\n"
        "cov.load()\n"
        "try:\n"
        "    if sys.argv[2] == 'start':\n"
        "        cov.start()\n"
        "        cov.stop()\n"
        "    else:\n"
        "        cov.json_report(outfile='report.json')\n"
        "except Exception as error:\n"
        "    print(json.dumps([type(error).__name__, str(error)]))\n"
        "else:\n"
        "    raise AssertionError('invalid file pattern was accepted')\n",
    )
    results = []
    for tool in ("coverage", "vfy"):
        completed = project.run_python("invalid.py", tool, boundary)
        completed.expect(0)
        results.append(json.loads(completed.stdout))
    assert [result[0] for result in results] == [error_type, error_type]
    if same_message:
        assert results[0] == results[1]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*/pkg/*.py", ["pkg/!.py", "pkg/a.py", "pkg/module.py", "pkg/é.py"]),
        ("*/pkg/*", ["pkg/!.py", "pkg/a.py", "pkg/deep/module.py", "pkg/module.py", "pkg/é.py"]),
        ("*/pkg/**/module.py", ["pkg/deep/module.py", "pkg/module.py"]),
        ("*/pkg/[!a].py", ["pkg/!.py", "pkg/a.py"]),
        ("*/pkg/[^a].py", ["pkg/!.py", "pkg/é.py"]),
        ("*/pkg/?.py", ["pkg/!.py", "pkg/a.py", "pkg/é.py"]),
        ("*/pkg/[a&&é].py", ["pkg/a.py", "pkg/é.py"]),
        (r"*\pkg\*.py", ["pkg/!.py", "pkg/a.py", "pkg/module.py", "pkg/é.py"]),
    ],
)
def test_report_wildcard_shapes_match_coverage_py(
    tmp_path: Path, pattern: str, expected: list[str]
) -> None:
    project = VfyProject(tmp_path)
    paths = [
        project.write(name, "x = 1\n")
        for name in (
            "pkg/module.py",
            "pkg/deep/module.py",
            "other/module.py",
            "pkg/a.py",
            "pkg/!.py",
            "pkg/é.py",
        )
    ]
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(path.resolve()): {1} for path in paths})
    data.write()
    project.write(
        "report.py",
        "import importlib, json, pathlib, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(config_file=False)\n"
        "cov.load()\n"
        "cov.json_report(outfile='report.json', include=[sys.argv[2]])\n"
        "files = json.load(open('report.json'))['files']\n"
        "root = pathlib.Path.cwd()\n"
        "print(json.dumps(sorted(pathlib.Path(p).resolve().relative_to(root).as_posix() "
        "for p in files)))\n",
    )
    results = []
    for tool in ("coverage", "vfy"):
        completed = project.run_python("report.py", tool, pattern)
        completed.expect(0)
        results.append(json.loads(completed.stdout))
    assert results == [expected, expected]
