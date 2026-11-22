"""Method explanations retain Python method names and ordinary message representations."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_bound_method_assertions_match_pytest_names_and_messages(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_observe.py",
        """import json
from pathlib import Path
from types import MethodType

class Subject:
    def __repr__(self):
        return 'subject'
    def method(self):
        return True
    @classmethod
    def class_method(cls):
        return True

class Callable:
    def __repr__(self):
        return 'callable'
    def __call__(self, *args):
        return True

def identity(value):
    return value

Subject.method.__name__ = 'renamed'

def test_observe():
    observed = {}
    subject = Subject()
    method = subject.method
    named = Callable()
    named.__name__ = 'not_a_method'
    nameless = Callable()
    nameless_method = MethodType(nameless, subject)
    for scenario in ['attribute', 'classmethod', 'local', 'returned', 'message',
                     'named_callable', 'nameless_callable', 'nameless_method']:
        try:
            if scenario == 'attribute':
                assert not subject.method()
            if scenario == 'classmethod':
                assert not subject.class_method()
            if scenario == 'local':
                assert not method()
            if scenario == 'returned':
                assert not identity(subject.method)()
            if scenario == 'message':
                assert False, subject.method
            if scenario == 'named_callable':
                assert not named()
            if scenario == 'nameless_callable':
                assert not nameless()
            if scenario == 'nameless_method':
                assert not nameless_method()
        except (AssertionError, AttributeError) as error:
            observed[scenario] = [type(error).__name__, str(error)]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_pytest("-q", "test_observe.py", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    (tmp_path / "observed.json").unlink()
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "test_observe.py", env={"PYTHONPATH": _SOURCE}
    )
    actual.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected
    assert expected["nameless_method"][0] == "AttributeError"
