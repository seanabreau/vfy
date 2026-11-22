//! Dispatch collection around the builtin collector at its hook position.

use super::Context;
use crate::hooks::NativeHookRegistry;
use crate::independent::collection::arena::{self, NativeCollectionArena};
use crate::independent::collection_state::OwnedCollector;
use crate::independent::{CollectedSuite, Collector, collection};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::cell::RefCell;
use std::path::PathBuf;

enum CollectionState {
    Pending(Box<OwnedCollector>),
    Running,
    Complete(Box<CollectedSuite>),
    Closed,
}

#[pyclass(frozen, unsendable)]
struct CollectionCallback {
    state: RefCell<CollectionState>,
    context: RefCell<Option<Context>>,
    root: PathBuf,
    keyword: Option<String>,
}

impl CollectionCallback {
    fn context(&self, py: Python<'_>) -> PyResult<Context> {
        self.context
            .borrow()
            .as_ref()
            .map(|context| context.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("collection callback has been closed"))
    }
}

#[pymethods]
impl CollectionCallback {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "PyO3 requires a bound getter for the callable signature protocol"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.context(py)?
            .session
            .bind(py)
            .call_method0("perform_collect")?;
        Ok(())
    }

    fn clear_reports(&self, py: Python<'_>) -> PyResult<()> {
        let arena = arena::for_context(py, &self.context(py)?)?;
        NativeCollectionArena::clear_reports(&arena);
        Ok(())
    }

    fn collect(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let mut collector = match self.state.replace(CollectionState::Running) {
            CollectionState::Pending(collector) => (*collector).bind(py),
            previous => {
                self.state.replace(previous);
                return Err(crate::UnsupportedFeatureError::new_err(
                    "independent Session.perform_collect is available once during pytest_collection",
                ));
            }
        };
        let collected = collection::collect(&mut collector, &self.root);
        let mut suite = match collector.finish() {
            Ok(suite) => suite,
            Err(error) => {
                return match collected {
                    Err(original) => Err(original),
                    Ok(()) => Err(error),
                };
            }
        };
        let prepared = if collected.is_ok() {
            (|| {
                self.context(py)?
                    .config
                    .bind(py)
                    .getattr("pluginmanager")?
                    .call_method0("check_pending")?;
                super::prepare_items(py, &mut suite, self.keyword.as_deref())
            })()
        } else {
            Ok(())
        };
        self.state
            .replace(CollectionState::Complete(Box::new(suite)));
        collected?;
        prepared?;
        let arena = arena::for_context(py, &self.context(py)?)?;
        Ok(NativeCollectionArena::items(&arena)?.unbind())
    }
}

pub(in crate::independent) fn run(
    py: Python<'_>,
    collector: Collector<'_>,
    root: PathBuf,
    keyword: Option<String>,
) -> PyResult<CollectedSuite> {
    let context = collector
        .context
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("collection requires a configured session"))?
        .clone_ref(py);
    let callback = Py::new(
        py,
        CollectionCallback {
            state: RefCell::new(CollectionState::Pending(Box::new(OwnedCollector::new(
                collector,
            )))),
            context: RefCell::new(Some(context.clone_ref(py))),
            root,
            keyword,
        },
    )?;
    let session = context.session.bind(py);
    session.setattr("_vfy_perform_collect", callback.bind(py))?;
    let outcome = (|| {
        let kwargs = PyDict::new(py);
        kwargs.set_item("session", session)?;
        NativeHookRegistry::call_with_default(
            context.registry.bind(py),
            py,
            "pytest_collection",
            &kwargs,
            callback.clone_ref(py).into_any(),
            true,
            "main",
        )
    })();
    let state = callback.borrow(py).state.replace(CollectionState::Closed);
    let released_context = callback.borrow(py).context.take();
    drop(released_context);
    let released = session.setattr("_vfy_perform_collect", py.None());
    outcome?;
    released?;
    let mut suite = match state {
        CollectionState::Complete(suite) => *suite,
        CollectionState::Pending(collector) => {
            let mut collector = (*collector).bind(py);
            // A non-None hook result bypasses argument resolution and collection.
            collector.specification = None;
            collector.finish()?
        }
        CollectionState::Running => {
            return Err(crate::UnsupportedFeatureError::new_err(
                "collection hook suppressed a failure before a native suite was available",
            ));
        }
        CollectionState::Closed => {
            return Err(PyRuntimeError::new_err(
                "collection callback has been closed",
            ));
        }
    };
    super::synchronize_items(py, &mut suite)?;
    crate::independent::configuration::unknown_ini(py, &context)?;
    Ok(suite)
}
