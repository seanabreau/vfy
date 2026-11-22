"""Exit helpers construct shared exceptions; native hooks own session control."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_exit_callable_and_exception_protocol_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "api.py",
        """import importlib
import inspect
import json
import sys
import warnings
api = importlib.import_module(sys.argv[1])
operation = api.exit
exception = operation.Exception
observed = {'signatures': []}
for target in [operation, exception]:
    signature = inspect.signature(target)
    observed['signatures'].append({
        'parameters': [[p.name, p.kind.name, p.default, p.annotation]
                       for p in signature.parameters.values()],
        'return': signature.return_annotation,
    })
observed['operation'] = [
    type(operation).__name__, vars(operation), getattr(operation, '__name__', None),
    getattr(operation, '__qualname__', None), type(operation).Exception is exception,
]
observed['inheritance'] = [issubclass(exception, Exception), issubclass(exception, SystemExit)]
observed['constructors'] = []
for arguments in [(), ('stop',), ('stop', 7), (None,), (17, 'opaque')]:
    value = exception(*arguments)
    observed['constructors'].append([
        list(value.args), str(value), repr(value), value.msg, value.returncode, vars(value),
    ])
observed['calls'] = []
for arguments, keywords in [
    ((), {}), (('stop',), {}), (('stop', 7), {}), ((), {'reason': None}),
    ((17,), {'returncode': 'opaque'}), ((), {'msg': 'deprecated'}),
    (('stop',), {'reason': 'duplicate'}), (('one', 2, 3), {}),
]:
    try:
        operation(*arguments, **keywords)
    except BaseException as error:
        traceback = error.__traceback__
        hidden = []
        while traceback is not None:
            if traceback.tb_frame.f_code.co_name == '__call__':
                hidden.append(traceback.tb_frame.f_locals.get('__tracebackhide__'))
            traceback = traceback.tb_next
        observed['calls'].append({
            'type': type(error).__name__, 'args': list(error.args), 'message': str(error),
            'attributes': vars(error), 'is_exit': isinstance(error, exception),
            'hidden': hidden,
        })
observed['warning_contexts'] = []
for name in ['warns', 'deprecated_call']:
    for control in ['exit', 'ordinary']:
        with warnings.catch_warnings(record=True) as escaped:
            warnings.simplefilter('always')
            try:
                factory = getattr(api, name)
                checker = factory(UserWarning) if name == 'warns' else factory()
                with checker:
                    warnings.warn('unmatched warning', RuntimeWarning)
                    if control == 'exit':
                        operation('stop', returncode=7)
                    raise ValueError('ordinary body error')
            except BaseException as error:
                observed['warning_contexts'].append([
                    name, control, type(error).__name__, str(error),
                    getattr(error, 'returncode', None),
                    [[item.category.__name__, str(item.message)] for item in escaped],
                ])
print(json.dumps(observed))
""",
    )
    oracle = project.run_python("api.py", "pytest")
    oracle.expect(0)
    actual = project.run_python("api.py", "vfy.independent.facade", env={"PYTHONPATH": _SOURCE})
    actual.expect(0)
    assert json.loads(actual.stdout) == json.loads(oracle.stdout)


def _exit_project(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write(
        "sitecustomize.py",
        """import json
import os
import sys
from pathlib import Path
if os.environ.get('VFY_BLOCK_UPSTREAM'):
    Path('attempts.jsonl').touch()
    class NoUpstream:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] in {'_pytest', 'pluggy', 'packaging', 'coverage'}:
                with Path('attempts.jsonl').open('a') as output:
                    output.write(json.dumps(fullname) + '\\n')
                raise RuntimeError('upstream import attempted: ' + fullname)
    sys.meta_path.insert(0, NoUpstream())
""",
    )
    project.write(
        "conftest.py",
        """import json
import os
from pathlib import Path
import pytest

events = []
original = None
live_session = None
controller = 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ

def describe(status):
    return [type(status).__name__, isinstance(status, pytest.ExitCode), int(status)]

def note(event, **values):
    if controller:
        events.append({'event': event, 'live_status': (
            None if live_session is None else describe(live_session.exitstatus)), **values})
        Path('events-' + os.environ['CONTROL_ENGINE'] + '.json').write_text(json.dumps(events))

def stop(phase):
    global original
    if controller and os.environ['CONTROL_PHASE'] == phase:
        try:
            pytest.exit('controlled stop', returncode=json.loads(os.environ['CONTROL_CODE']))
        except BaseException as error:
            original = error
            note('raise', exception=type(error).__name__, message=str(error),
                 returncode=error.returncode)
            raise

def pytest_configure(config):
    config.add_cleanup(lambda: note('cleanup'))
    note('configure')
    stop('configure')

def pytest_sessionstart(session):
    global live_session
    live_session = session
    note('start')

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    note('loop enter')
    result = yield
    note('loop exit')
    stop('loop')
    return result

def pytest_keyboard_interrupt(excinfo):
    note('keyboard_interrupt', original_identity=excinfo.value is original,
         handled_identity=__import__('sys').exception() is original,
         exception=type(excinfo.value).__name__)

def pytest_internalerror(excrepr, excinfo):
    note('internalerror', original_identity=excinfo.value is original,
         handled_identity=__import__('sys').exception() is original,
         exception=type(excinfo.value).__name__)

def pytest_sessionfinish(session, exitstatus):
    note('finish', argument=describe(exitstatus), same_object=exitstatus is session.exitstatus)
    stop('finish')

def pytest_unconfigure(config):
    note('unconfigure')
""",
    )
    for name in ["a", "b"]:
        project.write(
            f"test_{name}.py",
            """import os
from pathlib import Path
def test_body(request):
    with Path('body-' + os.environ['CONTROL_ENGINE'] + '-' + str(os.getpid())).open('a') as out:
        out.write(request.node.nodeid + '\\n')
""",
        )
    return project


@requires_native_pytest
@pytest.mark.parametrize("phase", ["configure", "loop", "finish"])
@pytest.mark.parametrize("returncode", [None, 7, 1, 0, -1])
def test_exit_lifecycle_order_status_types_and_original_notifications_match_pytest(
    tmp_path: Path, phase: str, returncode: int | None
) -> None:
    project = _exit_project(tmp_path)
    environment = {
        "PYTHONPATH": str(tmp_path),
        "CONTROL_PHASE": phase,
        "CONTROL_CODE": json.dumps(returncode),
    }
    oracle = project.run_python(
        "-m", "pytest", "-q", env={**environment, "CONTROL_ENGINE": "oracle"}
    )
    expected = json.loads((tmp_path / "events-oracle.json").read_text())
    assert "INTERNALERROR>" not in oracle.stdout + oracle.stderr
    assert [event["event"] for event in expected] == (
        ["configure", "raise", "keyboard_interrupt", "unconfigure", "cleanup"]
        if phase == "configure"
        else [
            "configure",
            "start",
            "loop enter",
            "loop exit",
            *(
                ["raise", "keyboard_interrupt", "finish"]
                if phase == "loop"
                else ["finish", "raise"]
            ),
            "unconfigure",
            "cleanup",
        ]
    )
    if returncode is None:
        assert oracle.returncode == (0 if phase == "finish" else 2)
    elif returncode >= 0:
        assert oracle.returncode == returncode
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        env={**environment, "CONTROL_ENGINE": "candidate", "VFY_BLOCK_UPSTREAM": "1"},
    )
    assert (tmp_path / "attempts.jsonl").read_text() == ""
    assert actual.returncode == oracle.returncode, (actual.stdout, actual.stderr)
    assert "INTERNALERROR>" not in actual.stdout + actual.stderr
    assert json.loads((tmp_path / "events-candidate.json").read_text()) == expected
    for engine in ["oracle", "candidate"]:
        bodies = [
            line
            for path in tmp_path.glob(f"body-{engine}-*")
            for line in path.read_text().splitlines()
        ]
        assert len(bodies) == (0 if phase == "configure" else 2)
