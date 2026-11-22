"""Native phase diagnostics preserve actual source frames and fixture values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = {
    "fail_without_trace": (
        "import pytest\ndef test_value():\n    pytest.fail('failure reason', pytrace=False)\n"
    ),
    "plain": "def test_value():\n    raise ValueError('deliberate failure')\n",
    "raises": (
        "import pytest\n@pytest.mark.parametrize('value', ['fixture-value'])\n"
        "def test_value(value):\n    with pytest.raises(ValueError):\n        pass\n"
    ),
    "helper": (
        "def helper(value):\n    raise ValueError(value)\n\n"
        "def test_value():\n    helper('helper failure')\n"
    ),
    "hidden": (
        "def hidden(value):\n    __tracebackhide__ = lambda info: info.type is ValueError\n"
        "    raise ValueError(value)\n\n"
        "def test_value():\n    hidden('hidden helper failure')\n"
    ),
    "multiline": (
        "class TestValue:\n    def test_value(self):\n        raise ValueError(\n"
        "            'complete statement'\n        )\n"
        "    def __repr__(self):\n        return 'TEST_INSTANCE'\n"
    ),
    "setup": (
        "import pytest\n@pytest.fixture\ndef value():\n"
        "    raise ValueError('setup failure')\n\n"
        "def test_value(value):\n    pass\n"
    ),
    "teardown": (
        "import pytest\n@pytest.fixture\ndef value():\n    yield 42\n"
        "    raise ValueError('teardown failure')\n\n"
        "def test_value(value):\n    pass\n"
    ),
    "keyword_arguments": (
        "def helper(value, *args, option='default', **kwargs):\n"
        "    raise ValueError('failure')\n\n"
        "def test_value():\n"
        "    helper(42, 'extra', option='selected', other='keyword')\n"
    ),
    "long_argument": (
        "import pytest\n@pytest.mark.parametrize('value', ['a'*300])\n"
        "def test_value(value):\n    raise ValueError('failure')\n"
    ),
    "cause": (
        "def test_value():\n    try:\n        raise ValueError('original')\n"
        "    except ValueError as error:\n"
        "        raise TypeError('replacement') from error\n"
    ),
}


def _write_project(project: VfyProject, source: str) -> None:
    project.write("test_value.py", source)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.failed:
        with Path('diagnostics.jsonl').open('a') as output:
            output.write(json.dumps({'when': report.when, 'text': report.longreprtext}) + '\\n')
""",
    )


def _observe(project: VfyProject, *, candidate: bool) -> list[dict[str, str]]:
    command = project.run_engine("native" if candidate else "stock", "-q")
    command.expect(1)
    path = project.root / "diagnostics.jsonl"
    records: list[dict[str, str]] = [json.loads(line) for line in path.read_text().splitlines()]
    path.unlink()
    assert records, command.stdout + command.stderr
    return records


@pytest.mark.parametrize("source", _CASES.values(), ids=_CASES)
def test_failure_source_and_exception_text_match_stock(tmp_path: Path, source: str) -> None:
    project = VfyProject(tmp_path)
    _write_project(project, source)
    assert _observe(project, candidate=True) == _observe(project, candidate=False)
