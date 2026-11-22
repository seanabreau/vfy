//! Successful request resolution owns canonical definitions and cache identities together.

use super::super::fixture_binding::NativeFixtureBinding;
use crate::UnsupportedFeatureError;
use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::{Mutex, PoisonError};
use vfy_core::FixtureCacheKey;

struct ResolvedFixture {
    name: String,
    key: FixtureCacheKey,
    binding: Py<NativeFixtureBinding>,
}

/// The ordered resolved registry shared by a test's top request and subrequests.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeResolvedFixtureDefinitions {
    entries: Mutex<Vec<ResolvedFixture>>,
}

impl NativeResolvedFixtureDefinitions {
    pub(super) fn new(py: Python<'_>) -> PyResult<Py<Self>> {
        Py::new(
            py,
            Self {
                entries: Mutex::new(Vec::new()),
            },
        )
    }

    pub(super) fn key(&self, name: &str) -> Option<FixtureCacheKey> {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .find(|entry| entry.name == name)
            .map(|entry| entry.key.clone())
    }

    pub(super) fn remember(
        &self,
        name: &str,
        key: &FixtureCacheKey,
        binding: Py<NativeFixtureBinding>,
    ) {
        let replacement = ResolvedFixture {
            name: name.to_owned(),
            key: key.clone(),
            binding,
        };
        // A replaced binding can run Python destructors; the registry is readable then.
        let previous = {
            let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some(index) = entries.iter().position(|entry| entry.name == name) {
                Some(std::mem::replace(&mut entries[index], replacement))
            } else {
                entries.push(replacement);
                None
            }
        };
        drop(previous);
    }

    fn name(key: &Bound<'_, PyAny>) -> PyResult<String> {
        if !key.is_exact_instance_of::<PyString>() {
            return Err(UnsupportedFeatureError::new_err(
                "resolved fixture registry requires an exact str key",
            ));
        }
        key.extract()
    }

    fn binding(&self, py: Python<'_>, name: &str) -> Option<Py<NativeFixtureBinding>> {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .find(|entry| entry.name == name)
            .map(|entry| entry.binding.clone_ref(py))
    }
}

#[pymethods]
impl NativeResolvedFixtureDefinitions {
    fn __len__(&self) -> usize {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .len()
    }

    fn __contains__(&self, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(self.key(&Self::name(key)?).is_some())
    }

    fn __getitem__(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
    ) -> PyResult<Py<NativeFixtureBinding>> {
        self.binding(py, &Self::name(key)?)
            .ok_or_else(|| PyKeyError::new_err(key.clone().unbind()))
    }

    fn __iter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.keys(py)?.bind(py).call_method0("__iter__")?.unbind())
    }

    #[pyo3(signature = (key, default=None))]
    fn get(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        Ok(self
            .binding(py, &Self::name(key)?)
            .map_or_else(|| default.unwrap_or_else(|| py.None()), Py::into_any))
    }

    fn keys(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let names = self
            .entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|entry| entry.name.clone())
            .collect::<Vec<_>>();
        Ok(PyList::new(py, names)?.unbind())
    }

    fn values(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let bindings = self
            .entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|entry| entry.binding.clone_ref(py))
            .collect::<Vec<_>>();
        Ok(PyList::new(py, bindings)?.unbind())
    }

    fn items(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let entries = self
            .entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .map(|entry| (entry.name.clone(), entry.binding.clone_ref(py)))
            .collect::<Vec<_>>();
        Ok(PyList::new(py, entries)?.unbind())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(entries) = self.entries.try_lock() {
            for entry in entries.iter() {
                visit.call(&entry.binding)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let entries =
            std::mem::take(&mut *self.entries.lock().unwrap_or_else(PoisonError::into_inner));
        drop(entries);
    }
}
