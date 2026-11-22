"""Active Hypothesis statistics join the native suite-property publication."""

from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("broken", [False, True])
def test_active_hypothesis_junit_properties_match_live_report_statistics(
    tmp_path: Path, engine: str, broken: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """from pathlib import Path
from hypothesis import settings
settings.register_profile('vfy-junit', max_examples=3, database=None, deadline=None)

def pytest_runtest_logreport(report):
    if report.when == 'teardown':
        Path('live-statistics.txt').write_text(report.__dict__['_hypothesis_stats'])
""",
    )
    project.write(
        "product.py", f"def allowed(value): return {('value < 3' if broken else 'True')}\n"
    )
    project.write(
        "test_property.py",
        """from pathlib import Path
from hypothesis import given, strategies as st
from product import allowed

@given(st.integers(min_value=0, max_value=10))
def test_value(record_testsuite_property, value):
    record_testsuite_property('example', value)
    with Path('examples.txt').open('a') as output: output.write(str(value) + '\\n')
    assert allowed(value)
""",
    )
    arguments = (
        "-p",
        "hypothesispytest",
        "--hypothesis-profile=vfy-junit",
        "--hypothesis-seed=17",
        "--junitxml=report.xml",
        "-q",
    )
    result = (
        project.run_python("-m", "pytest", *arguments)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    assert result.returncode == int(broken), result.stdout + result.stderr
    properties = [
        (element.attrib["name"], element.attrib["value"])
        for element in ET.parse(tmp_path / "report.xml").findall("./testsuite/properties/property")
    ]
    examples = (tmp_path / "examples.txt").read_text().splitlines()
    assert properties[:-1] == [("example", value) for value in examples]
    name, encoded = properties[-1]
    assert name == "hypothesis-statistics-test_property.py::test_value"
    statistics = base64.b64decode(encoded, validate=True).decode()
    assert statistics == (tmp_path / "live-statistics.txt").read_text()
    assert "test_property.py::test_value" in statistics
    assert "passing, " in statistics
    project.write("observed-properties.json", json.dumps(properties))
