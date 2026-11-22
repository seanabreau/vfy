//! A fixture's callable, cache, and finalizers have one GC-visible runtime owner.

use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::{Mutex, PoisonError};
use vfy_core::{FixtureId, FixtureScope};

/// The fixture's one live result, promoted to its public tuple on observation.
pub(super) enum FixtureOutcome {
    Empty,
    Native(NativeFixtureOutcome),
    Python(Py<PyAny>),
}

pub(super) enum FixtureValue {
    Empty,
    Native {
        value: Py<PyAny>,
        failure: Option<(Py<PyAny>, Py<PyAny>)>,
    },
    Python(Py<PyAny>),
}

pub(super) enum FixtureParameter {
    Empty,
    Native(Py<PyAny>),
    Python(Py<PyAny>),
}

pub(super) struct NativeFixtureOutcome {
    pub(super) value: Py<PyAny>,
    pub(super) parameter: Py<PyAny>,
    pub(super) failure: Option<(Py<PyAny>, Py<PyAny>)>,
}

impl NativeFixtureOutcome {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            value: self.value.clone_ref(py),
            parameter: self.parameter.clone_ref(py),
            failure: self
                .failure
                .as_ref()
                .map(|(error, traceback)| (error.clone_ref(py), traceback.clone_ref(py))),
        }
    }

    fn tuple(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let failure = match &self.failure {
            Some((error, traceback)) => {
                PyTuple::new(py, [error.clone_ref(py), traceback.clone_ref(py)])?
                    .into_any()
                    .unbind()
            }
            None => py.None(),
        };
        Ok(PyTuple::new(
            py,
            [
                self.value.clone_ref(py),
                self.parameter.clone_ref(py),
                failure,
            ],
        )?
        .into_any()
        .unbind())
    }
}

struct BindingObjects {
    callable: Py<PyAny>,
    finalizers: Py<PyList>,
    outcome: FixtureOutcome,
    outcome_generation: u64,
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeFixtureBinding {
    id: FixtureId,
    argname: String,
    declared_scope: FixtureScope,
    objects: Mutex<Option<BindingObjects>>,
}

impl NativeFixtureBinding {
    pub(super) fn new(
        py: Python<'_>,
        id: FixtureId,
        argname: String,
        declared_scope: FixtureScope,
        callable: Py<PyAny>,
    ) -> PyResult<Py<Self>> {
        Py::new(
            py,
            Self {
                id,
                argname,
                declared_scope,
                objects: Mutex::new(Some(BindingObjects {
                    callable,
                    finalizers: PyList::empty(py).unbind(),
                    outcome: FixtureOutcome::Empty,
                    outcome_generation: 0,
                })),
            },
        )
    }

    fn object<T>(&self, select: impl FnOnce(&BindingObjects) -> T) -> PyResult<T> {
        let selected = {
            let objects = self.objects.lock().unwrap_or_else(PoisonError::into_inner);
            objects.as_ref().map(select)
        };
        selected.ok_or_else(|| PyAssertionError::new_err("fixture binding has been cleared"))
    }

    pub(super) fn callable(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.object(|objects| objects.callable.clone_ref(py))
    }

    pub(super) fn finalizers(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.object(|objects| objects.finalizers.clone_ref(py))
    }

    pub(super) fn outcome_value(&self, py: Python<'_>) -> PyResult<FixtureValue> {
        self.object(|objects| match &objects.outcome {
            FixtureOutcome::Empty => FixtureValue::Empty,
            FixtureOutcome::Native(outcome) => FixtureValue::Native {
                value: outcome.value.clone_ref(py),
                failure: outcome
                    .failure
                    .as_ref()
                    .map(|(error, traceback)| (error.clone_ref(py), traceback.clone_ref(py))),
            },
            FixtureOutcome::Python(value) => FixtureValue::Python(value.clone_ref(py)),
        })
    }

    pub(super) fn outcome_parameter(&self, py: Python<'_>) -> PyResult<FixtureParameter> {
        self.object(|objects| match &objects.outcome {
            FixtureOutcome::Empty => FixtureParameter::Empty,
            FixtureOutcome::Native(outcome) => {
                FixtureParameter::Native(outcome.parameter.clone_ref(py))
            }
            FixtureOutcome::Python(value) => FixtureParameter::Python(value.clone_ref(py)),
        })
    }

    pub(super) fn outcome_ready(&self, py: Python<'_>) -> PyResult<bool> {
        self.object(|objects| match &objects.outcome {
            FixtureOutcome::Empty => false,
            FixtureOutcome::Native(_) => true,
            FixtureOutcome::Python(value) => !value.is_none(py),
        })
    }

    pub(super) fn cached(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        loop {
            let snapshot = self.object(|objects| match &objects.outcome {
                FixtureOutcome::Empty => None,
                FixtureOutcome::Python(value) => Some(Ok(value.clone_ref(py))),
                FixtureOutcome::Native(outcome) => {
                    Some(Err((objects.outcome_generation, outcome.clone_ref(py))))
                }
            })?;
            let Some(snapshot) = snapshot else {
                return Ok(py.None());
            };
            let (generation, outcome) = match snapshot {
                Ok(value) => return Ok(value),
                Err(snapshot) => snapshot,
            };
            // Tuple allocation can run GC and reenter the binding, so neither
            // the mutex nor a mutable reference crosses this boundary.
            let candidate = outcome.tuple(py)?;
            let previous = {
                let mut guard = self.objects.lock().unwrap_or_else(PoisonError::into_inner);
                let Some(objects) = guard.as_mut() else {
                    drop(guard);
                    return Err(PyAssertionError::new_err(
                        "fixture binding has been cleared",
                    ));
                };
                if objects.outcome_generation == generation {
                    objects.outcome_generation = objects.outcome_generation.wrapping_add(1);
                    Some(std::mem::replace(
                        &mut objects.outcome,
                        FixtureOutcome::Python(candidate.clone_ref(py)),
                    ))
                } else {
                    None
                }
            };
            if let Some(previous) = previous {
                drop(previous);
                return Ok(candidate);
            }
            drop(candidate);
        }
    }

    pub(super) fn replace_outcome(&self, outcome: FixtureOutcome) -> PyResult<()> {
        let previous = {
            let mut guard = self.objects.lock().unwrap_or_else(PoisonError::into_inner);
            let Some(objects) = guard.as_mut() else {
                drop(guard);
                return Err(PyAssertionError::new_err(
                    "fixture binding has been cleared",
                ));
            };
            objects.outcome_generation = objects.outcome_generation.wrapping_add(1);
            std::mem::replace(&mut objects.outcome, outcome)
        };
        drop(previous);
        Ok(())
    }

    pub(super) fn replace_cached(&self, cached_result: Py<PyAny>) -> PyResult<()> {
        self.replace_outcome(FixtureOutcome::Python(cached_result))
    }

    pub(super) fn identity(&self) -> FixtureId {
        self.id
    }
}

#[pymethods]
impl NativeFixtureBinding {
    #[getter]
    fn cached_result(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.cached(py)
    }

    #[setter(cached_result)]
    fn set_cached_result(&self, cached_result: Py<PyAny>) -> PyResult<()> {
        self.replace_cached(cached_result)
    }

    #[getter]
    fn _finalizers(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.finalizers(py)
    }

    fn addfinalizer(&self, py: Python<'_>, finalizer: &Bound<'_, PyAny>) -> PyResult<()> {
        self.finalizers(py)?.bind(py).append(finalizer)
    }

    fn cache_key(_slf: &Bound<'_, Self>, request: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        match request.getattr("param") {
            Ok(value) => Ok(value.unbind()),
            Err(error)
                if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(request.py()) =>
            {
                Ok(request.py().None())
            }
            Err(error) => Err(error),
        }
    }

    #[getter]
    fn argname(&self) -> &str {
        &self.argname
    }

    #[getter]
    fn scope<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        super::scope_text(py, self.declared_scope)
    }

    #[getter]
    fn func(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.callable(py)
    }

    #[setter(func)]
    fn set_func(&self, callable: Py<PyAny>) -> PyResult<()> {
        let previous = {
            let mut guard = self.objects.lock().unwrap_or_else(PoisonError::into_inner);
            let Some(objects) = guard.as_mut() else {
                drop(guard);
                return Err(PyAssertionError::new_err(
                    "fixture binding has been cleared",
                ));
            };
            std::mem::replace(&mut objects.callable, callable)
        };
        drop(previous);
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_lock()
            && let Some(objects) = objects.as_ref()
        {
            visit.call(&objects.callable)?;
            visit.call(&objects.finalizers)?;
            match &objects.outcome {
                FixtureOutcome::Empty => (),
                FixtureOutcome::Python(value) => visit.call(value)?,
                FixtureOutcome::Native(outcome) => {
                    visit.call(&outcome.value)?;
                    visit.call(&outcome.parameter)?;
                    if let Some((error, traceback)) = &outcome.failure {
                        visit.call(error)?;
                        visit.call(traceback)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let objects = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(objects);
    }
}
