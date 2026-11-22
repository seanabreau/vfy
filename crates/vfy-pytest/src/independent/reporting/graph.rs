//! Construct a mutable graph from a captured diagnostic.

use super::{diagnostics, failure};

use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use vfy_core::test_diagnostics::{
    DiagnosticException, DiagnosticFrameStyle, DiagnosticStyle, ExceptionRelation,
    project_traceback,
};
use vfy_core::test_execution::PhasePayload;

pub(super) mod terminal;

fn relation(relation: ExceptionRelation) -> &'static str {
    match relation {
        ExceptionRelation::Cause => {
            "The above exception was the direct cause of the following exception:"
        }
        ExceptionRelation::Context => {
            "During handling of the above exception, another exception occurred:"
        }
    }
}

fn location<'py>(
    py: Python<'py>,
    types: &Bound<'py, PyModule>,
    location: &vfy_core::test_diagnostics::DiagnosticLocation,
    text: &str,
) -> PyResult<Bound<'py, PyAny>> {
    types.getattr("ReprFileLocation")?.call1((
        &location.path,
        location.line,
        PyString::new(py, text).call_method0("rstrip")?,
    ))
}

fn traceback<'py>(
    py: Python<'py>,
    types: &Bound<'py, PyModule>,
    exception: &DiagnosticException,
    style: DiagnosticStyle,
    native_lines: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let projected = project_traceback(exception, style);
    if projected.native.is_some() {
        if native_lines.is_none() {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "native traceback has no protocol sequence",
            ));
        }
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("extraline", projected.extra)?;
        return types
            .getattr("ReprTracebackNative")?
            .call((native_lines,), Some(&kwargs));
    }
    let entries = PyList::empty(py);
    for entry in projected.entries {
        let arguments = entry
            .arguments
            .map(|arguments| {
                types
                    .getattr("ReprFuncArgs")?
                    .call1((PyList::new(py, arguments)?,))
            })
            .transpose()?;
        let file = entry
            .location
            .map(|(place, message)| location(py, types, &place, &message))
            .transpose()?;
        let locals = entry
            .locals
            .map(|lines| types.getattr("ReprLocals")?.call1((lines,)))
            .transpose()?;
        entries.append(types.getattr("ReprEntry")?.call1((
            entry.lines,
            arguments,
            locals,
            file,
            match entry.style {
                DiagnosticFrameStyle::Long => "long",
                DiagnosticFrameStyle::Short => "short",
            },
        ))?)?;
    }
    types.getattr("ReprTraceback")?.call1((
        entries,
        projected.extra,
        if style == DiagnosticStyle::Short {
            "short"
        } else {
            "long"
        },
    ))
}

pub(super) fn create(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    style: DiagnosticStyle,
    owner: diagnostics::TracebackOwner,
) -> PyResult<Py<PyAny>> {
    let captured = diagnostics::capture_structured(py, item, info, style, owner)?;
    // The opaque protocol resources have a GC-traced Python owner before any
    // public constructor can reenter; the core capture contains no Python refs.
    let bindings = PyList::new(py, captured.native_lines)?;
    let chain = &captured.chain;
    let types = py.import("vfy.independent.diagnostics")?;
    let values = PyList::empty(py);
    for (index, exception) in std::iter::once(&chain.first)
        .chain(chain.following.iter().map(|(_, exception)| exception))
        .enumerate()
    {
        let traceback = traceback(py, &types, exception, style, &bindings.get_item(index)?)?;
        let crash = exception
            .crash
            .as_ref()
            .map(|crash| location(py, &types, &crash.location, &crash.text))
            .transpose()?;
        values.append((
            traceback,
            crash,
            chain.following.get(index).map(|(kind, _)| relation(*kind)),
        ))?;
    }
    let representation = types.getattr("ExceptionChainRepr")?.call1((values,))?;
    let mut payload = PhasePayload::default();
    diagnostics::apply_captured(py, chain, &mut payload)?;
    representation.setattr("_vfy_diagnostic", failure::diagnostic(py, payload)?)?;
    Ok(representation.unbind())
}
