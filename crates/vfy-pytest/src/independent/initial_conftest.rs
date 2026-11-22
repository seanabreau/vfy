//! Run initial conftest discovery inside its hook default.

use super::Collector;
use super::imports::ImportMode;
use super::lifecycle::{Context, state::LifecycleStage};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::PathBuf;

struct ImportState {
    context: Context,
    root: PathBuf,
    invocation_dir: PathBuf,
    paths: Vec<String>,
    mode: ImportMode,
    modules: BTreeMap<PathBuf, Py<PyAny>>,
    plugins: BTreeMap<String, Py<PyAny>>,
}

enum State {
    Ready(ImportState),
    Running,
    Complete(ImportState),
    Consumed,
}

#[pyclass(frozen, unsendable)]
struct InitialConftest {
    state: RefCell<State>,
}

impl InitialConftest {
    fn restore(&self) -> PyResult<ImportState> {
        match self.state.replace(State::Consumed) {
            State::Ready(state) | State::Complete(state) => Ok(state),
            previous => {
                self.state.replace(previous);
                Err(PyRuntimeError::new_err(
                    "initial conftest callback is not complete",
                ))
            }
        }
    }
}

#[pymethods]
impl InitialConftest {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "the native callback exposes its bound signature"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py.import("inspect")?.call_method0("Signature")?.unbind())
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let state = match self.state.replace(State::Running) {
            State::Ready(state) => state,
            previous => {
                self.state.replace(previous);
                return Err(PyRuntimeError::new_err(
                    "initial conftest callback is consumed",
                ));
            }
        };
        let mut collector = match Collector::new(py) {
            Ok(collector) => collector,
            Err(error) => {
                self.state.replace(State::Complete(state));
                return Err(error);
            }
        };
        let context = state.context;
        collector.context = Some(context.clone_ref(py));
        collector.root = state.root;
        collector.import_mode = state.mode;
        collector.modules = state.modules;
        collector.plugins = state.plugins;
        if let Some(context) = &collector.context {
            context.stage.set(LifecycleStage::ConftestLoading);
        }
        let result =
            super::collection::preload(&mut collector, &state.invocation_dir, &state.paths);
        self.state.replace(State::Complete(ImportState {
            context,
            root: collector.root,
            invocation_dir: state.invocation_dir,
            paths: state.paths,
            mode: collector.import_mode,
            modules: collector.modules,
            plugins: collector.plugins,
        }));
        result
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(state) = self.state.try_borrow()
            && let State::Ready(state) | State::Complete(state) = &*state
        {
            visit.call(&state.context.config)?;
            visit.call(&state.context.session)?;
            visit.call(&state.context.parser)?;
            visit.call(&state.context.registry)?;
            for module in state.modules.values().chain(state.plugins.values()) {
                visit.call(module)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self.state.replace(State::Consumed);
        drop(removed);
    }
}

pub(super) fn load(
    collector: &mut Collector<'_>,
    arguments: &Bound<'_, PyAny>,
    invocation_dir: &std::path::Path,
    paths: Vec<String>,
) -> PyResult<()> {
    let py = collector.py;
    let context = collector
        .context
        .as_ref()
        .ok_or_else(|| {
            PyRuntimeError::new_err("initial conftest loading requires a configured session")
        })?
        .clone_ref(py);
    let callback = Py::new(
        py,
        InitialConftest {
            state: RefCell::new(State::Consumed),
        },
    )?;
    callback.borrow(py).state.replace(State::Ready(ImportState {
        context: context.clone_ref(py),
        root: collector.root.clone(),
        invocation_dir: invocation_dir.to_path_buf(),
        paths,
        mode: collector.import_mode,
        modules: std::mem::take(&mut collector.modules),
        plugins: std::mem::take(&mut collector.plugins),
    }));
    let result = (|| -> PyResult<()> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("early_config", context.config.bind(py))?;
        kwargs.set_item("parser", context.parser.bind(py))?;
        kwargs.set_item("args", arguments)?;
        context.registry.bind(py).call_method1(
            "call_with_default",
            (
                "pytest_load_initial_conftests",
                kwargs,
                &callback,
                false,
                "pytestconfig",
            ),
        )?;
        Ok(())
    })();
    let completed = callback.borrow(py).restore()?;
    collector.modules = completed.modules;
    collector.plugins = completed.plugins;
    collector.import_mode = completed.mode;
    result
}
