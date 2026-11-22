"""Public hook signatures used by the independent test driver.

These declarations describe the pytest 9.1.1 hook boundary. The driver checks
execution support separately before accepting a plugin implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_F = TypeVar("_F", bound=Callable[..., object])


def _spec(*, firstresult: bool = False, historic: bool = False) -> Callable[[_F], _F]:
    def annotate(function: _F) -> _F:
        vars(function)["__vfy_hookspec__"] = {
            "firstresult": firstresult,
            "historic": historic,
            "warn_on_impl": None,
            "warn_on_impl_args": None,
        }
        return function

    return annotate


@_spec(historic=True)
def pytest_addhooks(pluginmanager: object) -> None: ...


@_spec(historic=True)
def pytest_plugin_registered(plugin: object, plugin_name: str, manager: object) -> None: ...


@_spec(historic=True)
def pytest_addoption(parser: object, pluginmanager: object) -> None: ...


@_spec(historic=True)
def pytest_configure(config: object) -> None: ...


@_spec(firstresult=True)
def pytest_cmdline_parse(pluginmanager: object, args: object) -> object: ...


@_spec()
def pytest_load_initial_conftests(early_config: object, parser: object, args: object) -> None: ...


@_spec(firstresult=True)
def pytest_cmdline_main(config: object) -> object: ...


@_spec(firstresult=True)
def pytest_collection(session: object) -> object: ...


@_spec()
def pytest_collection_modifyitems(session: object, config: object, items: object) -> None: ...


@_spec()
def pytest_collection_finish(session: object) -> None: ...


@_spec(firstresult=True)
def pytest_ignore_collect(collection_path: object, config: object) -> object: ...


@_spec(firstresult=True)
def pytest_collect_directory(path: object, parent: object) -> object: ...


@_spec()
def pytest_collect_file(file_path: object, parent: object) -> object: ...


@_spec()
def pytest_collectstart(collector: object) -> None: ...


@_spec()
def pytest_itemcollected(item: object) -> None: ...


@_spec()
def pytest_collectreport(report: object) -> None: ...


@_spec()
def pytest_deselected(items: object) -> None: ...


@_spec(firstresult=True)
def pytest_make_collect_report(collector: object) -> object: ...


@_spec(firstresult=True)
def pytest_pycollect_makemodule(module_path: object, parent: object) -> object: ...


@_spec(firstresult=True)
def pytest_pycollect_makeitem(collector: object, name: str, obj: object) -> object: ...


@_spec(firstresult=True)
def pytest_pyfunc_call(pyfuncitem: object) -> object: ...


@_spec()
def pytest_generate_tests(metafunc: object) -> None: ...


@_spec(firstresult=True)
def pytest_make_parametrize_id(config: object, val: object, argname: str) -> object: ...


@_spec(firstresult=True)
def pytest_runtestloop(session: object) -> object: ...


@_spec(firstresult=True)
def pytest_runtest_protocol(item: object, nextitem: object) -> object: ...


@_spec()
def pytest_runtest_logstart(nodeid: str, location: object) -> None: ...


@_spec()
def pytest_runtest_logfinish(nodeid: str, location: object) -> None: ...


@_spec()
def pytest_runtest_setup(item: object) -> None: ...


@_spec()
def pytest_runtest_call(item: object) -> None: ...


@_spec()
def pytest_runtest_teardown(item: object, nextitem: object) -> None: ...


@_spec(firstresult=True)
def pytest_runtest_makereport(item: object, call: object) -> object: ...


@_spec()
def pytest_runtest_logreport(report: object) -> None: ...


@_spec(firstresult=True)
def pytest_report_to_serializable(config: object, report: object) -> object: ...


@_spec(firstresult=True)
def pytest_report_from_serializable(config: object, data: object) -> object: ...


@_spec(firstresult=True)
def pytest_fixture_setup(fixturedef: object, request: object) -> object: ...


@_spec()
def pytest_fixture_post_finalizer(fixturedef: object, request: object) -> None: ...


@_spec()
def pytest_sessionstart(session: object) -> None: ...


@_spec()
def pytest_sessionfinish(session: object, exitstatus: object) -> None: ...


@_spec()
def pytest_unconfigure(config: object) -> None: ...


@_spec()
def pytest_assertrepr_compare(config: object, op: str, left: object, right: object) -> object: ...


@_spec()
def pytest_assertion_pass(item: object, lineno: int, orig: str, expl: str) -> None: ...


@_spec()
def pytest_report_header(config: object, start_path: object) -> object: ...


@_spec()
def pytest_report_collectionfinish(config: object, start_path: object, items: object) -> object: ...


@_spec(firstresult=True)
def pytest_report_teststatus(report: object, config: object) -> object: ...


@_spec()
def pytest_terminal_summary(
    terminalreporter: object, exitstatus: object, config: object
) -> None: ...


@_spec(historic=True)
def pytest_warning_recorded(
    warning_message: object, when: str, nodeid: str, location: object
) -> None: ...


@_spec()
def pytest_markeval_namespace(config: object) -> object: ...


@_spec()
def pytest_internalerror(excrepr: object, excinfo: object) -> object: ...


@_spec()
def pytest_keyboard_interrupt(excinfo: object) -> None: ...


@_spec()
def pytest_exception_interact(node: object, call: object, report: object) -> None: ...


@_spec()
def pytest_enter_pdb(config: object, pdb: object) -> None: ...


@_spec()
def pytest_leave_pdb(config: object, pdb: object) -> None: ...
