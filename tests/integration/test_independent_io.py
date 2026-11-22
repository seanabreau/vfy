"""Private terminal imports retain pytest's caller-owned stream behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_WRITER = """import io, json, os, shutil, sys
from pathlib import Path
from _pytest._io import TerminalWriter, get_terminal_width

def test_writer():
    events = []
    class Stream(io.StringIO):
        def isatty(self):
            events.append('isatty')
            return TTY
    stream = Stream()
    writer = TerminalWriter(file=stream)
    initial_width = writer.fullwidth
    os.environ['COLUMNS'] = '93'
    lazy_width = writer.fullwidth
    writer.fullwidth = 47
    writer.line('message', red=True)
    writer.line(s='keyword-line')
    writer.sep('-', 'title')
    writer.write('tail', flush=True)
    first_output = stream.getvalue()
    replacement = io.StringIO()
    writer._file = replacement
    writer.line('replacement')
    original_stdout = sys.stdout
    sys.stdout = replacement
    try:
        implicit = TerminalWriter()
    finally:
        sys.stdout = original_stdout
    implicit.line('implicit')
    os.environ['COLUMNS'] = str(2**70)
    huge_width = get_terminal_width()
    os.environ['COLUMNS'] = '93'
    original_size = shutil.get_terminal_size
    shutil.get_terminal_size = lambda **kwargs: (42.5, 24)
    try:
        protocol_width = get_terminal_width()
        shutil.get_terminal_size = lambda **kwargs: iter((94, 24))
        iterator_width = get_terminal_width()
    finally:
        shutil.get_terminal_size = original_size
    Path('observed.json').write_text(json.dumps({
        'markup': writer.hasmarkup, 'events': events,
        'initial_width': initial_width, 'lazy_width': lazy_width,
        'explicit_width': writer.fullwidth, 'current_width': get_terminal_width(),
        'huge_width': huge_width, 'protocol_width': protocol_width,
        'iterator_width': iterator_width,
        'first': first_output, 'replacement': replacement.getvalue(),
        'line_width': writer.width_of_current_line,
        'code_highlight': writer.code_highlight,
    }))
"""


@pytest.mark.parametrize(
    ("tty", "colors"),
    [
        (False, {}),
        (True, {}),
        (True, {"TERM": "dumb"}),
        (False, {"PY_COLORS": "1", "NO_COLOR": "1"}),
        (True, {"PY_COLORS": "0", "FORCE_COLOR": "1"}),
        (True, {"NO_COLOR": "1", "FORCE_COLOR": "1"}),
        (False, {"NO_COLOR": "", "FORCE_COLOR": "1"}),
    ],
)
def test_private_terminal_writer_matches_stock_stream_and_color_policy(
    tmp_path: Path, tty: bool, colors: dict[str, str]
) -> None:
    environment: dict[str, str | None] = {
        "PY_COLORS": None,
        "NO_COLOR": None,
        "FORCE_COLOR": None,
        "TERM": None,
        "COLUMNS": "20",
        **colors,
    }
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_writer.py", f"TTY = {tty!r}\n" + _WRITER)
        result = (
            project.run_python("-m", "pytest", "-q", env=environment)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=environment)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "observed.json").read_text()))
    assert observations[0] == observations[1]


def test_writer_dispatch_observes_message_cleanup_and_retains_failed_write(
    tmp_path: Path,
) -> None:
    source = """import gc, io, json
from pathlib import Path
from _pytest._io import TerminalWriter

def test_writer():
    events = []
    stream = io.StringIO()
    class WriteError(Exception): pass
    class Writer(TerminalWriter):
        def __getattribute__(self, name):
            if name == 'write_raw':
                events.append('lookup')
            return super().__getattribute__(name)
    writer = Writer(file=stream)
    fail = True
    def selected(msg, *, flush=False):
        nonlocal fail
        events.append(['raw', str(msg), flush])
        if fail:
            fail = False
            raise WriteError('actual raw write failure')
        stream.write(msg)
    class Original(str):
        def __del__(self):
            events.append('original released')
            writer.write_raw = selected
    class Rendered(str):
        def __del__(self):
            events.append(['rendered released', str(self)])
    def markup(msg, **kwargs):
        return Rendered('rendered ' + msg)
    writer.markup = markup
    retained = None
    try:
        writer.write(Original('input'), flush=True)
    except WriteError as error:
        events.append(['caught', str(error)])
        retained = error.__traceback__
    gc.collect()
    events.append(['retained', ['rendered released', 'rendered input'] not in events])
    writer.write('retry')
    events.append(['reused', stream.getvalue()])
    del retained
    gc.collect()
    events.append('done')
    Path('dispatch.json').write_text(json.dumps(events))
"""
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_writer.py", source)
        result = (
            project.run_python("-m", "pytest", "-q")
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "dispatch.json").read_text()))
    assert observations[0] == [
        "original released",
        "lookup",
        ["raw", "rendered input", True],
        ["caught", "actual raw write failure"],
        ["retained", True],
        "lookup",
        ["raw", "rendered retry", False],
        ["rendered released", "rendered retry"],
        ["reused", "rendered retry"],
        ["rendered released", "rendered input"],
        "done",
    ]
    assert observations[1] == observations[0]


@pytest.mark.parametrize("disabled", [False, True])
def test_independent_runner_executes_upstream_benchmark_fixture(
    tmp_path: Path, disabled: bool
) -> None:
    source = """def test_benchmark(benchmark):
    assert benchmark.pedantic(sum, args=([1, 2],), rounds=2, iterations=1) == 3
"""
    options = ("-p", "pytest_benchmark.plugin")
    if disabled:
        options += ("--benchmark-disable",)
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_benchmark.py", source)
        result = (
            project.run_python("-m", "pytest", "-q", *options)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", *options)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        if not disabled:
            assert "test_benchmark" in result.stdout
            assert "Name (time in" in result.stdout
