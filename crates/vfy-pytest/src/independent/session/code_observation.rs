//! Collection owns observations of callable code before mutation identity probes.

use pyo3::prelude::*;
use pyo3::types::{PyFunction, PyTuple};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

const CANARY: &str = "vfy.mutation.collection_guard";

#[derive(Default)]
struct Observations {
    registered: bool,
    active: bool,
    functions: BTreeMap<usize, Py<PyAny>>,
}

type SharedObservations = Arc<Mutex<Observations>>;

thread_local! {
    static OBSERVER: RefCell<Option<SharedObservations>> = const { RefCell::new(None) };
}

#[pyclass]
struct CodeObserver {
    state: SharedObservations,
}

#[pymethods]
impl CodeObserver {
    fn __call__(&self, event: &str, arguments: &Bound<'_, PyTuple>) {
        if event == CANARY {
            let same = arguments.get_item(0).ok().and_then(|value| {
                value
                    .extract::<PyRef<'_, Self>>()
                    .ok()
                    .map(|owner| Arc::ptr_eq(&owner.state, &self.state))
            });
            if same == Some(true) {
                self.state
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .registered = true;
            }
            return;
        }
        if !matches!(event, "object.__getattr__" | "object.__setattr__") {
            return;
        }
        let code = arguments
            .get_item(1)
            .ok()
            .and_then(|name| name.extract::<String>().ok());
        if code.as_deref() != Some("__code__") {
            return;
        }
        let Ok(function) = arguments.get_item(0) else {
            return;
        };
        if !function.is_exact_instance_of::<PyFunction>() {
            return;
        }
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.active {
            state
                .functions
                .entry(function.as_ptr() as usize)
                .or_insert_with(|| function.unbind());
        }
    }
}

/// A blocked audit-hook registration denies retained execution, leaving fresh execution available.
pub(super) struct CollectionGuard {
    state: SharedObservations,
    active: bool,
}

impl CollectionGuard {
    pub(super) fn begin(py: Python<'_>) -> PyResult<Option<Self>> {
        let (state, install) = OBSERVER.with_borrow_mut(|slot| {
            if let Some(state) = slot.as_ref() {
                (Arc::clone(state), false)
            } else {
                let state = Arc::new(Mutex::new(Observations::default()));
                *slot = Some(Arc::clone(&state));
                (state, true)
            }
        });
        if install {
            let observer = Py::new(
                py,
                CodeObserver {
                    state: Arc::clone(&state),
                },
            )?;
            let sys = py.import("sys")?;
            let registered = sys
                .call_method1("addaudithook", (&observer,))
                .and_then(|_| sys.call_method1("audit", (CANARY, &observer)));
            if let Err(error) = registered {
                if error.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py) {
                    return Ok(None);
                }
                return Err(error);
            }
        }
        {
            let mut owner = state
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if !owner.registered || owner.active {
                return Ok(None);
            }
            owner.active = true;
        }
        Ok(Some(Self {
            state,
            active: true,
        }))
    }

    pub(super) fn finish(mut self) -> Vec<Py<PyAny>> {
        self.active = false;
        let mut owner = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        owner.active = false;
        std::mem::take(&mut owner.functions).into_values().collect()
    }
}

impl Drop for CollectionGuard {
    fn drop(&mut self) {
        if self.active {
            let functions = {
                let mut owner = self
                    .state
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                owner.active = false;
                std::mem::take(&mut owner.functions)
            };
            drop(functions);
        }
    }
}
