//! Attribute a startup hook failure to its registered implementation.

use super::{Implementation, invocation::CallArguments};
use pyo3::exceptions::PyException;
use pyo3::prelude::*;

pub(super) fn startup_hook(name: &str) -> bool {
    matches!(
        name,
        "pytest_addhooks"
            | "pytest_addoption"
            | "pytest_plugin_registered"
            | "pytest_load_initial_conftests"
            | "pytest_cmdline_main"
            | "pytest_configure"
            | "pytest_sessionstart"
    )
}

pub(super) fn startup_wrapper_unwind(name: &str) -> bool {
    // The command wrapper can resume after the complete test run.
    name != "pytest_cmdline_main" && startup_hook(name)
}

pub(super) fn note_failure(
    py: Python<'_>,
    implementation: &Py<Implementation>,
    arguments: &CallArguments<'_, '_>,
    error: &PyErr,
) {
    let CallArguments::Borrowed(kwargs) = arguments else {
        return;
    };
    let (plugin, name, hook) = {
        let implementation = implementation.borrow(py);
        if !startup_hook(&implementation.name) || !error.is_instance_of::<PyException>(py) {
            return;
        }
        (
            implementation.plugin.clone_ref(py),
            implementation.plugin_name.clone(),
            implementation.name.clone(),
        )
    };
    // Metadata and exception rendering can invoke user code. Keep the original
    // error if the optional attribution fails.
    let _ = py
        .import("vfy.independent.startup_diagnostics")
        .and_then(|diagnostics| {
            diagnostics.call_method1(
                "note_hook_error",
                (error.value(py), plugin, name, hook, kwargs),
            )
        });
}
