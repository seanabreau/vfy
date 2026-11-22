"""Code objects sharing a source line retain each reportable exit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("def helper(): return 1\nresult = 2\n", id="uncalled"),
        pytest.param("def helper(): return 1\nresult = helper()\n", id="called"),
        pytest.param(
            "def identity(value):\n"
            "    return value\n"
            "@identity\n"
            "def helper(): return 1\n"
            "result = 2\n",
            id="decorated-uncalled",
        ),
        pytest.param(
            "def identity(value):\n"
            "    return value\n"
            "@identity\n"
            "def helper(): return 1\n"
            "result = helper()\n",
            id="decorated-called",
        ),
        pytest.param(
            "def identity(value):\n"
            "    return value\n"
            "def helper(): return 1\n"
            "@identity\n"
            "def other():\n"
            "    return 2\n"
            "result = helper()\n",
            id="decorated-continuation",
        ),
        pytest.param("async def helper(): return 1\nresult = 2\n", id="async-uncalled"),
        pytest.param(
            "import asyncio\nasync def helper(): return 1\nresult = asyncio.run(helper())\n",
            id="async-called",
        ),
        pytest.param("class Model: value = 1\nresult = Model.value\n", id="class"),
        pytest.param(
            "class Model:\n    def helper(self): return 1\nresult = Model().helper()\n",
            id="method",
        ),
        pytest.param("result = 1\ndef helper(): return 2\n", id="module-final"),
    ],
)
def test_shared_source_line_reports_match_reference(tmp_path: Path, source: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", source)
    reports = []
    observations = []
    for engine in ("coverage", "vfy"):
        result = project.run_python(
            "-c",
            f"from {engine} import Coverage\n"
            "from pathlib import Path\n"
            "import json, runpy\n"
            "cov = Coverage(branch=True, source=['.'], config_file=False)\n"
            "cov.start()\n"
            "runpy.run_path('app.py', run_name='__main__')\n"
            "cov.stop()\n"
            f"cov.json_report(outfile='{engine}.json')\n"
            "data = cov.get_data()\n"
            "filename = str(Path('app.py').resolve())\n"
            "print(json.dumps([sorted(data.lines(filename)), sorted(data.arcs(filename))]))\n",
        )
        result.expect(0)
        observations.append(json.loads(result.stdout))
        report = json.loads((tmp_path / f"{engine}.json").read_text(encoding="utf-8"))
        files = {
            (
                Path(name).relative_to(tmp_path).as_posix() if Path(name).is_absolute() else name
            ): value
            for name, value in report["files"].items()
        }
        assert set(files) == {"app.py"}
        reports.append((files, report["totals"]))
    assert observations[0] == observations[1]
    assert reports[0] == reports[1]
