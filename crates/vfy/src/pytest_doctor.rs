//! Python bindings for the conftest compatibility scan.

use std::path::Path;

use pyo3::prelude::*;

type ConftestFindingRecord = (String, String, String, Option<usize>);

#[pyfunction]
pub(crate) fn native_pytest_doctor_scan(root: &str) -> (Vec<ConftestFindingRecord>, usize, bool) {
    let scan = vfy_core::scan_conftests(Path::new(root));
    let findings = scan
        .findings
        .into_iter()
        .map(|finding| {
            (
                finding.kind.as_str().to_owned(),
                finding.name,
                finding.path.to_string_lossy().into_owned(),
                finding.line,
            )
        })
        .collect();
    (findings, scan.conftest_count, scan.truncated)
}

#[pyfunction]
pub(crate) fn native_pytest_plugin_module_supported(path: &str) -> bool {
    vfy_core::native_plugin_module_supported(Path::new(path))
}

#[pyfunction]
pub(crate) fn native_pytest_plugin_hook_supported(hook: &str) -> bool {
    vfy_core::native_plugin_hook_supported(hook)
}

#[pyfunction]
pub(crate) fn native_pytest_supported_conftest_hooks() -> Vec<&'static str> {
    vfy_core::SUPPORTED_CONFTEST_HOOKS.to_vec()
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(native_pytest_doctor_scan, module)?)?;
    module.add_function(wrap_pyfunction!(
        native_pytest_supported_conftest_hooks,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        native_pytest_plugin_module_supported,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        native_pytest_plugin_hook_supported,
        module
    )?)?;
    Ok(())
}
