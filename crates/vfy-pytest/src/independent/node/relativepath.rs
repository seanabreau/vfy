//! Session-owned root and default relative-path operation.

use crate::hooks::native_caller::{self, BoundProgram};
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeRelativeRoot(PathBuf);

#[pymethods]
impl NativeRelativeRoot {
    #[new]
    fn new(root: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self(PathBuf::from(root.str()?.to_str()?)))
    }
}

fn relative(session: &Bound<'_, PyAny>, path: &Bound<'_, PyAny>) -> PyResult<Py<PyString>> {
    let py = session.py();
    let owner = session.getattr("_vfy_location_root")?;
    let root = owner.cast::<NativeRelativeRoot>()?.borrow();
    let text = path.str()?;
    let result = vfy_core::glob_path(&vfy_core::relative_path(&root.0, Path::new(text.to_str()?)));
    Ok(PyString::new(py, if result.is_empty() { "." } else { &result }).unbind())
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeRelativePath {
    program: Mutex<Option<BoundProgram>>,
}

impl NativeRelativePath {
    fn program(&self, py: Python<'_>) -> PyResult<BoundProgram> {
        self.program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|program| program.clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("relative path operation is closed")
            })
    }
}

#[pymethods]
impl NativeRelativePath {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Py<Self>> {
        let owner = Py::new(
            py,
            Self {
                program: Mutex::new(None),
            },
        )?;
        let program = BoundProgram::create(owner.bind(py).as_any(), "relative")?;
        *owner
            .borrow(py)
            .program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(program);
        Ok(owner)
    }

    #[expect(
        clippy::unused_self,
        reason = "the descriptor owns the bound operation"
    )]
    fn relative(
        &self,
        session: &Bound<'_, PyAny>,
        node_path: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyString>> {
        relative(session, node_path)
    }

    #[pyo3(signature = (*args, **kwargs))]
    fn __call__(
        slf: &Bound<'_, Self>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let function = slf.borrow().program(slf.py())?.function(slf.py());
        Ok(function.bind(slf.py()).call(args, kwargs)?.unbind())
    }

    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let function = slf.borrow().program(slf.py())?.function(slf.py());
        Ok(slf
            .py()
            .import("inspect")?
            .call_method1("signature", (function,))?
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

pub(super) fn is_default(
    session: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let py = session.py();
    let Some(descriptor) =
        native_caller::type_slot(session, pyo3::intern!(py, "_node_location_to_relpath"))?
    else {
        return Ok(false);
    };
    if !descriptor.is_exact_instance_of::<NativeRelativePath>() {
        return Ok(false);
    }
    let program = descriptor
        .cast::<NativeRelativePath>()?
        .borrow()
        .program(py)?;
    let Some(function) = program.bound_function(py, session, callback)? else {
        return Ok(false);
    };
    if !function.is_instance_of::<PyCFunction>() || !function.getattr("__name__")?.eq("relative")? {
        return Ok(false);
    }
    Ok(function
        .getattr("__self__")?
        .is_exact_instance_of::<NativeRelativePath>())
}

pub(super) fn location(session: &Bound<'_, PyAny>, path: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(relative(session, path)?.into_any())
}
