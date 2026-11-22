"""Compare long-source searches with the effective covdefaults 2.3.0 patterns."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, cast

import pytest

from tests.support import VfyProject

_PATTERNS = Path(__file__).parents[1] / "fixtures/coverage/exclusion-union-patterns.json"


def _run_case(
    root: Path,
    interpreter: str | Path,
    engine: Literal["stock", "vfy"],
    operation: Literal["analysis", "json"],
) -> tuple[dict[str, object], str, str]:
    project = VfyProject(root, interpreter=interpreter)
    project.write("source.py", "excluded = 1 # pragma: no cover\n" + "retained_value = 0\n" * 3000)
    patterns = json.loads(_PATTERNS.read_text())
    # These are the exact effective exclusions produced by the unmodified covdefaults config.
    project.write(
        "pyproject.toml",
        "[tool.coverage.report]\nfail_under = 76\nexclude_lines = " + json.dumps(patterns) + "\n",
    )
    data = root / "observed.coverage"
    backend = "coverage" if engine == "stock" else "vfy"
    if operation == "analysis":
        program = (
            f"from {backend} import Coverage\n"
            "import json\n"
            "coverage = Coverage(config_file='pyproject.toml')\n"
            "analysis = coverage.analysis2('source.py')\n"
            "print(json.dumps({'statements': analysis[1], 'excluded': analysis[2], "
            "'missing': analysis[3], 'formatted_missing': analysis[4]}))\n"
        )
        result = project.run_python("-c", program)
        result.expect(0)
        decoded = json.loads(result.stdout)
        assert isinstance(decoded, dict)
        return cast(dict[str, object], decoded), result.stdout, result.stderr
    reference = project.run_python(
        "-c",
        "from coverage import Coverage\nimport json\n"
        "print(json.dumps(Coverage(config_file='pyproject.toml').analysis2('source.py')[1]))\n",
    )
    reference.expect(0)
    statements = json.loads(reference.stdout)
    seed = project.run_python(
        "-c",
        "from coverage import CoverageData\n"
        "from pathlib import Path\n"
        f"data = CoverageData({str(data)!r})\n"
        f"data.add_lines({{str(Path('source.py').resolve()): {statements!r}}})\n"
        "data.write()\n",
    )
    seed.expect(0)
    report = root / "coverage.json"
    arguments = ("json", "--data-file", str(data), "-o", str(report))
    result = (
        project.run_coverage_py(*arguments)
        if engine == "stock"
        else project.run_vfy("coverage", *arguments)
    )
    result.expect(0)
    files = json.loads(report.read_text())["files"]
    assert len(files) == 1
    file = next(iter(files.values()))
    return (
        {key: file[key] for key in ["executed_lines", "missing_lines", "excluded_lines"]},
        result.stdout,
        result.stderr,
    )


@pytest.mark.parametrize("operation", ["analysis", "json"])
def test_long_effective_exclusion_union_keeps_full_source_matches(
    tmp_path: Path, operation: Literal["analysis", "json"]
) -> None:
    stock, _, _ = _run_case(tmp_path / "stock", sys.executable, "stock", operation)
    assert stock["excluded" if operation == "analysis" else "excluded_lines"] == [1]
    candidate, _, _ = _run_case(tmp_path / "vfy", sys.executable, "vfy", operation)
    assert candidate == stock
