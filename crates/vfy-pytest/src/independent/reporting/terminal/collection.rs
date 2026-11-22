//! Compact collection IDs belong to the authenticated invocation's terminal owner.

use crate::independent::lifecycle::state::InvocationState;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use std::rc::{Rc, Weak};

pub(in crate::independent::reporting) fn start(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let tty = reporter.call_method0("isatty")?.is_truthy()?;
    let verbose = reporter
        .getattr("config")?
        .getattr("option")?
        .getattr("verbose")?;
    if verbose.ge(i32::from(!tty))? {
        let markup = pyo3::types::PyDict::new(reporter.py());
        markup.set_item("flush", true)?;
        markup.set_item("bold", true)?;
        reporter.call_method("write", ("collecting ... ",), Some(&markup))?;
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum Lines {
    Items,
    NodeIds,
}

fn enabled(invocation: &InvocationState, session: &Bound<'_, PyAny>) -> PyResult<bool> {
    if invocation.is_worker() {
        return Ok(false);
    }
    session
        .getattr("config")?
        .call_method1("getoption", ("collect_only", false))?
        .is_truthy()
}

fn emit(reporter: &Bound<'_, PyAny>, values: &Bound<'_, PyAny>, lines: Lines) -> PyResult<()> {
    if !values.is_truthy()? {
        return Ok(());
    }
    if super::live::verbosity(reporter)?.lt(-1)? {
        let py = reporter.py();
        let counts = pyo3::types::PyDict::new(py);
        for value in values.try_iter()? {
            let value = value?;
            let nodeid = match lines {
                Lines::Items => value.getattr("nodeid")?,
                Lines::NodeIds => value.clone(),
            };
            let path = nodeid.call_method1("split", ("::", 1))?.get_item(0)?;
            let count = match counts.get_item(&path)? {
                Some(count) => count.add(1)?,
                None => 1_i32.into_pyobject(py)?.into_any(),
            };
            counts.set_item(path, count)?;
        }
        let sorted = py
            .import("builtins")?
            .getattr("sorted")?
            .call1((counts.call_method0("items")?,))?;
        for pair in sorted.try_iter()? {
            let pair = pair?;
            let line = reporter.getattr("_tw")?.getattr("line")?;
            let text = pyo3::types::PyString::new(py, "{}: {}")
                .call_method1("format", (pair.get_item(0)?, pair.get_item(1)?))?;
            line.call1((text,))?;
        }
        return Ok(());
    }
    for value in values.try_iter()? {
        let value = value?;
        // Select the live writer before reading the current item's nodeid.
        let result = {
            let line = reporter.getattr("_tw")?.getattr("line")?;
            let text = match lines {
                Lines::Items => value.getattr("nodeid")?,
                Lines::NodeIds => value.clone(),
            };
            line.call1((text,))
        }?;
        drop(result);
    }
    Ok(())
}

/// This resource weakly references the same native authority, never its Session.
#[pyclass(frozen, unsendable, module = "vfy._native._pytest")]
struct NativeCollectionDisplay {
    invocation: Weak<InvocationState>,
}

#[pymethods]
impl NativeCollectionDisplay {
    fn __call__(&self, reporter: &Bound<'_, PyAny>, session: &Bound<'_, PyAny>) -> PyResult<()> {
        let invocation = self.invocation.upgrade().ok_or_else(|| {
            PyRuntimeError::new_err("terminal collection invocation is no longer available")
        })?;
        if invocation.is_worker() {
            return Ok(());
        }
        let kwargs = pyo3::types::PyDict::new(reporter.py());
        kwargs.set_item("final", true)?;
        reporter.call_method("report_collect", (), Some(&kwargs))?;
        let config = reporter.getattr("config")?;
        let hook = config
            .getattr("hook")?
            .getattr("pytest_report_collectionfinish")?;
        let kwargs = pyo3::types::PyDict::new(reporter.py());
        kwargs.set_item("config", config)?;
        kwargs.set_item("start_path", reporter.getattr("startpath")?)?;
        kwargs.set_item("items", session.getattr("items")?)?;
        let results = hook.call((), Some(&kwargs))?;
        super::write_report_lines(reporter, &results)?;
        if enabled(&invocation, session)? {
            emit(reporter, &session.getattr("items")?, Lines::Items)?;
        }
        Ok(())
    }
}

pub(in crate::independent) fn callback(
    py: Python<'_>,
    invocation: &Rc<InvocationState>,
) -> PyResult<Py<PyAny>> {
    Ok(Py::new(
        py,
        NativeCollectionDisplay {
            invocation: Rc::downgrade(invocation),
        },
    )?
    .into_any())
}

/// The controller consumes its existing validated result without inventing Items.
pub(in crate::independent) fn result(
    invocation: &InvocationState,
    session: &Bound<'_, PyAny>,
    result: &Bound<'_, crate::results::NativeRunResult>,
) -> PyResult<()> {
    if !enabled(invocation, session)? {
        return Ok(());
    }
    let reporter = session
        .getattr("config")?
        .getattr("pluginmanager")?
        .call_method1("get_plugin", ("terminalreporter",))?;
    if !reporter.is_none() {
        emit(
            &reporter,
            &result.getattr("collection_nodeids")?,
            Lines::NodeIds,
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
