"""Live terminal output keeps stock subtest stdout and writer display widths."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_TEST = """def test_value(subtests):
    print('parent before')
    with subtests.test('child'):
        print('inside stdout')
    print('parent after')
"""


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_subtest_stdout_is_not_reordered_by_live_progress(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_a.py", _TEST)
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "--color=no")
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-s", "--color=never")
    )
    result.expect(0)
    assert result.stdout.startswith("parent before\ninside stdout\nuparent after\n.")


_WIDTH_TEST = r"""import io
import json
from pathlib import Path
from _pytest._io import TerminalWriter

def test_width():
    rows=[]
    for text in ['abc','e\u0301','界','\x00','\u200b','\x1b','\ud800','é界','\u0301','\u0600',
                 '', ' ~', '\x7f']:
        stream=io.StringIO()
        writer=TerminalWriter(file=stream)
        writer.write(text)
        rows.append([text,writer.width_of_current_line])
    Path('widths.json').write_text(json.dumps(rows))
"""


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_live_writer_width_uses_actual_unicode_database(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_width.py", _WIDTH_TEST)
    expected = None
    engines = ["stock"] if engine == "stock" else ["stock", "native"]
    for selected in engines:
        result = (
            project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
            if selected == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        project.write(f"{selected}.stdout", result.stdout)
        project.write(f"{selected}.stderr", result.stderr)
        result.expect(0)
        observed = json.loads((project.root / "widths.json").read_text())
        assert [row[1] for row in observed] == [3, 1, 2, 0, 0, -1, 1, 3, 0, 1, 0, 2, -1]
        if selected == "stock":
            expected = observed
        else:
            assert observed == expected


_LIVE_WIDTH_TEST = """import io
import json
import unicodedata
from pathlib import Path
from _pytest._io import TerminalWriter

def test_live_width():
    events = []
    stream = io.StringIO()
    writer = TerminalWriter(file=stream)
    class Text(str):
        def __iter__(self):
            events.append('subclass iteration')
            return iter('界e\\u0301')
    def replacement(form, text):
        events.append(['replacement', form, text])
        return '界'
    def normalize(form, text):
        events.append(['normalize', form, text])
        if text == 'ascii':
            return 'abcdef'
        if text == 'subclass':
            return Text('different contents')
        if text == 'iterable':
            return iter(['A', '\\x00', '\\u200b'])
        unicodedata.normalize = replacement
        return 'x'
    original = unicodedata.normalize
    unicodedata.normalize = normalize
    try:
        widths = []
        for text in ['ascii', 'subclass', 'iterable', 'replace', 'next']:
            writer._current_line = text
            widths.append(writer.width_of_current_line)
    finally:
        unicodedata.normalize = original
    Path('live-widths.json').write_text(json.dumps([widths, events]))
"""


@requires_native_pytest
def test_writer_width_reads_live_line_and_normalization_result(tmp_path: Path) -> None:
    expected = None
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_live_width.py", _LIVE_WIDTH_TEST)
        result = (
            project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observed = json.loads((project.root / "live-widths.json").read_text())
        if engine == "stock":
            expected = observed
            assert observed == [
                [6, 3, 1, 1, 2],
                [
                    ["normalize", "NFC", "ascii"],
                    ["normalize", "NFC", "subclass"],
                    "subclass iteration",
                    ["normalize", "NFC", "iterable"],
                    ["normalize", "NFC", "replace"],
                    ["replacement", "NFC", "next"],
                ],
            ]
        else:
            assert observed == expected


_VERBOSE_REASON_TEST = """import pytest
def test_value(subtests):
    with subtests.test('child'):
        pytest.skip('long reason ' * 12)
"""


@requires_native_pytest
@pytest.mark.parametrize("verbosity", ["-v", "-vv"])
def test_verbose_subtest_skip_reason_output_matches_stock(tmp_path: Path, verbosity: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", _VERBOSE_REASON_TEST)
    arguments = [verbosity, "-s", "test_body.py"]
    stock = project.run_python(
        "-B", "-m", "pytest", "-p", "no:cacheprovider", "--color=no", *arguments
    )
    independent = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--color=never", *arguments
    )
    assert stock.returncode == independent.returncode == 0, independent.stdout + independent.stderr
    assert _result_rows(independent.stdout) == _result_rows(stock.stdout)


@requires_native_pytest
def test_parallel_progress_preserves_subtest_reasons_and_captured_output(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_body.py",
        "import pytest\n"
        "def test_alpha(subtests):\n"
        "    print('captured 界 ' * 1000)\n"
        "    with subtests.test('子'):\n"
        "        pytest.skip('raison échec 界')\n"
        "def test_beta():\n"
        "    pass\n",
    )
    rows = []
    for workers in (1, 2):
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            f"--workers={workers}",
            "--native-scheduling=cases",
            "--color=never",
            "-vv",
            "-rP",
            f"--native-result-json=record-{workers}.json",
        )
        result.expect(0)
        assert "captured 界 " * 1000 in result.stdout
        topology = json.loads((project.root / f"record-{workers}.json").read_text())[
            "execution_topology"
        ]
        assert topology["requested_workers"] == topology["active_workers"] == workers
        assert len(set(topology["worker_pids"])) == workers
        rows.append(
            sorted(
                re.sub(r"\s+\[\s*\d+%\]", "", line)
                for line in result.stdout.splitlines()
                if line.startswith("test_body.py::")
            )
        )
    assert rows[0] == rows[1]
    assert any("raison échec 界" in row for row in rows[1])


@requires_native_pytest
def test_verbose_progress_origin_suffix_and_percent_match_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("base_mod.py", "class Base:\n    def test_inherited(self):\n        pass\n")
    project.write(
        "test_body.py",
        "from base_mod import Base\n\n\nclass TestChild(Base):\n"
        "    def test_own(self):\n        pass\n\n\ndef test_plain():\n    pass\n",
    )
    stock = project.run_python("-B", "-m", "pytest", "-vv", "-p", "no:cacheprovider", "--color=no")
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-vv", "--color=never")
    assert stock.returncode == independent.returncode == 0, independent.stdout + independent.stderr
    # Stock prints the origin suffix only at -vv, for a method inherited from another module.
    assert "test_body.py::TestChild::test_inherited <- base_mod.py PASSED" in stock.stdout
    assert "[ 33%]" in stock.stdout
    assert _result_rows(independent.stdout) == _result_rows(stock.stdout)


def _result_rows(text: str) -> list[str]:
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("test_body.py::")
        or (line.startswith("=") and "session starts" not in line)
    ]
    return [re.sub(r"in \d+\.\d+s", "in <duration>", row) for row in rows]
