//! Builtin plugins retain their fixture declarations and resources.

use super::Collector;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::{PyTraverseError, PyVisit};
use std::path::Path;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};

mod temporary_path;
pub(crate) use temporary_path::register as register_temporary_paths;

const EMPTY: u8 = 0;
const CREATING: u8 = 1;
const READY: u8 = 2;
const CANCELLED: u8 = 3;
const PROVIDERS: [&str; 6] = [
    "fixtures",
    "legacypath",
    "tmpdir",
    "monkeypatch",
    "recwarn",
    "logging",
];

pub(in crate::independent) enum TempPathRetention {
    All,
    Failed,
    None,
}

pub(in crate::independent) fn temp_path_retention(
    config: &Bound<'_, PyAny>,
) -> PyResult<TempPathRetention> {
    let policy = config
        .call_method1("getini", ("tmp_path_retention_policy",))?
        .extract::<String>()?;
    match policy.as_str() {
        "all" => Ok(TempPathRetention::All),
        "failed" => Ok(TempPathRetention::Failed),
        "none" => Ok(TempPathRetention::None),
        _ => Err(crate::IndependentUsageError::new_err(format!(
            "tmp_path_retention_policy must be either all, failed, none. Current input: {policy}."
        ))),
    }
}

#[pyclass(frozen, module = "vfy._native._pytest")]
struct NativeTemporaryDirectoryOwner {
    basetemp: Option<String>,
    retention_count: usize,
    resource: Mutex<Option<Py<PyAny>>>,
    state: AtomicU8,
    cleared: AtomicBool,
}

impl NativeTemporaryDirectoryOwner {
    fn factory(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.cleared.load(Ordering::Acquire) {
            return Err(PyRuntimeError::new_err(
                "temporary directory owner has been cleared",
            ));
        }
        if let Some(resource) = self
            .resource
            .lock()
            .map_err(|_| PyRuntimeError::new_err("temporary directory resource lock is poisoned"))?
            .as_ref()
            .map(|resource| resource.clone_ref(py))
        {
            return Ok(resource);
        }
        self.state
            .compare_exchange(EMPTY, CREATING, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| {
                PyRuntimeError::new_err("temporary directory owner is closed or initializing")
            })?;
        let created = (|| -> PyResult<Py<PyAny>> {
            if let Some(path) = &self.basetemp {
                vfy_core::independent_collection::prepare_basetemp(Path::new(path))
                    .map_err(|error| pyo3::exceptions::PyOSError::new_err(error.to_string()))?;
            }
            Ok(py
                .import("vfy.independent.helpers")?
                .getattr("TempPathFactory")?
                .call1((self.basetemp.as_deref(), self.retention_count))?
                .unbind())
        })();
        match created {
            Ok(resource) => {
                if self
                    .state
                    .compare_exchange(CREATING, READY, Ordering::AcqRel, Ordering::Acquire)
                    .is_err()
                {
                    let cleaned = resource.bind(py).call_method0("cleanup");
                    let _ = self.state.compare_exchange(
                        CANCELLED,
                        EMPTY,
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    );
                    cleaned?;
                    return Err(PyRuntimeError::new_err(
                        "temporary directory owner closed during initialization",
                    ));
                }
                *self.resource.lock().map_err(|_| {
                    PyRuntimeError::new_err("temporary directory resource lock is poisoned")
                })? = Some(resource.clone_ref(py));
                Ok(resource)
            }
            Err(error) => {
                let _ = self.state.compare_exchange(
                    CREATING,
                    EMPTY,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                );
                let _ = self.state.compare_exchange(
                    CANCELLED,
                    EMPTY,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                );
                Err(error)
            }
        }
    }

    fn finish(&self, py: Python<'_>) -> PyResult<()> {
        // A saved public factory can acquire its first resource after config cleanup.
        let _ =
            self.state
                .compare_exchange(CREATING, CANCELLED, Ordering::AcqRel, Ordering::Acquire);
        let resource = self
            .resource
            .lock()
            .map_err(|_| PyRuntimeError::new_err("temporary directory resource lock is poisoned"))?
            .as_ref()
            .map(|resource| resource.clone_ref(py));
        if let Some(resource) = resource {
            resource.bind(py).call_method0("cleanup")?;
        }
        Ok(())
    }
}

#[pymethods]
impl NativeTemporaryDirectoryOwner {
    fn getbasetemp(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self
            .factory(py)?
            .bind(py)
            .call_method0("getbasetemp")?
            .unbind())
    }

    fn mktemp(
        &self,
        py: Python<'_>,
        basename: &Bound<'_, PyAny>,
        numbered: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("numbered", numbered)?;
        Ok(self
            .factory(py)?
            .bind(py)
            .call_method("mktemp", (basename,), Some(&kwargs))?
            .unbind())
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.finish(py)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(resource) = self.resource.try_lock() {
            visit.call(&*resource)?;
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        self.cleared.store(true, Ordering::Release);
        let _ = self.finish(py);
        let resource = self
            .resource
            .lock()
            .ok()
            .and_then(|mut resource| resource.take());
        drop(resource);
    }
}

fn configured_attribute(
    config: &Bound<'_, PyAny>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let journal = config
        .py()
        .import("vfy.independent.helpers")?
        .call_method0("MonkeyPatch")?;
    config.call_method1("add_cleanup", (journal.getattr("undo")?,))?;
    journal.call_method1("setattr", (config, name, value, false))?;
    Ok(())
}

pub(super) fn temporary_base(config: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let py = config.py();
    let requested = config
        .call_method1("getoption", ("basetemp", py.None()))?
        .extract::<Option<String>>()?;
    let worker_index = match config.getattr("option")?.getattr("_worker_basetemp_index") {
        Ok(index) => Some(index.extract::<usize>()?),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
        Err(error) => return Err(error),
    };
    requested
        .map(|path| {
            let root = config
                .getattr("_invocation_dir")?
                .str()?
                .to_str()?
                .to_owned();
            let base = vfy_core::independent_collection::validate_basetemp(Path::new(&root), &path)
                .map_err(crate::IndependentUsageError::new_err)?;
            let path = match worker_index {
                Some(index) => base.join(format!("worker-{index}")),
                None => base,
            };
            vfy_core::independent_collection::validate_basetemp(
                Path::new(&root),
                &path.to_string_lossy(),
            )
            .map(|path| path.to_string_lossy().into_owned())
            .map_err(crate::IndependentUsageError::new_err)
        })
        .transpose()
}

#[pyfunction]
fn configure_tmpdir(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = config.py();
    let basetemp = temporary_base(config)?;
    let retention_count = config
        .call_method1("getini", ("tmp_path_retention_count",))?
        .str()?
        .to_str()?
        .trim()
        .parse::<usize>()
        .map_err(|_| {
            crate::IndependentUsageError::new_err(
                "tmp_path_retention_count must be a non-negative integer",
            )
        })?;
    let retention_count = match temp_path_retention(config)? {
        TempPathRetention::None => 0,
        TempPathRetention::All | TempPathRetention::Failed => retention_count,
    };
    let owner = Py::new(
        py,
        NativeTemporaryDirectoryOwner {
            basetemp,
            retention_count,
            resource: Mutex::new(None),
            state: AtomicU8::new(EMPTY),
            cleared: AtomicBool::new(false),
        },
    )?;
    let factory = py
        .import("vfy.independent.builtin_fixtures")?
        .getattr("ConfiguredTempPathFactory")?
        .call1((&owner,))?;
    // Config cleanup owns the one resource even if its public fixture is overridden.
    config.call_method1("add_cleanup", (owner.bind(py).getattr("close")?,))?;
    configured_attribute(config, "_tmp_path_factory", &factory)
}

#[pyfunction]
fn finish_tmpdir(session: &Bound<'_, PyAny>, exitstatus: i32) -> PyResult<()> {
    let config = session.getattr("config")?;
    if exitstatus != 0 || !matches!(temp_path_retention(&config)?, TempPathRetention::Failed) {
        return Ok(());
    }
    let factory = config.getattr("_tmp_path_factory")?;
    let owner = factory
        .getattr("_owner")?
        .extract::<Py<NativeTemporaryDirectoryOwner>>()?;
    let owner = owner.bind(session.py()).get();
    if owner.basetemp.is_some() {
        return Ok(());
    }
    let resource = owner
        .resource
        .lock()
        .map_err(|_| PyRuntimeError::new_err("temporary directory resource lock is poisoned"))?
        .as_ref()
        .map(|resource| resource.clone_ref(session.py()));
    if let Some(resource) = resource {
        let path = resource
            .bind(session.py())
            .call_method0("getbasetemp")?
            .extract::<std::path::PathBuf>()?;
        // Match pytest's best-effort removal without allocating an unused factory.
        let _ = session.py().detach(|| std::fs::remove_dir_all(path));
    }
    Ok(())
}

#[pyfunction]
fn configure_legacy_path(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let manager = config.getattr("pluginmanager")?;
    if !manager
        .call_method1("hasplugin", ("tmpdir",))?
        .is_truthy()?
    {
        return Ok(());
    }
    match config.getattr("_tmp_path_factory") {
        Ok(factory) => {
            let legacy = config
                .py()
                .import("vfy.independent.legacy_path")?
                .getattr("LegacyPathFactory")?
                .call1((factory,))?;
            configured_attribute(config, "_tmpdirhandler", &legacy)?;
        }
        Err(error) if error.is_instance_of::<PyAttributeError>(config.py()) => {}
        Err(error) => return Err(error),
    }
    let plugin = config
        .py()
        .import("vfy.independent.builtin_fixtures")?
        .call_method0("LegacyTmpdirProvider")?;
    manager.call_method1("register", (plugin, "legacypath-tmpdir"))?;
    Ok(())
}

#[pyfunction]
fn configure_logging(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = config.py();
    let capture = Py::new(py, super::capture::NativeLoggingHooks::new())?;
    capture.borrow(py).configure(py, config)?;
    let plugin = py
        .import("vfy.independent.protocol")?
        .getattr("BuiltinPhaseHooks")?
        .call1((capture,))?;
    config
        .getattr("pluginmanager")?
        .call_method1("register", (plugin, "logging-plugin"))?;
    Ok(())
}

impl Collector<'_> {
    pub(super) fn install_builtin_providers(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        for name in PROVIDERS {
            self.exclude_raw_provider(raw_plugins, name)?;
        }
        for name in PROVIDERS {
            self.activate_builtin_provider(name)?;
        }
        Ok(())
    }

    pub(super) fn activate_builtin_provider(&mut self, name: &str) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("builtin providers require a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager.call_method1("is_blocked", (name,))?.is_truthy()? {
            return Ok(());
        }
        let module = self.py.import("vfy.independent.builtin_fixtures")?;
        let plugin = match name {
            "fixtures" => module.call_method0("FixturesProvider")?,
            "legacypath" => module
                .getattr("LegacyPathProvider")?
                .call1((wrap_pyfunction!(configure_legacy_path, self.py)?,))?,
            "tmpdir" => module.getattr("TmpdirProvider")?.call1((
                wrap_pyfunction!(configure_tmpdir, self.py)?,
                wrap_pyfunction!(finish_tmpdir, self.py)?,
            ))?,
            "monkeypatch" => module.call_method0("MonkeyPatchProvider")?,
            "recwarn" => module.call_method0("RecwarnProvider")?,
            "logging" => module
                .getattr("LoggingProvider")?
                .call1((wrap_pyfunction!(configure_logging, self.py)?,))?,
            _ => return Err(PyValueError::new_err("unknown builtin provider")),
        };
        let registered = manager.call_method1("register", (&plugin, name))?;
        if !registered.is_none() {
            self.plugins.insert(name.to_owned(), plugin.unbind());
        }
        Ok(())
    }
}
