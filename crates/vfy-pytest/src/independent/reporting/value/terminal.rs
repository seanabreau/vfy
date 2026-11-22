//! Live representation fields drive the caller's writer operations.

use super::super::unpack;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySlice, PyString};

fn sections(representation: &Bound<'_, PyAny>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
    for section in representation.getattr("sections")?.try_iter()? {
        let [name, content, separator] = unpack::<3>(&section?)?;
        writer.call_method1("sep", (separator, name))?;
        writer.call_method1("line", (content,))?;
    }
    Ok(())
}

fn chain(representation: &Bound<'_, PyAny>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
    for node in representation.getattr("chain")?.try_iter()? {
        let [traceback, _crash, description] = unpack::<3>(&node?)?;
        traceback.call_method1("toterminal", (writer,))?;
        if !description.is_none() {
            writer.call_method1("line", ("",))?;
            let markup = PyDict::new(representation.py());
            markup.set_item("yellow", true)?;
            writer.call_method("line", (description,), Some(&markup))?;
        }
    }
    sections(representation, writer)
}

fn traceback(representation: &Bound<'_, PyAny>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
    for (index, entry) in representation
        .getattr("reprentries")?
        .try_iter()?
        .enumerate()
    {
        let entry = entry?;
        if entry.getattr("style")?.eq("long")? {
            writer.call_method1("line", ("",))?;
        }
        entry.call_method1("toterminal", (writer,))?;
        if index
            < representation
                .getattr("reprentries")?
                .len()?
                .saturating_sub(1)
        {
            let next = representation.getattr("reprentries")?.get_item(index + 1)?;
            let long = entry.getattr("style")?.eq("long")?;
            let separator = long
                || (entry.getattr("style")?.eq("short")? && next.getattr("style")?.eq("long")?);
            if separator {
                let sep = writer.getattr("sep")?;
                sep.call1((representation.getattr("entrysep")?,))?;
            }
        }
    }
    if representation.getattr("extraline")?.is_truthy()? {
        let line = writer.getattr("line")?;
        line.call1((representation.getattr("extraline")?,))?;
    }
    Ok(())
}

fn entry(representation: &Bound<'_, PyAny>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
    super::super::graph::terminal::entry(representation, writer)
}

fn location(representation: &Bound<'_, PyAny>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = representation.py();
    let mut message = representation.getattr("message")?;
    let newline = message.call_method1("find", ("\n",))?;
    if newline.ne(-1)? {
        message = message.get_item(py.get_type::<PySlice>().call1((
            py.None(),
            newline,
            py.None(),
        ))?)?;
    }
    let markup = PyDict::new(py);
    markup.set_item("bold", true)?;
    markup.set_item("red", true)?;
    let write = writer.getattr("write")?;
    write.call((representation.getattr("path")?,), Some(&markup))?;
    let line = writer.getattr("line")?;
    let text = PyString::new(py, ":{}: {}")
        .call_method1("format", (representation.getattr("lineno")?, message))?;
    line.call1((text,))?;
    Ok(())
}

#[pyfunction]
pub(super) fn independent_value_repr_terminal(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let types = py.import("vfy.independent.diagnostics")?;
    if representation.is_instance(&types.getattr("ExceptionChainRepr")?)? {
        chain(representation, writer)
    } else if representation.is_instance(&types.getattr("ReprTraceback")?)? {
        traceback(representation, writer)
    } else if representation.is_instance(&types.getattr("ReprEntryNative")?)? {
        let write = writer.getattr("write")?;
        let lines =
            PyString::new(py, "").call_method1("join", (representation.getattr("lines")?,))?;
        write.call1((lines,))?;
        Ok(())
    } else if representation.is_instance(&types.getattr("ReprEntry")?)? {
        entry(representation, writer)
    } else if representation.is_instance(&types.getattr("ReprFileLocation")?)? {
        location(representation, writer)
    } else if representation.is_instance(&types.getattr("ReprFuncArgs")?)? {
        super::super::graph::terminal::arguments(representation, writer)
    } else if representation.is_instance(&types.getattr("ReprLocals")?)? {
        super::super::graph::terminal::independent_repr_locals_terminal(
            representation,
            writer,
            PyString::new(py, "").as_any(),
        )
    } else if representation.is_instance(&types.getattr("ReprExceptionInfo")?)? {
        representation
            .getattr("reprtraceback")?
            .call_method1("toterminal", (writer,))?;
        sections(representation, writer)
    } else if representation.is_instance(&types.getattr("ExceptionRepr")?)? {
        sections(representation, writer)
    } else {
        Err(pyo3::exceptions::PyTypeError::new_err(
            "expected an independent value-style representation",
        ))
    }
}
