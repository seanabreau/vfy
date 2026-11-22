//! Nonpromoting parameter reads through the current callspec row anchor.

use crate::deferred_function::{
    NativeCallSpecField, NativeCallSpecGetAttribute, NativeDenseCallSpecRow, ParameterMappingItem,
};
use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

struct EntryGuard {
    namespace: Py<PyAny>,
    name: Py<PyAny>,
    expected: Option<Py<PyAny>>,
}

pub(crate) struct DenseCallSpecReader {
    constructor: Py<PyAny>,
    raw_get: Py<PyAny>,
    raw_descriptor: Py<PyAny>,
    method_type: Py<PyAny>,
    entries: Vec<EntryGuard>,
    getter: Py<PyAny>,
    getter_type: Py<PyAny>,
    getter_mro: Py<PyAny>,
    row_type: Py<PyAny>,
    row_mro: Py<PyAny>,
    descriptor_type: Py<PyAny>,
    descriptor_mro: Py<PyAny>,
    descriptors: Vec<(Py<PyAny>, Py<PyAny>)>,
    gettrace: Py<PyAny>,
    getprofile: Py<PyAny>,
}

pub(crate) struct ParameterFields {
    pub(crate) value: Option<Py<PyAny>>,
    pub(crate) index_present: bool,
}

impl DenseCallSpecReader {
    pub(crate) fn load(py: Python<'_>) -> PyResult<Option<Self>> {
        let context = py
            .import("vfy._pytest.lazy_function")?
            .call_method0("native_callspec_reader_context")?;
        if context.is_none() {
            return Ok(None);
        }
        let context = context.cast::<PyDict>()?;
        let read = |name: &str| {
            context
                .get_item(name)?
                .ok_or_else(|| PyKeyError::new_err(name.to_owned()))
        };
        let entries = read("entries")?
            .try_iter()?
            .map(|entry| {
                let entry = entry?.cast_into::<PyTuple>()?;
                let expected = entry.get_item(2)?.cast_into::<PyTuple>()?;
                Ok(EntryGuard {
                    namespace: entry.get_item(0)?.unbind(),
                    name: entry.get_item(1)?.unbind(),
                    expected: if expected.is_empty() {
                        None
                    } else {
                        Some(expected.get_item(0)?.unbind())
                    },
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(Some(Self {
            constructor: read("constructor")?.unbind(),
            raw_get: read("raw_get")?.unbind(),
            raw_descriptor: read("raw_descriptor")?.unbind(),
            method_type: read("method_type")?.unbind(),
            entries,
            getter: read("getter")?.unbind(),
            getter_type: read("getter_type")?.unbind(),
            getter_mro: read("getter_mro")?.unbind(),
            row_type: read("row_type")?.unbind(),
            row_mro: read("row_mro")?.unbind(),
            descriptor_type: read("descriptor_type")?.unbind(),
            descriptor_mro: read("descriptor_mro")?.unbind(),
            descriptors: read("descriptors")?.extract()?,
            gettrace: read("gettrace")?.unbind(),
            getprofile: read("getprofile")?.unbind(),
        }))
    }

    fn trusted(&self, py: Python<'_>) -> PyResult<bool> {
        if !self.gettrace.bind(py).call0()?.is_none()
            || !self.getprofile.bind(py).call0()?.is_none()
            || !self
                .getter
                .bind(py)
                .is_exact_instance(self.getter_type.bind(py))
            || !self
                .getter_type
                .bind(py)
                .getattr(pyo3::intern!(py, "__mro__"))?
                .is(self.getter_mro.bind(py))
            || !self
                .row_type
                .bind(py)
                .getattr(pyo3::intern!(py, "__mro__"))?
                .is(self.row_mro.bind(py))
            || !self
                .descriptor_type
                .bind(py)
                .getattr(pyo3::intern!(py, "__mro__"))?
                .is(self.descriptor_mro.bind(py))
        {
            return Ok(false);
        }
        if !self
            .getter
            .bind(py)
            .cast_exact::<NativeCallSpecGetAttribute>()?
            .borrow()
            .ordinary_reader_admitted(self.method_type.bind(py))
        {
            return Ok(false);
        }
        for guard in &self.entries {
            let namespace = guard.namespace.bind(py);
            let name = guard.name.bind(py);
            if let Ok(dictionary) = namespace.cast_exact::<PyDict>()
                && dictionary
                    .iter()
                    .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
            {
                return Ok(false);
            }
            if namespace.contains(name)? {
                let Some(expected) = &guard.expected else {
                    return Ok(false);
                };
                if !namespace.get_item(name)?.is(expected.bind(py)) {
                    return Ok(false);
                }
            } else if guard.expected.is_some() {
                return Ok(false);
            }
        }
        for (name, descriptor) in &self.descriptors {
            let descriptor = descriptor.bind(py);
            if !descriptor.is_exact_instance(self.descriptor_type.bind(py)) {
                return Ok(false);
            }
            if !descriptor
                .cast_exact::<NativeCallSpecField>()?
                .borrow()
                .raw_descriptor_is(self.raw_descriptor.bind(py))
            {
                return Ok(false);
            }
            let dictionary = descriptor.getattr(pyo3::intern!(py, "__dict__"))?;
            let Ok(dictionary) = dictionary.cast_exact::<PyDict>() else {
                return Ok(false);
            };
            if dictionary
                .iter()
                .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
                || dictionary
                    .get_item(pyo3::intern!(py, "name"))?
                    .is_none_or(|value| !value.is(name.bind(py)))
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn raw<'py>(&self, callspec: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let py = callspec.py();
        if !callspec.is_exact_instance(self.constructor.bind(py)) || !self.trusted(py)? {
            return Ok(None);
        }
        let raw = self
            .raw_get
            .bind(py)
            .call1((callspec,))?
            .cast_into::<PyDict>()?;
        // Captured C getters and exact-string namespaces keep this path callback-free.
        if raw
            .iter()
            .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(None);
        }
        Ok(Some(raw))
    }

    pub(crate) fn unused_params_admitted(&self, callspec: &Bound<'_, PyAny>) -> PyResult<bool> {
        let Some(raw) = self.raw(callspec)? else {
            return Ok(false);
        };
        let Some(anchor) = raw.get_item("_vfy_dense_row")? else {
            return Ok(false);
        };
        let Ok(anchor) = anchor.cast_exact::<NativeDenseCallSpecRow>() else {
            return Ok(false);
        };
        let result =
            anchor
                .borrow()
                .parameter_mapping_item(callspec.py(), callspec, &raw, "params", "")?;
        Ok(!matches!(result, ParameterMappingItem::Fallback))
    }

    pub(crate) fn parameter_fields(
        &self,
        callspec: &Bound<'_, PyAny>,
        name: &str,
    ) -> PyResult<Option<ParameterFields>> {
        let py = callspec.py();
        if callspec.is_exact_instance(self.constructor.bind(py)) {
            let raw = self
                .raw_get
                .bind(py)
                .call1((callspec,))?
                .cast_into::<PyDict>()?;
            if raw
                .iter()
                .all(|(key, _)| key.is_exact_instance_of::<PyString>())
                && (raw.contains("params")? || raw.contains("indices")?)
            {
                return Ok(None);
            }
        }
        let Some(raw) = self.raw(callspec)? else {
            return Ok(None);
        };
        if raw.contains("params")? || raw.contains("indices")? {
            return Ok(None);
        }
        let Some(anchor) = raw.get_item("_vfy_dense_row")? else {
            return Ok(None);
        };
        let Ok(anchor) = anchor.cast_exact::<NativeDenseCallSpecRow>() else {
            return Ok(None);
        };
        let (value, index_present) = {
            let anchor = anchor.borrow();
            let value = match anchor.parameter_mapping_item(py, callspec, &raw, "params", name)? {
                ParameterMappingItem::Fallback => return Ok(None),
                ParameterMappingItem::Missing => {
                    return Ok(Some(ParameterFields {
                        value: None,
                        index_present: false,
                    }));
                }
                ParameterMappingItem::Value(value) => value,
            };
            let index = anchor.parameter_mapping_item(py, callspec, &raw, "indices", name)?;
            if matches!(index, ParameterMappingItem::Fallback) {
                return Ok(None);
            }
            (Some(value), matches!(index, ParameterMappingItem::Value(_)))
        };
        // No Python callbacks or tracked allocations occur between the two
        // clones. Scope lookup remains on the existing Python path afterward.
        Ok(Some(ParameterFields {
            value,
            index_present,
        }))
    }
}
