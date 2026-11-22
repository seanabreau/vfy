//! Owns global output capture and stream bridging.

use super::native_session::{ReportProtocol, fd_error, io_error};
use super::runtime::SharedRuntime;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyResult, Python, pyclass, pymethods};
use pyo3::types::PyAny;
use rustix::{io, stdio};
use std::fs::File;
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::Mutex;
use vfy_core::CaptureReader;

pub(super) enum CaptureBuffers {
    Fd(Box<FdCaptureBuffers>),
    Borrowed(BorrowedCaptureBuffers),
}

pub(super) struct BorrowedCaptureBuffers {
    manager: Py<PyAny>,
    capture: Py<PyAny>,
    initial_state: Option<BorrowedCaptureState>,
    restored: bool,
}

struct BorrowedCaptureState {
    started: bool,
    stdin_suspended: bool,
}

impl BorrowedCaptureBuffers {
    fn restore_python_state(&mut self, py: Python<'_>) -> PyResult<()> {
        if !self.restored {
            let capture = self.capture.bind(py);
            if let Some(initial) = &self.initial_state {
                let started = capture.call_method0("is_started")?.extract::<bool>()?;
                let stdin_suspended = capture.getattr("_in_suspended")?.extract::<bool>()?;
                if initial.started != started || initial.stdin_suspended != stdin_suspended {
                    if initial.started || (!initial.stdin_suspended && stdin_suspended) {
                        capture.call_method0("resume_capturing")?;
                    }
                    if !initial.started {
                        capture.call_method1("suspend_capturing", (initial.stdin_suspended,))?;
                    }
                }
            } else {
                capture.call_method1("suspend_capturing", (true,))?;
            }
            self.restored = true;
        }
        Ok(())
    }
}

impl Drop for BorrowedCaptureBuffers {
    fn drop(&mut self) {
        if !self.restored {
            let _ = Python::try_attach(|py| self.restore_python_state(py));
        }
    }
}

pub(super) enum CaptureHandle {
    Fd(Py<NativeGlobalCapture>),
    Borrowed(Py<PyAny>),
}

impl CaptureHandle {
    pub(super) fn is_started(&self, py: Python<'_>) -> PyResult<bool> {
        match self {
            Self::Fd(capture) => Ok(capture.borrow(py).is_started()),
            Self::Borrowed(capture) => capture.bind(py).call_method0("is_started")?.extract(),
        }
    }

    pub(super) fn suspend_capturing(&self, py: Python<'_>) -> PyResult<()> {
        match self {
            Self::Fd(capture) => capture.borrow(py).suspend_capturing(py, false),
            Self::Borrowed(capture) => capture
                .bind(py)
                .call_method0("suspend_capturing")
                .map(|_| ()),
        }
    }

    pub(super) fn resume_capturing(&self, py: Python<'_>) -> PyResult<()> {
        match self {
            Self::Fd(capture) => capture.borrow(py).resume_capturing(py),
            Self::Borrowed(capture) => capture
                .bind(py)
                .call_method0("resume_capturing")
                .map(|_| ()),
        }
    }
}

impl CaptureBuffers {
    fn manager(&self) -> Option<&Py<PyAny>> {
        match self {
            Self::Fd(buffers) => buffers.capture_manager.as_ref(),
            Self::Borrowed(buffers) => Some(&buffers.manager),
        }
    }

    pub(super) fn capture_handle(&self, py: Python<'_>) -> Option<CaptureHandle> {
        match self {
            Self::Fd(buffers) => buffers
                .native_capture
                .as_ref()
                .map(|capture| CaptureHandle::Fd(capture.clone_ref(py))),
            Self::Borrowed(buffers) => Some(CaptureHandle::Borrowed(buffers.capture.clone_ref(py))),
        }
    }

    fn restore_python_state(&mut self, py: Python<'_>) -> PyResult<()> {
        match self {
            Self::Fd(buffers) => buffers.restore_python_state(py),
            Self::Borrowed(buffers) => buffers.restore_python_state(py),
        }
    }
}

pub(super) struct FdCaptureBuffers {
    stdout: File,
    stderr: File,
    saved_stdin: OwnedFd,
    saved_stdout: OwnedFd,
    saved_stderr: OwnedFd,
    capture_manager: Option<Py<PyAny>>,
    previous_capture: Option<Py<PyAny>>,
    original_stdin: Py<PyAny>,
    native_capture: Option<Py<NativeGlobalCapture>>,
    python_state_restored: bool,
}

impl FdCaptureBuffers {
    fn acquire(py: Python<'_>, manager: &Py<PyAny>) -> PyResult<Self> {
        let previous_capture = Some(manager.bind(py).getattr("_global_capturing")?.unbind());
        let stream_bridge = PytestStreamBridge::new(py, previous_capture.as_ref())?;
        let stdout = tempfile::tempfile().map_err(|error| io_error(&error))?;
        let stderr = tempfile::tempfile().map_err(|error| io_error(&error))?;
        flush_standard_streams(py);
        let saved_stdin = io::dup(stdio::stdin()).map_err(fd_error)?;
        let saved_stdout = io::dup(stdio::stdout()).map_err(fd_error)?;
        let saved_stderr = io::dup(stdio::stderr()).map_err(fd_error)?;
        let original_stdin = py.import("sys")?.getattr("stdin")?.unbind();
        stdio::dup2_stdout(&stdout).map_err(fd_error)?;
        if let Err(error) = stdio::dup2_stderr(&stderr) {
            let _ = stdio::dup2_stdout(&saved_stdout);
            return Err(fd_error(error));
        }
        let mut buffers = FdCaptureBuffers {
            stdout,
            stderr,
            saved_stdin,
            saved_stdout,
            saved_stderr,
            capture_manager: Some(manager.clone_ref(py)),
            previous_capture,
            original_stdin,
            native_capture: None,
            python_state_restored: false,
        };
        let setup = (|| -> PyResult<()> {
            let null_stdin = File::open("/dev/null").map_err(|error| io_error(&error))?;
            let captured_stdin = py
                .import("_pytest.capture")?
                .getattr("DontReadFromInput")?
                .call0()?
                .unbind();
            let service = Py::new(
                py,
                NativeGlobalCapture {
                    stdout: buffers
                        .stdout
                        .try_clone()
                        .map_err(|error| io_error(&error))?,
                    stderr: buffers
                        .stderr
                        .try_clone()
                        .map_err(|error| io_error(&error))?,
                    null_stdin: null_stdin.try_clone().map_err(|error| io_error(&error))?,
                    saved_stdin: io::dup(&buffers.saved_stdin).map_err(fd_error)?,
                    saved_stdout: io::dup(&buffers.saved_stdout).map_err(fd_error)?,
                    saved_stderr: io::dup(&buffers.saved_stderr).map_err(fd_error)?,
                    original_stdin: buffers.original_stdin.clone_ref(py),
                    captured_stdin: captured_stdin.clone_ref(py),
                    stream_bridge: Mutex::new(StreamBridgeState {
                        bridge: stream_bridge,
                        closed: false,
                    }),
                    state: Mutex::new(GlobalCaptureState {
                        started: true,
                        stdin_suspended: false,
                        stdout_offset: 0,
                        stderr_offset: 0,
                        reader: CaptureReader::default(),
                    }),
                },
            )?;
            buffers.native_capture = Some(service.clone_ref(py));
            stdio::dup2_stdin(&null_stdin).map_err(fd_error)?;
            py.import("sys")?.setattr("stdin", captured_stdin)?;
            if let Some(manager) = &buffers.capture_manager {
                manager.bind(py).setattr("_global_capturing", service)?;
            }
            if let Some(capture) = &buffers.native_capture {
                capture.borrow(py).resume_stream_bridge(py)?;
            }
            Ok(())
        })();
        setup?;
        Ok(buffers)
    }

    fn restore_python_state(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.python_state_restored {
            return Ok(());
        }

        let restore_bridge = if let Some(capture) = &self.native_capture {
            capture.borrow(py).close_stream_bridge(py)
        } else {
            Ok(())
        };
        let restore_manager = (|| -> PyResult<()> {
            let (Some(manager), Some(capture), Some(previous)) = (
                &self.capture_manager,
                &self.native_capture,
                &self.previous_capture,
            ) else {
                return Ok(());
            };
            let manager = manager.bind(py);
            let current_capture = manager.getattr("_global_capturing")?;
            if current_capture.is(capture) {
                manager.setattr("_global_capturing", previous.bind(py))?;
            }
            Ok(())
        })();
        let restore_stdin = py
            .import("sys")
            .and_then(|sys| sys.setattr("stdin", self.original_stdin.bind(py)));
        self.python_state_restored =
            restore_bridge.is_ok() && restore_manager.is_ok() && restore_stdin.is_ok();
        restore_bridge?;
        restore_manager?;
        restore_stdin
    }
}

impl Drop for FdCaptureBuffers {
    fn drop(&mut self) {
        if !self.python_state_restored {
            let _ = Python::try_attach(|py| self.restore_python_state(py));
        }
        let _ = stdio::dup2_stdin(&self.saved_stdin);
        let _ = stdio::dup2_stdout(&self.saved_stdout);
        let _ = stdio::dup2_stderr(&self.saved_stderr);
    }
}

/// Redirect pytest's fd-capture streams to per-test files without replacing the
/// stream objects. Imported code can retain them during collection and depend on
/// their identity later, as Colorama's POSIX initialization tests do.
struct PytestStreamBridge {
    os: Py<PyAny>,
    sys: Py<PyAny>,
    stdout_stream: Py<PyAny>,
    stderr_stream: Py<PyAny>,
    original_stdout: Py<PyAny>,
    original_stderr: Py<PyAny>,
    stdout_fd: i32,
    stderr_fd: i32,
    saved_stdout_fd: Option<i32>,
    saved_stderr_fd: Option<i32>,
    active: bool,
}

impl PytestStreamBridge {
    fn new(py: Python<'_>, previous_capture: Option<&Py<PyAny>>) -> PyResult<Option<Self>> {
        let Some(previous_capture) = previous_capture else {
            return Ok(None);
        };
        let capture = previous_capture.bind(py);
        if capture.is_none() {
            return Ok(None);
        }
        let stdout_capture = capture.getattr("out")?;
        let stderr_capture = capture.getattr("err")?;
        if stdout_capture.is_none() || stderr_capture.is_none() {
            return Ok(None);
        }
        let stdout_stream = stdout_capture.getattr("tmpfile")?;
        let stderr_stream = stderr_capture.getattr("tmpfile")?;
        if stdout_stream.is_none() || stderr_stream.is_none() {
            return Ok(None);
        }
        let stdout_fd = stdout_stream.call_method0("fileno")?.extract::<i32>()?;
        let stderr_fd = stderr_stream.call_method0("fileno")?.extract::<i32>()?;
        // Pytest's fd capture streams are independent temporary files, not the
        // process standard descriptors. Leave unusual/custom capture objects
        // alone instead of risking a self-redirect or aliased stream.
        if stdout_fd <= 2 || stderr_fd <= 2 || stdout_fd == stderr_fd {
            return Ok(None);
        }

        let os = py.import("os")?;
        let sys = py.import("sys")?;
        let original_stdout = sys.getattr("stdout")?.unbind();
        let original_stderr = sys.getattr("stderr")?.unbind();
        let saved_stdout_fd = os.call_method1("dup", (stdout_fd,))?.extract::<i32>()?;
        let saved_stderr_fd = match os
            .call_method1("dup", (stderr_fd,))
            .and_then(|value| value.extract::<i32>())
        {
            Ok(fd) => fd,
            Err(error) => {
                let _ = os.call_method1("close", (saved_stdout_fd,));
                return Err(error);
            }
        };
        Ok(Some(Self {
            os: os.into(),
            sys: sys.clone().unbind().into(),
            stdout_stream: stdout_stream.unbind(),
            stderr_stream: stderr_stream.unbind(),
            original_stdout,
            original_stderr,
            stdout_fd,
            stderr_fd,
            saved_stdout_fd: Some(saved_stdout_fd),
            saved_stderr_fd: Some(saved_stderr_fd),
            active: false,
        }))
    }

    fn resume(&mut self, py: Python<'_>, stdout: &File, stderr: &File) -> PyResult<()> {
        if self.active {
            return Ok(());
        }
        let saved_stdout_fd = self.saved_stdout_fd.ok_or_else(|| {
            PyRuntimeError::new_err("pytest stdout capture bridge is already closed")
        })?;
        let saved_stderr_fd = self.saved_stderr_fd.ok_or_else(|| {
            PyRuntimeError::new_err("pytest stderr capture bridge is already closed")
        })?;
        let os = self.os.bind(py);
        let sys = self.sys.bind(py);
        let result = (|| -> PyResult<()> {
            os.call_method1(intern!(py, "dup2"), (stdout.as_raw_fd(), self.stdout_fd))?;
            os.call_method1(intern!(py, "dup2"), (stderr.as_raw_fd(), self.stderr_fd))?;
            sys.setattr(intern!(py, "stdout"), self.stdout_stream.bind(py))?;
            sys.setattr(intern!(py, "stderr"), self.stderr_stream.bind(py))?;
            Ok(())
        })();
        if let Err(error) = result {
            let _ = os.call_method1(intern!(py, "dup2"), (saved_stdout_fd, self.stdout_fd));
            let _ = os.call_method1(intern!(py, "dup2"), (saved_stderr_fd, self.stderr_fd));
            let _ = sys.setattr(intern!(py, "stdout"), self.original_stdout.bind(py));
            let _ = sys.setattr(intern!(py, "stderr"), self.original_stderr.bind(py));
            return Err(error);
        }
        self.active = true;
        Ok(())
    }

    fn suspend(&mut self, py: Python<'_>, stdout: &File, stderr: &File) -> PyResult<()> {
        if !self.active {
            return Ok(());
        }
        let saved_stdout_fd = self.saved_stdout_fd.ok_or_else(|| {
            PyRuntimeError::new_err("pytest stdout capture bridge is already closed")
        })?;
        let saved_stderr_fd = self.saved_stderr_fd.ok_or_else(|| {
            PyRuntimeError::new_err("pytest stderr capture bridge is already closed")
        })?;
        let os = self.os.bind(py);
        let sys = self.sys.bind(py);
        let result = (|| -> PyResult<()> {
            os.call_method1(intern!(py, "dup2"), (saved_stdout_fd, self.stdout_fd))?;
            os.call_method1(intern!(py, "dup2"), (saved_stderr_fd, self.stderr_fd))?;
            sys.setattr(intern!(py, "stdout"), self.original_stdout.bind(py))?;
            sys.setattr(intern!(py, "stderr"), self.original_stderr.bind(py))?;
            Ok(())
        })();
        if let Err(error) = result {
            let _ = os.call_method1(intern!(py, "dup2"), (stdout.as_raw_fd(), self.stdout_fd));
            let _ = os.call_method1(intern!(py, "dup2"), (stderr.as_raw_fd(), self.stderr_fd));
            let _ = sys.setattr(intern!(py, "stdout"), self.stdout_stream.bind(py));
            let _ = sys.setattr(intern!(py, "stderr"), self.stderr_stream.bind(py));
            return Err(error);
        }
        self.active = false;
        Ok(())
    }

    fn close(&mut self, py: Python<'_>, stdout: &File, stderr: &File) -> PyResult<()> {
        self.suspend(py, stdout, stderr)?;
        let os = self.os.bind(py);
        if let Some(fd) = self.saved_stdout_fd {
            os.call_method1("close", (fd,))?;
            self.saved_stdout_fd = None;
        }
        if let Some(fd) = self.saved_stderr_fd {
            os.call_method1("close", (fd,))?;
            self.saved_stderr_fd = None;
        }
        Ok(())
    }
}

impl Drop for PytestStreamBridge {
    fn drop(&mut self) {
        let _ = Python::try_attach(|py| {
            let os = self.os.bind(py);
            if self.active {
                if let Some(fd) = self.saved_stdout_fd {
                    let _ = os.call_method1("dup2", (fd, self.stdout_fd));
                }
                if let Some(fd) = self.saved_stderr_fd {
                    let _ = os.call_method1("dup2", (fd, self.stderr_fd));
                }
                let sys = self.sys.bind(py);
                let _ = sys.setattr("stdout", self.original_stdout.bind(py));
                let _ = sys.setattr("stderr", self.original_stderr.bind(py));
                self.active = false;
            }
            if let Some(fd) = self.saved_stdout_fd.take() {
                let _ = os.call_method1("close", (fd,));
            }
            if let Some(fd) = self.saved_stderr_fd.take() {
                let _ = os.call_method1("close", (fd,));
            }
        });
    }
}

struct GlobalCaptureState {
    started: bool,
    stdin_suspended: bool,
    stdout_offset: u64,
    stderr_offset: u64,
    reader: CaptureReader,
}

struct StreamBridgeState {
    bridge: Option<PytestStreamBridge>,
    closed: bool,
}

#[pyclass(module = "vfy._native._pytest")]
pub(super) struct NativeGlobalCapture {
    stdout: File,
    stderr: File,
    null_stdin: File,
    saved_stdin: OwnedFd,
    saved_stdout: OwnedFd,
    saved_stderr: OwnedFd,
    original_stdin: Py<PyAny>,
    captured_stdin: Py<PyAny>,
    stream_bridge: Mutex<StreamBridgeState>,
    state: Mutex<GlobalCaptureState>,
}

impl NativeGlobalCapture {
    fn with_stream_bridge(
        &self,
        py: Python<'_>,
        run: impl FnOnce(&mut PytestStreamBridge) -> PyResult<()>,
    ) -> PyResult<()> {
        // Stream replacement can run destructors that re-enter capture. Keep
        // the bridge and its Python references outside the slot lock throughout.
        let bridge = self
            .stream_bridge
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .bridge
            .take();
        let Some(mut bridge) = bridge else {
            return Ok(());
        };
        let result = run(&mut bridge);
        let mut slot = self
            .stream_bridge
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !slot.closed {
            let displaced = slot.bridge.replace(bridge);
            drop(slot);
            drop(displaced);
            return result;
        }
        drop(slot);
        // A reentrant close owns the terminal transition even while the bridge
        // is out of its slot. Never restore its descriptors or references later.
        let closed = bridge.close(py, &self.stdout, &self.stderr);
        result.and(closed)
    }
}

#[pymethods]
impl NativeGlobalCapture {
    fn read_output(&self, py: Python<'_>) -> PyResult<(String, String)> {
        flush_standard_streams(py);
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let GlobalCaptureState {
            stdout_offset,
            stderr_offset,
            reader,
            ..
        } = &mut *state;
        Ok((
            reader
                .read(&self.stdout, stdout_offset)
                .map_err(|error| io_error(&error))?,
            reader
                .read(&self.stderr, stderr_offset)
                .map_err(|error| io_error(&error))?,
        ))
    }

    fn drain_output(&self) -> PyResult<(String, String)> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let GlobalCaptureState {
            stdout_offset,
            stderr_offset,
            reader,
            ..
        } = &mut *state;
        let stdout = reader
            .snapshot(&self.stdout, stdout_offset)
            .map_err(|error| io_error(&error))?;
        let stderr = reader
            .snapshot(&self.stderr, stderr_offset)
            .map_err(|error| io_error(&error))?;
        Ok((stdout, stderr))
    }

    fn is_started(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .started
    }

    #[getter]
    fn _in_suspended(&self) -> bool {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .stdin_suspended
    }

    fn resume_stream_bridge(&self, py: Python<'_>) -> PyResult<()> {
        self.with_stream_bridge(py, |bridge| bridge.resume(py, &self.stdout, &self.stderr))
    }

    fn suspend_stream_bridge(&self, py: Python<'_>) -> PyResult<()> {
        self.with_stream_bridge(py, |bridge| bridge.suspend(py, &self.stdout, &self.stderr))
    }

    fn close_stream_bridge(&self, py: Python<'_>) -> PyResult<()> {
        let mut bridge = {
            let mut slot = self
                .stream_bridge
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            slot.closed = true;
            slot.bridge.take()
        };
        if let Some(bridge) = bridge.as_mut() {
            bridge.close(py, &self.stdout, &self.stderr)?;
        }
        Ok(())
    }

    fn readouterr(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (stdout, stderr) = self.read_output(py)?;
        Ok(py
            .import("_pytest.capture")?
            .getattr("CaptureResult")?
            .call1((stdout, stderr))?
            .unbind())
    }

    #[pyo3(signature = (in_ = false))]
    fn suspend_capturing(&self, py: Python<'_>, in_: bool) -> PyResult<()> {
        flush_standard_streams(py);
        if !self.is_started() {
            return Ok(());
        }
        self.suspend_stream_bridge(py)?;
        stdio::dup2_stdout(&self.saved_stdout).map_err(fd_error)?;
        if let Err(error) = stdio::dup2_stderr(&self.saved_stderr) {
            let _ = stdio::dup2_stdout(&self.stdout);
            let _ = self.resume_stream_bridge(py);
            return Err(fd_error(error));
        }
        if in_ {
            if let Err(error) = stdio::dup2_stdin(&self.saved_stdin) {
                let _ = stdio::dup2_stdout(&self.stdout);
                let _ = stdio::dup2_stderr(&self.stderr);
                let _ = self.resume_stream_bridge(py);
                return Err(fd_error(error));
            }
            py.import("sys")?
                .setattr(intern!(py, "stdin"), self.original_stdin.bind(py))?;
        }
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if in_ {
            state.stdin_suspended = true;
        }
        state.started = false;
        Ok(())
    }

    fn resume_capturing(&self, py: Python<'_>) -> PyResult<()> {
        let (started, stdin_suspended) = {
            let state = self
                .state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            (state.started, state.stdin_suspended)
        };
        if started {
            return Ok(());
        }
        flush_standard_streams(py);
        stdio::dup2_stdout(&self.stdout).map_err(fd_error)?;
        if let Err(error) = stdio::dup2_stderr(&self.stderr) {
            let _ = stdio::dup2_stdout(&self.saved_stdout);
            return Err(fd_error(error));
        }
        if let Err(error) = self.resume_stream_bridge(py) {
            let _ = stdio::dup2_stdout(&self.saved_stdout);
            let _ = stdio::dup2_stderr(&self.saved_stderr);
            return Err(error);
        }
        if stdin_suspended {
            if let Err(error) = stdio::dup2_stdin(&self.null_stdin) {
                let _ = self.suspend_stream_bridge(py);
                let _ = stdio::dup2_stdout(&self.saved_stdout);
                let _ = stdio::dup2_stderr(&self.saved_stderr);
                return Err(fd_error(error));
            }
            py.import("sys")?
                .setattr(intern!(py, "stdin"), self.captured_stdin.bind(py))?;
        }
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if stdin_suspended {
            state.stdin_suspended = false;
        }
        state.started = true;
        Ok(())
    }
}

fn flush_standard_streams(py: Python<'_>) {
    let Ok(sys) = py.import("sys") else {
        return;
    };
    for name in ["stdout", "stderr"] {
        if let Ok(stream) = sys.getattr(name)
            && !stream.is_none()
        {
            let _ = stream.call_method0(intern!(py, "flush"));
        }
    }
}

pub(super) fn create_capture_buffers(
    py: Python<'_>,
    runtime: &SharedRuntime,
    enabled: bool,
    protocol: ReportProtocol,
) -> PyResult<Option<CaptureBuffers>> {
    if !enabled {
        return Ok(None);
    }
    let capture_manager = {
        let state = runtime.lock();
        state
            .suite
            .test_bindings
            .first()
            .map(|test| test.item.clone_ref(py))
    }
    .map(|item| {
        item.bind(py)
            .getattr("config")?
            .getattr("pluginmanager")?
            .call_method1("getplugin", ("capturemanager",))
            .map(Bound::unbind)
    })
    .transpose()?
    .filter(|manager| !manager.bind(py).is_none());
    let Some(manager) = capture_manager.as_ref() else {
        return Ok(None);
    };
    let method = manager.bind(py).getattr("_method")?.extract::<String>()?;
    if method == "sys" || (method == "fd" && matches!(protocol, ReportProtocol::Pytest)) {
        // Upstream owns its capture files across dispatched protocols. Borrow
        // them without replacing collection's streams or closing its storage.
        let capture = manager.bind(py).getattr("_global_capturing")?;
        // Pytest closes global capture before sessionfinish. Late finalizers
        // still run, with their exceptions and warnings handled by the session.
        if capture.is_none() {
            return Ok(None);
        }
        let initial_state = if method == "fd" {
            Some(BorrowedCaptureState {
                started: capture.call_method0("is_started")?.extract()?,
                stdin_suspended: capture.getattr("_in_suspended")?.extract()?,
            })
        } else {
            None
        };
        let mut buffers = BorrowedCaptureBuffers {
            manager: manager.clone_ref(py),
            capture: capture.unbind(),
            initial_state,
            restored: false,
        };
        if let Err(error) = buffers.capture.bind(py).call_method0("resume_capturing") {
            let _ = buffers.restore_python_state(py);
            return Err(error);
        }
        return Ok(Some(CaptureBuffers::Borrowed(buffers)));
    }
    FdCaptureBuffers::acquire(py, manager)
        .map(Box::new)
        .map(CaptureBuffers::Fd)
        .map(Some)
}

pub(super) fn with_uncaptured_output<T>(
    py: Python<'_>,
    buffers: Option<&CaptureBuffers>,
    render: impl FnOnce() -> PyResult<T>,
) -> PyResult<T> {
    let capture = buffers.and_then(|buffers| buffers.capture_handle(py));
    let active = capture
        .as_ref()
        .map(|capture| capture.is_started(py))
        .transpose()?
        .unwrap_or(false);
    if let Some(capture) = capture.as_ref().filter(|_| active) {
        capture.suspend_capturing(py)?;
    }
    let rendered = render();
    let resumed = match capture.as_ref().filter(|_| active) {
        Some(capture) => capture.resume_capturing(py),
        None => Ok(()),
    };
    let value = rendered?;
    resumed?;
    Ok(value)
}

pub(super) fn close_capture_buffers(
    py: Python<'_>,
    buffers: &mut Option<CaptureBuffers>,
) -> PyResult<()> {
    let Some(mut buffers) = buffers.take() else {
        return Ok(());
    };
    let restore_python = buffers.restore_python_state(py);
    drop(buffers);
    restore_python
}

pub(super) fn uses_capture_fixture(runtime: &SharedRuntime, test_index: usize) -> bool {
    let state = runtime.lock();
    let Some(test) = state.suite.tests().get(test_index) else {
        return false;
    };
    ["capfd", "capfdbinary", "capsys", "capsysbinary"]
        .iter()
        .any(|name| test.contains_fixture(name))
}

pub(super) fn start_capture(
    py: Python<'_>,
    buffers: &mut Option<CaptureBuffers>,
    fixture_capture: bool,
) -> PyResult<Option<()>> {
    let Some(buffers) = buffers else {
        return Ok(None);
    };
    if let Some(manager) = buffers.manager() {
        manager
            .bind(py)
            .call_method0(intern!(py, "resume_global_capture"))?;
    }
    if fixture_capture && let Some(manager) = buffers.manager() {
        manager
            .bind(py)
            .call_method0(intern!(py, "activate_fixture"))?;
    }
    Ok(Some(()))
}

pub(super) fn finish_capture(
    py: Python<'_>,
    buffers: &mut Option<CaptureBuffers>,
    state: Option<()>,
    fixture_capture: bool,
) -> PyResult<(String, String)> {
    let Some(()) = state else {
        return Ok((String::new(), String::new()));
    };
    let Some(buffers) = buffers else {
        return Err(PyRuntimeError::new_err("capture state has no buffers"));
    };
    if fixture_capture && let Some(manager) = buffers.manager() {
        manager
            .bind(py)
            .call_method0(intern!(py, "deactivate_fixture"))?;
    }
    match buffers {
        CaptureBuffers::Fd(buffers) => {
            flush_standard_streams(py);
            buffers
                .native_capture
                .as_ref()
                .ok_or_else(|| {
                    PyRuntimeError::new_err("capture buffers have no native capture state")
                })?
                .borrow(py)
                .drain_output()
        }
        CaptureBuffers::Borrowed(buffers) => {
            let capture = buffers.capture.bind(py);
            capture.call_method0(intern!(py, "suspend_capturing"))?;
            capture.call_method0(intern!(py, "readouterr"))?.extract()
        }
    }
}
