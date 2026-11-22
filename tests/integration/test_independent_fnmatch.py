"""Live assertion-rewrite naming follows Python's fnmatch semantics."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


_MATRIX_TEST = """\
import importlib
import itertools
import json
import sys
from pathlib import Path

import pytest


def test_matrix(request):
    root = Path.cwd()
    sys.path.insert(0, str(root))
    names = ["a", "b", "!", "[", "\u03bb", "a\\nb", "sub.a", "sub.test_case"]
    alphabet = "ab*!?[]-"
    patterns = [
        "".join(characters) + ".py"
        for length in range(4)
        for characters in itertools.product(alphabet, repeat=length)
    ] + ["[b-a!].py", "[b-a!a].py", "[!-a].py", "[a--b].py", "[\u03bb-\u03c9].py", "sub/*.py",
         str(root / "sub" / "*.py")]
    results = {}
    for pattern in patterns:
        request.config.getini("python_files")[:] = [pattern]
        row = []
        for name in names:
            sys.modules.pop(name, None)
            try:
                module = importlib.import_module(name)
                with pytest.raises(AssertionError) as raised:
                    module.fail()
                row.append(str(raised.value))
            except BaseException as error:
                row.append(type(error).__name__)
        results[pattern.replace(str(root), "<root>")] = row
    Path("observed.json").write_text(json.dumps(results))
"""


def test_assertion_rewrite_names_follow_python_fnmatch_for_every_pattern(tmp_path: Path) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("driver.py", _MATRIX_TEST)
        body = "def fail():\n    assert 1 == 2\n"
        for name in ("a", "b", "!", "[", "\u03bb", "a\nb", "sub/a", "sub/test_case"):
            project.write(f"{name}.py", body)
        result = project.run_engine(engine, "-q", "driver.py::test_matrix")
        result.expect(0)
        observed.append(json.loads((project.root / "observed.json").read_text(encoding="utf-8")))
    assert observed[1] == observed[0]
    assert any("assert 1 == 2" in row for row in observed[0].values())


def test_import_rewriting_tracks_live_python_files(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        "def pytest_configure(config):\n    config.getini('python_files')[:] = ['picked_*.py']\n",
    )
    helper = "def fail(value, other):\n    assert value == other\n"
    project.write("picked_helper.py", helper)
    project.write("late_helper.py", helper)
    project.write(
        "driver.py",
        "import importlib\n"
        "import json\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def test_live_patterns(request):\n"
        "    picked = importlib.import_module('picked_helper')\n"
        "    with pytest.raises(AssertionError) as early:\n"
        "        picked.fail(1, 2)\n"
        "    request.config.getini('python_files')[:] = ['late_*.py']\n"
        "    late = importlib.import_module('late_helper')\n"
        "    with pytest.raises(AssertionError) as later:\n"
        "        late.fail(3, 4)\n"
        "    Path('observed.json').write_text(json.dumps([str(early.value), str(later.value)]))\n",
    )

    oracle = project.run_python("-m", "pytest", "-q", "driver.py::test_live_patterns")
    expected = (tmp_path / "observed.json").read_text(encoding="utf-8")
    (tmp_path / "observed.json").unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "driver.py::test_live_patterns"
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 0, diagnostic
    assert (tmp_path / "observed.json").read_text(encoding="utf-8") == expected, diagnostic
    assert expected == '["assert 1 == 2", "assert 3 == 4"]', diagnostic
