//! Compact `CallSpec` ownership and eager construction of ordinary pytest functions.
use crate::dense_callspec::DenseCallSpecFamily;
use pyo3::exceptions::{PyAttributeError, PyKeyError, PyValueError};
use pyo3::prelude::*;
use pyo3::pyclass::{PyTraverseError, PyVisit};
use pyo3::types::{
    PyDict, PyList, PyString, PyTuple, PyType, PyWeakrefMethods, PyWeakrefReference,
};
use std::collections::HashMap;

struct CallSpecBinding {
    identity: Py<PyWeakrefReference>,
    handle: Py<PyWeakrefReference>,
    handle_identity: usize,
}
const ANCHOR: &str = "_vfy_dense_row";
const CALLSPEC_FIELDS: [&str; 5] = ["params", "indices", "_arg2scope", "_idlist", "marks"];

#[pyclass(module = "vfy._native._pytest", dict, weakref)]
pub(crate) struct NativeCallSpecGetAttribute {
    original: Option<Py<PyAny>>,
    dictionary_getter: Option<Py<PyAny>>,
    method_type: Option<Py<PyAny>>,
    ordinary_reader_admitted: bool,
}

impl NativeCallSpecGetAttribute {
    pub(crate) fn ordinary_reader_admitted(&self, method_type: &Bound<'_, PyAny>) -> bool {
        self.ordinary_reader_admitted
            && self
                .method_type
                .as_ref()
                .is_some_and(|value| value.bind(method_type.py()).is(method_type))
    }
}

#[pymethods]
#[expect(
    clippy::needless_pass_by_value,
    reason = "callbacks and decrefs must run without a native receiver borrow"
)]
impl NativeCallSpecGetAttribute {
    #[new]
    fn new(
        py: Python<'_>,
        original: Py<PyAny>,
        dictionary_getter: Py<PyAny>,
        method_type: Py<PyAny>,
    ) -> PyResult<Self> {
        let ordinary_reader_admitted = original
            .bind(py)
            .is(&py.get_type::<PyAny>().getattr("__getattribute__")?);
        Ok(Self {
            original: Some(original),
            dictionary_getter: Some(dictionary_getter),
            method_type: Some(method_type),
            ordinary_reader_admitted,
        })
    }

    fn _ordinary_dispatch_admitted(
        &self,
        original: &Bound<'_, PyAny>,
        method_type: &Bound<'_, PyAny>,
    ) -> bool {
        self.ordinary_reader_admitted(method_type)
            && self
                .original
                .as_ref()
                .is_some_and(|value| value.bind(original.py()).is(original))
    }

    fn __get__(
        slf: Py<Self>,
        py: Python<'_>,
        instance: &Bound<'_, PyAny>,
        owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let _ = owner;
        if instance.is_none() {
            return Ok(slf.into_any());
        }
        let method_type = slf
            .borrow(py)
            .method_type
            .as_ref()
            .map(|value| value.clone_ref(py))
            .ok_or_else(|| PyAttributeError::new_err("cleared callspec getter"))?;
        Ok(method_type.bind(py).call1((slf, instance))?.unbind())
    }

    fn __call__(
        slf: Py<Self>,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        // Dispatch must not add rich equality before the ordinary lookup.
        let dictionary = name.cast::<PyString>().is_ok_and(|name| name == "__dict__");
        let direct_dictionary = dictionary && name.is_exact_instance_of::<PyString>();
        let getter = {
            let state = slf.borrow(py);
            if direct_dictionary {
                &state.dictionary_getter
            } else {
                &state.original
            }
            .as_ref()
            .map(|value| value.clone_ref(py))
        }
        .ok_or_else(|| PyAttributeError::new_err("cleared callspec getter"))?;
        if direct_dictionary {
            return Ok(getter.bind(py).call1((callspec,))?.unbind());
        }
        // CPython may compare a str subclass during descriptor lookup. Preserve
        // its callbacks and errors before exposing a materialized dictionary.
        let value = getter.bind(py).call1((callspec, name))?;
        if dictionary && value.is_instance_of::<PyDict>() {
            let dictionary_getter = slf
                .borrow(py)
                .dictionary_getter
                .as_ref()
                .map(|value| value.clone_ref(py))
                .ok_or_else(|| PyAttributeError::new_err("cleared callspec getter"))?;
            dictionary_getter.bind(py).call1((callspec,))?;
        }
        Ok(value.unbind())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.original)?;
        visit.call(&self.dictionary_getter)?;
        visit.call(&self.method_type)?;
        Ok(())
    }

    fn __clear__(mut slf: PyRefMut<'_, Self>) {
        let removed = (
            slf.original.take(),
            slf.dictionary_getter.take(),
            slf.method_type.take(),
        );
        drop(slf);
        drop(removed);
    }
}

#[pyclass(module = "vfy._native._pytest", dict, weakref, subclass)]
pub(crate) struct NativeCallSpecField {
    raw_descriptor: Option<Py<PyAny>>,
    dictionary_getter: Option<Py<PyAny>>,
}

impl NativeCallSpecField {
    pub(crate) fn raw_descriptor_is(&self, expected: &Bound<'_, PyAny>) -> bool {
        self.raw_descriptor
            .as_ref()
            .is_some_and(|value| value.bind(expected.py()).is(expected))
    }

    fn missing_attribute(callspec: &Bound<'_, PyAny>, name: &Bound<'_, PyAny>) -> PyResult<PyErr> {
        if !name.is_exact_instance_of::<PyString>() {
            return Ok(PyAttributeError::new_err((name.clone().unbind(),)));
        }
        let message = PyString::new(callspec.py(), "'{}' object has no attribute '{}'")
            .call_method1("format", (callspec.get_type().name()?, name))?;
        Ok(PyAttributeError::new_err(message.unbind()))
    }

    /// Whether `object.__delattr__` attaches `name` and `obj` to the error. `CPython`
    /// 3.13 does so when the instance dictionary is a subclass; 3.15 always does.
    fn delete_error_has_context(raw: &Bound<'_, PyDict>) -> bool {
        let version = raw.py().version_info();
        version >= (3, 15) || (version >= (3, 13) && !raw.is_exact_instance_of::<PyDict>())
    }

    fn raw<'py>(
        slf: &Py<Self>,
        py: Python<'py>,
        callspec: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let descriptor = {
            let state = slf.borrow(py);
            state
                .raw_descriptor
                .as_ref()
                .map(|value| value.clone_ref(py))
        }
        .ok_or_else(|| PyAttributeError::new_err("cleared callspec descriptor"))?;
        descriptor
            .bind(py)
            .call_method1("__get__", (callspec,))?
            .cast_into::<PyDict>()
            .map_err(Into::into)
    }

    fn row<'py>(raw: &Bound<'py, PyDict>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(value) = raw.get_item(ANCHOR)? else {
            return Ok(None);
        };
        if !value.is_exact_instance_of::<NativeDenseCallSpecRow>()
            || !value.call_method1("owns", (raw,))?.is_truthy()?
        {
            return Ok(None);
        }
        Ok(Some(value))
    }
}

#[pymethods]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 owns receivers and traversal visitors; callbacks must run without receiver borrows"
)]
impl NativeCallSpecField {
    #[new]
    fn new(
        name: &Bound<'_, PyAny>,
        raw_descriptor: Py<PyAny>,
        dictionary_getter: Py<PyAny>,
    ) -> Self {
        let _ = name;
        Self {
            raw_descriptor: Some(raw_descriptor),
            dictionary_getter: Some(dictionary_getter),
        }
    }

    fn __init__(
        slf: Py<Self>,
        py: Python<'_>,
        name: Py<PyAny>,
        raw_descriptor: Py<PyAny>,
        dictionary_getter: Py<PyAny>,
    ) -> PyResult<()> {
        let _ = (raw_descriptor, dictionary_getter);
        slf.bind(py).setattr("name", name)
    }

    fn _raw_dispatch_admitted(&self, expected: &Bound<'_, PyAny>) -> bool {
        self.raw_descriptor_is(expected)
    }

    fn __get__(
        slf: Py<Self>,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let _ = owner;
        if callspec.is_none() {
            return Ok(slf.into_any());
        }
        let raw = Self::raw(&slf, py, callspec)?;
        // Membership callbacks can change name before the subscript lookup.
        if raw.contains(slf.bind(py).getattr("name")?)? {
            let name = slf.bind(py).getattr("name")?;
            return raw
                .get_item(&name)?
                .map(Bound::unbind)
                .ok_or_else(|| PyKeyError::new_err((name.unbind(),)));
        }
        let Some(handle) = Self::row(&raw)? else {
            return Err(Self::missing_attribute(
                callspec,
                &slf.bind(py).getattr("name")?,
            )?);
        };
        Ok(handle
            .call_method1("field", (&raw, slf.bind(py).getattr("name")?))?
            .unbind())
    }

    fn __set__(
        slf: Py<Self>,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let raw = Self::raw(&slf, py, callspec)?;
        raw.set_item(slf.bind(py).getattr("name")?, value)?;
        // Destructors must see the replacement before native ownership ends.
        if let Some(handle) = Self::row(&raw)? {
            handle.call_method1("forget", (&raw, slf.bind(py).getattr("name")?))?;
        }
        Ok(())
    }

    fn __delete__(slf: Py<Self>, py: Python<'_>, callspec: &Bound<'_, PyAny>) -> PyResult<()> {
        let getter = {
            let state = slf.borrow(py);
            state
                .dictionary_getter
                .as_ref()
                .map(|value| value.clone_ref(py))
        }
        .ok_or_else(|| PyAttributeError::new_err("cleared callspec descriptor"))?;
        let raw = getter.bind(py).call1((callspec,))?.cast_into::<PyDict>()?;
        let name = slf.bind(py).getattr("name")?;
        match raw.del_item(&name) {
            Err(error)
                if error.is_instance_of::<PyKeyError>(py)
                    && name.is_exact_instance_of::<PyString>() =>
            {
                let error = Self::missing_attribute(callspec, &name)?;
                if Self::delete_error_has_context(&raw) {
                    let value = error.value(py);
                    value.setattr("name", &name)?;
                    value.setattr("obj", callspec)?;
                }
                Err(error)
            }
            result => result,
        }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.raw_descriptor)?;
        visit.call(&self.dictionary_getter)?;
        Ok(())
    }

    fn __clear__(mut slf: PyRefMut<'_, Self>) {
        let removed = (slf.raw_descriptor.take(), slf.dictionary_getter.take());
        drop(slf);
        drop(removed);
    }
}

#[derive(Clone, Copy)]
enum ProjectionFields {
    All,
    Scope,
}
impl ProjectionFields {
    const fn names(self) -> &'static [&'static str] {
        match self {
            Self::All => &CALLSPEC_FIELDS,
            Self::Scope => &["indices", "_arg2scope"],
        }
    }
}

pub(crate) enum ParameterMappingItem {
    Fallback,
    Missing,
    Value(Py<PyAny>),
}

#[pyclass(module = "vfy._native._pytest", weakref)]
pub(crate) struct NativeDenseCallSpecRow {
    family: Option<Py<DenseCallSpecFamily>>,
    inventory: Option<Py<NativeDeferredFunctionInventory>>,
    identity: usize,
    raw_identity: usize,
    handle_identity: usize,
    row: usize,
    opaque_params: Vec<(u32, Py<PyAny>)>,
}
impl NativeDenseCallSpecRow {
    pub(crate) fn parameter_mapping_item(
        &self,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        raw: &Bound<'_, PyDict>,
        field: &str,
        key: &str,
    ) -> PyResult<ParameterMappingItem> {
        if self.identity != callspec.as_ptr() as usize
            || self.raw_identity != raw.as_ptr() as usize
            || raw
                .iter()
                .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(ParameterMappingItem::Fallback);
        }
        if raw
            .get_item(ANCHOR)?
            .is_none_or(|value| value.as_ptr() as usize != self.handle_identity)
            || raw.contains(field)?
        {
            return Ok(ParameterMappingItem::Fallback);
        }
        let Some(family) = &self.family else {
            return Ok(ParameterMappingItem::Fallback);
        };
        let family = family.borrow(py);
        if !family.anchor_mapping_available(self.row, field)? {
            return Ok(ParameterMappingItem::Fallback);
        }
        Ok(
            match family.anchor_mapping_item(py, self.row, field, key, &self.opaque_params)? {
                Some(value) => ParameterMappingItem::Value(value),
                None => ParameterMappingItem::Missing,
            },
        )
    }

    fn release(&mut self, py: Python<'_>) {
        let opaque = std::mem::take(&mut self.opaque_params);
        let family = self.family.take();
        let inventory = self.inventory.take();
        if let Some(inventory) = &inventory {
            let old = {
                let mut state = inventory.borrow_mut(py);
                if state
                    .callspecs
                    .get(&self.identity)
                    .is_some_and(|binding| binding.handle_identity == self.handle_identity)
                {
                    state.callspecs.remove(&self.identity)
                } else {
                    None
                }
            };
            drop(old);
        }
        if let Some(family) = &family {
            let released = family.borrow_mut(py).detach_if_active(self.row);
            drop(released);
        }
        drop((family, inventory, opaque));
    }
}
impl Drop for NativeDenseCallSpecRow {
    fn drop(&mut self) {
        // During interpreter shutdown the remaining Py fields use PyO3's
        // normal deferred-decref path; attaching to a finalizing runtime fails.
        let _ = Python::try_attach(|py| self.release(py));
    }
}
#[pymethods]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 owns receivers and traversal visitors; short borrows must end before allocation or decref callbacks"
)]
impl NativeDenseCallSpecRow {
    fn field(
        slf: Py<Self>,
        py: Python<'_>,
        raw: &Bound<'_, PyDict>,
        name: &str,
    ) -> PyResult<Py<PyAny>> {
        {
            let state = slf.borrow(py);
            if state.raw_identity != raw.as_ptr() as usize {
                return Err(PyValueError::new_err(
                    "copied private row anchors do not own the target dictionary",
                ));
            }
        }
        if let Some(value) = raw.get_item(name)? {
            return Ok(value.unbind());
        }
        // A tracked params allocation can run a finalizer that replaces the
        // field. Allocate before snapshotting opaque values so the setter can
        // release its old value at the ordinary boundary.
        let mapping = if name == "params" {
            Some(PyDict::new(py))
        } else {
            None
        };
        if let Some(value) = raw.get_item(name)? {
            return Ok(value.unbind());
        }
        let (family, row, snapshot) = {
            let state = slf.borrow(py);
            let family = state
                .family
                .as_ref()
                .ok_or_else(|| PyValueError::new_err("row handle was detached"))?
                .clone_ref(py);
            let snapshot =
                family
                    .borrow(py)
                    .anchor_snapshot(py, state.row, name, &state.opaque_params)?;
            (family, state.row, snapshot)
        };
        let candidate = match mapping {
            Some(mapping) => {
                snapshot.fill_mapping(&mapping)?;
                mapping.into_any().unbind()
            }
            None => snapshot.materialize(py)?,
        };
        // Allocation can run a finalizer that reads this exact field first.
        // That reentrant result is already the authoritative header state.
        let value = if let Some(value) = raw.get_item(name)? {
            value.unbind()
        } else {
            raw.set_item(name, candidate.bind(py))?;
            candidate.clone_ref(py)
        };
        let released = family.borrow_mut(py).anchor_forget(row, name)?;
        let opaque = if name == "params" {
            std::mem::take(&mut slf.borrow_mut(py).opaque_params)
        } else {
            Vec::new()
        };
        drop((released, candidate, opaque));
        Ok(value)
    }
    fn owns(&self, raw: &Bound<'_, PyDict>) -> bool {
        self.raw_identity == raw.as_ptr() as usize && self.family.is_some()
    }
    fn forget(slf: Py<Self>, py: Python<'_>, raw: &Bound<'_, PyDict>, name: &str) -> PyResult<()> {
        let (family, row) = {
            let state = slf.borrow(py);
            if state.family.is_none() {
                return Ok(());
            }
            if state.raw_identity != raw.as_ptr() as usize {
                return Err(PyValueError::new_err(
                    "copied private row anchors do not own the target dictionary",
                ));
            }
            (
                state
                    .family
                    .as_ref()
                    .ok_or_else(|| PyValueError::new_err("row handle was detached"))?
                    .clone_ref(py),
                state.row,
            )
        };
        let released = family.borrow_mut(py).anchor_forget(row, name)?;
        let opaque = if name == "params" {
            std::mem::take(&mut slf.borrow_mut(py).opaque_params)
        } else {
            Vec::new()
        };
        drop((released, opaque));
        Ok(())
    }
    fn detach(
        mut slf: PyRefMut<'_, Self>,
        py: Python<'_>,
        raw: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        if slf.family.is_none() {
            return Ok(());
        }
        if slf.raw_identity != raw.as_ptr() as usize {
            return Err(PyValueError::new_err(
                "copied private row anchors do not own the target dictionary",
            ));
        }
        let mut removed = Self {
            family: slf.family.take(),
            inventory: slf.inventory.take(),
            identity: slf.identity,
            raw_identity: slf.raw_identity,
            handle_identity: slf.handle_identity,
            row: slf.row,
            opaque_params: std::mem::take(&mut slf.opaque_params),
        };
        drop(slf);
        removed.release(py);
        Ok(())
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.family)?;
        visit.call(&self.inventory)?;
        for (_, value) in &self.opaque_params {
            visit.call(value)?;
        }
        Ok(())
    }
    fn __clear__(mut slf: PyRefMut<'_, Self>, py: Python<'_>) {
        let mut removed = Self {
            family: slf.family.take(),
            inventory: slf.inventory.take(),
            identity: slf.identity,
            raw_identity: slf.raw_identity,
            handle_identity: slf.handle_identity,
            row: slf.row,
            opaque_params: std::mem::take(&mut slf.opaque_params),
        };
        drop(slf);
        removed.release(py);
    }
}
impl CallSpecBinding {
    fn snapshot(&self, py: Python<'_>) -> Self {
        Self {
            identity: self.identity.clone_ref(py),
            handle: self.handle.clone_ref(py),
            handle_identity: self.handle_identity,
        }
    }
    /// The captured built-in dictionary descriptor is called with no inventory
    /// or handle borrow held. A replaced physical dictionary is authoritative.
    fn projection(
        self,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        descriptor: &Py<PyAny>,
        fields: ProjectionFields,
    ) -> PyResult<Option<(Py<PyAny>, usize)>> {
        if !self
            .identity
            .bind(py)
            .upgrade()
            .is_some_and(|owner| owner.is(callspec))
        {
            return Ok(None);
        }
        let Some(handle) = self.handle.bind(py).upgrade() else {
            return Ok(None);
        };
        let raw = descriptor
            .bind(py)
            .call_method1("__get__", (callspec,))?
            .cast_into::<PyDict>()?;
        if matches!(fields, ProjectionFields::Scope)
            && raw
                .iter()
                .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
        {
            return Ok(None);
        }
        if !raw.get_item(ANCHOR)?.is_some_and(|value| value.is(&handle)) {
            return Ok(None);
        }
        let projection = {
            let handle = handle.cast::<NativeDenseCallSpecRow>()?.borrow();
            if handle.raw_identity != raw.as_ptr() as usize {
                return Ok(None);
            }
            handle
                .family
                .as_ref()
                .map(|family| (family.clone_ref(py).into_any(), handle.row))
        };
        if fields
            .names()
            .iter()
            .any(|field| raw.contains(*field).unwrap_or(true))
        {
            return Ok(None);
        }
        Ok(projection)
    }
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeDeferredFunctionInventory {
    callspecs: HashMap<usize, CallSpecBinding>,
    direct_rows: usize,
    eager_function_rows: usize,
    scopes: Vec<Py<PyAny>>,
    raw_descriptor: Py<PyAny>,
}
#[pymethods]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 owns receivers, Python arguments and traversal visitors; short borrows must end before callbacks"
)]
impl NativeDeferredFunctionInventory {
    #[new]
    fn new(scopes: &Bound<'_, PyTuple>, raw_descriptor: Py<PyAny>) -> Self {
        Self {
            callspecs: HashMap::new(),
            direct_rows: 0,
            eager_function_rows: 0,
            scopes: scopes.iter().map(Bound::unbind).collect(),
            raw_descriptor,
        }
    }
    /// Build ordinary eager functions without any per-item deferred registration.
    fn eager_function_family<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        callspecs: &Bound<'py, PyList>,
        context_factory: &Bound<'py, PyAny>,
        context_args: &Bound<'py, PyTuple>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        py.check_signals()?;
        let context = context_factory.call1(context_args)?;
        if context.is_none() {
            return Ok(None);
        }
        let context = context.cast_exact::<PyDict>()?;
        if !crate::function_batch::admitted(context)? {
            return Ok(None);
        }
        let constructor = context
            .get_item("callspec_type")?
            .ok_or_else(|| PyValueError::new_err("missing Function batch callspec constructor"))?;
        let marker_type = context
            .get_item("marker_type")?
            .ok_or_else(|| PyValueError::new_err("missing Function batch marker type"))?;
        let raw_get = context
            .get_item("callspec_dict")?
            .ok_or_else(|| {
                PyValueError::new_err("missing Function batch callspec dictionary descriptor")
            })?
            .getattr("__get__")?;
        let mut cases = Vec::with_capacity(callspecs.len());
        for callspec in callspecs {
            if !callspec.is_exact_instance(&constructor) {
                return Ok(None);
            }
            let raw = raw_get.call1((&callspec,))?.cast_into::<PyDict>()?;
            if raw
                .iter()
                .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
            {
                return Ok(None);
            }
            if raw.contains("marks")? || raw.contains("_idlist")? {
                return Ok(None);
            }
            let binding = Self::project_callspec(&slf, py, &callspec, ProjectionFields::All)?;
            let Some((family, row)) = binding else {
                return Ok(None);
            };
            let case = family
                .bind(py)
                .cast::<DenseCallSpecFamily>()?
                .borrow()
                .eager_case(py, row, callspec.unbind())?;
            let Some(case) = case else {
                return Ok(None);
            };
            for marker in &case.markers {
                if !marker.bind(py).is_exact_instance(&marker_type)
                    || !marker
                        .bind(py)
                        .getattr("name")?
                        .is_exact_instance_of::<PyString>()
                {
                    return Ok(None);
                }
            }
            cases.push(case);
        }
        let function_type = context
            .get_item("function_type")?
            .ok_or_else(|| PyValueError::new_err("missing Function batch Function type"))?;
        let set_diamond_flag = context
            .get_item("set_diamond_flag")?
            .ok_or_else(|| PyValueError::new_err("missing Function batch diamond flag"))?
            .extract::<bool>()?;
        if !cases.is_empty() && set_diamond_flag {
            // Exact Function has no Collector diamond. The stock Item helper
            // performs this class side effect on its first successful item.
            function_type.setattr("_pytest_diamond_inheritance_warning_shown", true)?;
        }
        let items = crate::function_batch::build(py, context, &cases)?;
        slf.borrow_mut(py).eager_function_rows += items.len();
        py.check_signals()?;
        Ok(Some(items))
    }

    #[getter]
    fn eager_function_rows(&self) -> usize {
        self.eager_function_rows
    }

    /// Inspect every exact header without completing its deferred fields.
    fn function_scoped_items(
        slf: Py<Self>,
        py: Python<'_>,
        items: &Bound<'_, PyAny>,
        dictionary_descriptor: &Bound<'_, PyAny>,
        function_type: &Bound<'_, PyAny>,
        callspec_type: &Bound<'_, PyAny>,
        function_scope: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        if !items.is_exact_instance_of::<PyList>() {
            return Ok(false);
        }
        // Bind the built-in getset descriptor once. Reading the public __dict__
        // would complete each deferred header before this projection begins.
        let dictionary_get = dictionary_descriptor.getattr("__get__")?;
        for item in items.cast::<PyList>()?.iter() {
            if !item.is_exact_instance(function_type) {
                return Ok(false);
            }
            let dictionary = dictionary_get.call1((&item,))?.cast_into::<PyDict>()?;
            let Some(callspec) = dictionary.get_item("callspec")? else {
                continue;
            };
            if !callspec.is_exact_instance(callspec_type) {
                return Ok(false);
            }
            let binding = Self::project_callspec(&slf, py, &callspec, ProjectionFields::Scope)?;
            let scoped = if let Some((family, row)) = binding {
                let family = family
                    .bind(py)
                    .cast::<crate::dense_callspec::DenseCallSpecFamily>()?;
                crate::dense_callspec::DenseCallSpecFamily::function_scoped(
                    family.borrow(),
                    py,
                    row,
                    Some(function_scope.clone().unbind()),
                )?
            } else {
                crate::collection_order::callspec_function_scoped(&callspec, function_scope)?
            };
            if !scoped {
                return Ok(false);
            }
        }
        Ok(true)
    }

    #[getter]
    fn direct_rows(&self) -> usize {
        self.direct_rows
    }

    /// Preserve canonical argument-major ordering and last writes for duplicates.
    fn recompute_direct_indices(
        slf: Py<Self>,
        py: Python<'_>,
        calls: &Bound<'_, PyAny>,
        directness: &Bound<'_, PyAny>,
        constructor: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let (Ok(calls), Ok(directness)) = (
            calls.cast_exact::<PyList>(),
            directness.cast_exact::<PyDict>(),
        ) else {
            return Ok(false);
        };
        let mut names = Vec::new();
        for (name, direction) in directness {
            if !name.is_exact_instance_of::<PyString>()
                || !direction.is_exact_instance_of::<PyString>()
            {
                return Ok(false);
            }
            if direction.eq("direct")? {
                names.push(name);
            }
        }
        if names.is_empty() {
            return Ok(true);
        }
        let mut bindings = Vec::with_capacity(calls.len());
        for callspec in calls {
            if !callspec.is_exact_instance(constructor) {
                return Ok(false);
            }
            let binding = Self::project_callspec(&slf, py, &callspec, ProjectionFields::All)?;
            let Some((family, row)) = binding else {
                return Ok(false);
            };
            let family = family
                .bind(py)
                .cast::<DenseCallSpecFamily>()?
                .clone()
                .unbind();
            if !family.borrow(py).direct_indices_admitted(py, row, &names)? {
                return Ok(false);
            }
            bindings.push((family, row));
        }
        // No Python callbacks occur between validation and mutation: all names
        // and replaced cell values are exact builtins, and no arena borrow spans
        // a reference drop. Promoted or custom rows take the original helper.
        for name in names {
            for (index, (family, row)) in bindings.iter().enumerate() {
                let index = index.into_pyobject(py)?.into_any();
                DenseCallSpecFamily::set_direct_index(family, py, *row, &name, &index)?;
            }
        }
        Ok(true)
    }

    fn pack_callspecs(
        slf: Py<Self>,
        py: Python<'_>,
        callspecs: &Bound<'_, PyList>,
        constructor: &Bound<'_, PyType>,
        raw_descriptor: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let unpacked = {
            let state = slf.borrow(py);
            callspecs
                .iter()
                .filter(|callspec| {
                    !state
                        .callspecs
                        .get(&(callspec.as_ptr() as usize))
                        .is_some_and(|binding| {
                            binding
                                .identity
                                .bind(py)
                                .upgrade()
                                .is_some_and(|owner| owner.is(callspec))
                        })
                })
                .collect::<Vec<_>>()
        };
        if unpacked.is_empty() {
            return Ok(());
        }
        let unpacked = PyList::new(py, unpacked)?;
        let Some(packed) =
            DenseCallSpecFamily::prepare_packed(py, &unpacked, constructor, raw_descriptor)?
        else {
            return Ok(());
        };
        let mut dense = packed.family;
        let scopes = {
            let state = slf.borrow(py);
            state
                .scopes
                .iter()
                .map(|scope| scope.bind(py).clone())
                .collect::<Vec<_>>()
        };
        if !dense.admit_anchor(py, &scopes, false) {
            return Ok(());
        }
        let family = Py::new(py, dense)?;
        if !Self::attach_callspecs(slf, py, &packed.callspecs, family.clone_ref(py).into_any())? {
            return Ok(());
        }
        DenseCallSpecFamily::strip_packed(py, &family, &packed.raw_rows)
    }
    fn attach_callspecs(
        slf: Py<Self>,
        py: Python<'_>,
        callspecs: &Bound<'_, PyList>,
        family: Py<PyAny>,
    ) -> PyResult<bool> {
        let family = family
            .cast_bound::<DenseCallSpecFamily>(py)?
            .clone()
            .unbind();
        if !family.borrow(py).anchor_ready() {
            return Ok(false);
        }
        let (descriptor, scopes) = {
            let state = slf.borrow(py);
            (
                state.raw_descriptor.clone_ref(py),
                state
                    .scopes
                    .iter()
                    .map(|scope| scope.bind(py).clone())
                    .collect::<Vec<_>>(),
            )
        };
        let (mut opaque_rows, removed) = family.borrow_mut(py).take_row_parameters(py, &scopes);
        drop(removed);
        let mut staged = Vec::with_capacity(callspecs.len());
        for (row, callspec) in callspecs.iter().enumerate() {
            let raw = descriptor
                .bind(py)
                .call_method1("__get__", (&callspec,))?
                .cast_into::<PyDict>()?;
            if raw.contains(ANCHOR)? {
                return Ok(false);
            }
            let handle = Py::new(
                py,
                NativeDenseCallSpecRow {
                    family: Some(family.clone_ref(py)),
                    inventory: Some(slf.clone_ref(py)),
                    identity: callspec.as_ptr() as usize,
                    raw_identity: raw.as_ptr() as usize,
                    handle_identity: 0,
                    row,
                    opaque_params: opaque_rows
                        .as_mut()
                        .map(|rows| std::mem::take(&mut rows[row]))
                        .unwrap_or_default(),
                },
            )?;
            handle.borrow_mut(py).handle_identity = handle.as_ptr() as usize;
            let binding = CallSpecBinding {
                handle_identity: handle.as_ptr() as usize,
                identity: PyWeakrefReference::new(&callspec)?.unbind(),
                handle: PyWeakrefReference::new(handle.bind(py))?.unbind(),
            };
            staged.push((callspec.as_ptr() as usize, binding, raw, handle));
        }
        for (_, _, raw, handle) in &staged {
            let row = handle.borrow(py).row;
            if raw.contains(ANCHOR)?
                || raw
                    .iter()
                    .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
            {
                return Ok(false);
            }
            #[expect(
                deprecated,
                reason = "Read-only CPython alias admission; PyO3's replacement requires unsafe code."
            )]
            let count = raw.get_refcnt();
            if raw.is_empty() {
                if count != 2 {
                    return Ok(false);
                }
            } else if count != 3 || !family.borrow(py).anchor_matches_raw(row, raw)? {
                return Ok(false);
            }
        }
        let mut bindings = Vec::with_capacity(staged.len());
        let mut opaque_mappings = Vec::new();
        for (key, binding, raw, handle) in staged {
            if !handle.borrow(py).opaque_params.is_empty() {
                opaque_mappings.push((raw.clone(), handle.clone_ref(py)));
            }
            raw.set_item(ANCHOR, handle)?;
            bindings.push((key, binding));
        }
        let mut old = Vec::new();
        {
            let mut state = slf.borrow_mut(py);
            for (key, value) in bindings {
                if let Some(previous) = state.callspecs.insert(key, value) {
                    old.push(previous);
                }
            }
        }
        drop(old);
        // Opaque values can read their owner from a cyclic finalizer. Creating
        // their params dict there adds a new tracked cycle during collection,
        // delaying reclamation on CPython 3.15. Establish that owner now, while
        // leaving primitive params and the other fields lazy.
        for (raw, handle) in opaque_mappings {
            NativeDenseCallSpecRow::field(handle, py, &raw, "params")?;
        }
        Ok(true)
    }
    fn callspec_binding(
        slf: Py<Self>,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
    ) -> PyResult<Option<Py<PyAny>>> {
        let projection = Self::project_callspec(&slf, py, callspec, ProjectionFields::All)?;
        projection
            .map(|(family, row)| {
                PyTuple::new(
                    py,
                    [family.into_bound(py), row.into_pyobject(py)?.into_any()],
                )
                .map(|tuple| tuple.into_any().unbind())
            })
            .transpose()
    }
    fn detach_callspec(slf: Py<Self>, py: Python<'_>, callspec: &Bound<'_, PyAny>) {
        let old = slf
            .borrow_mut(py)
            .callspecs
            .remove(&(callspec.as_ptr() as usize));
        drop(old);
    }
    fn prune(slf: Py<Self>, py: Python<'_>) {
        let callspec_ids = {
            let state = slf.borrow(py);
            state
                .callspecs
                .iter()
                .filter_map(|(id, row)| {
                    (row.identity.bind(py).upgrade().is_none()
                        || row.handle.bind(py).upgrade().is_none())
                    .then_some(*id)
                })
                .collect::<Vec<_>>()
        };
        let old_callspecs = {
            let mut state = slf.borrow_mut(py);
            callspec_ids
                .into_iter()
                .filter_map(|id| state.callspecs.remove(&id))
                .collect::<Vec<_>>()
        };
        drop(old_callspecs);
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.raw_descriptor)?;
        for scope in &self.scopes {
            visit.call(scope)?;
        }
        for row in self.callspecs.values() {
            visit.call(&row.identity)?;
            visit.call(&row.handle)?;
        }
        Ok(())
    }
    fn __clear__(mut slf: PyRefMut<'_, Self>) {
        let callspecs = std::mem::take(&mut slf.callspecs);
        drop(slf);
        drop(callspecs);
    }
}
impl NativeDeferredFunctionInventory {
    fn project_callspec(
        slf: &Py<Self>,
        py: Python<'_>,
        callspec: &Bound<'_, PyAny>,
        fields: ProjectionFields,
    ) -> PyResult<Option<(Py<PyAny>, usize)>> {
        let staged = {
            let state = slf.borrow(py);
            state
                .callspecs
                .get(&(callspec.as_ptr() as usize))
                .map(|binding| (binding.snapshot(py), state.raw_descriptor.clone_ref(py)))
        };
        match staged {
            Some((binding, descriptor)) => binding.projection(py, callspec, &descriptor, fields),
            None => Ok(None),
        }
    }
    /// A callspec batch is published only after all headers have dense bindings.
    #[expect(
        clippy::needless_pass_by_value,
        reason = "an owning inventory receiver keeps it live across header allocations and reentrant callbacks"
    )]
    pub(crate) fn first_table_callspecs<'py>(
        slf: Py<Self>,
        py: Python<'py>,
        family: DenseCallSpecFamily,
        constructor: &Bound<'py, PyAny>,
        allocator: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        let count = family.row_count();
        let mut family = family;
        let scopes = {
            let state = slf.borrow(py);
            state
                .scopes
                .iter()
                .map(|scope| scope.bind(py).clone())
                .collect::<Vec<_>>()
        };
        if !family.admit_anchor(py, &scopes, true) {
            return Ok(None);
        }
        let family = Py::new(py, family)?;
        let callspecs = PyList::empty(py);
        for _ in 0..count {
            callspecs.append(allocator.call1((constructor,))?)?;
        }
        if !Self::attach_callspecs(slf.clone_ref(py), py, &callspecs, family.into_any())? {
            return Ok(None);
        }
        slf.borrow_mut(py).direct_rows += count;
        Ok(Some(callspecs))
    }
}
