//! The public `HookCaller` call operation and its native dispatch owner.

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::Mutex;

/// Native callable descriptor for the public `HookCaller.__call__` method.
pub(crate) struct BoundProgram {
    function: Py<PyAny>,
    method_type: Py<PyAny>,
}

impl BoundProgram {
    pub(crate) fn create(owner: &Bound<'_, PyAny>, method: &str) -> PyResult<Self> {
        let py = owner.py();
        let function = owner.getattr(method)?.unbind();
        Ok(Self {
            function,
            method_type: super::nativeop::method_type(py)?,
        })
    }

    pub(crate) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            function: self.function.clone_ref(py),
            method_type: self.method_type.clone_ref(py),
        }
    }

    pub(crate) fn function(&self, py: Python<'_>) -> Py<PyAny> {
        self.function.clone_ref(py)
    }

    pub(crate) fn bind(&self, py: Python<'_>, instance: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(self
            .method_type
            .bind(py)
            .call1((self.function.bind(py), instance))?
            .unbind())
    }

    pub(crate) fn bound_function<'py>(
        &self,
        py: Python<'py>,
        instance: &Bound<'_, PyAny>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        if !value.get_type().is(self.method_type.bind(py)) {
            return Ok(None);
        }
        if !value.getattr(pyo3::intern!(py, "__self__"))?.is(instance) {
            return Ok(None);
        }
        Ok(Some(value.getattr(pyo3::intern!(py, "__func__"))?))
    }

    pub(crate) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.function)?;
        visit.call(&self.method_type)
    }
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeHookCall {
    program: Mutex<Option<BoundProgram>>,
}

impl NativeHookCall {
    fn program(&self, py: Python<'_>) -> PyResult<BoundProgram> {
        self.program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|program| program.clone_ref(py))
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("hook call program is closed"))
    }
}

#[pymethods]
impl NativeHookCall {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Py<Self>> {
        let owner = Py::new(
            py,
            Self {
                program: Mutex::new(None),
            },
        )?;
        let program = BoundProgram::create(owner.bind(py).as_any(), "call")?;
        *owner
            .borrow(py)
            .program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(program);
        Ok(owner)
    }

    #[expect(
        clippy::unused_self,
        reason = "the descriptor is the native call owner"
    )]
    #[pyo3(signature = (caller, **kwargs))]
    fn call(
        &self,
        caller: &Bound<'_, PyAny>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        invoke(caller, kwargs)
    }

    #[pyo3(signature = (*args, **kwargs))]
    fn __call__(
        slf: &Bound<'_, Self>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let program = slf.borrow().program(slf.py())?;
        Ok(program.function.bind(slf.py()).call(args, kwargs)?.unbind())
    }

    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let program = slf.borrow().program(slf.py())?;
        Ok(slf
            .py()
            .import("inspect")?
            .call_method1("signature", (program.function.bind(slf.py()),))?
            .unbind())
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        if instance.is_none() {
            return Ok(slf.clone().into_any().unbind());
        }
        let program = slf.borrow().program(slf.py())?;
        program.bind(slf.py(), instance)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 requires its traversal visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(program) = self.program.try_lock()
            && let Some(program) = &*program
        {
            program.traverse(&visit)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let program = self
            .program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        drop(program);
    }
}

fn invoke(caller: &Bound<'_, PyAny>, kwargs: Option<&Bound<'_, PyDict>>) -> PyResult<Py<PyAny>> {
    let py = caller.py();
    let call = caller.getattr("_registry")?.getattr("call_subset")?;
    let name = caller.getattr("name")?;
    let empty = PyDict::new(py);
    let arguments = kwargs.unwrap_or(&empty);
    let removed = caller.getattr("_remove_plugins")?;
    Ok(call.call1((name, arguments, removed))?.unbind())
}

/// An already resolved call retains a foreign descriptor's single binding.
pub(crate) enum ResolvedHookCall {
    Native {
        registry: Py<super::NativeHookRegistry>,
        name: String,
        removed: Py<PyAny>,
    },
    ForeignCaller(Py<PyAny>),
    ForeignRegistry {
        call: Py<PyAny>,
        name: Py<PyAny>,
        removed: Py<PyAny>,
    },
}

/// Read a type slot without invoking user metaclass attribute hooks.
pub(crate) fn type_slot<'py>(
    object: &Bound<'py, PyAny>,
    name: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = object.py();
    let owner = object.get_type();
    let getattribute = if owner.is_exact_instance_of::<PyType>() {
        None
    } else {
        Some(
            py.get_type::<PyType>()
                .getattr(pyo3::intern!(py, "__getattribute__"))?,
        )
    };
    let read = |class: &Bound<'py, PyAny>, attribute: &Bound<'py, PyString>| match &getattribute {
        Some(getattribute) => getattribute.call1((class, attribute)),
        None => class.getattr(attribute),
    };
    let mro = read(owner.as_any(), pyo3::intern!(py, "__mro__"))?.cast_into::<PyTuple>()?;
    for class in mro.iter() {
        let namespace = read(&class, pyo3::intern!(py, "__dict__"))?;
        if namespace.contains(name)? {
            return Ok(Some(namespace.get_item(name)?));
        }
    }
    Ok(None)
}

pub(crate) fn resolve(caller: &Bound<'_, PyAny>) -> PyResult<ResolvedHookCall> {
    let py = caller.py();
    let owner = caller.get_type();
    let method = type_slot(caller, pyo3::intern!(py, "__call__"))?
        .ok_or_else(|| pyo3::exceptions::PyTypeError::new_err("hook caller is not callable"))?;
    if !method.is_exact_instance_of::<NativeHookCall>() {
        let bound = match type_slot(&method, pyo3::intern!(py, "__get__"))? {
            Some(descriptor) => descriptor.call1((&method, caller, &owner))?,
            None => method,
        };
        return Ok(ResolvedHookCall::ForeignCaller(bound.unbind()));
    }
    let registry = caller.getattr(pyo3::intern!(py, "_registry"))?;
    let call = registry.getattr(pyo3::intern!(py, "call_subset"))?;
    let name = caller.getattr(pyo3::intern!(py, "name"))?;
    let removed = caller.getattr(pyo3::intern!(py, "_remove_plugins"))?;
    if call.is_instance_of::<PyCFunction>() {
        let owner = call.getattr(pyo3::intern!(py, "__self__"))?;
        if owner.is_exact_instance_of::<super::NativeHookRegistry>()
            && owner.is(&registry)
            && call
                .getattr(pyo3::intern!(py, "__name__"))?
                .eq(pyo3::intern!(py, "call_subset"))?
        {
            return Ok(ResolvedHookCall::Native {
                registry: owner.cast_into::<super::NativeHookRegistry>()?.unbind(),
                name: name.extract()?,
                removed: removed.unbind(),
            });
        }
    }
    Ok(ResolvedHookCall::ForeignRegistry {
        call: call.unbind(),
        name: name.unbind(),
        removed: removed.unbind(),
    })
}
