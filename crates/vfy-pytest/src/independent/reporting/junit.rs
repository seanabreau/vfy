//! Install pytest's `--junitxml` reporter for one configured session.

use crate::independent::assignment;
use crate::independent::lifecycle::Context;
use crate::results::NativeRunResult;
use pyo3::prelude::*;

impl Context {
    pub(in crate::independent) fn install_junit_lifecycle(&self, py: Python<'_>) -> PyResult<()> {
        self.config.bind(py).setattr("_vfy_junit_writer", py.None())
    }

    pub(in crate::independent) fn install_junit_writer(&self, py: Python<'_>) -> PyResult<()> {
        let config = self.config.bind(py);
        let destination = config.call_method1("getoption", ("junitxml", py.None()))?;
        if destination.is_none()
            || !destination.is_truthy()?
            || self
                .registry
                .bind(py)
                .call_method1("is_blocked", ("junitxml",))?
                .is_truthy()?
        {
            return Ok(());
        }
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("worker", self.invocation.is_worker())?;
        let writer = py
            .import("vfy.independent.junit")?
            .getattr("LogXML")?
            .call((config, destination), Some(&kwargs))?;
        config.setattr("_vfy_junit_writer", &writer)?;
        self.register(py, writer.unbind(), "vfy_junit_writer")
    }

    pub(in crate::independent) fn bind_junit_result(
        &self,
        py: Python<'_>,
        result: &Py<NativeRunResult>,
    ) -> PyResult<()> {
        let writer = self.config.bind(py).getattr("_vfy_junit_writer")?;
        if !writer.is_none() {
            writer.call_method1("bind_result", (result,))?;
        }
        Ok(())
    }

    /// Publish an empty run for a collection-stage failure, as pytest's sessionfinish does.
    pub(in crate::independent) fn bind_empty_junit_result(&self, py: Python<'_>) -> PyResult<()> {
        let writer = self.config.bind(py).getattr("_vfy_junit_writer")?;
        if writer.is_none() || !writer.getattr("result")?.is_none() {
            return Ok(());
        }
        let result = Py::new(py, assignment::empty_result(py, self)?)?;
        writer.call_method1("bind_result", (result,))?;
        Ok(())
    }
}
