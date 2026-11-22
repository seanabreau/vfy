//! Node warning protocols and source locations.

mod relativepath;
mod reportinfo;

use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyAttributeError, PyValueError, PyWarning};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySlice, PyString};

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<reportinfo::NativeFunctionReportInfo>()?;
    module.add_class::<relativepath::NativeRelativePath>()?;
    module.add_class::<relativepath::NativeRelativeRoot>()?;
    Module::Os.get(module.py())?;
    Module::Pathlib.get(module.py())?;
    module.add_function(wrap_pyfunction!(independent_node_warn, module)?)?;
    module.add_function(wrap_pyfunction!(independent_item_location, module)?)?;
    Ok(())
}

fn optional_attribute<'py>(node: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    match node.getattr(name) {
        Ok(value) => Ok(value),
        Err(error) if error.is_instance_of::<PyAttributeError>(node.py()) => {
            Ok(node.py().None().into_bound(node.py()))
        }
        Err(error) => Err(error),
    }
}

fn warning_location<'py>(
    node: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = node.py();
    let location = optional_attribute(node, "location")?;
    if !location.is_none() {
        let slice = py.get_type::<PySlice>().call1((py.None(), 2, py.None()))?;
        let sliced = location.get_item(slice)?;
        // The source helper releases its location before caller-owned pair iteration.
        drop(location);
        let fields = py
            .import("vfy.independent.node")?
            .call_method1("unpack_location", (sliced,))?;
        return fields.extract();
    }
    let object = optional_attribute(node, "obj")?;
    if !object.is_none() {
        return super::fixture_lookup::source_location(&object);
    }
    let path = match node.getattr("path") {
        Ok(path) => path,
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {
            PyString::new(py, "unknown location").into_any()
        }
        Err(error) => return Err(error),
    };
    Ok((path, (-1_i32).into_pyobject(py)?.into_any()))
}

#[pyfunction]
fn independent_node_warn(node: &Bound<'_, PyAny>, warning: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = node.py();
    if !warning.is_instance(&py.get_type::<PyWarning>())? {
        let message = PyString::new(
            py,
            "warning must be an instance of Warning or subclass, got {!r}",
        )
        .call_method1("format", (warning,))?;
        return Err(PyErr::from_value(
            py.get_type::<PyValueError>().call1((message,))?,
        ));
    }
    let (path, lineno) = warning_location(node)?;
    if lineno.is_none() {
        return Err(PyAssertionError::new_err(()));
    }
    let emit = py.import("warnings")?.getattr("warn_explicit")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("category", py.None())?;
    kwargs.set_item("filename", path.str()?)?;
    kwargs.set_item("lineno", lineno.add(1)?)?;
    emit.call((warning,), Some(&kwargs))?;
    Ok(())
}

#[pyfunction]
fn independent_item_location(
    item: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    let py = item.py();
    let method = item.getattr(pyo3::intern!(py, "reportinfo"))?;
    if reportinfo::is_default(item, &method)? {
        let info = super::reporting::item_reportinfo(
            py,
            item,
            super::reporting::ReportInfoPath::Location,
        )?;
        let path = match info.normalization {
            super::reporting::LocationNormalization::Complete => info.path.into_bound(py),
            super::reporting::LocationNormalization::Required => Module::Os
                .get(py)?
                .getattr(pyo3::intern!(py, "path"))?
                .call_method1(pyo3::intern!(py, "abspath"), (info.path,))?,
        };
        let relative = location_relative(item, &path)?;
        return Ok((
            relative,
            info.line.into_pyobject(py)?.into_any().unbind(),
            info.domain.into_pyobject(py)?.into_any().unbind(),
        ));
    }
    let location = method.call0()?;
    let path = Module::Os
        .get(py)?
        .getattr(pyo3::intern!(py, "path"))?
        .call_method1(pyo3::intern!(py, "abspath"), (location.get_item(0)?,))?;
    let relative = location_relative(item, &path)?;
    if !location
        .get_item(2)?
        .get_type()
        .is(py.get_type::<PyString>())
    {
        return Err(PyAssertionError::new_err(()));
    }
    Ok((
        relative,
        location.get_item(1)?.unbind(),
        location.get_item(2)?.unbind(),
    ))
}

fn location_relative(item: &Bound<'_, PyAny>, path: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = item.py();
    let session = item.getattr(pyo3::intern!(py, "session"))?;
    let callback = session.getattr(pyo3::intern!(py, "_node_location_to_relpath"))?;
    if relativepath::is_default(&session, &callback)? {
        return relativepath::location(&session, path);
    }
    let path = Module::Pathlib
        .get(py)?
        .getattr(pyo3::intern!(py, "Path"))?
        .call1((path,))?;
    Ok(callback.call1((path,))?.unbind())
}
