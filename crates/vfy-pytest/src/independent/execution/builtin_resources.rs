//! Resource acquisition receives the ordinary fixture's request and owner.

use super::super::builtin_providers::{TempPathRetention, temp_path_retention};
use super::super::reporting::NativeSessionReports;
use super::super::reporting::origin::ReportOrigin;
use super::request::NativeIndependentRequest;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};
use std::path::PathBuf;
use std::sync::{Mutex, PoisonError};

#[pyclass(frozen)]
struct TempPathCleanup {
    objects: Mutex<Option<(Py<NativeSessionReports>, Py<PyAny>)>>,
    origin: ReportOrigin,
}

#[pymethods]
impl TempPathCleanup {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let objects = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        let Some((history, path)) = objects else {
            return Ok(());
        };
        let passed = history.borrow(py).call_passed(py, &self.origin);
        if passed {
            let path = path.bind(py).extract::<PathBuf>()?;
            // pytest's tmp_path finalizer removes successful paths on a best-effort basis.
            let _ = py.detach(|| std::fs::remove_dir_all(path));
        }
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_lock()
            && let Some((history, path)) = objects.as_ref()
        {
            visit.call(history)?;
            visit.call(path)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(removed);
    }
}

#[derive(Clone, Copy)]
enum ResourceKind {
    TempFactory,
    TempPath,
    LegacyFactory,
    Monkeypatch,
    WarningRecorder,
    LogCapture,
}

impl ResourceKind {
    fn parse(name: &str) -> PyResult<Self> {
        match name {
            "tmp_path_factory" => Ok(Self::TempFactory),
            "tmp_path" => Ok(Self::TempPath),
            "tmpdir_factory" => Ok(Self::LegacyFactory),
            "monkeypatch" => Ok(Self::Monkeypatch),
            "recwarn" => Ok(Self::WarningRecorder),
            "caplog" => Ok(Self::LogCapture),
            _ => Err(PyValueError::new_err("unknown fixture resource kind")),
        }
    }
}

#[pyfunction]
pub(crate) fn independent_fixture_resource(
    py: Python<'_>,
    request: &Bound<'_, NativeIndependentRequest>,
    kind: &str,
    dependency: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let kind = ResourceKind::parse(kind)?;
    let (owner, context) = request.get().parts()?;
    owner.suite()?;
    let (value, cleanup) = match kind {
        ResourceKind::TempFactory | ResourceKind::LegacyFactory => {
            let config = request.as_any().getattr("config")?;
            let attribute = if matches!(kind, ResourceKind::TempFactory) {
                "_tmp_path_factory"
            } else {
                "_tmpdirhandler"
            };
            (config.getattr(attribute)?.unbind(), None)
        }
        ResourceKind::TempPath => {
            let path = owner.temporary_path(py, &context, dependency)?;
            let cleanup = if matches!(
                temp_path_retention(&request.as_any().getattr("config")?)?,
                TempPathRetention::Failed
            ) {
                let history = request
                    .as_any()
                    .getattr("session")?
                    .getattr("_vfy_report")?
                    .extract::<Py<NativeSessionReports>>()?;
                Some(
                    Py::new(
                        py,
                        TempPathCleanup {
                            objects: Mutex::new(Some((history, path.clone_ref(py)))),
                            origin: ReportOrigin::bound(
                                py,
                                &owner.suite()?.context,
                                context.test_index,
                            )?,
                        },
                    )?
                    .into_any(),
                )
            } else {
                None
            };
            (path, cleanup)
        }
        ResourceKind::Monkeypatch | ResourceKind::WarningRecorder => {
            let name = if matches!(kind, ResourceKind::Monkeypatch) {
                "monkeypatch"
            } else {
                "recwarn"
            };
            let (value, cleanup) = owner.builtin_resource(py, name)?;
            (value, Some(cleanup))
        }
        ResourceKind::LogCapture => {
            let capture = owner.capture.borrow().clone();
            let (value, cleanup) = capture
                .ok_or_else(|| {
                    PyValueError::new_err(
                        "fixture 'caplog' is unavailable because logging capture is disabled",
                    )
                })?
                .caplog(py)?;
            (value, Some(cleanup))
        }
    };
    if let Some(cleanup) = cleanup
        && let Err(error) = request.call_method1("addfinalizer", (&cleanup,))
    {
        cleanup.bind(py).call0()?;
        return Err(error);
    }
    Ok(value)
}
