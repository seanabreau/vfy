"""Display filters preserve report totals, API percentages, and CLI gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


def _total(output: str, output_format: str) -> list[str]:
    if output_format == "total":
        return [output.strip()]
    row = next(line for line in output.splitlines() if "TOTAL" in line)
    return row.replace("|", " ").replace("*", "").split()


@pytest.mark.parametrize("output_format", ["text", "markdown", "total"])
@pytest.mark.parametrize("all_covered", [False, True], ids=["partial", "all-hidden"])
def test_display_filters_preserve_totals_return_value_and_gate_like_coverage_py(
    tmp_path: Path, output_format: str, *, all_covered: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("full.py", "a = 1\nb = 2\nc = 3\nd = 4\n")
    project.write("half.py", "a = 1\nb = 2\n")
    project.write("empty.py", "# no statements\n")
    setup = project.run_python(
        "-c",
        "from coverage import CoverageData\n"
        "from pathlib import Path\n"
        "data = CoverageData('.coverage')\n"
        "data.add_lines({str(Path('full.py').resolve()): [1, 2, 3, 4],\n"
        f"str(Path('half.py').resolve()): {[1, 2] if all_covered else [1]!r},\n"
        "str(Path('empty.py').resolve()): []})\n"
        "data.write()\n",
    )
    setup.expect(0)
    api_results = []
    for engine in ("coverage", "vfy"):
        result = project.run_python(
            "-c",
            f"from {engine} import Coverage\n"
            "from io import StringIO\n"
            "import json\n"
            "cov = Coverage(config_file=False)\n"
            "cov.load()\n"
            "output = StringIO()\n"
            "percent = cov.report(file=output, skip_covered=True, skip_empty=True, "
            f"sort='-cover', output_format={output_format!r})\n"
            "print(json.dumps([percent, output.getvalue()]))\n",
        )
        result.expect(0)
        percent, output = json.loads(result.stdout)
        if output_format != "total":
            assert "full.py" not in output, output
            assert "empty.py" not in output, output
            assert ("half.py" in output) is (not all_covered), output
        api_results.append((percent, _total(output, output_format)))
    assert api_results[0][0] == pytest.approx(100 if all_covered else 500 / 6)

    for threshold in (80, 90):
        options = (
            "report",
            "--skip-covered",
            "--skip-empty",
            "--sort=cover",
            f"--format={output_format}",
            f"--fail-under={threshold}",
        )
        reference = project.run_coverage_py(*options)
        actual = project.run_vfy("coverage", *options)
        assert reference.returncode == (0 if all_covered or threshold == 80 else 2)
        assert actual.returncode == reference.returncode, actual.stdout + actual.stderr
        # The total format can append a failure diagnostic after its percentage.
        expected_total = (
            reference.stdout.splitlines()[0] if output_format == "total" else reference.stdout
        )
        actual_total = actual.stdout.splitlines()[0] if output_format == "total" else actual.stdout
        assert _total(actual_total, output_format) == _total(expected_total, output_format)
    assert api_results[1] == api_results[0]
