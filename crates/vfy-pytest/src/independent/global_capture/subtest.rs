//! Separate subtest output buffers over `CPython` IO resources.

use super::{NativeGlobalCapture, owner};
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};
use vfy_core::test_execution::PhasePayload;

/// Closing restores the saved outer descriptors without draining their buffers.
pub(in crate::independent) struct SubtestOutput {
    output: Option<Py<PyAny>>,
}

impl SubtestOutput {
    pub(in crate::independent) fn start(config: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = config.py();
        let global = owner(config)?;
        if global.is_none() {
            return Ok(Self { output: None });
        }
        if Self::has_fixture(py, global.as_ref())? {
            return Ok(Self { output: None });
        }
        let mode = config
            .call_method1("getoption", ("capture", py.None()))?
            .extract::<Option<String>>()?;
        let resource = match mode.as_deref() {
            Some("fd") => "FdCaptureIO",
            Some("sys") => "SysCaptureIO",
            // The builtin adds no local output capture for no or tee-sys.
            _ => return Ok(Self { output: None }),
        };
        let output = py.import("vfy.independent.stdio")?.call_method0(resource)?;
        let started = (|| {
            if let Some(global) = global {
                output.call_method1("bind", (global,))?;
            }
            output.call_method0("resume")?;
            Ok(())
        })();
        if let Err(error) = started {
            output.call_method0("close")?;
            return Err(error);
        }
        Ok(Self {
            output: Some(output.unbind()),
        })
    }

    fn has_fixture(py: Python<'_>, global: Option<&Py<NativeGlobalCapture>>) -> PyResult<bool> {
        global.map_or(Ok(false), |global| {
            global.get().fixture(py).map(|fixture| fixture.is_some())
        })
    }

    pub(in crate::independent) fn traverse(
        &self,
        visit: &PyVisit<'_>,
    ) -> Result<(), PyTraverseError> {
        visit.call(&self.output)
    }

    pub(in crate::independent) fn complete(mut self, py: Python<'_>) -> PyResult<PhasePayload> {
        let Some(output) = self.output.take() else {
            return Ok(PhasePayload::default());
        };
        let output = output.bind(py);
        let captured = output
            .call_method0("readouterr")
            .and_then(|value| value.extract::<(String, String)>());
        // Restore outer resources even when an overridable stream flush fails.
        output.call_method0("close")?;
        let (stdout, stderr) = captured?;
        Ok(PhasePayload {
            stdout,
            stderr,
            ..PhasePayload::default()
        })
    }
}

impl Drop for SubtestOutput {
    fn drop(&mut self) {
        if let Some(output) = self.output.take() {
            Python::attach(|py| {
                let _ = output.bind(py).call_method0("close");
            });
        }
    }
}
