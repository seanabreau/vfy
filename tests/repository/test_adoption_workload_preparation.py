"""Verify reviewed Rich preparation against real pipe closure and patch drift."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from validation.pytest_runner import check_native_adoption_matrix as matrix

RICH_PIPE_TEST = '''import os
import subprocess


def test_brokenpipeerror() -> None:
    """Test BrokenPipe works as expected."""
    which_py, which_head = (["which", cmd] for cmd in ("python", "head"))
    rich_cmd = "python -m rich".split()
    for cmd in [which_py, which_head, rich_cmd]:
        check = subprocess.run(cmd).returncode
        if check != 0:
            return  # Only test on suitable Unix platforms
    head_cmd = "head -1".split()
    proc1 = subprocess.Popen(rich_cmd, stdout=subprocess.PIPE)
    proc2 = subprocess.Popen(head_cmd, stdin=proc1.stdout, stdout=subprocess.PIPE)
    proc1.stdout.close()
    output, _ = proc2.communicate()
    proc1.wait()
    proc2.wait()
    assert proc1.returncode == 1
    assert proc2.returncode == 0


def test_capture_and_record() -> None:
    pass
'''

RICH_LIVE_TEST = '''# encoding=utf-8
import time
from typing import Optional

# import pytest


def test_growing_display_autorefresh() -> None:
    """Test generating a table but using auto-refresh from threading"""
    console = create_capture_console(height=5)
    console.begin_capture()
    with Live(console=console, auto_refresh=True, vertical_overflow="visible") as live:
        display = ""
        for step in range(10):
            display += f"Step {step}\\n"
            live.update(display)
            time.sleep(0.2)

    # no way to truly test w/ multithreading, just make sure it doesn't crash


def test_growing_display_console_redirect() -> None:
    pass
'''

RICH_TRACEBACK_TEST = """
def test_recursive():
    def foo(n):
        return bar(n)

    def bar(n):
        return foo(n)

    console = Console(width=100, file=io.StringIO())
    try:
        foo(1)
    except Exception:
        console.print_exception(max_frames=6)
    result = console.file.getvalue()
    print(result)
    assert "frames hidden" in result
    assert result.count("in foo") < 4
"""


@pytest.fixture
def rich_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "rich"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_console.py").write_text(RICH_PIPE_TEST)
    (root / "tests/test_live.py").write_text(RICH_LIVE_TEST)
    (root / "tests/test_traceback.py").write_text(RICH_TRACEBACK_TEST)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    # The small fixture retains the pinned patch context; git apply itself is
    # real, while the revision guard is exercised separately below.
    monkeypatch.setattr(matrix, "git", lambda *_args: matrix.PROJECTS_BY_KEY["rich"].revision)
    return root


def prepare_rich(root: Path, output: Path) -> None:
    matrix.prepare_project(matrix.PROJECTS_BY_KEY["rich"], root, Path(sys.executable), output, 10)


@pytest.mark.parametrize("broken_pipe_status", [1, 0])
@pytest.mark.skipif(sys.platform == "win32", reason="the pinned pipe test uses Unix commands")
def test_prepared_rich_pipe_contract_is_independent_of_producer_scheduling(
    rich_checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken_pipe_status: int,
) -> None:
    source = rich_checkout / "tests/test_console.py"
    before = matrix.file_sha256(source)
    output = tmp_path / "preparation"
    prepare_rich(rich_checkout, output)

    provenance = json.loads((output / "workload-patch.json").read_text())
    source_hashes = provenance["sources"]["tests/test_console.py"]
    assert source_hashes["before_sha256"] == before
    assert source_hashes["after_sha256"] == matrix.file_sha256(source)
    assert provenance["patch_sha256"] == matrix.file_sha256(matrix.ROOT / provenance["patch"])
    assert before != source_hashes["after_sha256"]

    # This real subprocess has the demo's success-versus-broken-pipe exit
    # distinction without requiring Rich as a repository-test dependency.
    package = rich_checkout / "rich"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import os\n"
        "try:\n"
        "    os.write(1, b'demo output\\n' * 600)\n"
        "except BrokenPipeError:\n"
        f"    raise SystemExit({broken_pipe_status})\n"
    )
    monkeypatch.chdir(rich_checkout)
    monkeypatch.setenv("PYTHONPATH", str(rich_checkout))
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    run = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_path('tests/test_console.py')['test_brokenpipeerror']()",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert run.returncode == (0 if broken_pipe_status == 1 else 1)
    assert "demo output" in run.stdout
    if broken_pipe_status == 0:
        assert "AssertionError" in run.stderr


def test_rich_preparation_rejects_changed_patch_context_without_mutation(
    rich_checkout: Path, tmp_path: Path
) -> None:
    source = rich_checkout / "tests/test_console.py"
    changed = RICH_PIPE_TEST.replace('head_cmd = "head -1".split()', 'head_cmd = ["different"]')
    source.write_text(changed)

    with pytest.raises(subprocess.CalledProcessError):
        prepare_rich(rich_checkout, tmp_path / "preparation")

    assert source.read_text() == changed
    assert not (tmp_path / "preparation/workload-patch.json").exists()


def test_rich_preparation_requires_reviewed_revision(rich_checkout: Path, tmp_path: Path) -> None:
    project = replace(matrix.PROJECTS_BY_KEY["rich"], revision="different-revision")

    with pytest.raises(RuntimeError, match="reviewed revision"):
        matrix.prepare_project(project, rich_checkout, Path(sys.executable), tmp_path, 10)

    assert (rich_checkout / "tests/test_console.py").read_text() == RICH_PIPE_TEST


def test_prepared_recursive_workload_retains_real_bounded_traceback(
    rich_checkout: Path, tmp_path: Path
) -> None:
    prepare_rich(rich_checkout, tmp_path / "preparation")
    tree = ast.parse((rich_checkout / "tests/test_traceback.py").read_text())
    test = tree.body[0]
    assert isinstance(test, ast.FunctionDef)
    definitions = ast.Module(body=test.body[:2], type_ignores=[])
    namespace: dict[str, object] = {}
    exec(compile(definitions, "prepared-recursive-workload.py", "exec"), namespace)
    foo = namespace["foo"]
    assert callable(foo)

    with pytest.raises(RecursionError, match=r"^deterministic recursion depth$") as raised:
        foo(1)

    traceback = raised.value.__traceback__
    frames = []
    while traceback is not None:
        frames.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next

    assert frames.count("foo") == 20
    assert frames.count("bar") == 19
