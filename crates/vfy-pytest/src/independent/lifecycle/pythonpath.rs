//! Apply configured import paths before plugins and restore them at native cleanup.
use super::Context;
use pyo3::prelude::*;
use pyo3::types::PyString;
use std::cell::RefCell;

fn configured_paths(config: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyString>>> {
    config
        .call_method1("getini", ("pythonpath",))?
        .try_iter()?
        .map(|path| path?.str().map(Bound::unbind))
        .collect()
}

#[pyclass(frozen, unsendable)]
struct NativePythonPathCleanup {
    config: RefCell<Option<Py<PyAny>>>,
}

#[pymethods]
impl NativePythonPathCleanup {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let config = self
            .config
            .borrow()
            .as_ref()
            .map(|config| config.clone_ref(py));
        let Some(config) = config else {
            return Ok(());
        };
        let path = py.import("sys")?.getattr("path")?;
        for configured in configured_paths(config.bind(py))? {
            let configured = configured.bind(py);
            if path.contains(configured)? {
                path.call_method1("remove", (configured,))?;
            }
        }
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(config) = self.config.try_borrow() {
            visit.call(&*config)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let config = self.config.borrow_mut().take();
        drop(config);
    }
}

impl Context {
    pub(in crate::independent) fn configure_pythonpath(&self, py: Python<'_>) -> PyResult<()> {
        let configured = configured_paths(self.config.bind(py))?;
        let cleanup = Py::new(
            py,
            NativePythonPathCleanup {
                config: RefCell::new(Some(self.config.clone_ref(py))),
            },
        )?;
        // Register first so partial insertion is restored if a Python path
        // implementation raises while handling an insertion.
        self.config
            .bind(py)
            .call_method1("add_cleanup", (cleanup,))?;
        let path = py.import("sys")?.getattr("path")?;
        for configured in configured.into_iter().rev() {
            path.call_method1("insert", (0, configured))?;
        }
        Ok(())
    }
}
