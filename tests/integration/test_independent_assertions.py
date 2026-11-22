"""Assertion and hook decorator protocols match the pinned pytest boundary."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from tests.support import VfyProject

_MODULE = """import json
import re
import warnings
from pathlib import Path

import pytest


def messages(records):
    return [(record.category.__name__, str(record.message)) for record in records]


def test_observe(pytestconfig):
    api = pytest
    observed = {}
"""


def _differential(tmp_path: Path, source: str) -> str:
    module = _MODULE + textwrap.indent(source, "    ")
    module += "\n    Path('observed.json').write_text(json.dumps(observed, sort_keys=True))\n"
    outputs = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_assertions.py", module)
        result = project.run_engine(engine, "-q", "test_assertions.py")
        assert result.returncode == 0, (engine, result.stdout, result.stderr)
        outputs.append((project.root / "observed.json").read_text())
    assert json.loads(outputs[1]) == json.loads(outputs[0])
    return outputs[1]


def test_warning_contexts_record_reemit_and_restore_filters(tmp_path: Path) -> None:
    observed = json.loads(
        _differential(
            tmp_path,
            """before = warnings.filters[:]
checker = api.warns((UserWarning, RuntimeWarning), match=re.compile('accept'))
assert warnings.filters == before
with warnings.catch_warnings(record=True) as escaped:
    warnings.simplefilter('always')
    with checker as recorded:
        warnings.warn('accept user', UserWarning)
        warnings.warn('other type', DeprecationWarning)
        warnings.warn('wrong message', RuntimeWarning)
        warnings.warn('accept runtime', RuntimeWarning)
    assert recorded.list is checker.list
    observed['recorded'] = messages(recorded)
    observed['reemitted'] = messages(escaped)
observed['filters_restored'] = warnings.filters == before
observed['popped'] = str(checker.pop(UserWarning).message)
checker.clear()
observed['cleared'] = len(checker)
with checker as reused:
    try:
        checker.__enter__()
    except RuntimeError as error:
        observed['nested_enter'] = str(error)
    warnings.warn('accept reused', RuntimeWarning)
observed['reused'] = messages(reused)
try:
    checker.__exit__(None, None, None)
except RuntimeError as error:
    observed['double_exit'] = str(error)
with api.warns(Warning) as specific:
    warnings.warn('child', UserWarning)
    warnings.warn('base', Warning)
observed['specific_pop'] = str(specific.pop(Warning).message)
""",
        )
    )
    assert observed["filters_restored"] is True
    assert observed["reemitted"] == [
        ["DeprecationWarning", "other type"],
        ["RuntimeWarning", "wrong message"],
    ]
    assert observed["specific_pop"] == "base"


def test_warning_failures_and_control_flow_preserve_exception_and_warning_behavior(
    tmp_path: Path,
) -> None:
    observed = json.loads(
        _differential(
            tmp_path,
            """modes = [
    'silent', 'category', 'pattern', 'body', 'matched_body', 'skip', 'invalid_message',
]
for mode in modes:
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter('always')
        try:
            with api.warns(UserWarning, match='wanted') as recorded:
                if mode in ['category', 'skip']:
                    warnings.warn('wrong type', RuntimeWarning)
                if mode == 'pattern':
                    warnings.warn('other message', UserWarning)
                if mode == 'matched_body':
                    warnings.warn('wanted warning', UserWarning)
                if mode in ['body', 'matched_body']:
                    raise ValueError('body failure')
                if mode == 'skip':
                    api.skip('control flow')
                if mode == 'invalid_message':
                    warnings.warn(UserWarning(12))
        except BaseException as error:
            observed[mode] = [type(error).__name__, str(error), messages(escaped)]
for name in ['warns', 'raises']:
    try:
        getattr(api, name)(UserWarning, match='wanted', unknown=True)
    except TypeError as error:
        observed[name + '_unknown'] = str(error)
""",
        )
    )
    assert observed["silent"][0] == "Failed"
    assert observed["matched_body"][:2] == ["ValueError", "body failure"]
    assert observed["skip"] == ["Skipped", "control flow", []]
    assert observed["invalid_message"][0] == "TypeError"


def test_callable_assertions_forward_arguments_and_return_the_public_results(
    tmp_path: Path,
) -> None:
    observed = json.loads(
        _differential(
            tmp_path,
            """def warning_call(value, *, match, check):
    warnings.warn('future API', FutureWarning)
    return [value, match, check]

observed['warns_return'] = api.warns(
    FutureWarning, warning_call, 7, match='forwarded', check='also forwarded',
)
observed['deprecated_return'] = api.deprecated_call(
    warning_call, 8, match='forwarded', check='also forwarded',
)
for category in [DeprecationWarning, PendingDeprecationWarning, FutureWarning]:
    with api.deprecated_call(match='old API') as recorded:
        warnings.warn('old API', category)
    observed[category.__name__] = messages(recorded)

def exception_call(value, *, match, check):
    raise ValueError(f'{value}:{match}:{check}')

info = api.raises(ValueError, exception_call, 9, match='forwarded', check='also forwarded')
observed['raises_info'] = [
    info.type.__name__, str(info.value), info.match('9:forwarded:also forwarded'),
    info.tb is not None,
]
with api.raises(match='note', check=lambda error: isinstance(error, ValueError)) as context:
    error = ValueError('message')
    error.add_note('note')
    raise error
observed['check_only'] = str(context.value)
try:
    api.raises(ValueError, lambda: None)
except BaseException as error:
    observed['missing_exception'] = [type(error).__name__, str(error)]
try:
    api.warns(UserWarning, lambda: None)
except BaseException as error:
    observed['missing_warning'] = [type(error).__name__, str(error)]
""",
        )
    )
    assert observed["warns_return"] == [7, "forwarded", "also forwarded"]
    assert observed["raises_info"] == [
        "ValueError",
        "9:forwarded:also forwarded",
        True,
        True,
    ]
    assert observed["missing_exception"][0] == "Failed"


def test_hook_decorators_preserve_signatures_defaults_and_validation_timing(tmp_path: Path) -> None:
    observed = json.loads(
        _differential(
            tmp_path,
            """import inspect
for name in ['hookimpl', 'hookspec']:
    decorator = getattr(api, name)
    observed[name + '_signature'] = [
        [parameter.name, parameter.kind.name, parameter.default]
        for parameter in inspect.signature(decorator).parameters.values()
    ]
    try:
        decorator(unknown=True)
    except TypeError as error:
        observed[name + '_unknown'] = "unexpected keyword argument 'unknown'" in str(error)

class Specifications:
    @staticmethod
    def pytest_decorated(value): pass
warning = DeprecationWarning('old implementation')
argument_warning = DeprecationWarning('old argument')
assert api.hookspec(Specifications.pytest_decorated, False, False, warning,
                    {'value': argument_warning}) is Specifications.pytest_decorated
manager = pytestconfig.pluginmanager
manager.add_hookspecs(Specifications)
options = manager.hook.pytest_decorated.spec.opts
observed['specification'] = [
    options['firstresult'], options['historic'], str(options['warn_on_impl']),
    str(options['warn_on_impl_args']['value']),
]

def implementation(value): pass
def default_implementation(value): pass
decorated = api.hookimpl(implementation, False, True, True, False, 'pytest_decorated', False)
assert decorated is implementation
assert api.hookimpl(default_implementation) is default_implementation
class Plugin:
    pytest_decorated = staticmethod(implementation)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    manager.register(Plugin(), 'decorated')
observed['registration_warnings'] = messages(caught)
impl, = manager.hook.pytest_decorated.get_hookimpls()
observed['implementation'] = [
    impl.hookwrapper, impl.wrapper, impl.optionalhook, impl.tryfirst, impl.trylast,
]
invalid = api.hookspec(firstresult=True, historic=True)
observed['invalid_constructed'] = True
try:
    invalid(lambda: None)
except ValueError as error:
    observed['invalid_applied'] = str(error)
""",
        )
    )
    assert observed["hookimpl_unknown"] is True
    assert observed["hookspec_unknown"] is True
    assert observed["invalid_constructed"] is True
    assert observed["invalid_applied"] == "cannot have a historic firstresult hook"
