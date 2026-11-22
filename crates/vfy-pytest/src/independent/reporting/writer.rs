//! `TerminalWriter` markup, stream and width operations.

use crate::module_cache::Module;
use pyo3::IntoPyObjectExt;
use pyo3::exceptions::{PyAssertionError, PyImportError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};

mod width;
pub(crate) use width::measure as measure_width;

const MARKUP: [(&str, u8); 20] = [
    ("black", 30),
    ("red", 31),
    ("green", 32),
    ("yellow", 33),
    ("blue", 34),
    ("purple", 35),
    ("cyan", 36),
    ("white", 37),
    ("Black", 40),
    ("Red", 41),
    ("Green", 42),
    ("Yellow", 43),
    ("Blue", 44),
    ("Purple", 45),
    ("Cyan", 46),
    ("White", 47),
    ("bold", 1),
    ("light", 2),
    ("blink", 5),
    ("invert", 7),
];

fn environment_value<'py>(py: Python<'py>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    Module::Os
        .get(py)?
        .getattr("environ")?
        .call_method1("get", (key,))
}

#[pyfunction]
fn terminal_writer_markup(file: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = file.py();
    let forced = environment_value(py, "PY_COLORS")?;
    if forced.eq("1")? {
        return true.into_py_any(py);
    }
    if forced.eq("0")? || environment_value(py, "NO_COLOR")?.is_truthy()? {
        return false.into_py_any(py);
    }
    if environment_value(py, "FORCE_COLOR")?.is_truthy()? {
        return true.into_py_any(py);
    }
    if !file.hasattr("isatty")? {
        return false.into_py_any(py);
    }
    let tty = file.call_method0("isatty")?;
    if !tty.is_truthy()? {
        return Ok(tty.unbind());
    }
    Ok(environment_value(py, "TERM")?
        .rich_compare("dumb", pyo3::basic::CompareOp::Ne)?
        .unbind())
}

#[pyfunction]
#[pyo3(signature = (file=None))]
fn terminal_writer_stream(py: Python<'_>, file: Option<Bound<'_, PyAny>>) -> PyResult<Py<PyAny>> {
    let sys = Module::Sys.get(py)?;
    let mut file = file.map_or_else(|| sys.getattr("stdout"), Ok)?;
    if file.hasattr("isatty")?
        && file.call_method0("isatty")?.is_truthy()?
        && sys.getattr("platform")?.eq("win32")?
    {
        match py.import("colorama") {
            Ok(colorama) => {
                file = colorama
                    .getattr("AnsiToWin32")?
                    .call1((&file,))?
                    .getattr("stream")?;
                if file.is_none() {
                    return Err(PyAssertionError::new_err(""));
                }
            }
            Err(error) if error.is_instance_of::<PyImportError>(py) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(file.unbind())
}

#[pyfunction]
fn terminal_writer_width(width: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = width.py();
    if width.lt(40)? {
        return Ok(80_i64.into_pyobject(py)?.into_any().unbind());
    }
    Ok(width.clone().unbind())
}

pub(in crate::independent) fn markup_code(name: &str) -> Option<u8> {
    MARKUP
        .iter()
        .find_map(|(found, code)| (*found == name).then_some(*code))
}

#[pyfunction]
fn terminal_markup_table(py: Python<'_>) -> PyResult<Py<PyDict>> {
    let table = PyDict::new(py);
    for (name, code) in MARKUP {
        table.set_item(name, code)?;
    }
    Ok(table.unbind())
}

#[pyfunction]
fn terminal_markup_codes(
    writer: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyDict>,
) -> PyResult<Option<Py<PyList>>> {
    let py = writer.py();
    for name in markup.as_any().try_iter()? {
        let name = name?;
        if !writer.getattr("_esctable")?.contains(&name)? {
            return Err(PyValueError::new_err(format!(
                "unknown markup: {}",
                name.repr()?
            )));
        }
    }
    if writer.getattr("hasmarkup")?.is_truthy()? {
        let escapes = PyList::empty(py);
        for entry in markup.call_method0("items")?.try_iter()? {
            let (name, enabled) = entry?.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
            if enabled.is_truthy()? {
                escapes.append(writer.getattr("_esctable")?.get_item(name)?)?;
            }
        }
        return Ok(Some(escapes.unbind()));
    }
    Ok(None)
}

#[pyfunction]
#[pyo3(signature = (text, escapes))]
fn terminal_markup_render(
    text: &Bound<'_, PyAny>,
    escapes: Option<&Bound<'_, PyList>>,
) -> PyResult<Py<PyAny>> {
    if let Some(escapes) = escapes
        && !escapes.is_empty()
    {
        let py = text.py();
        let pieces = PyList::empty(py);
        for code in escapes.as_any().try_iter()? {
            pieces.append(PyString::new(py, "\u{1b}[{0}m").call_method1("format", (code?,))?)?;
        }
        return Ok(PyString::new(py, "")
            .call_method1("join", (pieces,))?
            .add(text)?
            .add("\u{1b}[0m")?
            .unbind());
    }
    Ok(text.clone().unbind())
}

#[pyfunction]
fn terminal_prepare_write(
    writer: &Bound<'_, PyAny>,
    message: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyDict>,
) -> PyResult<Option<(Py<PyAny>, Py<PyAny>)>> {
    let py = writer.py();
    if !message.is_truthy()? {
        return Ok(None);
    }
    let current_line = message.call_method1("rsplit", ("\n", 1))?.get_item(-1)?;
    if message.contains("\n")? {
        writer.setattr("_current_line", &current_line)?;
    } else {
        let updated = Module::Operator
            .get(py)?
            .call_method1("iadd", (writer.getattr("_current_line")?, &current_line))?;
        writer.setattr("_current_line", updated)?;
    }
    let callback = writer.getattr("markup")?;
    let message = callback.call((message,), Some(markup))?;
    Ok(Some((current_line.unbind(), message.unbind())))
}

/// `wcswidth(text)`, the display width pytest measures terminal text with.
#[pyfunction]
fn terminal_text_width(text: &Bound<'_, PyAny>) -> PyResult<i64> {
    width::measure(text)
}

/// The width of the writer's current line, as pytest's `TerminalWriter` measures it.
#[pyfunction]
fn terminal_line_width(writer: &Bound<'_, PyAny>) -> PyResult<i64> {
    width::current(writer)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(terminal_writer_stream, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_writer_markup, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_writer_width, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_line_width, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_text_width, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_markup_table, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_markup_codes, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_markup_render, module)?)?;
    module.add_function(wrap_pyfunction!(terminal_prepare_write, module)?)?;
    Ok(())
}
