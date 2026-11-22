//! Exception interaction retains the original public call and report objects.

use super::NativeIndependentCallInfo;
use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(in crate::independent) fn eligible(
    py: Python<'_>,
    call: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let info = call.getattr("excinfo")?;
    if info.is_none() || report.hasattr("wasxfail")? {
        return Ok(false);
    }
    let exception = info.getattr("value")?;
    if exception.is_instance(&py.import("vfy.independent.facade")?.getattr("Skipped")?)?
        || exception.is_instance(&py.import("bdb")?.getattr("BdbQuit")?)?
    {
        return Ok(false);
    }
    let modules = py.import("sys")?.getattr("modules")?;
    if let Some(unittest) = modules.cast::<PyDict>()?.get_item("unittest")?
        && !unittest.is_none()
        && exception.is_instance(&unittest.getattr("SkipTest")?)?
    {
        return Ok(false);
    }
    Ok(true)
}

/// The caller dispatches this only after publishing the same completed report.
pub(in crate::independent) fn dispatch(
    py: Python<'_>,
    registry: &Bound<'_, NativeHookRegistry>,
    node: &Bound<'_, PyAny>,
    call: &Bound<'_, NativeIndependentCallInfo>,
    report: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if !call.borrow().has_exception() {
        return Ok(());
    }
    let removed_plugins = NativeHookRegistry::item_exclusions(registry, node)?;
    if !eligible(py, call.as_any(), report)? {
        return Ok(());
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("node", node)?;
    kwargs.set_item("call", call)?;
    kwargs.set_item("report", report)?;
    let result = NativeHookRegistry::call_subset(
        registry,
        "pytest_exception_interact",
        &kwargs,
        &removed_plugins,
        false,
    );
    drop(removed_plugins);
    drop(kwargs);
    result?;
    Ok(())
}

/// The public Subtests constructor retains its supplied hook relay.
pub(in crate::independent) fn dispatch_subtest(
    py: Python<'_>,
    relay: &Bound<'_, PyAny>,
    item: &Bound<'_, PyAny>,
    call: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("node", item)?;
    kwargs.set_item("call", call)?;
    kwargs.set_item("report", report)?;
    relay
        .getattr("pytest_exception_interact")?
        .call((), Some(&kwargs))?;
    Ok(())
}
