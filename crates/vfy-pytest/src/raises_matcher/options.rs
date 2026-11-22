//! Validate matcher inputs before publishing a matching handle.

use super::Options;
use pyo3::exceptions::{PyBaseException, PyException, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple, PyType};

fn expected_type(py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let base = py.get_type::<PyBaseException>();
    if let Ok(category) = value.cast::<PyType>() {
        if category.is_subclass(&base)? {
            return Ok(value.clone().unbind());
        }
        return Err(PyValueError::new_err(format!(
            "Expected a BaseException type, but got {}",
            value.getattr("__name__")?.repr()?.to_str()?
        )));
    }
    let typing = py.import("typing")?;
    let origin = typing.call_method1("get_origin", (value,))?;
    let group = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    if let Ok(category) = origin.cast::<PyType>()
        && category.is_subclass(&group)?
    {
        let arguments = typing.call_method1("get_args", (value,))?;
        let argument = arguments.get_item(0)?;
        let exception_group = py.import("builtins")?.getattr("ExceptionGroup")?;
        if (category.is_subclass(&exception_group)? && argument.is(py.get_type::<PyException>()))
            || argument.is(&base)
            || argument.is(typing.getattr("Any")?)
        {
            return Ok(origin.unbind());
        }
        return Err(PyValueError::new_err(format!(
            "Only `ExceptionGroup[Exception]` or `BaseExceptionGroup[BaseException]` \
             are accepted as generic types but got `{value}`. \
             As `raises` will catch all instances of the specified group regardless of the \
             generic argument specific nested exceptions has to be checked \
             with `RaisesGroup`."
        )));
    }
    let description = if value.is_instance(&base)? {
        format!(
            "an exception instance: {}",
            value.get_type().getattr("__name__")?.extract::<String>()?
        )
    } else {
        value
            .get_type()
            .getattr("__name__")?
            .repr()?
            .to_str()?
            .to_owned()
    };
    Err(PyTypeError::new_err(format!(
        "Expected a BaseException type, but got {description}"
    )))
}

fn compile_pattern(
    py: Python<'_>,
    pattern: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<Py<PyAny>>> {
    let Some(pattern) = pattern else {
        return Ok(None);
    };
    if !pattern.is_instance_of::<PyString>() {
        return Ok(Some(pattern.clone().unbind()));
    }
    let compiled = match py.import("re")?.call_method1("compile", (pattern,)) {
        Ok(compiled) => compiled,
        Err(error) => {
            if !error
                .value(py)
                .is_instance(&py.import("re")?.getattr("error")?)?
            {
                return Err(error);
            }
            let failure = py
                .import("vfy.independent.facade")?
                .getattr("Failed")?
                .call1((format!(
                    "Invalid regex pattern provided to 'match': {}",
                    error.value(py).str()?.to_str()?
                ),))?;
            return Err(PyErr::from_value(failure));
        }
    };
    if pattern.extract::<String>()?.is_empty() {
        let warning = py
            .import("vfy.independent.facade")?
            .getattr("PytestWarning")?
            .call1((
                "matching against an empty string will *always* pass. If you want \
                 to check for an empty message you need to pass '^$'. If you don't \
                 want to match you should pass `None` or leave out the parameter.",
            ))?;
        py.import("warnings")?
            .call_method1("warn", (warning, py.None(), 2))?;
    }
    Ok(Some(compiled.unbind()))
}

pub(super) fn build(
    py: Python<'_>,
    expected: Option<&Bound<'_, PyAny>>,
    pattern: Option<&Bound<'_, PyAny>>,
    check: Option<&Bound<'_, PyAny>>,
) -> PyResult<Options> {
    let compiled = compile_pattern(py, pattern)?;
    let raw_pattern = compiled
        .as_ref()
        .map(|pattern| super::format::raw_pattern(py, pattern.bind(py)))
        .transpose()?
        .flatten();
    let expected = match expected {
        Some(expected) => match expected.cast::<PyTuple>() {
            Ok(expected) => expected.clone(),
            Err(_) => PyTuple::new(py, [expected])?,
        },
        None => PyTuple::empty(py),
    };
    if expected.is_empty() && pattern.is_none() && check.is_none() {
        return Err(PyValueError::new_err(
            "You must specify at least one parameter to match on.",
        ));
    }
    let expected = expected
        .iter()
        .map(|value| expected_type(py, &value))
        .collect::<PyResult<Vec<_>>>()?;
    let exception = py.get_type::<PyException>();
    let is_baseexception = expected.iter().try_fold(false, |found, value| {
        Ok::<_, PyErr>(found || !value.bind(py).cast::<PyType>()?.is_subclass(&exception)?)
    })?;
    Ok(Options {
        expected: PyTuple::new(py, expected)?.unbind(),
        pattern: compiled,
        raw_pattern,
        check: check.map(|check| check.clone().unbind()),
        is_baseexception,
    })
}
