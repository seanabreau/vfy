//! The ordinary unraisable provider owns capture, draining and shutdown policy.

use super::Collector;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyString;

mod drain;
mod queue;

const PROVIDER: &str = "unraisableexception";
const RESOURCE: &str = "vfy.independent.unraisable";

#[derive(Clone, Copy)]
pub(super) enum ExceptionKind {
    Unraisable,
    Thread,
}

impl ExceptionKind {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "unraisable" => Ok(Self::Unraisable),
            "thread" => Ok(Self::Thread),
            _ => Err(PyValueError::new_err("unknown exception capture kind")),
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Unraisable => "unraisable",
            Self::Thread => "thread",
        }
    }
}

impl Collector<'_> {
    pub(super) fn install_unraisable_provider(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_unraisable_provider()
    }

    pub(super) fn activate_unraisable_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("unraisable provider requires a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", (PROVIDER,))?
            .is_truthy()?
        {
            return Ok(());
        }
        let provider = self
            .py
            .import(RESOURCE)?
            .getattr("UnraisableProvider")?
            .call0()?;
        let registered = manager.call_method1("register", (&provider, PROVIDER))?;
        if !registered.is_none() {
            self.plugins.insert(PROVIDER.to_owned(), provider.unbind());
        }
        Ok(())
    }
}

#[pyfunction]
fn independent_unraisable_iterations(config: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = config.py();
    let default = if py
        .import("sys")?
        .getattr("implementation")?
        .getattr("name")?
        .eq("pypy")?
    {
        5
    } else {
        1
    };
    Ok(config
        .getattr("stash")?
        .call_method1(
            "get",
            (
                py.import(RESOURCE)?.getattr("gc_collect_iterations_key")?,
                default,
            ),
        )?
        .unbind())
}

#[pyfunction]
fn independent_unraisable_configured(config: &Bound<'_, PyAny>) -> PyResult<bool> {
    config.getattr("stash")?.contains(
        config
            .py()
            .import(RESOURCE)?
            .getattr("unraisable_exceptions")?,
    )
}

#[pyfunction]
fn independent_unraisable_prefix(unraisable: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    if unraisable.getattr("err_msg")?.is_none() {
        Ok(PyString::new(unraisable.py(), "Exception ignored in")
            .into_any()
            .unbind())
    } else {
        Ok(unraisable.getattr("err_msg")?.unbind())
    }
}

#[pyfunction]
fn independent_unraisable_metadata(
    summary: &Bound<'_, PyString>,
    traceback_message: &Bound<'_, PyString>,
    allocation: &Bound<'_, PyString>,
    exception: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    exception_metadata(
        ExceptionKind::Unraisable,
        summary,
        traceback_message,
        allocation,
        exception,
    )
}

pub(super) fn exception_metadata(
    kind: ExceptionKind,
    summary: &Bound<'_, PyString>,
    traceback_message: &Bound<'_, PyString>,
    allocation: &Bound<'_, PyString>,
    exception: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = summary.py();
    // Join real Unicode objects: names and diagnostics may contain lone surrogates.
    let empty = PyString::new(py, "");
    let message = empty.call_method1("join", ((summary, traceback_message, allocation),))?;
    let cause_message = empty.call_method1("join", ((summary, allocation),))?;
    let (resource, metadata) = match kind {
        ExceptionKind::Unraisable => (RESOURCE, "UnraisableMeta"),
        ExceptionKind::Thread => ("vfy.independent.threadexception", "ThreadExceptionMeta"),
    };
    Ok(py
        .import(resource)?
        .getattr(metadata)?
        .call1((message, cause_message, exception))?
        .unbind())
}

#[pyfunction(signature = (formatted))]
fn independent_unraisable_allocation(
    py: Python<'_>,
    formatted: Option<&Bound<'_, PyString>>,
) -> PyResult<Py<PyAny>> {
    if let Some(formatted) = formatted {
        return Ok(PyString::new(py, "\nObject allocated at:\n{}")
            .call_method1("format", (formatted,))?
            .unbind());
    }
    Ok(PyString::new(
        py,
        "Enable tracemalloc to get traceback where the object was allocated.\n\
         See https://docs.pytest.org/en/stable/how-to/capture-warnings.html#resource-warnings for more info.",
    )
    .into_any()
    .unbind())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<queue::NativeUnraisableQueue>()?;
    module.add_class::<drain::NativeUnraisableDrain>()?;
    module.add_function(wrap_pyfunction!(independent_unraisable_iterations, module)?)?;
    module.add_function(wrap_pyfunction!(independent_unraisable_configured, module)?)?;
    module.add_function(wrap_pyfunction!(independent_unraisable_prefix, module)?)?;
    module.add_function(wrap_pyfunction!(independent_unraisable_metadata, module)?)?;
    module.add_function(wrap_pyfunction!(independent_unraisable_allocation, module)?)?;
    Ok(())
}
