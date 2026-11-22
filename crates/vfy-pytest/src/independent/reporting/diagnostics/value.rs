//! Capture value-style exception descriptions and crash locations.

use pyo3::prelude::*;
use std::collections::HashSet;
use vfy_core::test_diagnostics::{
    DiagnosticCrash, ExceptionDescription, ExceptionNameStyle, ExceptionRelation,
    HIDDEN_TRACEBACK_MESSAGE, TracebackState,
};

pub(in crate::independent::reporting) enum TracebackPresentation {
    Value {
        description: ExceptionDescription,
        lines: Vec<String>,
        state: TracebackState,
    },
    Native {
        // CPython's actual formatted sequence is passed to the public native entry.
        lines: Py<PyAny>,
        extraline: Option<&'static str>,
    },
}

pub(in crate::independent::reporting) struct ValueNode {
    pub(in crate::independent::reporting) traceback: TracebackPresentation,
    pub(in crate::independent::reporting) crash: Option<DiagnosticCrash>,
    pub(in crate::independent::reporting) description: Option<ExceptionRelation>,
}

pub(super) fn native_lines(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
    traceback: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("traceback")?
        .call_method1(
            "format_exception",
            (exception.get_type(), exception, traceback),
        )?
        .unbind())
}

pub(super) fn visible_tracebacks<'py>(
    py: Python<'py>,
    item: &Bound<'py, PyAny>,
    info: &Bound<'py, PyAny>,
    owner: super::TracebackOwner,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let entries = super::selected_tracebacks(py, &info.getattr("tb")?, item, owner)?;
    super::visible_entries(entries.selected, info)
}

fn capture_node(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    root: &Bound<'_, PyAny>,
    exception: &Bound<'_, PyAny>,
    info: Option<&Bound<'_, PyAny>>,
    relation: Option<ExceptionRelation>,
    owner: super::TracebackOwner,
) -> PyResult<ValueNode> {
    let Some(info) = info else {
        return Ok(ValueNode {
            traceback: TracebackPresentation::Native {
                lines: native_lines(py, exception, py.None().bind(py))?,
                extraline: None,
            },
            crash: None,
            description: relation,
        });
    };
    let traceback =
        if exception.is_instance(&py.import("builtins")?.getattr("BaseExceptionGroup")?)? {
            // pytest's group fallback formats the original excinfo, including its chain.
            let visible = visible_tracebacks(py, item, root, owner)?;
            let start = visible
                .first()
                .cloned()
                .unwrap_or_else(|| py.None().into_bound(py));
            TracebackPresentation::Native {
                lines: native_lines(py, &root.getattr("value")?, &start)?,
                extraline: visible.is_empty().then_some(HIDDEN_TRACEBACK_MESSAGE),
            }
        } else {
            let visible = visible_tracebacks(py, item, info, owner)?;
            let text = exception.str()?.to_str()?.to_owned();
            let crash = super::exception_crash(py, info)?;
            let description = if let Some(crash) = &crash {
                ExceptionDescription {
                    name: exception.get_type().name()?.to_str()?.to_owned(),
                    text: crash.text.clone(),
                }
            } else {
                let snapshot = super::exception_snapshot(py, exception, &info.getattr("tb")?)?;
                super::description(exception, &snapshot, ExceptionNameStyle::Complete)?
            };
            return Ok(ValueNode {
                traceback: TracebackPresentation::Value {
                    description,
                    lines: text.split('\n').map(str::to_owned).collect(),
                    state: if visible.is_empty() {
                        TracebackState::Hidden
                    } else {
                        TracebackState::Visible
                    },
                },
                crash,
                description: relation,
            });
        };
    Ok(ValueNode {
        traceback,
        crash: super::exception_crash(py, info)?,
        description: relation,
    })
}

pub(in crate::independent::reporting) fn capture(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    exception_info: &Bound<'_, PyAny>,
    owner: super::TracebackOwner,
) -> PyResult<Vec<ValueNode>> {
    let mut info = Some(exception_info.clone());
    let mut exception = exception_info.getattr("value")?;
    let mut seen = HashSet::new();
    let mut nodes = Vec::new();
    let mut relation = None;
    while seen.insert(exception.as_ptr() as usize) {
        nodes.push(capture_node(
            py,
            item,
            exception_info,
            &exception,
            info.as_ref(),
            relation,
            owner,
        )?);
        let Some((previous_relation, previous)) = super::chained_exception(&exception)? else {
            break;
        };
        relation = Some(previous_relation);
        let traceback = previous.getattr("__traceback__")?;
        info = if traceback.is_none() {
            None
        } else {
            Some(super::named_info(py, &previous, &traceback)?)
        };
        exception = previous;
    }
    nodes.reverse();
    Ok(nodes)
}
