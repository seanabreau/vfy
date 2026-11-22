//! Own thread-warning capture and fault-handler scopes.

use super::Collector;
use super::unraisable::{ExceptionKind, exception_metadata};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyString};

const FAULT_RESOURCE: &str = "vfy.independent.faulthandler_support";
const PROVIDERS: [&str; 2] = ["threadexception", "faulthandler"];

impl Collector<'_> {
    pub(super) fn install_runtime_providers(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        for name in PROVIDERS {
            self.exclude_raw_provider(raw_plugins, name)?;
            self.activate_runtime_provider(name)?;
        }
        Ok(())
    }

    pub(super) fn activate_runtime_provider(&mut self, name: &str) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("runtime providers require a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager.call_method1("is_blocked", (name,))?.is_truthy()? {
            return Ok(());
        }
        let (resource, class) = match name {
            "threadexception" => ("vfy.independent.threadexception", "ThreadExceptionProvider"),
            "faulthandler" => (FAULT_RESOURCE, "FaulthandlerProvider"),
            _ => return Err(PyValueError::new_err("unknown runtime provider")),
        };
        let provider = self.py.import(resource)?.getattr(class)?.call0()?;
        if !manager
            .call_method1("register", (&provider, name))?
            .is_none()
        {
            self.plugins.insert(name.to_owned(), provider.unbind());
        }
        Ok(())
    }
}

#[pyfunction]
fn independent_thread_exception_metadata(
    summary: &Bound<'_, PyString>,
    traceback_message: &Bound<'_, PyString>,
    allocation: &Bound<'_, PyString>,
    exception: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    exception_metadata(
        ExceptionKind::Thread,
        summary,
        traceback_message,
        allocation,
        exception,
    )
}

fn fault_key<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(FAULT_RESOURCE)?.getattr(name)
}

pub(super) fn fork_descriptors(config: &Bound<'_, PyAny>) -> PyResult<Vec<i32>> {
    let stash = config.getattr("stash")?;
    let key = fault_key(config.py(), "fault_handler_stderr_fd_key")?;
    if stash.contains(&key)? {
        Ok(vec![stash.get_item(key)?.extract()?])
    } else {
        Ok(Vec::new())
    }
}

pub(super) fn rebind_fork(config: &Bound<'_, PyAny>) -> PyResult<()> {
    for descriptor in fork_descriptors(config)? {
        config
            .py()
            .import("os")?
            .call_method1("dup2", (2, descriptor))?;
    }
    Ok(())
}

#[pyfunction]
fn independent_faulthandler_configure(
    config: &Bound<'_, PyAny>,
    stderr: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = config.py();
    let handler = py.import("faulthandler")?;
    let stash = config.getattr("stash")?;
    if handler.call_method0("is_enabled")?.is_truthy()? {
        stash.set_item(
            fault_key(py, "fault_handler_original_stderr_fd_key")?,
            stderr,
        )?;
    }
    let descriptor = py.import("os")?.call_method1("dup", (stderr,))?;
    stash.set_item(fault_key(py, "fault_handler_stderr_fd_key")?, &descriptor)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("file", descriptor)?;
    handler.call_method("enable", (), Some(&kwargs))?;
    Ok(())
}

#[pyfunction]
fn independent_faulthandler_unconfigure(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = config.py();
    let handler = py.import("faulthandler")?;
    handler.call_method0("disable")?;
    let stash = config.getattr("stash")?;
    let descriptor_key = fault_key(py, "fault_handler_stderr_fd_key")?;
    if stash.contains(&descriptor_key)? {
        py.import("os")?
            .call_method1("close", (stash.get_item(&descriptor_key)?,))?;
        stash.del_item(descriptor_key)?;
    }
    let original_key = fault_key(py, "fault_handler_original_stderr_fd_key")?;
    if stash.contains(&original_key)? {
        handler.call_method1("enable", (stash.get_item(&original_key)?,))?;
        stash.del_item(original_key)?;
    }
    Ok(())
}

#[pyfunction]
fn independent_faulthandler_start(config: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = config.py();
    let timeout = config.call_method1("getini", ("faulthandler_timeout",))?;
    let timeout = if timeout.is_truthy()? {
        py.get_type::<PyFloat>().call1((timeout,))?
    } else {
        PyFloat::new(py, 0.0).into_any()
    };
    let exit = config.call_method1("getini", ("faulthandler_exit_on_timeout",))?;
    if !exit.is_instance_of::<PyBool>() {
        return Err(PyAssertionError::new_err(""));
    }
    if !timeout.gt(0)? {
        return Ok(false);
    }
    let descriptor = config
        .getattr("stash")?
        .get_item(fault_key(py, "fault_handler_stderr_fd_key")?)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("file", descriptor)?;
    kwargs.set_item("exit", exit)?;
    py.import("faulthandler")?
        .call_method("dump_traceback_later", (timeout,), Some(&kwargs))?;
    Ok(true)
}

#[pyfunction]
fn independent_faulthandler_cancel(py: Python<'_>) -> PyResult<()> {
    py.import("faulthandler")?
        .call_method0("cancel_dump_traceback_later")?;
    Ok(())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        independent_thread_exception_metadata,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent_faulthandler_configure,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent_faulthandler_unconfigure,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(independent_faulthandler_start, module)?)?;
    module.add_function(wrap_pyfunction!(independent_faulthandler_cancel, module)?)?;
    Ok(())
}
