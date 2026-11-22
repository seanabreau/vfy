//! Native descriptor for the default Python function report-info protocol.

use crate::independent::reporting;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyString, PyTuple};

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeFunctionReportInfo;

#[pymethods]
impl NativeFunctionReportInfo {
    #[new]
    fn new() -> Self {
        Self
    }

    #[expect(
        clippy::unused_self,
        reason = "the bound builtin identifies this report-info descriptor"
    )]
    // The descriptor receiver is separate from Function.reportinfo's public
    // `self` argument, which PyO3 cannot use as another Rust parameter name.
    #[pyo3(signature = (*args, **kwargs), text_signature = "($descriptor, self)")]
    fn reportinfo(
        &self,
        py: Python<'_>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<(Py<PyAny>, i64, String)> {
        let mut item = if args.is_empty() {
            None
        } else {
            Some(args.get_item(0)?)
        };
        if let Some(kwargs) = kwargs {
            if kwargs
                .iter()
                .any(|(name, _)| !name.is_instance_of::<PyString>())
            {
                return Err(PyTypeError::new_err("keywords must be strings"));
            }
            for (name, value) in kwargs.iter() {
                if !name.eq(pyo3::intern!(py, "self"))? {
                    let name = name.extract::<String>()?;
                    return Err(PyTypeError::new_err(format!(
                        "Function.reportinfo() got an unexpected keyword argument '{name}'"
                    )));
                }
                if item.is_some() {
                    return Err(PyTypeError::new_err(
                        "Function.reportinfo() got multiple values for argument 'self'",
                    ));
                }
                item = Some(value);
            }
        }
        if args.len() > 1 {
            return Err(PyTypeError::new_err(format!(
                "Function.reportinfo() takes 1 positional argument but {} were given",
                args.len()
            )));
        }
        let item = item.ok_or_else(|| {
            PyTypeError::new_err(
                "Function.reportinfo() missing 1 required positional argument: 'self'",
            )
        })?;
        reporting::independent_item_reportinfo(py, &item)
    }

    #[pyo3(signature = (*args, **kwargs))]
    fn __call__(
        slf: &Bound<'_, Self>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        Ok(slf.getattr("reportinfo")?.call(args, kwargs)?.unbind())
    }

    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Ok(slf
            .py()
            .import("inspect")?
            .call_method1("signature", (slf.getattr("reportinfo")?,))?
            .unbind())
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let _ = owner;
        if instance.is_none() {
            return Ok(slf.clone().into_any().unbind());
        }
        let method = slf.getattr("reportinfo")?;
        Ok(slf
            .py()
            .import("types")?
            .getattr("MethodType")?
            .call1((method, instance))?
            .unbind())
    }
}

/// Test the bound method, never a cached collection-time assumption.
pub(super) fn is_default(item: &Bound<'_, PyAny>, method: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = item.py();
    let method_type = py.import("types")?.getattr("MethodType")?;
    if !method.get_type().is(&method_type) || !method.getattr("__self__")?.is(item) {
        return Ok(false);
    }
    let function = method.getattr("__func__")?;
    if !function.is_instance_of::<PyCFunction>() {
        return Ok(false);
    }
    let owner = function.getattr("__self__")?;
    Ok(owner.is_exact_instance_of::<NativeFunctionReportInfo>()
        && function.getattr("__name__")?.eq("reportinfo")?)
}
