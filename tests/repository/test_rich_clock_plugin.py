from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from benchmarks.benchmark_harness import benchmark_environment
from benchmarks.rich_clock_plugin import _FIXED_TIMESTAMP


def _write_proxy_package(root: Path) -> None:
    package = root / "rich"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "file_proxy.py").write_text(
        "import io\n"
        "from pathlib import Path\n"
        "\n"
        "class FileProxy(io.TextIOBase):\n"
        "    def __init__(self, console, file):\n"
        "        self.label = console\n"
        "\n"
        "    def flush(self):\n"
        "        with Path('finalizers.txt').open('a') as output:\n"
        "            output.write(self.label + '\\n')\n",
        encoding="utf-8",
    )


def test_rich_clock_fixture_is_scoped_and_restores_record_factory(tmp_path: Path) -> None:
    _write_proxy_package(tmp_path)
    (tmp_path / "conftest.py").write_text(
        "import logging\nORIGINAL_FACTORY = logging.getLogRecordFactory()\n",
        encoding="utf-8",
    )
    (tmp_path / "test_logging.py").write_text(
        "import logging\n"
        "def test_fixed_clock():\n"
        "    record = logging.getLogger('rich').makeRecord(\n"
        "        'rich', 20, __file__, 1, 'x', (), None)\n"
        f"    assert record.created == {_FIXED_TIMESTAMP!r}\n"
        "    assert record.msecs == 0\n",
        encoding="utf-8",
    )
    (tmp_path / "test_other.py").write_text(
        "import logging\n"
        "from conftest import ORIGINAL_FACTORY\n"
        "def test_factory_restored():\n"
        "    assert logging.getLogRecordFactory() is ORIGINAL_FACTORY\n"
        "    record = logging.getLogger('rich').makeRecord(\n"
        "        'rich', 20, __file__, 1, 'x', (), None)\n"
        f"    assert record.created != {_FIXED_TIMESTAMP!r}\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "rich_clock_plugin",
            "test_logging.py",
            "test_other.py",
        ],
        cwd=tmp_path,
        env=benchmark_environment({"PYTHONPATH": str(Path(__file__).parents[2] / "benchmarks")}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_rich_fixture_closes_file_proxies_at_test_teardown(tmp_path: Path) -> None:
    _write_proxy_package(tmp_path)
    (tmp_path / "test_finalizers.py").write_text(
        "from pathlib import Path\n"
        "from rich.file_proxy import FileProxy\n"
        "\n"
        "def test_01_finalizer_waits_until_teardown():\n"
        "    victim = FileProxy('first', None)\n"
        "    del victim\n"
        "    assert not Path('finalizers.txt').exists()\n"
        "\n"
        "def test_02_previous_finalizer_has_run():\n"
        "    assert Path('finalizers.txt').read_text() == 'first\\n'\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "rich_clock_plugin", "test_finalizers.py"],
        cwd=tmp_path,
        env=benchmark_environment({"PYTHONPATH": str(Path(__file__).parents[2] / "benchmarks")}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
