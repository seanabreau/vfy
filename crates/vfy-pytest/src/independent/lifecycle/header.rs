//! Consume plugin header results at the terminal reporter's session-start priority.

use super::Context;
use crate::hooks::NativeHookRegistry;
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::RefCell;

struct HeaderContext {
    config: Py<PyAny>,
    registry: Py<NativeHookRegistry>,
    invocation: std::rc::Rc<super::state::InvocationState>,
}

#[pyclass(frozen, unsendable)]
pub(super) struct NativeHeaderCallback {
    context: RefCell<Option<HeaderContext>>,
}

#[pymethods]
impl NativeHeaderCallback {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let context = self.context.borrow_mut().take().ok_or_else(|| {
            PyAssertionError::new_err("native terminal header callback is no longer available")
        })?;
        let config = context.config.bind(py);
        let verbosity = config
            .getattr("option")?
            .getattr("verbose")?
            .extract::<i64>()?;
        // Workers leave the session header to the terminal-owning controller.
        if verbosity < 0 || context.invocation.is_worker() {
            return Ok(());
        }
        let registry = context.registry.bind(py);
        let reporter = registry.call_method1("get_plugin", ("terminalreporter",))?;
        if reporter.is_none() {
            return Err(PyAssertionError::new_err(
                "terminal header requires the configured terminalreporter plugin",
            ));
        }
        let bold = PyDict::new(py);
        bold.set_item("bold", true)?;
        reporter.call_method("write_sep", ("=", "test session starts"), Some(&bold))?;
        if config
            .call_method1("getoption", ("no_header", false))?
            .is_truthy()?
        {
            return Ok(());
        }
        reporter.call_method1("write_line", (platform_line(py, verbosity)?,))?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("config", config)?;
        kwargs.set_item("start_path", config.getattr("_invocation_dir")?)?;
        let results =
            NativeHookRegistry::call(registry, py, "pytest_report_header", &kwargs, false)?;
        crate::independent::reporting::terminal::write_report_lines(&reporter, results.bind(py))?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(context) = self.context.try_borrow()
            && let Some(context) = context.as_ref()
        {
            visit.call(&context.config)?;
            visit.call(&context.registry)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let context = self.context.borrow_mut().take();
        drop(context);
    }
}

/// Report the Python platform and the vfy package version.
fn platform_line(py: Python<'_>, verbosity: i64) -> PyResult<String> {
    let sys = py.import("sys")?;
    let mut line = format!(
        "platform {} -- Python {}, vfy-{}",
        sys.getattr("platform")?.extract::<String>()?,
        py.import("platform")?
            .call_method0("python_version")?
            .extract::<String>()?,
        py.import("vfy")?
            .getattr("__version__")?
            .extract::<String>()?,
    );
    if verbosity > 0 {
        line.push_str(" -- ");
        line.push_str(&sys.getattr("executable")?.extract::<String>()?);
    }
    Ok(line)
}

pub(super) fn callback(py: Python<'_>, context: &Context) -> PyResult<Py<NativeHeaderCallback>> {
    Py::new(
        py,
        NativeHeaderCallback {
            context: RefCell::new(Some(HeaderContext {
                config: context.config.clone_ref(py),
                registry: context.registry.clone_ref(py),
                invocation: context.invocation.clone(),
            })),
        },
    )
}
