//! Long and short graph nodes call the live terminal protocol.

use super::super::unpack;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyList, PySlice, PyString};

pub(in crate::independent::reporting) fn entry(
    repr: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if repr.getattr("style")?.eq("short")? {
        if repr.getattr("reprfileloc")?.is_truthy()? {
            repr.getattr("reprfileloc")?
                .call_method1("toterminal", (writer,))?;
        }
        repr.call_method1("_write_entry_lines", (writer,))?;
        if repr.getattr("reprlocals")?.is_truthy()? {
            let kwargs = PyDict::new(repr.py());
            kwargs.set_item("indent", "        ")?;
            repr.getattr("reprlocals")?
                .call_method("toterminal", (writer,), Some(&kwargs))?;
        }
        return Ok(());
    }
    if repr.getattr("reprfuncargs")?.is_truthy()? {
        repr.getattr("reprfuncargs")?
            .call_method1("toterminal", (writer,))?;
    }
    repr.call_method1("_write_entry_lines", (writer,))?;
    if repr.getattr("reprlocals")?.is_truthy()? {
        writer.call_method1("line", ("",))?;
        repr.getattr("reprlocals")?
            .call_method1("toterminal", (writer,))?;
    }
    if repr.getattr("reprfileloc")?.is_truthy()? {
        if repr.getattr("lines")?.is_truthy()? {
            writer.call_method1("line", ("",))?;
        }
        repr.getattr("reprfileloc")?
            .call_method1("toterminal", (writer,))?;
    }
    Ok(())
}

#[pyfunction]
pub(in crate::independent::reporting) fn independent_repr_entry_lines(
    py: Python<'_>,
    repr: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if !repr.getattr("lines")?.is_truthy()? {
        return Ok(());
    }
    if repr.getattr("style")?.eq("value")? {
        for line in repr.getattr("lines")?.try_iter()? {
            writer.call_method1("write", (line?,))?;
            writer.call_method1("write", ("\n",))?;
        }
        return Ok(());
    }
    let indents = PyList::empty(py);
    let source = PyList::empty(py);
    let failures = PyList::empty(py);
    for (index, line) in repr.getattr("lines")?.try_iter()?.enumerate() {
        let line = line?;
        if line.call_method1("startswith", ("E   ",))?.is_truthy()? {
            failures.call_method1(
                "extend",
                (repr
                    .getattr("lines")?
                    .get_item(
                        py.get_type::<PySlice>()
                            .call1((index, py.None(), py.None()))?,
                    )?,),
            )?;
            break;
        }
        indents.append(line.get_item(py.get_type::<PySlice>().call1((
            py.None(),
            4,
            py.None(),
        ))?)?)?;
        source.append(line.get_item(py.get_type::<PySlice>().call1((
            4,
            py.None(),
            py.None(),
        ))?)?)?;
    }
    writer.call_method1("_write_source", (source, indents))?;
    for line in failures.try_iter()? {
        let markup = PyDict::new(py);
        markup.set_item("bold", true)?;
        markup.set_item("red", true)?;
        writer.call_method("line", (line?,), Some(&markup))?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn arguments(
    repr: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = repr.py();
    if !repr.getattr("args")?.is_truthy()? {
        return Ok(());
    }
    let mut current = PyString::new(py, "").into_any();
    for argument in repr.getattr("args")?.try_iter()? {
        let [name, value] = unpack::<2>(&argument?)?;
        let text = PyString::new(py, "{} = {}").call_method1("format", (name, value))?;
        if PyInt::new(py, text.len()? + current.len()? + 2).gt(writer.getattr("fullwidth")?)? {
            if current.is_truthy()? {
                writer.call_method1("line", (&current,))?;
            }
            current = text;
        } else if current.is_truthy()? {
            current = current.add(PyString::new(py, ", ").add(text)?)?;
        } else {
            current = text;
        }
    }
    if current.is_truthy()? {
        writer.call_method1("line", (current,))?;
    }
    writer.call_method1("line", ("",))?;
    Ok(())
}

#[pyfunction]
pub(in crate::independent::reporting) fn independent_repr_locals_terminal(
    repr: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
    indent: &Bound<'_, PyAny>,
) -> PyResult<()> {
    for line in repr.getattr("lines")?.try_iter()? {
        let line = line?;
        let write = writer.getattr("line")?;
        write.call1((indent.add(line)?,))?;
    }
    Ok(())
}

#[pyfunction]
pub(in crate::independent::reporting) fn independent_terminal_write_source(
    py: Python<'_>,
    writer: &Bound<'_, PyAny>,
    lines: &Bound<'_, PyAny>,
    indents: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if indents.is_truthy()? && indents.len()? != lines.len()? {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "indents size ({}) should have same size as lines ({})",
            indents.len()?,
            lines.len()?
        )));
    }
    let empty = if indents.is_truthy()? {
        None
    } else {
        Some(PyList::new(py, std::iter::repeat_n("", lines.len()?))?)
    };
    let indents = empty
        .as_ref()
        .map_or_else(|| indents.clone(), |value| value.clone().into_any());
    let source = PyString::new(py, "\n").call_method1("join", (lines,))?;
    let highlighted = writer.call_method1("_highlight", (source,))?;
    let new_lines = highlighted.call_method0("splitlines")?;
    let options = PyDict::new(py);
    options.set_item("strict", false)?;
    let pairs = py
        .import("builtins")?
        .getattr("zip")?
        .call((indents, new_lines), Some(&options))?;
    for pair in pairs.try_iter()? {
        let [indent, line] = unpack::<2>(&pair?)?;
        let write = writer.getattr("line")?;
        write.call1((indent.add(line)?,))?;
    }
    Ok(())
}
