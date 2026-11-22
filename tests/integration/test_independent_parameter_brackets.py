"""Parameter IDs retain literal brackets in reports and explicit selectors."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [1, 2])
def test_nested_parameter_ids_match_pytest_reports_and_selectors(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_brackets.py",
        """import pytest

@pytest.mark.parametrize('tail', ['', 'x'])
@pytest.mark.parametrize('value', ['[a]', '[a,b]'])
@pytest.mark.parametrize('prefix', [''])
def test_stacked(prefix, value, tail):
    assert value

@pytest.fixture(params=['[fixture]'])
def resource(request):
    return request.param

@pytest.mark.parametrize('value', ['[a]'])
def test_fixture(resource, value):
    assert resource and value
""",
    )
    expected_names = [
        "test_stacked[-[a]-]",
        "test_stacked[-[a]-x]",
        "test_stacked[-[a,b]-]",
        "test_stacked[-[a,b]-x]",
        "test_fixture[[fixture]-[a]]",
    ]
    selections = [(), (f"test_brackets.py::{expected_names[0]}",)]
    for selection in selections:
        options = ("-q", "--junitxml=results.xml", *selection)
        stock = project.run_pytest(*options, load_xdist=False)
        stock.expect(0)
        expected = [
            ({key: value for key, value in case.attrib.items() if key != "time"}, list(case))
            for case in ET.parse(project.root / "results.xml").iter("testcase")
        ]
        assert [attributes["name"] for attributes, _ in expected] == (
            expected_names[:1] if selection else expected_names
        )

        native = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-j",
            str(workers),
            "--native-result-json=result.json",
            *options,
        )
        native.expect(0)
        actual = [
            ({key: value for key, value in case.attrib.items() if key != "time"}, list(case))
            for case in ET.parse(project.root / "results.xml").iter("testcase")
        ]
        assert actual == expected
        record = json.loads((project.root / "result.json").read_text())
        assert [case["nodeid"] for case in record["cases"]] == [
            f"test_brackets.py::{attributes['name']}" for attributes, _ in expected
        ]
        assert all(
            phase["outcome"] == "passed" for case in record["cases"] for phase in case["phases"]
        )
