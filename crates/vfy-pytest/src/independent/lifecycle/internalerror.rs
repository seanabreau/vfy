//! Terminal error output runs at the builtin hook priority.
use pyo3::prelude::*;
use pyo3::types::PyWeakrefReference;

/// Mirror stock `TerminalReporter.pytest_internalerror`: one `write_line` per split line.
fn write(reporter: &Bound<'_, PyAny>, representation: &str) -> PyResult<()> {
    for line in representation.split('\n') {
        reporter.call_method1("write_line", (format!("INTERNALERROR> {line}"),))?;
    }
    Ok(())
}

#[pyclass(frozen, module = "vfy.independent.terminal")]
pub(super) struct NativeInternalErrorHooks {
    /// The terminal reporter owns the progress-line state that `write_line` must respect.
    /// A weak reference keeps this native plugin out of the reporter's reference cycles.
    pub(super) reporter: Py<PyWeakrefReference>,
}

#[pymethods]
impl NativeInternalErrorHooks {
    fn pytest_internalerror(&self, py: Python<'_>, excrepr: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Some(reporter) = self.reporter.bind(py).upgrade() else {
            return Ok(false);
        };
        write(&reporter, excrepr.str()?.to_str()?)?;
        Ok(true)
    }
}
