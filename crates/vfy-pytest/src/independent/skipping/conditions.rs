//! Marker conditions use `CPython` evaluation at the skipping hook boundary.

use pyo3::exceptions::{PyAttributeError, PyException, PySyntaxError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

pub(super) fn failure(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    Ok(PyErr::from_value(
        py.import("vfy.independent.facade")?
            .getattr("Failed")?
            .call((message,), Some(&kwargs))?,
    ))
}

fn namespace<'py>(item: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = item.py();
    let namespace = PyDict::new(py);
    for name in ["os", "sys", "platform"] {
        namespace.set_item(name, py.import(name)?)?;
    }
    let config = item.getattr("config")?;
    namespace.set_item("config", &config)?;
    let namespaces = item
        .getattr("ihook")?
        .getattr("pytest_markeval_namespace")?
        .call(
            (),
            Some(&{
                let kwargs = PyDict::new(py);
                kwargs.set_item("config", config)?;
                kwargs
            }),
        )?
        .extract::<Vec<Py<PyAny>>>()?;
    let mapping = py.import("collections.abc")?.getattr("Mapping")?;
    for dictionary in namespaces.into_iter().rev() {
        let dictionary = dictionary.bind(py);
        if !dictionary.is_instance(&mapping)? {
            return Err(PyValueError::new_err(format!(
                "pytest_markeval_namespace() needs to return a dict, got {}",
                dictionary.repr()?
            )));
        }
        namespace.call_method1("update", (dictionary,))?;
    }
    match item.getattr("obj") {
        Ok(function) => {
            namespace.call_method1("update", (function.getattr("__globals__")?,))?;
        }
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
        Err(error) => return Err(error),
    }
    Ok(namespace)
}

fn exception_lines(py: Python<'_>, error: &PyErr) -> PyResult<String> {
    Ok(py
        .import("traceback")?
        .call_method1("format_exception_only", (error.value(py),))?
        .extract::<Vec<String>>()?
        .concat())
}

fn condition_failure(py: Python<'_>, message: &str, original: PyErr) -> PyResult<PyErr> {
    let error = failure(py, message)?;
    error.set_context(py, Some(original));
    Ok(error)
}

/// A false condition still validates its reason, as the public marker protocol does.
pub(super) fn evaluate<'py>(
    item: &Bound<'py, PyAny>,
    mark: &Bound<'py, PyAny>,
    condition: &Bound<'py, PyAny>,
) -> PyResult<(bool, Bound<'py, PyAny>)> {
    let py = item.py();
    let name = mark.getattr("name")?.extract::<String>()?;
    let string = condition.cast::<PyString>().ok();
    let result = if let Some(source) = string {
        let namespace = namespace(item)?;
        let builtins = py.import("builtins")?;
        let evaluated = (|| {
            let code = builtins
                .call_method1("compile", (source, format!("<{name} condition>"), "eval"))?;
            builtins.call_method1("eval", (code, namespace))
        })();
        match evaluated {
            Ok(value) => value,
            Err(error) if error.is_instance_of::<PySyntaxError>(py) => {
                let offset = error
                    .value(py)
                    .getattr("offset")?
                    .extract::<Option<usize>>()?
                    .unwrap_or(0);
                return Err(condition_failure(
                    py,
                    &format!(
                        "Error evaluating '{name}' condition\n    {}\n    {}^\nSyntaxError: invalid syntax",
                        source.to_str()?,
                        " ".repeat(offset)
                    ),
                    error,
                )?);
            }
            Err(error) if error.is_instance_of::<PyException>(py) => {
                return Err(condition_failure(
                    py,
                    &format!(
                        "Error evaluating '{name}' condition\n    {}\n{}",
                        source.to_str()?,
                        exception_lines(py, &error)?
                    ),
                    error,
                )?);
            }
            Err(error) => return Err(error),
        }
    } else {
        match condition.is_truthy() {
            Ok(value) => value.into_pyobject(py)?.to_owned().into_any(),
            Err(error) if error.is_instance_of::<PyException>(py) => {
                return Err(condition_failure(
                    py,
                    &format!(
                        "Error evaluating '{name}' condition as a boolean\n{}",
                        exception_lines(py, &error)?
                    ),
                    error,
                )?);
            }
            Err(error) => return Err(error),
        }
    };
    let reason = mark
        .getattr("kwargs")?
        .cast_into::<PyDict>()?
        .get_item("reason")?;
    let reason = if let Some(reason) = reason.filter(|value| !value.is_none()) {
        reason
    } else if let Some(source) = string {
        PyString::new(py, &format!("condition: {}", source.to_str()?)).into_any()
    } else {
        return Err(failure(
            py,
            &format!(
                "Error evaluating '{name}': you need to specify reason=STRING when using booleans as conditions."
            ),
        )?);
    };
    Ok((result.is_truthy()?, reason))
}
