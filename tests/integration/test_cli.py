from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from coverage import Coverage, CoverageData as ReferenceData

from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def main(arguments: list[str]) -> int:
    """Run the real ``vfy`` command in the current directory and replay its streams."""

    result = VfyProject(Path.cwd()).run_vfy(*arguments)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _coverage_json_analysis(
    data_file: Path, source: Path, output_file: Path
) -> dict[str, list[int] | list[list[int]]]:
    coverage = Coverage(data_file=str(data_file), branch=True)
    coverage.load()
    coverage.json_report(morfs=[str(source)], outfile=str(output_file))
    files = json.loads(output_file.read_text(encoding="utf-8"))["files"]
    assert len(files) == 1
    result = next(iter(files.values()))
    return {
        "executed_lines": result["executed_lines"],
        "missing_lines": result["missing_lines"],
        "executed_branches": result["executed_branches"],
        "missing_branches": result["missing_branches"],
    }


def _assert_branch_data_parity(
    actual_file: Path, reference_file: Path, source: Path, output_directory: Path
) -> None:
    actual = ReferenceData(str(actual_file))
    reference = ReferenceData(str(reference_file))
    actual.read()
    reference.read()
    assert actual.lines(str(source)) == reference.lines(str(source))
    actual_arcs = set(actual.arcs(str(source)) or ())
    reference_arcs = set(reference.arcs(str(source)) or ())
    assert _coverage_json_analysis(
        actual_file, source, output_directory / "actual.json"
    ) == _coverage_json_analysis(reference_file, source, output_directory / "reference.json")
    assert actual_arcs == reference_arcs


def _write_data(filename: Path, source: Path, lines: set[int]) -> None:
    data = ReferenceData(str(filename))
    data.add_lines({str(source): set(lines)})
    data.write()


def test_cli_xml_json_and_setup_cfg_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    _write_data(tmp_path / ".coverage", source, {1})
    (tmp_path / "setup.cfg").write_text(
        """
[coverage:report]
precision = 2
show_missing = true

[coverage:xml]
output = reports/coverage.xml

[coverage:json]
output = reports/coverage.json
pretty_print = true
""".strip(),
        encoding="utf-8",
    )

    assert main(["coverage", "report"]) == 0
    assert "100.00%" in capsys.readouterr().out

    assert main(["coverage", "xml"]) == 0
    capsys.readouterr()
    assert (tmp_path / "reports/coverage.xml").read_text().startswith("<?xml")

    assert main(["coverage", "json"]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "reports/coverage.json").read_text())
    assert payload["meta"]["format"] == 3


def test_run_collects_a_python_program(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\nif value:\n    value += 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"

    assert main(["coverage", "run", "--data-file", str(data_file), str(source)]) == 0
    data = ReferenceData(str(data_file))
    data.read()
    assert data.lines(str(source)) == [1, 2, 3]


def test_identical_code_objects_are_attributed_to_their_own_files(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    module = "def answer():\n    return 42\n\nanswer()\n"
    first.write_text(module, encoding="utf-8")
    second.write_text(module, encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text("import first\nimport second\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"

    assert (
        main(
            [
                "coverage",
                "run",
                "--source",
                str(tmp_path),
                "--data-file",
                str(data_file),
                str(runner),
            ]
        )
        == 0
    )
    data = ReferenceData(str(data_file))
    data.read()
    assert data.lines(str(first)) == [1, 2, 4]
    assert data.lines(str(second)) == [1, 2, 4]


def test_branch_collection_matches_coverage_py_for_basic_control_flow(tmp_path: Path) -> None:
    source = tmp_path / "branchy.py"
    source.write_text(
        "value = 1\nif value:\n    result = 2\nelse:\n    result = 3\n", encoding="utf-8"
    )
    actual_file = tmp_path / ".coverage-vfy"
    reference_file = tmp_path / ".coverage-reference"

    assert main(["coverage", "run", "--branch", "--data-file", str(actual_file), str(source)]) == 0
    assert (
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--branch",
                "--data-file",
                str(reference_file),
                str(source),
            ],
            check=False,
        ).returncode
        == 0
    )
    _assert_branch_data_parity(actual_file, reference_file, source, tmp_path)


def test_branch_collection_matches_coverage_core_conventions(tmp_path: Path) -> None:
    source = tmp_path / "branchy.py"
    source.write_text(
        "value = 1\nif value:\n    result = 2\nelse:\n    result = 3\n", encoding="utf-8"
    )
    files = {
        None: (tmp_path / ".coverage-vfy-default", tmp_path / ".coverage-default"),
        "ctrace": (tmp_path / ".coverage-vfy-ctrace", tmp_path / ".coverage-ctrace"),
        "sysmon": (tmp_path / ".coverage-vfy-sysmon", tmp_path / ".coverage-sysmon"),
    }

    def run_vfy(data_file: Path, core: str | None) -> None:
        result = VfyProject(tmp_path).run_vfy(
            "coverage",
            "run",
            "--branch",
            "--data-file",
            str(data_file),
            str(source),
            env={"COVERAGE_CORE": core},
        )
        result.expect(0)

    for core, (actual_file, reference_file) in files.items():
        run_vfy(actual_file, core)
        environment = os.environ.copy()
        if core is None:
            environment.pop("COVERAGE_CORE", None)
        else:
            environment["COVERAGE_CORE"] = core
        assert (
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--branch",
                    "--data-file",
                    str(reference_file),
                    str(source),
                ],
                check=False,
                env=environment,
            ).returncode
            == 0
        )
    for actual_file, reference_file in files.values():
        actual = ReferenceData(str(actual_file))
        reference = ReferenceData(str(reference_file))
        actual.read()
        reference.read()
        assert actual.arcs(str(source)) == reference.arcs(str(source))


def test_branch_collection_matches_coverage_py_for_loops_match_and_functions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "flow.py"
    source.write_text(
        "def classify(value):\n"
        "    if value > 0:\n        return 'positive'\n"
        "    for candidate in range(2):\n"
        "        if candidate == value:\n            break\n"
        "    else:\n        return 'other'\n"
        "    match value:\n"
        "        case 0:\n            return 'zero'\n"
        "        case _:\n            return 'matched'\n\n"
        "for item in (-1, 0, 1, 9):\n    classify(item)\n",
        encoding="utf-8",
    )
    actual_file = tmp_path / ".coverage-vfy"
    reference_file = tmp_path / ".coverage-reference"
    assert main(["coverage", "run", "--branch", "--data-file", str(actual_file), str(source)]) == 0
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--data-file",
            str(reference_file),
            str(source),
        ],
        check=True,
    )
    _assert_branch_data_parity(actual_file, reference_file, source, tmp_path)


def test_branch_collection_matches_exceptions_and_generator_throw(tmp_path: Path) -> None:
    source = tmp_path / "exceptions.py"
    source.write_text(
        "def generator():\n"
        "    try:\n        yield 1\n"
        "    except ValueError:\n        yield 2\n"
        "    finally:\n        marker = 3\n\n"
        "def fail():\n"
        "    try:\n        raise RuntimeError\n"
        "    except RuntimeError:\n        return 1\n\n"
        "values = generator()\n"
        "next(values)\n"
        "values.throw(ValueError)\n"
        "values.close()\n"
        "fail()\n",
        encoding="utf-8",
    )
    actual_file = tmp_path / ".coverage-vfy"
    reference_file = tmp_path / ".coverage-reference"
    assert main(["coverage", "run", "--branch", "--data-file", str(actual_file), str(source)]) == 0
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--data-file",
            str(reference_file),
            str(source),
        ],
        check=True,
    )
    _assert_branch_data_parity(actual_file, reference_file, source, tmp_path)


def test_branch_collection_matches_suspended_and_repeated_control_flow(tmp_path: Path) -> None:
    source = tmp_path / "all_control_flow.py"
    source.write_text(
        "import asyncio\n"
        "\n"
        "def classify(value):\n"
        "    if value < 0:\n"
        "        return -1\n"
        "    if value == 0:\n"
        "        return 0\n"
        "    return 1\n"
        "\n"
        "def recurse(depth):\n"
        "    if depth <= 0:\n"
        "        return 0\n"
        "    return 1 + recurse(depth - 1)\n"
        "\n"
        "def rare(limit):\n"
        "    found = -1\n"
        "    for value in range(limit):\n"
        "        if value == limit - 1:\n"
        "            found = value\n"
        "    return found\n"
        "\n"
        "def generator(value):\n"
        "    try:\n"
        "        yield value\n"
        "        if value:\n"
        "            yield value + 1\n"
        "    except ValueError:\n"
        "        yield -1\n"
        "\n"
        "def fail(value):\n"
        "    try:\n"
        "        if value:\n"
        "            raise ValueError\n"
        "    except ValueError:\n"
        "        return 1\n"
        "    return 0\n"
        "\n"
        "async def coroutine(value):\n"
        "    if value:\n"
        "        await asyncio.sleep(0)\n"
        "        return 1\n"
        "    return 0\n"
        "\n"
        "classify(-1)\n"
        "classify(0)\n"
        "classify(1)\n"
        "recurse(5)\n"
        "rare(100000)\n"
        "items = generator(1)\n"
        "next(items)\n"
        "items.throw(ValueError)\n"
        "fail(True)\n"
        "fail(False)\n"
        "asyncio.run(coroutine(True))\n"
        "asyncio.run(coroutine(False))\n",
        encoding="utf-8",
    )
    actual_file = tmp_path / ".coverage-vfy"
    reference_file = tmp_path / ".coverage-reference"
    assert main(["coverage", "run", "--branch", "--data-file", str(actual_file), str(source)]) == 0
    subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--data-file",
            str(reference_file),
            str(source),
        ],
        check=True,
    )
    _assert_branch_data_parity(actual_file, reference_file, source, tmp_path)


def test_pytest_xdist_workers_write_mergeable_context_shards(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    product = project.write(
        "product.py",
        "def choose(value):\n    if value:\n        return 1\n    return 2\n",
    )
    project.write(
        "test_product.py",
        "from product import choose\n\n"
        "def test_one():\n    assert choose(True) == 1\n\n"
        "def test_two():\n    assert choose(False) == 2\n",
    )
    completed = project.run_vfy(
        "coverage",
        "run",
        "--branch",
        "--parallel-mode",
        "--source",
        str(tmp_path),
        "-m",
        "pytest",
        "-q",
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "xdist.plugin",
        "-n",
        "2",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    assert len(list(tmp_path.glob(".coverage.*"))) >= 2

    assert main(["coverage", "report", "--data-file", str(tmp_path / ".coverage")]) == 0
    combined = ReferenceData(str(tmp_path / ".coverage"))
    combined.read()
    assert combined.has_arcs()
    assert combined.lines(str(product)) == [1, 2, 3, 4]
    # Coverage.py changes its sys.monitoring arc format on CPython 3.14.
    sysmon_arcs = sys.version_info >= (3, 14)
    expected_arcs = (
        {
            (1, -1),
            (1, 1),
            (2, 2),
            (2, 3),
            (2, 4),
            (3, -1),
            (3, 3),
            (4, -1),
            (4, 4),
        }
        if sysmon_arcs
        else {(-1, 1), (-1, 2), (1, -1), (2, 3), (2, 4), (3, -1), (4, -1)}
    )
    assert set(combined.arcs(str(product)) or ()) == expected_arcs
    test_contexts = {
        context
        for context in combined.measured_contexts()
        if context.startswith("test_product.py::test_")
    }
    assert test_contexts == {
        "test_product.py::test_one",
        "test_product.py::test_two",
    }
    combined.set_query_context("test_product.py::test_one")
    expected_one = {(2, 2), (2, 3), (3, -1), (3, 3)} if sysmon_arcs else {(-1, 2), (2, 3), (3, -1)}
    assert set(combined.arcs(str(product)) or ()) == expected_one
    combined.set_query_context("test_product.py::test_two")
    expected_two = {(2, 2), (2, 4), (4, -1), (4, 4)} if sysmon_arcs else {(-1, 2), (2, 4), (4, -1)}
    assert set(combined.arcs(str(product)) or ()) == expected_two


def test_python_subprocesses_are_collected_automatically(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    child = project.write(
        "child.py",
        "import sys\nassert 'vfy.mutation' not in sys.modules\nanswer = 40\nanswer += 2\n",
    )
    project.write(
        "parent.py",
        "import subprocess\nimport sys\n"
        f"subprocess.run([sys.executable, {str(child)!r}], check=True)\n",
    )

    collected = project.run_vfy(
        "coverage", "run", "--source", ".", "parent.py", env={"VFY_MUTATION": None}
    )
    collected.expect(0)
    reported = project.run_vfy("coverage", "report")
    reported.expect(0)
    assert project.lines(child) == [1, 2, 3, 4]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX-only")
def test_fork_without_exec_writes_an_isolated_child_shard(tmp_path: Path) -> None:
    source = tmp_path / "forked.py"
    source.write_text(
        "import os\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    child_only = 1\n"
        "    raise SystemExit(0)\n"
        "os.waitpid(pid, 0)\n"
        "parent_only = 1\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vfy",
            "coverage",
            "run",
            "--source",
            str(tmp_path),
            "--data-file",
            str(data_file),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.glob(".coverage.*"))

    assert main(["coverage", "report", "--data-file", str(data_file)]) == 0
    combined = ReferenceData(str(data_file))
    combined.read()
    assert {4, 7}.issubset(set(combined.lines(str(source)) or []))


@pytest.mark.skipif(
    not hasattr(os, "fork")
    or not hasattr(sys, "monitoring")
    or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
    or not hasattr(sys.monitoring.events, "BRANCH_RIGHT"),
    reason="branch event restoration requires POSIX CPython 3.14+",
)
def test_branch_fork_restores_disabled_locations(tmp_path: Path) -> None:
    source = tmp_path / "forked_branch.py"
    source.write_text(
        "import os\n"
        "def run(value):\n"
        "    total = 0\n"
        "    if value:\n"
        "        total += 1\n"
        "    else:\n"
        "        total -= 1\n"
        "    return total\n"
        "run(True)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    run(False)\n"
        "    raise SystemExit(0)\n"
        "os.waitpid(pid, 0)\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vfy",
            "coverage",
            "run",
            "--branch",
            "--source",
            str(tmp_path),
            "--data-file",
            str(data_file),
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    subprocess.run(
        [
            sys.executable,
            "-m",
            "vfy",
            "coverage",
            "combine",
            "--keep",
            "--data-file",
            str(data_file),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    combined = ReferenceData(str(data_file))
    combined.read()
    # The parent takes the true arm; the false arm is child-only.  If the
    # inherited disabled line/branch locations were not restored, line 7 is
    # absent from the child shard and therefore from the combined result.
    assert 7 in set(combined.lines(str(source)) or [])


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(signal, "SIGTERM"),
    reason="SIGTERM exit codes are POSIX-specific",
)
def test_sigterm_configuration_saves_before_preserving_signal_exit(tmp_path: Path) -> None:
    source = tmp_path / "terminated.py"
    ready = tmp_path / "ready"
    source.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "marker = 1\n"
        "while True:\n"
        "    time.sleep(0.01)\n"
        f"    Path({str(ready)!r}).write_text('ready')\n",
        encoding="utf-8",
    )
    config = tmp_path / ".coveragerc"
    config.write_text("[run]\nsigterm = true\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    process = VfyProject(tmp_path).start_vfy(
        "coverage",
        "run",
        "--rcfile",
        str(config),
        "--source",
        str(tmp_path),
        "--data-file",
        str(data_file),
        str(source),
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        process.terminate()
        assert process.wait(timeout=15).returncode == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    data = ReferenceData(str(data_file))
    data.read()
    assert {3, 4, 5, 6}.issubset(set(data.lines(str(source)) or []))


def test_coverage_rcfile_can_explicitly_disable_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "example.py"
    source.write_text("x = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    _write_data(data_file, source, {1})
    monkeypatch.setenv("COVERAGE_RCFILE", os.devnull)

    assert main(["coverage", "report", "--data-file", str(data_file)]) == 0
    assert "100%" in capsys.readouterr().out


def test_report_sort_and_format_are_applied_by_the_rust_renderer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one = 1\ntwo = 2\n", encoding="utf-8")
    second.write_text("one = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    data = ReferenceData(str(data_file))
    data.add_lines({str(first): {1}, str(second): {1}})
    data.write()

    assert (
        main(
            [
                "coverage",
                "report",
                "--data-file",
                str(data_file),
                "--sort",
                "stmts",
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("| Name")
    assert "second.py" in lines[2]
    assert "first.py" in lines[3]

    assert main(["coverage", "report", "--data-file", str(data_file), "--format", "total"]) == 0
    assert capsys.readouterr().out.strip() == "67"


def test_report_implicit_combine_applies_configured_path_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    shard = ReferenceData(str(tmp_path / ".coverage.worker"))
    shard.add_lines({"/remote/build/src/example.py": {1}})
    shard.write()
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[paths]\nsource =\n    src\n    /remote/build/src\n",
        encoding="utf-8",
    )

    assert main(["coverage", "report", "--rcfile", str(config)]) == 0
    assert "example.py" in capsys.readouterr().out
    combined = ReferenceData(str(tmp_path / ".coverage"))
    combined.read()
    assert combined.measured_files() == {source.as_posix()}


def test_coveragerc_toml_and_context_filtered_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "example.py"
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    data = ReferenceData(str(tmp_path / ".coverage"))
    data.set_context("test_first")
    data.add_lines({str(source): {1}})
    data.set_context("test_second")
    data.add_lines({str(source): {2}})
    data.write()
    (tmp_path / ".coveragerc.toml").write_text(
        '[report]\ncontexts = ["test_first"]\n'
        '[json]\noutput = "filtered.json"\nshow_contexts = true\n',
        encoding="utf-8",
    )

    assert main(["coverage", "json"]) == 0
    capsys.readouterr()
    payload = json.loads((tmp_path / "filtered.json").read_text(encoding="utf-8"))
    report = payload["files"][source.name]
    assert report["executed_lines"] == [1]
    assert report["missing_lines"] == [2]
    assert report["contexts"] == {"1": ["test_first"]}


@pytest.mark.parametrize(
    ("core", "environment_core"),
    [
        ("ctrace", None),
        ("pytrace", None),
        ("sysmon", None),
        ("ctrace", "sysmon"),
        ("sysmon", "ctrace"),
        ("sysmon", "pytrace"),
        ("ctrace", ""),
    ],
)
def test_configured_tracer_core_matches_coverage_py_branch_data(
    tmp_path: Path, core: str, environment_core: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", f"[run]\ncore = {core}\nbranch = true\n")
    source = project.write(
        "branchy.py",
        "def values():\n"
        "    for value in range(3):\n"
        "        if value:\n"
        "            yield value\n"
        "result = list(values())\n",
    )
    environment = {"COVERAGE_CORE": environment_core}
    actual = project.run_vfy(
        "coverage", "run", "--data-file=.coverage-vfy", "branchy.py", env=environment
    )
    reference = project.run_coverage_py(
        "run", "--data-file=.coverage-reference", "branchy.py", env=environment
    )
    actual.expect(0)
    reference.expect(0)
    _assert_branch_data_parity(
        tmp_path / ".coverage-vfy", tmp_path / ".coverage-reference", source, tmp_path
    )


def _git_test_project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("src/calculator.py", "def add(left, right):\n    return left + right\n")
    project.write(
        "tests/test_calculator.py",
        "from src.calculator import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_add_again():\n    assert add(2, 3) == 5\n",
    )
    project.git_init()
    return project


def _native_runner_installed() -> bool:
    try:
        return (
            importlib.util.find_spec("vfy._pytest.native_pytest") is not None
            and sys.platform.startswith(("linux", "darwin"))
            and (importlib.metadata.version("pytest") == "9.1.1")
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def test_test_cli_describes_default_and_explicit_engines(tmp_path: Path) -> None:
    completed = VfyProject(tmp_path).run_vfy("test", "--help")

    completed.expect(0)
    help_text = " ".join(completed.stdout.split())
    assert "--engine {native,pytest-native,pytest}" in help_text
    assert (
        "select the test engine (default: native; pytest-native "
        "and pytest require installed pytest)"
    ) in help_text


def test_vfy_test_pytest_engine_forces_pytest_with_cache_service(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_engine.py",
        "def test_pytest_engine_keeps_cache(request):\n"
        "    request.config.cache.set('engine', 'pytest')\n"
        "    assert request.config.cache.get('engine', None) == 'pytest'\n",
    )

    completed = project.run_vfy("test", "--engine=pytest", "--workers=1", "--no-replay", "-q")

    completed.expect(0)
    assert "1 passed" in completed.stdout


def test_vfy_test_pytest_engine_supports_auto_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_engine.py",
        "def test_auto_worker_route_keeps_cache_service(request):\n"
        "    request.config.cache.set('engine', 'pytest')\n"
        "    assert request.config.cache.get('engine', None) == 'pytest'\n",
    )

    completed = project.run_vfy("test", "--engine=pytest", "--workers=auto", "--no-replay", "-q")

    completed.expect(0)
    assert "1 passed" in completed.stdout


def test_vfy_test_pytest_engine_uses_configured_asyncio(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nasyncio_mode = auto\n")
    project.write(
        "tests/test_async_engine.py",
        "import asyncio\nasync def test_async_engine_route():\n    await asyncio.sleep(0)\n",
    )

    completed = project.run_vfy("test", "--engine=pytest", "--workers=1", "--no-replay", "-q")

    completed.expect(0)
    assert "1 passed" in completed.stdout


@requires_native_pytest
@pytest.mark.parametrize("no_cov_source", ["cli", "config"])
def test_vfy_test_native_no_cov_ignores_configured_append(
    tmp_path: Path, no_cov_source: str
) -> None:
    project = VfyProject(tmp_path)
    configured = " --no-cov" if no_cov_source == "config" else ""
    arguments = ("--no-cov",) if no_cov_source == "cli" else ()
    project.write("pytest.ini", f"[pytest]\naddopts = --cov=product --cov-append{configured}\n")
    project.write("product.py", "def value():\n    return 42\n")
    project.write(
        "test_product.py",
        "from pathlib import Path\n"
        "from product import value\n"
        "def test_value():\n"
        "    assert value() == 42\n"
        "    Path('executed').write_text('yes')\n",
    )
    data = project.write(".coverage", "disabled coverage must preserve existing data\n")
    original = data.read_bytes()

    stock = project.run_pytest("-p", "pytest_cov.plugin", *arguments, "-q", load_xdist=False)
    stock.expect(0)
    assert "1 passed" in stock.stdout
    assert (tmp_path / "executed").read_text() == "yes"
    assert data.read_bytes() == original
    (tmp_path / "executed").unlink()

    native = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-replay", *arguments, "-q"
    )
    assert native.returncode == stock.returncode, native.stdout + native.stderr
    assert "1 passed" in native.stdout
    assert (tmp_path / "executed").read_text() == "yes"
    assert data.read_bytes() == original
    assert not list(tmp_path.glob(".coverage.*"))

    diagnostics = project.run_vfy(
        "diagnostics", "--engine=pytest-native", "--json", "--", *arguments
    )
    diagnostics.expect(0)
    assert not any(
        conflict["kind"] == "coverage_append"
        for conflict in json.loads(diagnostics.stdout)["plugin_conflicts"]
    )


def test_vfy_test_native_engine_requires_the_installed_runner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_engine.py",
        "from pathlib import Path\n"
        "def test_must_not_run():\n    Path('executed').write_text('yes')\n",
    )

    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "-q")

    if _native_runner_installed():
        completed.expect(0)
        assert "1 passed" in completed.stdout
    else:
        assert completed.returncode == 1
        assert "native pytest cannot run this workflow" in completed.stderr
        assert not (tmp_path / "executed").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native execution rejection")
def test_windows_native_engine_rejects_before_collection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "raise AssertionError('must not collect')\n")
    project.write(".coverage", "previous coverage")
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
    result.expect(1)
    assert "native pytest is supported only on Linux and macOS" in result.stderr
    assert "must not collect" not in result.stdout + result.stderr
    assert (tmp_path / ".coverage").read_text() == "previous coverage"


@pytest.mark.skipif(
    not _native_runner_installed(),
    reason="native pytest requires Linux or macOS with pytest 9.1.1",
)
def test_vfy_test_explicit_native_preaccept_failure_does_not_retry_pytest(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_runtest_call(item):\n"
        "    with open('hook-ran', 'a', encoding='utf-8') as stream:\n"
        "        stream.write(item.nodeid + '\\n')\n",
    )
    project.write(
        "tests/test_engine.py",
        "from pathlib import Path\n"
        "def test_must_not_run():\n    Path('executed').write_text('yes')\n",
    )

    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "-q")

    assert completed.returncode != 0
    assert "does not execute conftest hook 'pytest_runtest_call'" in (
        completed.stdout + completed.stderr
    )
    assert not (tmp_path / "hook-ran").exists()
    assert not (tmp_path / "executed").exists()


def test_pytest_engine_selects_after_a_baseline(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("src/first.py", "def value():\n    return 1\n")
    project.write("src/second.py", "def value():\n    return 2\n")
    project.write(
        "tests/test_first.py",
        "from src.first import value\n\ndef test_first():\n    assert value() == 1\n",
    )
    project.write(
        "tests/test_second.py",
        "from src.second import value\n\ndef test_second():\n    assert value() == 2\n",
    )
    project.git_init()

    baseline = project.run_vfy("test", "--engine=pytest", "--all", "-q")
    baseline.expect(0)
    assert (tmp_path / ".vfy/import-graph.json").is_file()
    project.write("src/first.py", "def value():\n    return int('1')\n")

    completed = project.run_vfy("test", "--engine=pytest", "-q")
    completed.expect(0)
    assert "1 passed" in completed.stdout
    assert "`vfy test --all` runs everything" in completed.stderr


def test_auto_workers_run_a_single_affected_test_without_xdist(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage\n.pytest_cache/\n__pycache__/\nworker-mode\n")
    project.write("src/first.py", "def value():\n    return 1\n")
    project.write("src/second.py", "def value():\n    return 2\n")
    project.write(
        "tests/test_first.py",
        "import os\nfrom pathlib import Path\nfrom src.first import value\n\n"
        "def test_first():\n"
        "    Path('worker-mode').write_text(os.environ.get('PYTEST_XDIST_WORKER', 'serial'))\n"
        "    assert value() == 1\n",
    )
    project.write(
        "tests/test_second.py",
        "from src.second import value\n\ndef test_second():\n    assert value() == 2\n",
    )
    project.git_init()
    baseline = project.run_vfy("test", "--engine=pytest", "--all", "-q")
    baseline.expect(0)
    project.write("src/first.py", "def value():\n    return int('1')\n")

    completed = project.run_vfy("test", "--engine=pytest", "-q")

    completed.expect(0)
    assert "1 passed" in completed.stdout
    assert (tmp_path / "worker-mode").read_text() == "serial"


def test_auto_workers_preserve_a_selected_failure_status(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage\n.vfy/\n.pytest_cache/\n__pycache__/\n")
    project.write("src/value.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_value.py",
        "from src.value import value\n\ndef test_value():\n    assert value() == 1\n",
    )
    project.git_init()
    baseline = project.run_vfy("test", "--engine=pytest", "--all", "-q")
    baseline.expect(0)
    # Change the size so stock Python invalidates bytecode within the same second.
    project.write("src/value.py", "def value():\n    return 20\n")

    completed = project.run_vfy("test", "--engine=pytest", "-q")

    assert completed.returncode == 1
    assert "1 failed" in completed.stdout


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("workers", ["0", "2"])
def test_selection_resolves_final_collection_ids_or_fails(
    tmp_path: Path, missing: bool, workers: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item._nodeid += '[renamed]'\n",
    )
    project.write(
        "tests/test_values.py",
        "from pathlib import Path\n"
        "def test_one():\n    Path('executed').write_text('one')\n"
        "def test_two():\n    raise AssertionError('must be deselected')\n",
    )
    selected = ["tests/test_values.py::test_one[renamed]"]
    if missing:
        selected.append("tests/test_values.py::test_missing[renamed]")
    candidates = project.write("selected.json", json.dumps(selected))
    result = project.run_pytest(
        "-n",
        workers,
        "-q",
        env={
            "VFY_SELECTED_TESTS": str(candidates),
            "VFY_TEST_RESULTS": str(tmp_path / "result.json"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
    )
    if missing:
        # xdist transports a collection failure; serial pytest uses a usage error.
        assert result.returncode == (4 if workers == "0" else 1), result.stdout + result.stderr
        assert "vfy failed to collect selected tests" in result.stdout + result.stderr
        assert not (tmp_path / "executed").exists()
        assert json.loads((tmp_path / "result.json").read_text()) == {
            "infrastructure_failed": True,
            "tests": [],
        }
    else:
        result.expect(0)
        assert "1 passed" in result.stdout
        assert (tmp_path / "executed").read_text() == "one"


@pytest.mark.parametrize(
    "width", ["auto", "explicit", "configured", "grouped", "grouped-auto", "grouped-configured"]
)
def test_selection_uses_final_node_ids_with_each_worker_configuration(
    tmp_path: Path, width: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage\n.vfy/\n.pytest_cache/\n__pycache__/\nexecuted\n")
    project.write(
        "conftest.py",
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    yield\n"
        "    for item in items:\n"
        "        item._nodeid += '[renamed]'\n",
    )
    if width == "configured":
        project.write("pytest.ini", "[pytest]\naddopts = -n 0\n")
    if width == "grouped-configured":
        project.write("pytest.ini", "[pytest]\naddopts = --dist=loadgroup\n")
    project.write("src/value.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_value.py",
        "import os\nimport pytest\nfrom pathlib import Path\nfrom src.value import value\n"
        "@pytest.mark.xdist_group('values')\n"
        "def test_value(request):\n"
        "    Path('executed').write_text(request.node.nodeid + ':' + "
        "os.environ.get('PYTEST_XDIST_WORKER_COUNT', 'serial'))\n"
        "    assert value() == 1\n",
    )
    project.write("tests/test_other.py", "def test_other():\n    pass\n")
    project.git_init()
    workers = ("-j", "2") if width in {"explicit", "grouped"} else ()
    if width in {"grouped", "grouped-auto"}:
        workers += ("--dist=loadgroup",)
    baseline = project.run_vfy("test", "--engine=pytest", "--all", *workers, "-q")
    baseline.expect(0)
    baseline_width = (tmp_path / "executed").read_text().rsplit(":", 1)[1]
    project.write("src/value.py", "def value():\n    return int('1')\n")

    selected = project.run_vfy("test", "--engine=pytest", *workers, "-q")

    selected.expect(0)
    assert "1 passed" in selected.stdout
    assert "complete coverage reconstructed" in selected.stderr
    actual = (tmp_path / "executed").read_text()
    expected_width = "2" if width in {"explicit", "grouped"} else "serial"
    if width in {"grouped-auto", "grouped-configured"}:
        expected_width = baseline_width
        assert expected_width.isdigit(), actual
    group = "@values" if width.startswith("grouped") else ""
    assert actual == (f"tests/test_value.py::test_value{group}[renamed]:{expected_width}")


def test_vfy_test_no_cov_still_uses_xdist_workers(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_worker_mode.py",
        "import os\n\n"
        "def test_vfy_uses_xdist():\n"
        "    assert os.environ['PYTEST_XDIST_WORKER'].startswith('gw')\n",
    )
    completed = project.run_vfy("test", "--engine=pytest", "-j", "2", "--no-cov")
    completed.expect(0)
    assert "3 passed" in completed.stdout


def test_vfy_test_workers_combine_coverage(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j", "2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    completed.expect(0)
    assert "100%" in completed.stdout
    assert project.lines("src/calculator.py") == [1, 2]


def test_vfy_workers_preserve_xdist_worker_protocol(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_worker_protocol.py",
        "def test_workerinput_is_available(request):\n"
        "    workerinput = getattr(request.config, 'workerinput', None)\n"
        "    assert workerinput is not None\n"
        "    assert workerinput['workerid'].startswith('gw')\n",
    )
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j", "2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    completed.expect(0)
    assert "3 passed" in completed.stdout


def test_vfy_workers_propagate_real_worker_loss(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_worker_loss.py",
        "import os\n\n"
        "def test_worker_crash():\n"
        "    os._exit(1)\n\n"
        "def test_surviving_one():\n"
        "    assert True\n\n"
        "def test_surviving_two():\n"
        "    assert True\n",
    )
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j", "2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    assert completed.returncode == 1
    assert "node down" in completed.stdout
    assert "worker 'gw" in completed.stdout
    assert "1 failed, 4 passed" in completed.stdout


def test_workers_honor_a_custom_data_file(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    data_file = tmp_path / "artifacts" / "coverage.data"
    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "-j2",
        "--data-file",
        str(data_file),
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    completed.expect(0)
    assert project.lines("src/calculator.py", data_file) == [1, 2]


def test_explicit_workers_override_matching_xdist_addopts(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write("pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["-n", "2"]\n')
    project.git_commit("configure xdist")
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    completed.expect(0)
    assert "100%" in completed.stdout


def test_workers_preserve_distribution_from_addopts(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\naddopts = ["-n", "2", "--dist=loadscope"]\n',
    )
    project.git_commit("configure xdist")
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    completed.expect(0)
    assert "2 passed" in completed.stdout


def test_vfy_test_workers_keep_pytest_failure_status(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_calculator.py",
        "from src.calculator import add\n\ndef test_failure():\n    assert add(1, 2) == 4\n",
    )
    completed = project.run_vfy(
        "test", "--engine=pytest", "-j2", "-q", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    assert completed.returncode == 1
    assert "1 failed" in completed.stdout
    assert (tmp_path / ".coverage").is_file()


def test_vfy_test_confirms_a_failure_without_hiding_it(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_calculator.py",
        "from pathlib import Path\n\n"
        "def test_unstable():\n"
        "    attempt = Path('.attempt')\n"
        "    if not attempt.exists():\n"
        "        attempt.write_text('primary')\n"
        "        assert False\n"
        "    assert True\n",
    )
    project.git_commit("make test unstable")

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert completed.returncode == 1
    assert "Confirming 1 failed test in a fresh serial process" in completed.stderr
    assert (
        "1 failed test passed on confirmation; the original run remains failed" in completed.stderr
    )
    assert "1 failed" in completed.stdout
    assert "1 passed" in completed.stdout
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    observations = history["tests"]["tests/test_calculator.py::test_unstable"]["observations"]
    assert [item["attempt"] for item in observations] == ["primary", "confirmation"]
    assert [item["outcome"] for item in observations] == ["failed", "passed"]
    assert observations[0]["run_id"] == observations[1]["run_id"]
    last_failed = json.loads(
        (tmp_path / ".pytest_cache/v/cache/lastfailed").read_text(encoding="utf-8")
    )
    assert last_failed == {"tests/test_calculator.py::test_unstable": True}


def test_vfy_test_does_not_confirm_a_successful_run(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-j1",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert completed.returncode == 0
    assert "Confirming" not in completed.stderr
    assert completed.stdout.count("2 passed") == 1


def test_vfy_test_confirms_a_persistent_failure_exactly_once(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_calculator.py",
        "from pathlib import Path\n\n"
        "def test_failure():\n"
        "    attempts = Path('.attempts')\n"
        "    count = int(attempts.read_text()) if attempts.exists() else 0\n"
        "    attempts.write_text(str(count + 1))\n"
        "    assert False\n",
    )
    project.git_commit("make test fail")

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert completed.returncode == 1
    assert (tmp_path / ".attempts").read_text(encoding="utf-8") == "2"
    assert "1 test remained failed on confirmation" in completed.stderr


def test_vfy_test_confirmation_is_serial_after_parallel_failure(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_calculator.py",
        "import os\n\n"
        "def test_worker_sensitive():\n"
        "    assert 'PYTEST_XDIST_WORKER' not in os.environ\n",
    )
    project.git_commit("make test worker sensitive")

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-j2",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert completed.returncode == 1
    assert "1 failed test passed on confirmation" in completed.stderr


def test_vfy_test_confirms_setup_and_teardown_failures(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write(
        "tests/test_calculator.py",
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def unstable_setup():\n"
        "    marker = Path('.setup-attempt')\n"
        "    if not marker.exists():\n"
        "        marker.write_text('failed')\n"
        "        raise RuntimeError('setup failed')\n\n"
        "@pytest.fixture\n"
        "def unstable_teardown():\n"
        "    yield\n"
        "    marker = Path('.teardown-attempt')\n"
        "    if not marker.exists():\n"
        "        marker.write_text('failed')\n"
        "        raise RuntimeError('teardown failed')\n\n"
        "def test_setup(unstable_setup):\n"
        "    pass\n\n"
        "def test_teardown(unstable_teardown):\n"
        "    pass\n",
    )
    project.git_commit("make fixture phases unstable")

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-j1",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    assert completed.returncode == 1
    assert "Confirming 2 failed tests" in completed.stderr
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    setup = history["tests"]["tests/test_calculator.py::test_setup"]["observations"]
    teardown = history["tests"]["tests/test_calculator.py::test_teardown"]["observations"]
    assert setup[0]["failed_phase"] == "setup"
    assert setup[1]["outcome"] == "passed"
    assert teardown[0]["failed_phase"] == "teardown"
    assert teardown[1]["outcome"] == "passed"


def test_vfy_test_does_not_confirm_collection_or_threshold_failures(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    project.write("tests/test_broken.py", "def broken(:\n")
    project.git_commit("break collection")
    collection = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert collection.returncode != 0
    assert "Confirming" not in collection.stderr

    (tmp_path / "tests/test_broken.py").unlink()
    project.write("src/uncovered.py", "def missed():\n    return 1\n")
    project.git_commit("add uncovered source")
    threshold = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "--source",
        "src",
        "--cov-fail-under=100",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert threshold.returncode == 1
    assert "Confirming" not in threshold.stderr


def test_workers_render_reports_and_thresholds_after_combine(tmp_path: Path) -> None:
    _git_test_project(tmp_path)
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vfy",
            "test",
            "--engine=pytest",
            "-j2",
            "--cov-report=json:coverage.json",
            "--cov-fail-under=100",
            "-q",
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
    assert payload["totals"]["percent_covered"] == 100.0
    assert "WARNING: Failed to generate report" not in completed.stdout


@requires_native_pytest
def test_vfy_test_one_worker_forces_serial_execution(tmp_path: Path) -> None:
    project = _git_test_project(tmp_path)
    completed = project.run_vfy("test", "--engine=native", "-j1", "-q")
    completed.expect(0)
    assert "2 passed" in completed.stdout


@pytest.mark.parametrize(
    "distribution", ["loadscope", "loadfile", "loadgroup", "worksteal", "each"]
)
def test_vfy_test_workers_run_every_stock_distribution_mode(
    tmp_path: Path, distribution: str
) -> None:
    _git_test_project(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "vfy",
            "test",
            "--engine=pytest",
            "-j",
            "2",
            f"--dist={distribution}",
            "-q",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    # `each` runs every test on every worker.
    assert ("4 passed" if distribution == "each" else "2 passed") in completed.stdout


def test_vfy_test_honors_existing_xdist_addopts(tmp_path: Path) -> None:
    _git_test_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-n", "2"]\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "configure xdist"], cwd=tmp_path, check=True)
    completed = subprocess.run(
        [sys.executable, "-m", "vfy", "test", "--engine=pytest"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "2 passed" in completed.stdout


def test_vfy_test_one_worker_overrides_existing_xdist_addopts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    _git_test_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["-n", "2"]\n', encoding="utf-8"
    )
    (tmp_path / "tests" / "test_worker_mode.py").write_text(
        "import os\n\n"
        "def test_vfy_one_worker_is_serial():\n"
        '    assert "PYTEST_XDIST_WORKER" not in os.environ\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "configure xdist"], cwd=tmp_path, check=True)
    completed = VfyProject(tmp_path).run_vfy("test", "--engine=pytest", "-j", "1", "-q")
    completed.expect(0)
    assert "3 passed" in completed.stdout


@requires_native_pytest
def test_native_workers_reject_combining_native_and_xdist_schedulers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["test", "--engine=pytest-native", "-n", "2", "--numprocesses", "2"]) == 1
    assert "cannot combine vfy's --workers/-j/-n with pytest-xdist" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["auto", "logical", "1", "2", "16"])
def test_vfy_test_worker_values_are_explicitly_validated(tmp_path: Path, value: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    assert True\n")

    completed = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-j", value)

    completed.expect(0)
    assert "1 passed" in completed.stdout
