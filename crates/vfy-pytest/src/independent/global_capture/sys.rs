//! Native global sys capture retaining the original Python stream protocols.

use super::{CaptureTransition, locked};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::{PyTraverseError, PyVisit};
use std::sync::{Arc, Mutex};

struct Resources {
    original: [Py<PyAny>; 2],
    captured: [Py<PyAny>; 2],
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeSysCapture {
    resources: Mutex<Option<Arc<Resources>>>,
}

impl NativeSysCapture {
    pub(super) fn rebind_fork(&self, py: Python<'_>) -> PyResult<()> {
        let previous = self.resources()?;
        let sys = py.import("sys")?;
        let original = [sys.getattr("stdout")?, sys.getattr("stderr")?];
        let captured = previous
            .captured
            .each_ref()
            .map(|stream| stream.clone_ref(py));
        for (stream, outer) in captured.iter().zip(&original) {
            stream
                .bind(py)
                .setattr("_vfy_outer_capture_stream", outer)?;
        }
        let removed =
            locked(&self.resources, "sys capture resources")?.replace(Arc::new(Resources {
                original: original.map(Bound::unbind),
                captured,
            }));
        drop(removed);
        Ok(())
    }

    pub(super) fn create(
        py: Python<'_>,
        adapter: &Bound<'_, PyModule>,
        tee: bool,
    ) -> PyResult<Py<Self>> {
        let sys = py.import("sys")?;
        let original = [sys.getattr("stdout")?, sys.getattr("stderr")?];
        let stream_type = adapter.getattr(if tee { "TeeBuffer" } else { "CaptureBuffer" })?;
        let captured = [
            stream_type.call1((&original[0],))?,
            stream_type.call1((&original[1],))?,
        ];
        Py::new(
            py,
            Self {
                resources: Mutex::new(Some(Arc::new(Resources {
                    original: original.map(Bound::unbind),
                    captured: captured.map(Bound::unbind),
                }))),
            },
        )
    }

    fn resources(&self) -> PyResult<Arc<Resources>> {
        locked(&self.resources, "sys capture resources")?
            .as_ref()
            .cloned()
            .ok_or_else(|| PyRuntimeError::new_err("sys capture is closed"))
    }

    pub(super) fn bind(&self, py: Python<'_>, owner: &Bound<'_, PyAny>) -> PyResult<()> {
        let resources = self.resources()?;
        for stream in &resources.captured {
            stream.bind(py).setattr("_vfy_global_capture", owner)?;
        }
        Ok(())
    }

    pub(super) fn transition(&self, py: Python<'_>, transition: CaptureTransition) -> PyResult<()> {
        let resources = self.resources()?;
        resources.transition(py, transition)
    }

    pub(super) fn readouterr(&self, py: Python<'_>) -> PyResult<(String, String)> {
        let resources = self.resources()?;
        let mut values = [String::new(), String::new()];
        for (index, value) in values.iter_mut().enumerate() {
            let stream = resources.captured[index].bind(py);
            let buffer = stream.getattr("buffer")?;
            if !buffer.is_instance(&py.import("io")?.getattr("BytesIO")?)? {
                return Err(PyAssertionError::new_err(""));
            }
            let data = buffer.call_method0("getvalue")?;
            *value = if let Ok(bytes) = data.cast_exact::<PyBytes>() {
                match std::str::from_utf8(bytes.as_bytes()) {
                    Ok(text) => text.to_owned(),
                    Err(_) => data.call_method1("decode", ("UTF-8",))?.extract()?,
                }
            } else {
                data.call_method1("decode", ("UTF-8",))?.extract()?
            };
            stream.call_method1("seek", (0,))?;
            stream.call_method0("truncate")?;
        }
        let [out, err] = values;
        Ok((out, err))
    }

    pub(super) fn write_original(&self, py: Python<'_>, out: &str, err: &str) -> PyResult<()> {
        let resources = self.resources()?;
        for (stream, value) in resources.original.iter().zip([out, err]) {
            if !value.is_empty() {
                stream.bind(py).call_method1("write", (value,))?;
                stream.bind(py).call_method0("flush")?;
            }
        }
        Ok(())
    }

    pub(super) fn close(&self, py: Python<'_>) -> PyResult<()> {
        let resources = locked(&self.resources, "sys capture resources")?.take();
        let Some(resources) = resources else {
            return Ok(());
        };
        resources.transition(py, CaptureTransition::Suspend)?;
        for stream in &resources.captured {
            stream.bind(py).call_method0("close")?;
        }
        Ok(())
    }
}

impl Resources {
    fn transition(&self, py: Python<'_>, transition: CaptureTransition) -> PyResult<()> {
        let sys = py.import("sys")?;
        let streams = match transition {
            CaptureTransition::Resume => &self.captured,
            CaptureTransition::Suspend => &self.original,
        };
        sys.setattr("stdout", streams[0].bind(py))?;
        sys.setattr("stderr", streams[1].bind(py))?;
        Ok(())
    }
}

#[pymethods]
impl NativeSysCapture {
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(resources) = self.resources.try_lock()
            && let Some(resources) = resources.as_ref()
        {
            for stream in resources.original.iter().chain(resources.captured.iter()) {
                visit.call(stream)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        let _ = self.close(py);
    }
}
