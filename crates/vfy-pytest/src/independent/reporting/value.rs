//! Construct mutable value-style representations from captured diagnostics.

use super::diagnostics::value::{TracebackPresentation, ValueNode};
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use vfy_core::test_diagnostics::{
    ExceptionRelation, HIDDEN_TRACEBACK_MESSAGE, TracebackState, render_message_only,
};
use vfy_core::test_execution::PhasePayload;

mod terminal;

struct ProjectedNode {
    traceback: Py<PyAny>,
    crash: Option<Py<PyAny>>,
    relation: Option<ExceptionRelation>,
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        terminal::independent_value_repr_terminal,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(independent_value_repr_text, module)?)?;
    module.add_function(wrap_pyfunction!(independent_value_entry_text, module)?)?;
    module.add_function(wrap_pyfunction!(
        super::graph::terminal::independent_repr_entry_lines,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        super::graph::terminal::independent_repr_locals_terminal,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        super::graph::terminal::independent_terminal_write_source,
        module
    )?)?;
    Ok(())
}

fn relation_description(relation: Option<ExceptionRelation>) -> Option<&'static str> {
    relation.map(|relation| match relation {
        ExceptionRelation::Cause => {
            "The above exception was the direct cause of the following exception:"
        }
        ExceptionRelation::Context => {
            "During handling of the above exception, another exception occurred:"
        }
    })
}

pub(super) fn create(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    owner: super::diagnostics::TracebackOwner,
) -> PyResult<Py<PyAny>> {
    let nodes = super::diagnostics::value::capture(py, item, info, owner)?;
    let last = nodes
        .last()
        .ok_or_else(|| PyTypeError::new_err("value-style exception chain is empty"))?;
    let TracebackPresentation::Value {
        description, lines, ..
    } = &last.traceback
    else {
        return Err(PyTypeError::new_err(
            "outer value-style exception requires a value traceback",
        ));
    };
    let rendered = render_message_only(description, &lines.join("\n"));
    let diagnostic = super::failure::diagnostic(
        py,
        PhasePayload {
            detail: rendered.detail,
            terminal_lead: rendered.terminal_lead,
            exception: Some(rendered.exception.into()),
            ..PhasePayload::default()
        },
    )?;
    let types = py.import("vfy.independent.diagnostics")?;
    let chain = PyList::empty(py);
    for node in nodes {
        let projected = project(py, &types, node)?;
        chain.append((
            projected.traceback,
            projected.crash,
            relation_description(projected.relation),
        ))?;
    }
    let representation = types.getattr("ExceptionChainRepr")?.call1((chain,))?;
    representation.setattr("_vfy_diagnostic", diagnostic)?;
    Ok(representation.unbind())
}

fn project<'py>(
    py: Python<'py>,
    types: &Bound<'py, PyModule>,
    node: ValueNode,
) -> PyResult<ProjectedNode> {
    let traceback = match node.traceback {
        TracebackPresentation::Value { lines, state, .. } => {
            let entry = types.getattr("ReprEntry")?.call1((
                PyList::new(py, &lines)?,
                py.None(),
                py.None(),
                py.None(),
                "value",
            ))?;
            let extraline = match state {
                TracebackState::Native { hidden, .. } => hidden.then_some(HIDDEN_TRACEBACK_MESSAGE),
                TracebackState::Visible => None,
                TracebackState::Hidden | TracebackState::Missing => Some(HIDDEN_TRACEBACK_MESSAGE),
            };
            types.getattr("ReprTraceback")?.call1((
                PyList::new(py, [entry])?,
                extraline,
                "value",
            ))?
        }
        TracebackPresentation::Native { lines, extraline } => {
            let kwargs = pyo3::types::PyDict::new(py);
            kwargs.set_item("extraline", extraline)?;
            types
                .getattr("ReprTracebackNative")?
                .call((lines,), Some(&kwargs))?
        }
    };
    let crash = node
        .crash
        .map(|crash| {
            types
                .getattr("ReprFileLocation")?
                .call1((
                    crash.location.path,
                    crash.location.line,
                    PyString::new(py, &crash.text).call_method0("rstrip")?,
                ))
                .map(Bound::unbind)
        })
        .transpose()?;
    Ok(ProjectedNode {
        traceback: traceback.unbind(),
        crash,
        relation: node.description,
    })
}

#[pyfunction]
fn independent_value_repr_text(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let stream = py.import("io")?.call_method0("StringIO")?;
    let writer = py
        .import("vfy.independent.terminal")?
        .call_method1("report_writer", (&stream,))?;
    representation.call_method1("toterminal", (writer,))?;
    stream
        .call_method0("getvalue")?
        .call_method0("strip")?
        .extract()
}

#[pyfunction]
fn independent_value_entry_text(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let lines =
        PyString::new(py, "\n").call_method1("join", (representation.getattr("lines")?,))?;
    PyString::new(py, "{}\n{}\n{}")
        .call_method1(
            "format",
            (
                lines,
                representation.getattr("reprlocals")?,
                representation.getattr("reprfileloc")?,
            ),
        )?
        .extract()
}
