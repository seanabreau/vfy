"""Surrogate parameter IDs retain pytest's escapes without false collisions."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = r"""import pytest

VALUES = ['\udcbf', '\udc80', '\udcbf\udcbf\udcbf', '\ud800',
          '\ud800\udc80', '\ufffd', r'\udcbf', 'é雪😀\x00\\\n']

@pytest.mark.parametrize('value', VALUES)
def test_automatic(value):
    pass

@pytest.mark.parametrize('tail', [''])
@pytest.mark.parametrize('value', VALUES)
def test_stacked(value, tail):
    pass

@pytest.mark.parametrize('value', [pytest.param(value, id=value) for value in VALUES])
def test_explicit(value):
    pass

@pytest.mark.parametrize('value', range(len(VALUES)), ids=VALUES)
def test_listed(value):
    pass

@pytest.mark.parametrize('value', VALUES, ids=lambda value: value)
def test_callable(value):
    pass

@pytest.fixture(params=VALUES)
def resource(request):
    return request.param

def test_fixture(resource):
    pass

@pytest.mark.parametrize('value', [1, -1, True, None, 1.5, 2j, b"'\x00\\\n", '"'])
def test_other_values(value):
    pass
"""


def _cases(path: Path) -> list[dict[str, str]]:
    cases = list(ET.parse(path).iter("testcase"))
    assert all(not list(case) for case in cases)
    return [{key: value for key, value in case.attrib.items() if key != "time"} for case in cases]


@pytest.mark.parametrize("workers", [1, 2])
def test_surrogate_parameter_ids_match_pytest_and_explicit_selectors(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_surrogates.py", _SOURCE)
    modes = ["automatic", "stacked", "explicit", "listed", "callable", "fixture"]
    ids = [
        r"\udcbf",
        r"\udc80",
        r"\udcbf\udcbf\udcbf",
        r"\ud800",
        r"\ud800\udc80",
        r"\ufffd",
        r"\\udcbf",
        r"\xe9\u96ea\U0001f600\x00\\\n",
    ]
    names = [
        f"test_{mode}[{identifier}{'-' if mode == 'stacked' else ''}]"
        for mode in modes
        for identifier in ids
    ]
    selected = [names[index * len(ids)] for index in range(len(modes))]
    for selectors in ([], [f"test_surrogates.py::{name}" for name in selected]):
        options = ("-q", "--junitxml=results.xml", *selectors)
        stock = project.run_pytest(*options, load_xdist=False)
        stock.expect(0)
        expected = _cases(project.root / "results.xml")
        expected_names = [case["name"] for case in expected]
        if selectors:
            assert expected_names == selected
        else:
            assert expected_names[: len(names)] == names

        native = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-j",
            str(workers),
            "--native-result-json=results.json",
            *options,
        )
        native.expect(0)
        assert _cases(project.root / "results.xml") == expected
        record = json.loads((project.root / "results.json").read_text())
        assert [case["nodeid"] for case in record["cases"]] == [
            f"test_surrogates.py::{name}" for name in expected_names
        ]
        assert all(
            phase["outcome"] == "passed" for case in record["cases"] for phase in case["phases"]
        )


def test_surrogate_parameter_ids_reject_disabled_escaping(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n",
    )
    project.write(
        "test_surrogates.py",
        r"""import pytest
@pytest.mark.parametrize('value', ['\udcbf'])
def test_value(value):
    pass
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "--collect-only", "-q")
    result.expect(2)
    assert "UnicodeEncodeError" in result.stdout + result.stderr
    assert "surrogates not allowed" in result.stdout + result.stderr
