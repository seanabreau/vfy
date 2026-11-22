"""Partial branch counts retain each missing exit from an executed origin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("observed_exit", [False, True], ids=["no-exits", "one-exit"])
def test_partial_exit_counts_match_reference(tmp_path: Path, *, observed_exit: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def choose(value):\n    if value:\n        return 1\n    return 0\n")
    reports = []
    arcs = [(-1, 2), *([(2, 3)] if observed_exit else [])]
    for engine in ("coverage", "vfy"):
        result = project.run_python(
            "-c",
            f"from {engine} import Coverage\n"
            "from pathlib import Path\n"
            "cov = Coverage(branch=True, source=['.'], config_file=False, data_file=None)\n"
            f"cov.get_data().add_arcs({{str(Path('app.py').resolve()): {arcs!r}}})\n"
            f"cov.json_report(outfile='{engine}.json')\n",
        )
        result.expect(0)
        report = json.loads((tmp_path / f"{engine}.json").read_text(encoding="utf-8"))
        files = {
            (
                Path(name).relative_to(tmp_path).as_posix() if Path(name).is_absolute() else name
            ): value
            for name, value in report["files"].items()
        }
        assert set(files) == {"app.py"}
        reports.append((files, report["totals"]))
    assert reports[0][0]["app.py"]["summary"]["num_partial_branches"] == (1 if observed_exit else 2)
    assert reports[0] == reports[1]
