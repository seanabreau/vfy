"""Phase-sensitive cache and JUnit results match pinned pytest when the native
engine's pytest plug-in (`-p vfy._pytest.native_pytest`) replaces its cache provider."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, native: bool, *arguments: str) -> CommandResult:
    plugins = ["-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"] if native else []
    return project.run_pytest("-q", *plugins, *arguments, load_xdist=False)


def _last_step(project: VfyProject) -> object:
    state = json.loads((project.root / ".pytest_cache/v/cache/stepwise").read_text())
    return state["last_failed"] if isinstance(state, dict) else state


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_cache_numeric_subclasses_preserve_values_and_dictionary_keys(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_numeric.py",
        "class Integer(int):\n"
        "    def __str__(self): return '0'\n"
        "    def __repr__(self): return '0'\n"
        "    def __int__(self): return 0\n"
        "class Float(float):\n"
        "    def __str__(self): return '0'\n"
        "    def __repr__(self): return '0'\n"
        "    def __float__(self): return 0.0\n"
        "def test_cache(cache):\n"
        "    cache.set('review/numeric', {\n"
        "        'value': Integer(42), 'large': Integer(10**100),\n"
        "        Integer(43): 'integer key', Float(2.5): 'float key',\n"
        "        'float': Float(1.5)})\n",
    )
    result = _run(project, native)
    result.expect(0)
    stored = json.loads((project.root / ".pytest_cache/v/review/numeric").read_text())
    assert stored == {
        "value": 42,
        "large": 10**100,
        "43": "integer key",
        "2.5": "float key",
        "float": 1.5,
    }


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_cache_reads_overflowing_floats_and_respects_integer_conversion_limits(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(".pytest_cache/v/review/floats", "[1e999, -1e999, 1e-999]")
    project.write(".pytest_cache/v/review/integer", '{"value":' + "9" * 5000 + "}")
    project.write(
        "test_cache.py",
        "import math\n"
        "import sys\n"
        "def test_cache(cache):\n"
        "    assert cache.get('review/floats', None) == [math.inf, -math.inf, 0.0]\n"
        "    limit = sys.get_int_max_str_digits()\n"
        "    missing = object()\n"
        "    try:\n"
        "        sys.set_int_max_str_digits(4300)\n"
        "        assert cache.get('review/integer', missing) is missing\n"
        "        sys.set_int_max_str_digits(0)\n"
        "        assert cache.get('review/integer', missing) == {'value': 10**5000 - 1}\n"
        "    finally:\n"
        "        sys.set_int_max_str_digits(limit)\n",
    )
    result = _run(project, native)
    result.expect(0)


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_stepwise_retains_resume_point_when_setup_skips(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_steps.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def gate():\n"
        "    if Path('skip.flag').exists(): pytest.skip('skip setup')\n"
        "def test_a(): pass\n"
        "def test_b(): assert False\n",
    )
    failed = _run(project, native, "--sw")
    failed.expect(2)
    assert _last_step(project) == "test_steps.py::test_b"
    project.write("skip.flag", "skip\n")
    skipped = _run(project, native, "--sw")
    skipped.expect(0)
    assert _last_step(project) == "test_steps.py::test_b"


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_stepwise_skip_counts_call_and_teardown_failures_separately(
    tmp_path: Path, native: bool
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_steps.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def fail_teardown():\n"
        "    yield\n"
        "    raise ValueError('teardown failed')\n"
        "def test_a(fail_teardown): assert False\n"
        "def test_b(): Path('later.flag').touch()\n",
    )
    failed = _run(project, native, "--sw-skip")
    failed.expect(2)
    assert not (project.root / "later.flag").exists()
    assert _last_step(project) == "test_steps.py::test_a"
    lastfailed = json.loads((project.root / ".pytest_cache/v/cache/lastfailed").read_text())
    assert lastfailed == {"test_steps.py::test_a": True}


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
def test_junit_attributes_preserve_whitespace_on_round_trip(tmp_path: Path, native: bool) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_properties.py",
        "def test_property(record_property):\n"
        "    record_property('review', 'line1\\nline2\\ttab\\rend')\n",
    )
    result = _run(project, native, "--junitxml=results.xml", "-o", "junit_family=xunit1")
    result.expect(0)
    prop = ET.parse(project.root / "results.xml").find(".//testcase/properties/property")
    assert prop is not None
    assert prop.attrib == {"name": "review", "value": "line1\nline2\ttab\rend"}


@pytest.mark.parametrize("native", [False, True], ids=["stock", "native"])
@pytest.mark.parametrize("outcome", ["skip", "xfail"])
def test_junit_expected_failure_capture_has_one_output_element(
    tmp_path: Path, native: bool, outcome: str
) -> None:
    project = VfyProject(tmp_path / ("native" if native else "stock"))
    project.write(
        "test_capture.py",
        "import pytest\n"
        "def test_output():\n"
        "    print('before outcome')\n"
        f"    pytest.{outcome}('reason')\n",
    )
    result = _run(project, native, "--junitxml=results.xml", "-o", "junit_logging=system-out")
    result.expect(0)
    outputs = ET.parse(project.root / "results.xml").findall(".//testcase/system-out")
    assert len(outputs) == (2 if outcome == "skip" else 1)
    assert all("before outcome" in (output.text or "") for output in outputs)
