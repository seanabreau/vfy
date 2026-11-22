//! Native ownership of global fd capture, retaining the original Python streams.

use super::locked;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};
use rustix::stdio::{dup2_stderr, dup2_stdout, stderr, stdout};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::fd::AsRawFd;
use std::sync::{Arc, Mutex};

struct Resources {
    saved: [File; 2],
    files: [Mutex<File>; 2],
    original: [Py<PyAny>; 2],
    captured: [Py<PyAny>; 2],
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeFdCapture {
    resources: Mutex<Option<Arc<Resources>>>,
}

/// Preserve the earlier failure as context when a later cleanup also fails.
fn replace_error(py: Python<'_>, result: &mut PyResult<()>, error: PyErr) {
    if let Err(previous) = std::mem::replace(result, Ok(()))
        && !error.value(py).is(previous.value(py))
    {
        let mut tail = error.clone_ref(py);
        while let Some(context) = tail.context(py) {
            if context.value(py).is(previous.value(py)) || context.value(py).is(error.value(py)) {
                *result = Err(error);
                return;
            }
            tail = context;
        }
        tail.set_context(py, Some(previous));
    }
    *result = Err(error);
}

impl NativeFdCapture {
    pub(super) fn rebind_fork(&self, py: Python<'_>) -> PyResult<()> {
        let previous = self.resources()?;
        let sys = py.import("sys")?;
        let original = [sys.getattr("stdout")?, sys.getattr("stderr")?];
        let saved = [
            File::from(rustix::io::fcntl_dupfd_cloexec(stdout(), 0).map_err(std::io::Error::from)?),
            File::from(rustix::io::fcntl_dupfd_cloexec(stderr(), 0).map_err(std::io::Error::from)?),
        ];
        let files = [
            Mutex::new(locked(&previous.files[0], "fd capture file")?.try_clone()?),
            Mutex::new(locked(&previous.files[1], "fd capture file")?.try_clone()?),
        ];
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
            locked(&self.resources, "fd capture resources")?.replace(Arc::new(Resources {
                saved,
                files,
                original: original.map(Bound::unbind),
                captured,
            }));
        drop(removed);
        Ok(())
    }

    pub(super) fn fork_descriptors(&self, py: Python<'_>) -> PyResult<Vec<i32>> {
        let resources = self.resources()?;
        let mut descriptors = resources
            .saved
            .iter()
            .map(AsRawFd::as_raw_fd)
            .collect::<Vec<_>>();
        for file in &resources.files {
            descriptors.push(locked(file, "fd capture file")?.as_raw_fd());
        }
        for stream in &resources.captured {
            descriptors.push(stream.bind(py).call_method0("fileno")?.extract()?);
        }
        Ok(descriptors)
    }

    pub(super) fn create(py: Python<'_>, adapter: &Bound<'_, PyModule>) -> PyResult<Py<Self>> {
        let sys = py.import("sys")?;
        let original = [sys.getattr("stdout")?, sys.getattr("stderr")?];
        for stream in &original {
            stream.call_method0("flush")?;
        }
        let saved = [
            File::from(rustix::io::fcntl_dupfd_cloexec(stdout(), 0).map_err(std::io::Error::from)?),
            File::from(rustix::io::fcntl_dupfd_cloexec(stderr(), 0).map_err(std::io::Error::from)?),
        ];
        let files = [tempfile::tempfile()?, tempfile::tempfile()?];
        let build = adapter.getattr("_native_fd_stream")?;
        let first = build.call1((&original[0], files[0].as_raw_fd()))?;
        let second = match build.call1((&original[1], files[1].as_raw_fd())) {
            Ok(stream) => stream,
            Err(error) => {
                if let Err(close_error) = first.call_method0("close") {
                    let mut result = Err(error);
                    replace_error(py, &mut result, close_error);
                    result?;
                } else {
                    return Err(error);
                }
                return Err(PyRuntimeError::new_err("fd capture stream creation failed"));
            }
        };
        let [out, err] = files;
        Py::new(
            py,
            Self {
                resources: Mutex::new(Some(Arc::new(Resources {
                    saved,
                    files: [Mutex::new(out), Mutex::new(err)],
                    original: original.map(Bound::unbind),
                    captured: [first.unbind(), second.unbind()],
                }))),
            },
        )
    }

    fn resources(&self) -> PyResult<Arc<Resources>> {
        locked(&self.resources, "fd capture resources")?
            .as_ref()
            .cloned()
            .ok_or_else(|| PyRuntimeError::new_err("fd capture is closed"))
    }
}

impl Resources {
    fn suspend(&self, py: Python<'_>) -> PyResult<()> {
        let mut result = Ok(());
        for stream in &self.captured {
            if let Err(error) = stream.bind(py).call_method0("flush") {
                result = Err(error);
                break;
            }
        }
        match py.import("sys") {
            Ok(sys) => {
                for (name, value) in [("stdout", &self.original[0]), ("stderr", &self.original[1])]
                {
                    if let Err(error) = sys.setattr(name, value.bind(py)) {
                        replace_error(py, &mut result, error);
                        break;
                    }
                }
            }
            Err(error) => replace_error(py, &mut result, error),
        }
        if let Err(error) = dup2_stderr(&self.saved[1]) {
            replace_error(py, &mut result, std::io::Error::from(error).into());
        }
        if let Err(error) = dup2_stdout(&self.saved[0]) {
            replace_error(py, &mut result, std::io::Error::from(error).into());
        }
        result
    }
}

#[pymethods]
impl NativeFdCapture {
    pub(super) fn bind(&self, py: Python<'_>, owner: &Bound<'_, PyAny>) -> PyResult<()> {
        let resources = self.resources()?;
        for stream in &resources.captured {
            stream.bind(py).setattr("_vfy_global_capture", owner)?;
        }
        Ok(())
    }

    pub(super) fn resume(&self, py: Python<'_>) -> PyResult<()> {
        let resources = self.resources()?;
        let mut result = (|| -> PyResult<()> {
            dup2_stdout(&*locked(&resources.files[0], "fd capture file")?)
                .map_err(std::io::Error::from)?;
            dup2_stderr(&*locked(&resources.files[1], "fd capture file")?)
                .map_err(std::io::Error::from)?;
            let sys = py.import("sys")?;
            sys.setattr("stdout", resources.captured[0].bind(py))?;
            sys.setattr("stderr", resources.captured[1].bind(py))?;
            Ok(())
        })();
        if result.is_err()
            && let Err(error) = resources.suspend(py)
        {
            replace_error(py, &mut result, error);
        }
        result
    }

    pub(super) fn suspend(&self, py: Python<'_>) -> PyResult<()> {
        self.resources()?.suspend(py)
    }

    pub(super) fn readouterr(&self, py: Python<'_>) -> PyResult<(String, String)> {
        let resources = self.resources()?;
        for stream in &resources.captured {
            stream.bind(py).call_method0("flush")?;
        }
        let mut values = [String::new(), String::new()];
        for (index, result) in values.iter_mut().enumerate() {
            let mut file = locked(&resources.files[index], "fd capture file")?;
            file.seek(SeekFrom::Start(0))?;
            let mut bytes = Vec::new();
            file.read_to_end(&mut bytes)?;
            if !bytes.is_empty() {
                file.seek(SeekFrom::Start(0))?;
                file.set_len(0)?;
            }
            *result = String::from_utf8_lossy(&bytes).into_owned();
        }
        let [out, err] = values;
        Ok((out, err))
    }

    pub(super) fn write_original(&self, py: Python<'_>, out: &str, err: &str) -> PyResult<()> {
        let resources = self.resources()?;
        py.detach(|| {
            for (file, value) in resources.saved.iter().zip([out, err]) {
                let mut file = file;
                file.write_all(value.as_bytes())?;
            }
            Ok(())
        })
    }

    pub(super) fn close(&self, py: Python<'_>) -> PyResult<()> {
        let resources = locked(&self.resources, "fd capture resources")?.take();
        let Some(resources) = resources else {
            return Ok(());
        };
        let mut result = resources.suspend(py);
        for stream in resources.captured.iter().rev() {
            if let Err(error) = stream.bind(py).call_method0("close") {
                replace_error(py, &mut result, error);
            }
        }
        drop(resources);
        result
    }

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
