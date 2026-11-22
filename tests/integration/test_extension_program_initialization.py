"""Failed native program initialization preserves import state and can be retried."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


@pytest.mark.parametrize("foreign_module", [False, True])
def test_program_initialization_restores_modules_after_failure_and_retry(
    tmp_path: Path, foreign_module: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    result = project.run_python(
        "-c",
        f"""
import sys, types
from pathlib import Path

assert 'vfy' not in sys.modules
name = '_vfy_skipping_program'
foreign = types.ModuleType(name) if {foreign_module!r} else None
if foreign is not None:
    sys.modules[name] = foreign
attempts = 0
observations = 0

def observe_program(frame, event, argument):
    global observations
    if event == 'call' and frame.f_code.co_filename == '<vfy skipping program>':
        observations += 1
        assert sys.modules.get(name) is foreign, 'private program entered sys.modules'

def deny_first_compile(event, arguments):
    global attempts
    if event == 'compile' and arguments[1] == '<vfy skipping program>':
        attempts += 1
        if attempts == 1:
            raise RuntimeError('program initialization blocked')

sys.setprofile(observe_program)
sys.addaudithook(deny_first_compile)
try:
    from vfy import Coverage
except RuntimeError as error:
    assert str(error) == 'program initialization blocked'
else:
    raise AssertionError('the initialization failure was not observed')
assert attempts == 1
assert sys.modules.get(name) is foreign

from vfy import Coverage
assert attempts == 2
assert observations == 1
sys.setprofile(None)
assert sys.modules.get(name) is foreign
coverage = Coverage(source=['app.py'])
coverage.start()
import app
assert app.value() == 1
coverage.stop()
assert coverage.get_data().lines(str(Path('app.py').resolve())) == [1, 2]
print('initialization-retry-ok')
""",
    ).expect(0)
    assert result.stdout == "initialization-retry-ok\n"
