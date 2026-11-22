//! The independent driver admits only the hook services it dispatches, so a
//! plugin relying on any other hook fails visibly.

use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;

const SUPPORTED: &[&str] = &[
    "pytest_addhooks",
    "pytest_addoption",
    "pytest_cmdline_main",
    "pytest_plugin_registered",
    "pytest_warning_recorded",
    "pytest_internalerror",
    "pytest_keyboard_interrupt",
    "pytest_configure",
    "pytest_unconfigure",
    "pytest_sessionstart",
    "pytest_sessionfinish",
    "pytest_load_initial_conftests",
    "pytest_runtestloop",
    "pytest_terminal_summary",
    "pytest_report_header",
    "pytest_report_collectionfinish",
    "pytest_report_teststatus",
    "pytest_report_to_serializable",
    "pytest_report_from_serializable",
    "pytest_ignore_collect",
    "pytest_collection",
    "pytest_collection_modifyitems",
    "pytest_collection_finish",
    "pytest_collectreport",
    "pytest_generate_tests",
    "pytest_itemcollected",
    "pytest_deselected",
    "pytest_runtest_protocol",
    "pytest_runtest_setup",
    "pytest_fixture_setup",
    "pytest_fixture_post_finalizer",
    "pytest_runtest_call",
    "pytest_pyfunc_call",
    "pytest_runtest_teardown",
    "pytest_runtest_makereport",
    "pytest_markeval_namespace",
    "pytest_runtest_logstart",
    "pytest_runtest_logreport",
    "pytest_runtest_logfinish",
    "pytest_collect_file",
    "pytest_pycollect_makeitem",
    "pytest_pycollect_makemodule",
    "pytest_exception_interact",
    "pytest_assertrepr_compare",
    // Only pytest's debugger calls these, and the independent engine has no
    // --pdb or --trace; plug-ins such as pytest-timeout implement them.
    "pytest_enter_pdb",
    "pytest_leave_pdb",
];

pub(super) fn install(
    py: Python<'_>,
    registry: &Bound<'_, NativeHookRegistry>,
    manager: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let namespace = py.import("vfy.independent.hookspecs")?;
    manager.call_method1("add_hookspecs", (&namespace,))?;
    NativeHookRegistry::restrict_builtin_hooks(registry, &namespace, SUPPORTED)
}
