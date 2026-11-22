//! Binary and tee fixture state share the invocation's stream resources and capture owner.

use super::global_capture::{NativeGlobalCapture, locked};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyType};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Clone, Copy)]
enum CaptureKind {
    Sys,
    Fd,
    TeeSys,
    SysText,
    FdText,
}

impl CaptureKind {
    const fn name(self) -> &'static str {
        match self {
            Self::Sys => "capsysbinary",
            Self::Fd => "capfdbinary",
            Self::TeeSys => "capteesys",
            Self::SysText => "capsys",
            Self::FdText => "capfd",
        }
    }

    const fn resource(self) -> &'static str {
        match self {
            Self::Sys | Self::TeeSys | Self::SysText => "SysCaptureIO",
            Self::Fd | Self::FdText => "FdCaptureIO",
        }
    }

    /// Text fixtures decode captured output and keep the `str` replay protocol.
    const fn is_text(self) -> bool {
        matches!(self, Self::TeeSys | Self::SysText | Self::FdText)
    }
}

#[derive(Default)]
struct CapturedBytes {
    out: Vec<u8>,
    err: Vec<u8>,
}

impl CapturedBytes {
    fn read(py: Python<'_>, resource: &Bound<'_, PyAny>, kind: CaptureKind) -> PyResult<Self> {
        if kind.is_text() {
            let (out, err) = resource
                .call_method0("readouterr")?
                .extract::<(String, String)>()?;
            return Ok(Self {
                out: out.into_bytes(),
                err: err.into_bytes(),
            });
        }
        let (out, err) = resource
            .call_method0("readouterr_bytes")?
            .extract::<(Py<PyBytes>, Py<PyBytes>)>()?;
        Ok(Self {
            out: out.bind(py).as_bytes().to_vec(),
            err: err.bind(py).as_bytes().to_vec(),
        })
    }

    fn append(&mut self, other: Self) {
        self.out.extend(other.out);
        self.err.extend(other.err);
    }
}

/// Decode captured UTF-8 bytes for the text replay protocol.
fn utf8(bytes: &[u8]) -> PyResult<&str> {
    std::str::from_utf8(bytes).map_err(|error| PyValueError::new_err(error.to_string()))
}

struct FixtureContext {
    owner: Py<NativeGlobalCapture>,
    request: Py<PyAny>,
}

#[pyclass(frozen, module = "vfy._native._pytest")]
struct NativeCaptureFixture {
    kind: CaptureKind,
    context: Mutex<Option<FixtureContext>>,
    resource: Mutex<Option<Py<PyAny>>>,
    pending: Mutex<CapturedBytes>,
    active: AtomicBool,
}

impl NativeCaptureFixture {
    fn context(&self, py: Python<'_>) -> PyResult<FixtureContext> {
        locked(&self.context, "capture fixture")?
            .as_ref()
            .map(|context| FixtureContext {
                owner: context.owner.clone_ref(py),
                request: context.request.clone_ref(py),
            })
            .ok_or_else(|| PyRuntimeError::new_err("capture fixture has been cleared"))
    }

    fn resource(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        Ok(locked(&self.resource, "capture fixture")?
            .as_ref()
            .map(|resource| resource.clone_ref(py)))
    }

    fn close_resource(&self, py: Python<'_>) -> PyResult<()> {
        let Some(resource) = self.resource(py)? else {
            return Ok(());
        };
        let captured = CapturedBytes::read(py, resource.bind(py), self.kind)?;
        // Stock replay callbacks observe drained resources and earlier pending data.
        match self.kind {
            CaptureKind::TeeSys => {}
            CaptureKind::SysText | CaptureKind::FdText => {
                resource.bind(py).call_method1(
                    "write_original",
                    (utf8(&captured.out)?, utf8(&captured.err)?),
                )?;
            }
            CaptureKind::Sys | CaptureKind::Fd => {
                resource.bind(py).call_method1(
                    "write_original_bytes",
                    (
                        PyBytes::new(py, &captured.out),
                        PyBytes::new(py, &captured.err),
                    ),
                )?;
            }
        }
        locked(&self.pending, "capture fixture")?.append(captured);
        self.active.store(false, Ordering::Release);
        resource.bind(py).call_method0("close")?;
        let removed = locked(&self.resource, "capture fixture")?.take();
        drop(removed);
        Ok(())
    }
}

#[pymethods]
impl NativeCaptureFixture {
    /// Stock pytest annotates fixtures as `CaptureFixture[str]`; every fixture is this class.
    #[classmethod]
    fn __class_getitem__<'py>(
        cls: &Bound<'py, PyType>,
        _item: &Bound<'py, PyAny>,
    ) -> Bound<'py, PyType> {
        cls.clone()
    }

    #[getter(_vfy_capture_name)]
    fn capture_name(&self) -> &'static str {
        self.kind.name()
    }

    #[getter]
    fn request(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.context(py)?.request)
    }

    fn _start(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let fixture = slf.get();
        let context = fixture.context(py)?;
        context
            .owner
            .bind(py)
            .call_method1("fixture_started", (slf,))?;
        let started = (|| -> PyResult<()> {
            if let Some(resource) = fixture.resource(py)? {
                resource.bind(py).call_method0("resume")?;
                return Ok(());
            }
            let kwargs = PyDict::new(py);
            if matches!(fixture.kind, CaptureKind::TeeSys) {
                kwargs.set_item("tee", true)?;
            }
            let resource = py
                .import("vfy.independent.stdio")?
                .getattr(fixture.kind.resource())?
                .call((), Some(&kwargs))?;
            if let Err(error) = resource.call_method0("resume") {
                resource.call_method0("close")?;
                return Err(error);
            }
            *locked(&fixture.resource, "capture fixture")? = Some(resource.unbind());
            Ok(())
        })();
        if started.is_ok() {
            fixture.active.store(true, Ordering::Release);
        } else {
            context
                .owner
                .bind(py)
                .call_method1("fixture_stopped", (slf,))?;
        }
        started
    }

    fn _stop(&self, py: Python<'_>) -> PyResult<()> {
        if !self.active.load(Ordering::Acquire) {
            return Ok(());
        }
        if let Some(resource) = self.resource(py)? {
            resource.bind(py).call_method0("suspend")?;
        }
        self.active.store(false, Ordering::Release);
        Ok(())
    }

    fn _is_started(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    fn _resume(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(resource) = self.resource(py)? {
            resource.bind(py).call_method0("resume")?;
            self.active.store(true, Ordering::Release);
        }
        Ok(())
    }

    fn readouterr(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let current = self
            .resource(py)?
            .map(|resource| CapturedBytes::read(py, resource.bind(py), self.kind))
            .transpose()?;
        let mut captured = std::mem::take(&mut *locked(&self.pending, "capture fixture")?);
        if let Some(current) = current {
            captured.append(current);
        }
        let out = PyBytes::new(py, &captured.out);
        let err = PyBytes::new(py, &captured.err);
        let (out, err) = if self.kind.is_text() {
            (
                out.call_method1("decode", ("UTF-8",))?,
                err.call_method1("decode", ("UTF-8",))?,
            )
        } else {
            (out.into_any(), err.into_any())
        };
        Ok(py
            .import("vfy.independent.helpers")?
            .call_method1("CaptureResult", (out, err))?
            .unbind())
    }

    fn disabled(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let sys = py.import("sys")?;
        Ok(py
            .import("vfy.independent.helpers")?
            .call_method1(
                "_capture_disabled",
                (
                    sys.getattr("stdout")?,
                    sys.getattr("stderr")?,
                    self.context(py)?.owner,
                ),
            )?
            .unbind())
    }

    fn close(slf: &Bound<'_, Self>) -> PyResult<()> {
        slf.get().close_resource(slf.py())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(context) = self.context.try_lock()
            && let Some(context) = context.as_ref()
        {
            visit.call(&context.owner)?;
            visit.call(&context.request)?;
        }
        if let Ok(resource) = self.resource.try_lock() {
            visit.call(&*resource)?;
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let _ = slf.get().close_resource(slf.py());
        let context = slf
            .get()
            .context
            .lock()
            .ok()
            .and_then(|mut context| context.take());
        drop(context);
    }
}

fn create(
    py: Python<'_>,
    kind: CaptureKind,
    owner: Py<NativeGlobalCapture>,
    request: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let fixture = Py::new(
        py,
        NativeCaptureFixture {
            kind,
            context: Mutex::new(Some(FixtureContext {
                owner,
                request: request.clone().unbind(),
            })),
            resource: Mutex::new(None),
            pending: Mutex::new(CapturedBytes::default()),
            active: AtomicBool::new(false),
        },
    )?;
    fixture.bind(py).call_method0("_start")?;
    Ok(fixture.into_any())
}

struct CleanupObjects {
    owner: Py<NativeGlobalCapture>,
    fixture: Py<PyAny>,
}

#[pyclass(frozen, module = "vfy._native._pytest")]
struct NativeCaptureCleanup {
    objects: Mutex<Option<CleanupObjects>>,
}

impl NativeCaptureCleanup {
    fn finish(&self, py: Python<'_>) -> PyResult<()> {
        let objects = locked(&self.objects, "capture fixture")?.take();
        if let Some(objects) = objects {
            objects.fixture.bind(py).call_method0("close")?;
            objects
                .owner
                .bind(py)
                .call_method1("fixture_stopped", (objects.fixture,))?;
        }
        Ok(())
    }
}

#[pymethods]
impl NativeCaptureCleanup {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.finish(py)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_lock()
            && let Some(objects) = objects.as_ref()
        {
            visit.call(&objects.owner)?;
            visit.call(&objects.fixture)?;
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        let _ = self.finish(py);
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeCaptureFixture>()
}

#[pyfunction]
pub(crate) fn independent_capture_fixture(
    py: Python<'_>,
    request: &Bound<'_, super::execution::NativeIndependentRequest>,
    name: &str,
) -> PyResult<Py<PyAny>> {
    let request = request.as_any();
    let manager = request
        .getattr("config")?
        .getattr("pluginmanager")?
        .call_method1("getplugin", ("capturemanager",))?;
    if manager.is_none() {
        // A retained declaration can outlive its initial capture hook. The upstream
        // fixture then raises through the absent manager's set_fixture protocol.
        manager.getattr("set_fixture")?;
    }
    let owner = manager.extract::<Py<NativeGlobalCapture>>()?;
    let value = match name {
        "capsysbinary" => create(py, CaptureKind::Sys, owner.clone_ref(py), request)?,
        "capfdbinary" => create(py, CaptureKind::Fd, owner.clone_ref(py), request)?,
        "capteesys" => create(py, CaptureKind::TeeSys, owner.clone_ref(py), request)?,
        "capsys" => create(py, CaptureKind::SysText, owner.clone_ref(py), request)?,
        "capfd" => create(py, CaptureKind::FdText, owner.clone_ref(py), request)?,
        _ => return Err(PyValueError::new_err("invalid capture fixture name")),
    };
    let cleanup = Py::new(
        py,
        NativeCaptureCleanup {
            objects: Mutex::new(Some(CleanupObjects {
                owner,
                fixture: value.clone_ref(py),
            })),
        },
    )?;
    if let Err(error) = request.call_method1("addfinalizer", (&cleanup,)) {
        cleanup.get().finish(py)?;
        return Err(error);
    }
    Ok(value)
}
