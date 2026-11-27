//! Public callspec containers are the sole source of execution parameters.

use super::parameters::ParameterRow;
use super::{CollectedSuite, FixtureBinding};
use crate::module_cache::Module;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use vfy_core::FixtureScope;
use vfy_core::test_plan::FixturePlan;

pub(super) fn from_row(
    py: Python<'_>,
    row: &ParameterRow,
    fixtures: &[FixturePlan],
) -> PyResult<Option<Py<PyAny>>> {
    if row.parameter_order.is_empty() {
        return Ok(None);
    }
    let metadata = Module::Parameters.get(py)?;
    let scope_type = metadata.getattr("Scope")?;
    let params = PyDict::new(py);
    let indices = PyDict::new(py);
    let scopes = PyDict::new(py);
    for name in &row.parameter_order {
        let key = PyString::new(py, name);
        if let Some(value) = row.parameters.get(name) {
            params.set_item(&key, value)?;
            indices.set_item(&key, row.indices[name])?;
        } else {
            let parameter = row
                .fixture_parameters
                .iter()
                .find_map(|(id, parameter)| (fixtures[*id].name == *name).then_some(parameter))
                .ok_or_else(|| {
                    PyValueError::new_err(format!("parameter {name:?} has no planned value"))
                })?;
            params.set_item(&key, &parameter.value)?;
            indices.set_item(&key, parameter.index)?;
        }
        let scope_name = match row.scopes[name] {
            FixtureScope::Function => pyo3::intern!(py, "Function"),
            FixtureScope::Class => pyo3::intern!(py, "Class"),
            FixtureScope::Module => pyo3::intern!(py, "Module"),
            FixtureScope::Package => pyo3::intern!(py, "Package"),
            FixtureScope::Session => pyo3::intern!(py, "Session"),
        };
        scopes.set_item(&key, scope_type.getattr(scope_name)?)?;
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("params", params)?;
    kwargs.set_item("indices", indices)?;
    kwargs.set_item("_arg2scope", scopes)?;
    kwargs.set_item("_idlist", PyList::new(py, &row.idlist)?)?;
    kwargs.set_item("marks", PyList::new(py, &row.marks)?)?;
    Ok(Some(
        metadata
            .getattr("CallSpec2")?
            .call((), Some(&kwargs))?
            .unbind(),
    ))
}

pub(super) struct Parameter {
    pub(super) value: Py<PyAny>,
    pub(super) index: Py<PyAny>,
    pub(super) scope: FixtureScope,
}

/// Return the value captured by a direct-parameter fixture request.
#[pyfunction]
pub(crate) fn direct_parameter(request: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(request.getattr("param")?.unbind())
}

pub(super) fn scope(value: &Bound<'_, PyAny>) -> PyResult<FixtureScope> {
    let text = value.getattr("value")?.extract::<String>()?;
    match text.as_str() {
        "function" => Ok(FixtureScope::Function),
        "class" => Ok(FixtureScope::Class),
        "module" => Ok(FixtureScope::Module),
        "package" => Ok(FixtureScope::Package),
        "session" => Ok(FixtureScope::Session),
        _ => Err(PyValueError::new_err(format!(
            "unknown fixture scope {text:?}"
        ))),
    }
}

pub(super) fn parameter(
    py: Python<'_>,
    suite: &CollectedSuite,
    index: usize,
    name: &str,
) -> PyResult<Option<Parameter>> {
    let Some(item) = suite.context.items.get(index) else {
        return Ok(None);
    };
    let Some(callspec) = item.bind(py).getattr_opt("callspec")? else {
        return Ok(None);
    };
    if callspec.is_none() {
        return Ok(None);
    }
    let params = callspec.getattr("params")?;
    if !params.contains(name)? {
        return Ok(None);
    }
    // Keep the original CPython objects; equality and request capture happen later.
    let value = params.get_item(name)?.unbind();
    let index = callspec.getattr("indices")?.get_item(name)?.unbind();
    let scope = scope(&callspec.getattr("_arg2scope")?.get_item(name)?)?;
    Ok(Some(Parameter {
        value,
        index,
        scope,
    }))
}

pub(super) fn direct_binding(
    py: Python<'_>,
    fixture: &vfy_core::test_plan::FixturePlan,
) -> PyResult<FixtureBinding> {
    let callable = py
        .import("vfy._native._pytest")?
        .getattr("direct_parameter")?
        .unbind();
    let origin = super::plugin_fixtures::FixtureOrigin::DirectParameter;
    // Like pytest's DirectParamFixtureDef, the definition declares no parameters of its own.
    let attributes = super::fixture_binding::FixtureDefinitionAttributes {
        argnames: PyTuple::new(py, &fixture.dependencies)?.unbind(),
        baseid: origin.public_baseid(&fixture.package_baseid).to_owned(),
        params: py.None(),
        ids: py.None(),
    };
    Ok(FixtureBinding {
        definition: callable.clone_ref(py),
        runtime: super::fixture_binding::NativeFixtureBinding::new(
            py,
            fixture.id,
            fixture.name.clone(),
            fixture.declared_scope,
            callable,
            attributes,
        )?,
        autouse: false,
        origin,
        parameters: None,
    })
}

#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "the native operation consumes the public CallSpec2.setmulti protocol in one call"
)]
pub(crate) fn independent_callspec_setmulti(
    py: Python<'_>,
    callspec: &Bound<'_, PyAny>,
    argnames: &Bound<'_, PyAny>,
    valset: &Bound<'_, PyAny>,
    identifier: &str,
    marks: &Bound<'_, PyAny>,
    scope: &Bound<'_, PyAny>,
    param_index: &Bound<'_, PyAny>,
    nodeid: &str,
) -> PyResult<Py<PyAny>> {
    let params = callspec.getattr("params")?.call_method0("copy")?;
    let indices = callspec.getattr("indices")?.call_method0("copy")?;
    let scopes = Module::Builtins
        .get(py)?
        .getattr("dict")?
        .call1((callspec.getattr("_arg2scope")?,))?;
    let names = argnames.try_iter()?;
    let mut values = valset.try_iter()?;
    for name in names {
        let name = name?;
        let Some(value) = values.next() else {
            return Err(PyValueError::new_err(
                "zip() argument 2 is shorter than argument 1",
            ));
        };
        let value = value?;
        if params.contains(&name)? {
            let message = format!("{nodeid}: duplicate parametrization of {}", name.repr()?);
            return Err(PyErr::from_value(
                Module::Parameters
                    .get(py)?
                    .getattr("CollectError")?
                    .call1((message,))?,
            ));
        }
        params.set_item(&name, value)?;
        indices.set_item(&name, param_index)?;
        scopes.set_item(&name, scope)?;
    }
    if values.next().transpose()?.is_some() {
        return Err(PyValueError::new_err(
            "zip() argument 2 is longer than argument 1",
        ));
    }
    let ids = PyList::new(
        py,
        callspec
            .getattr("_idlist")?
            .try_iter()?
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    ids.append(identifier)?;
    let combined_marks = PyList::new(
        py,
        callspec
            .getattr("marks")?
            .try_iter()?
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    let mark_type = Module::Facade.get(py)?.getattr("MarkDecorator")?;
    for mark in marks.try_iter()? {
        let mark = mark?;
        let mark = mark.getattr_opt("mark")?.unwrap_or(mark);
        if !mark.is_instance(&mark_type)? {
            return Err(PyTypeError::new_err(format!(
                "got {} instead of Mark",
                mark.repr()?
            )));
        }
        combined_marks.append(mark)?;
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("params", params)?;
    kwargs.set_item("indices", indices)?;
    kwargs.set_item("_arg2scope", scopes)?;
    kwargs.set_item("_idlist", ids)?;
    kwargs.set_item("marks", combined_marks)?;
    Ok(Module::Parameters
        .get(py)?
        .getattr("CallSpec2")?
        .call((), Some(&kwargs))?
        .unbind())
}
