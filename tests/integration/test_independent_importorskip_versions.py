"""Optional dependency version gates compare against pytest 9.1.1."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


def _version_cases() -> str:
    return """import json
import sys
from pathlib import Path
from types import ModuleType
import pytest

def test_observe():
    cases = [
        ('older', '1.9', '2.0'), ('equal', '2.0.0', '2'),
        ('newer', '2.1', '2.0'), ('missing', None, 'invalid'),
        ('invalid', 'invalid', '1'), ('invalid_minimum', '1', 'invalid'),
        ('both_invalid', 'bad-left', 'bad-right'),
        ('integer', 42, '1'), ('bytes', b'1', '1'), ('list', [], '1'),
        ('minimum_integer', '1', 42), ('unchecked', 'invalid', None),
        ('epoch', '100000000000000000000!1', '99999999999999999999!99'),
        ('release', '1.100000000000000000001', '1.100000000000000000002'),
        ('zeros', '01.000.0', '1'), ('implicit_zero', '0.1', '0.0.1'),
        ('alpha', '2alpha', '2a0'), ('beta', '2b1', '2rc1'),
        ('prerelease', '2rc100000000000000000001', '2rc100000000000000000002'),
        ('post', '2post100000000000000000001', '2post100000000000000000002'),
        ('dev', '2dev100000000000000000001', '2dev100000000000000000002'),
        ('implicit_post', '2post', '2post0'), ('implicit_dev', '2dev', '2dev0'),
        ('local_number', '1+100000000000000000001', '1+100000000000000000002'),
        ('local_string', '1+abc100000000000000000001', '1+abc99999999999999999999'),
        ('local_kind', '1+100000000000000000001', '1+z'),
        ('local_separators', '1+ABC_01-def', '1+abc.1.def'),
        ('python_whitespace', '\\x1c\\u00a0v01.0\\x85\\x1f', '1.0'),
        ('invalid_whitespace', '1.\\u00a00', '1'),
        ('unicode_digit', '\\u0661.0', '1'), ('unicode_local', '1+abc\\u0669', '1'),
        ('unicode_alias', '1po\\u017ft1', '1'), ('unicode_kelvin', '1+\\u212a', '1'),
        ('invalid_huge', '9' * 700 + 'bad', '1'),
        ('integer_limit', '1.' + '9' * 700, '1'),
        ('zero_digit_limit', '1.' + '0' * 700, '1'),
        ('local_digit_limit', '1+' + '9' * 700, '1'),
        ('minimum_digit_limit', '1', '1.' + '9' * 700),
        ('left_validation_first', 'bad', '1.' + '9' * 700),
    ]
    observed = {}
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        for label, version, minimum in cases:
            module = ModuleType('vfy_optional_version_case')
            if label != 'missing':
                module.__version__ = version
            sys.modules[module.__name__] = module
            try:
                result = pytest.importorskip(module.__name__, minversion=minimum, reason='ignored')
            except BaseException as error:
                error_module = (type(error).__module__
                                if type(error).__name__ == 'InvalidVersion' else None)
                observed[label] = [error_module, type(error).__name__, str(error),
                                   getattr(error, 'allow_module_level', None)]
            else:
                observed[label] = result is module
    finally:
        sys.set_int_max_str_digits(previous_limit)
        sys.modules.pop('vfy_optional_version_case', None)
    sys.set_int_max_str_digits(0)
    try:
        module = ModuleType('vfy_optional_version_case')
        sys.modules[module.__name__] = module
        for label, version, minimum in [
            ('unlimited_release', '1.' + '9' * 2000, '1.' + '9' * 1999),
            ('unlimited_epoch', '9' * 2000 + '!1', '9' * 1999 + '!1'),
            ('unlimited_local', '1+' + '9' * 1999, '1+' + '9' * 2000),
            ('unlimited_zero', '1.' + '0' * 2000, '1.0'),
        ]:
            module.__version__ = version
            try:
                result = pytest.importorskip(module.__name__, minversion=minimum)
            except BaseException as error:
                observed[label] = [type(error).__name__, str(error)]
            else:
                observed[label] = result is module
    finally:
        sys.set_int_max_str_digits(previous_limit)
        sys.modules.pop('vfy_optional_version_case', None)
    Path('observed.json').write_text(json.dumps(observed))
"""


@requires_native_pytest
def test_optional_dependency_version_ordering_and_errors_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_versions.py", _version_cases())
    oracle = project.run_pytest("-q", "test_versions.py", load_xdist=False)
    oracle.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    result = project.run_vfy("test", "--engine=native", "--no-cov", "test_versions.py")
    result.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected


@requires_native_pytest
def test_version_gate_never_imports_packaging(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_versions.py", _version_cases())
    project.write(
        "blocker.py",
        """import atexit
import importlib.abc
import json
import sys
from pathlib import Path

class BlockPackaging(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'packaging', 'pluggy'}:
            raise RuntimeError('forbidden runtime import: ' + fullname)
        return None

sys.meta_path.insert(0, BlockPackaging())

def snapshot():
    Path('blocked.json').write_text(json.dumps(sorted(
        name for name in sys.modules if name.split('.')[0] in {'packaging', 'pluggy'}
    )))

atexit.register(snapshot)
""",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "blocker", "test_versions.py"
    )
    result.expect(0)
    assert json.loads((tmp_path / "blocked.json").read_text()) == []
