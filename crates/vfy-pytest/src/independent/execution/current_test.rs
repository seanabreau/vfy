//! The runner owns the current-test environment at each phase boundary.

use crate::module_cache::Module;
use pyo3::prelude::*;
use std::borrow::Cow;
use vfy_core::test_execution::ExecutionPhase;

pub(super) fn update(item: &Bound<'_, PyAny>, phase: Option<ExecutionPhase>) -> PyResult<()> {
    let py = item.py();
    let environment = Module::Os.get(py)?.getattr(pyo3::intern!(py, "environ"))?;
    let key = pyo3::intern!(py, "PYTEST_CURRENT_TEST");
    if let Some(phase) = phase {
        let nodeid = item.getattr(pyo3::intern!(py, "nodeid"))?;
        let nodeid = nodeid.extract::<Cow<'_, str>>()?;
        let mut value = format!("{nodeid} ({})", phase.as_str());
        if value.contains('\0') {
            value = value.replace('\0', "(null)");
        }
        environment.set_item(key, value)?;
    } else {
        environment.call_method1(pyo3::intern!(py, "pop"), (key,))?;
    }
    Ok(())
}
