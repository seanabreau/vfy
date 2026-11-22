"""Coverage.py CLI ownership of subprocess tracing under vfy auto startup."""

from __future__ import annotations

import json
from pathlib import Path

from coverage import CoverageData

from tests.support.vfy_project import VfyProject


def test_coverage_run_owns_its_collector_but_ordinary_grandchild_is_measured(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write(
        "pkg/__main__.py",
        "import os\nimport subprocess\nimport sys\n"
        "from pkg import worker\n"
        "worker.from_cli()\n"
        "child_env = os.environ.copy()\n"
        "child_env['COVERAGE_FILE'] = os.environ['GRANDCHILD_DATA_FILE']\n"
        "subprocess.run([sys.executable, '-m', 'pkg.grandchild'], env=child_env, check=True)\n",
    )
    project.write(
        "pkg/grandchild.py",
        "import os\nfrom pkg import worker\n"
        "assert os.environ['COVERAGE_RUN'] == 'true'\n"
        "worker.from_grandchild()\n",
    )
    project.write(
        "pkg/worker.py",
        "def from_cli():\n    return 'cli'\n\ndef from_grandchild():\n    return 'grandchild'\n",
    )
    project.write(
        "outer.coveragerc", "[run]\nsource = pkg\npatch = subprocess\ncontext = benchmark\n"
    )
    project.write(
        "inner.coveragerc", "[run]\nsource = pkg\nrelative_files = true\ncontext = benchmark\n"
    )
    project.write(
        "run.py",
        "import os\nimport subprocess\nimport sys\n"
        "from coverage import Coverage as ReferenceCoverage\n"
        "from vfy import Coverage as VfyCoverage\n"
        "from vfy.process_startup import subprocess_environment\n"
        "kind = sys.argv[1]\n"
        "data_file = f'.coverage.{kind}.outer'\n"
        "coverage_type = VfyCoverage if kind == 'vfy' else ReferenceCoverage\n"
        "cov = coverage_type(config_file='outer.coveragerc', data_file=data_file, "
        "source=['pkg'], context='benchmark')\n"
        "cov.start()\n"
        "child_env = os.environ.copy()\n"
        "child_env['COVERAGE_RCFILE'] = 'inner.coveragerc'\n"
        "child_env['COVERAGE_FILE'] = f'.coverage.{kind}.cli'\n"
        "child_env['GRANDCHILD_DATA_FILE'] = data_file\n"
        "if kind == 'vfy':\n"
        "    with subprocess_environment(cov):\n"
        "        child_env.update({key: os.environ[key] for key in "
        "('PYTHONPATH', 'VFY_PROCESS_START', 'VFY_PROCESS_OPTIONS')})\n"
        "        subprocess.run([sys.executable, '-m', 'coverage', 'run', '-m', 'pkg'], "
        "env=child_env, check=True)\n"
        "else:\n"
        "    subprocess.run([sys.executable, '-m', 'coverage', 'run', '-m', 'pkg'], "
        "env=child_env, check=True)\n"
        "cov.stop()\ncov.save()\n",
    )

    for kind in ("reference", "vfy"):
        result = project.run_python("run.py", kind, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        shards = sorted(project.root.glob(f".coverage.{kind}.outer.*"))
        measured = [
            project.data(shard.name).lines(str(project.root / "pkg/worker.py")) for shard in shards
        ]
        assert sorted(lines for lines in measured if lines) == [[1, 4, 5]], (kind, measured)

        combined = CoverageData(str(project.root / f".coverage.{kind}.combined"))
        try:
            for source in (
                project.root / f".coverage.{kind}.cli",
                project.root / f".coverage.{kind}.outer",
                *shards,
            ):
                data = CoverageData(str(source))
                data.read()
                combined.update(data)
                data.close(force=True)
            combined.write()
        finally:
            combined.close(force=True)
        report = project.run_coverage_py(
            "json",
            "--rcfile=outer.coveragerc",
            "-o",
            f"{kind}.json",
            "--data-file",
            f".coverage.{kind}.combined",
        )
        report.expect(0)

    reference = json.loads((project.root / "reference.json").read_text(encoding="utf-8"))
    actual = json.loads((project.root / "vfy.json").read_text(encoding="utf-8"))
    assert actual["files"] == reference["files"]
    assert actual["totals"] == reference["totals"]

    def schema7_snapshot(kind: str) -> dict[str, object]:
        data = project.data(f".coverage.{kind}.combined")
        try:
            return {
                file: {
                    "lines": sorted(data.lines(file) or []),
                    "arcs": sorted(data.arcs(file) or []),
                    "file_tracer": data.file_tracer(file),
                    "contexts_by_lineno": {
                        line: sorted(contexts)
                        for line, contexts in data.contexts_by_lineno(file).items()
                    },
                }
                for file in sorted(data.measured_files())
            }
        finally:
            data.close(force=True)

    assert schema7_snapshot("vfy") == schema7_snapshot("reference")
