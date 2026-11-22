//! Owned lookup exceptions and their live terminal representation protocol.

use crate::module_cache::Module;
use pyo3::exceptions::{PyAttributeError, PyException, PyIndexError, PyOSError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PySlice, PyString, PyType};
use std::cell::RefCell;
use std::collections::BTreeSet;

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(independent_fixture_lookup_format, module)?)?;
    module.add_function(wrap_pyfunction!(
        independent_fixture_lookup_terminal,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(independent_fixture_lookup_text, module)?)?;
    Ok(())
}

pub(super) fn error(py: Python<'_>, name: &str, request: &Bound<'_, PyAny>) -> PyResult<PyErr> {
    let value = py
        .import("vfy.independent.fixture_lookup")?
        .getattr("FixtureLookupError")?
        .call1((name, request))?;
    Ok(PyErr::from_value(value))
}

pub(super) fn raiseerror(
    py: Python<'_>,
    request: &Bound<'_, PyAny>,
    message: &Bound<'_, PyAny>,
) -> PyResult<PyErr> {
    let value = py
        .import("vfy.independent.fixture_lookup")?
        .getattr("FixtureLookupError")?
        .call1((py.None(), request, message))?;
    Ok(PyErr::from_value(value))
}

pub(super) fn real_function<'py>(function: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = function.py();
    let function = Module::Inspect
        .get(py)?
        .call_method1("unwrap", (function,))?;
    if function.is_instance(&Module::Functools.get(py)?.getattr("partial")?)? {
        function.getattr("func")
    } else {
        Ok(function)
    }
}

fn raw_code<'py>(function: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = function.py();
    match function.getattr("__code__") {
        Ok(code) => return Ok(code),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
        Err(error) => return Err(error),
    }
    let call = match function.getattr("__call__") {
        Ok(call) => Some(call),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
        Err(error) => return Err(error),
    };
    if let Some(call) = call
        && call.is_truthy()?
        && !function.is_instance(&py.get_type::<PyType>())?
    {
        match call.getattr("__code__") {
            Ok(code) => return Ok(code),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
            Err(error) => return Err(error),
        }
    }
    Err(PyTypeError::new_err(format!(
        "failed to get code object for {}",
        function.repr()?.to_str()?
    )))
}

fn absolute_path<'py>(filename: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = filename.py();
    let absolute = Module::OsPath
        .get(py)?
        .call_method1("abspath", (filename,))?;
    Module::Pathlib.get(py)?.getattr("Path")?.call1((absolute,))
}

fn code_path<'py>(code: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = code.py();
    let filename = Module::Inspect.get(py)?.call_method1("getfile", (code,))?;
    if !filename.is_truthy()? {
        return Ok(PyString::new(py, "").into_any());
    }
    let path = absolute_path(&filename).and_then(|path| {
        if path.call_method0("exists")?.is_truthy()? {
            Ok(Some(path))
        } else {
            Ok(None)
        }
    });
    match path {
        Ok(Some(path)) => Ok(path),
        Ok(None) => Ok(filename),
        Err(error) if error.is_instance_of::<PyOSError>(py) => Ok(filename),
        Err(error) => Err(error),
    }
}

fn fallback_location<'py>(
    function: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = function.py();
    let inspect = Module::Inspect.get(py)?;
    let filename = inspect
        .call_method1("getsourcefile", (function,))
        .and_then(|filename| {
            if filename.is_truthy()? {
                Ok(filename)
            } else {
                inspect.call_method1("getfile", (function,))
            }
        });
    let filename = match filename {
        Ok(filename) => filename,
        Err(error) if error.is_instance_of::<PyTypeError>(py) => {
            return Ok((
                PyString::new(py, "").into_any(),
                (-1_i32).into_pyobject(py)?.into_any(),
            ));
        }
        Err(error) => return Err(error),
    };
    let path = if filename.is_truthy()? {
        absolute_path(&filename)?
    } else {
        PyString::new(py, "").into_any()
    };
    let mut lineno = (-1_i32).into_pyobject(py)?.into_any();
    if path.is_truthy()? {
        match inspect.call_method1("findsource", (function,)) {
            Ok(source) => {
                // pytest's findsource also strips the complete source before returning.
                for line in source.get_item(0)?.try_iter()? {
                    line?.call_method0("rstrip")?;
                }
                lineno = source.get_item(1)?;
            }
            Err(error) if error.is_instance_of::<PyException>(py) => {}
            Err(error) => return Err(error),
        }
    }
    Ok((path, lineno))
}

pub(super) fn source_location<'py>(
    function: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let py = function.py();
    let mut function = real_function(function)?;
    if function.hasattr("place_as")? {
        function = function.getattr("place_as")?;
    }
    match raw_code(&function) {
        Ok(code) => {
            let path = code_path(&code)?;
            let lineno = code.getattr("co_firstlineno")?.sub(1)?;
            Ok((path, lineno))
        }
        Err(error) if error.is_instance_of::<PyTypeError>(py) => fallback_location(&function),
        Err(error) => Err(error),
    }
}

fn formatted(value: &Bound<'_, PyAny>) -> PyResult<String> {
    PyString::new(value.py(), "{}")
        .call_method1("format", (value,))?
        .extract()
}

fn append_source(
    py: Python<'_>,
    function: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
    lineno: &Bound<'_, PyAny>,
    lines: &Bound<'_, PyList>,
) -> PyResult<()> {
    // Only getsourcelines/get_real_func failures use pytest's unavailable-source text.
    let source = real_function(function).and_then(|function| {
        Module::Inspect
            .get(py)?
            .call_method1("getsourcelines", (function,))
    });
    let source = match source {
        Ok(source) => source,
        Err(error)
            if error.is_instance_of::<PyOSError>(py)
                || error.is_instance_of::<PyIndexError>(py)
                || error.is_instance_of::<PyTypeError>(py) =>
        {
            let firstlineno = lineno.add(1)?;
            lines.append(format!(
                "file {}, line {}: source code not available",
                path.str()?.to_str()?,
                firstlineno.str()?.to_str()?
            ))?;
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    lines.append(format!(
        "file {}, line {}",
        formatted(path)?,
        formatted(&lineno.add(1)?)?
    ))?;
    for line in source.get_item(0)?.try_iter()? {
        let line = line?.call_method0("rstrip")?;
        lines.append(PyString::new(py, "  ").add(&line)?)?;
        if line
            .call_method0("lstrip")?
            .call_method1("startswith", ("def",))?
            .is_truthy()?
        {
            break;
        }
    }
    Ok(())
}

#[pyfunction]
pub(crate) fn independent_fixture_lookup_format(
    py: Python<'_>,
    error: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let mut stack = error
        .getattr("request")?
        .getattr("_pyfuncitem")?
        .getattr_opt("obj")?
        .into_iter()
        .collect::<Vec<_>>();
    for binding in error.getattr("fixturestack")?.try_iter()? {
        stack.push(binding?.getattr("func")?);
    }
    let mut message = error.getattr("msg")?;
    if !message.is_none() && stack.len() > 1 {
        stack.pop();
    }
    let lines = PyList::empty(py);
    let mut location = None;
    for function in stack {
        let (path, lineno) = source_location(&function)?;
        append_source(py, &function, &path, &lineno, &lines)?;
        location = Some((path, lineno));
    }
    if message.is_none() {
        let available = error
            .getattr("request")?
            .call_method0("_vfy_available_fixture_names")?
            .try_iter()?
            .map(|name| name?.extract::<String>())
            .collect::<PyResult<BTreeSet<_>>>()?;
        let names = PySet::new(py, &available)?;
        let recursive = names.contains(error.getattr("argname")?)?;
        let argname = formatted(&error.getattr("argname")?)?;
        let headline = if recursive {
            format!(" recursive dependency involving fixture '{argname}' detected")
        } else {
            format!("fixture '{argname}' not found")
        };
        message = PyString::new(
            py,
            &format!(
                "{headline}\n available fixtures: {}\n use 'pytest --fixtures [testpath]' for help on them.",
                available.into_iter().collect::<Vec<_>>().join(", ")
            ),
        )
        .into_any();
    }
    // The test callable always contributes a source location, including unavailable source.
    let (path, lineno) =
        location.ok_or_else(|| PyTypeError::new_err("fixture lookup has no test callable"))?;
    Ok(py
        .import("vfy.independent.fixture_lookup")?
        .getattr("FixtureLookupErrorRepr")?
        .call1((path, lineno, lines, message, error.getattr("argname")?))?
        .unbind())
}

#[pyfunction]
pub(crate) fn independent_fixture_lookup_terminal(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let tblines = representation.getattr("tblines")?;
    for line in tblines.try_iter()? {
        let line = line?;
        let write_line = writer.getattr("line")?;
        write_line.call1((line.call_method0("rstrip")?,))?;
    }
    let lines = representation
        .getattr("errorstring")?
        .call_method1("split", ("\n",))?;
    if lines.is_truthy()? {
        let markup = PyDict::new(py);
        markup.set_item("red", true)?;
        let write_line = writer.getattr("line")?;
        write_line.call(
            (format!(
                "E       {}",
                formatted(&lines.get_item(0)?.call_method0("strip")?)?
            ),),
            Some(&markup),
        )?;
        let rest = lines.get_item(py.get_type::<PySlice>().call1((1, py.None(), py.None()))?)?;
        for line in rest.try_iter()? {
            let line = line?;
            let write_line = writer.getattr("line")?;
            write_line.call(
                (format!(
                    ">       {}",
                    formatted(&line.call_method0("strip")?)?
                ),),
                Some(&markup),
            )?;
        }
    }
    writer.call_method0("line")?;
    let write_line = writer.getattr("line")?;
    let filename = Module::Os
        .get(py)?
        .getattr("fspath")?
        .call1((representation.getattr("filename")?,))?;
    let filename = formatted(&filename)?;
    let lineno = representation.getattr("firstlineno")?.add(1)?;
    write_line.call1((format!("{filename}:{}", formatted(&lineno)?),))?;
    Ok(())
}

// No Python references are retained by this sink, so it adds no GC ownership edges.
#[pyclass(frozen, unsendable)]
struct LookupTextWriter {
    text: RefCell<String>,
}

#[pymethods]
impl LookupTextWriter {
    #[pyo3(signature = (s="", **_markup))]
    fn line(&self, s: &str, _markup: Option<&Bound<'_, PyDict>>) {
        let mut text = self.text.borrow_mut();
        text.push_str(s);
        text.push('\n');
    }

    #[pyo3(signature = (msg, *, flush=false, **_markup))]
    fn write(&self, msg: &str, flush: bool, _markup: Option<&Bound<'_, PyDict>>) {
        let _ = flush;
        self.text.borrow_mut().push_str(msg);
    }

    fn flush(_slf: &Bound<'_, Self>) {}
}

#[pyfunction]
pub(crate) fn independent_fixture_lookup_text(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let writer = Py::new(
        py,
        LookupTextWriter {
            text: RefCell::new(String::new()),
        },
    )?;
    representation.call_method1("toterminal", (&writer,))?;
    let text = writer.bind(py).borrow().text.borrow().clone();
    PyString::new(py, &text).call_method0("strip")?.extract()
}
