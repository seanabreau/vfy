//! HTML attachment collection, validation, and publication.

use super::partial_native_result;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{BTreeMap, BTreeSet};
use vfy_core::test_execution::CaseResult;

pub(super) fn merge_html_records<T>(
    nodeids: &BTreeSet<&str>,
    additions: BTreeMap<String, T>,
    merged: &mut BTreeMap<String, T>,
) -> PyResult<()> {
    for (nodeid, attachments) in additions {
        if !nodeids.contains(nodeid.as_str()) || merged.insert(nodeid, attachments).is_some() {
            return Err(PyValueError::new_err(
                "native HTML attachments are misaligned or duplicated",
            ));
        }
    }
    Ok(())
}

pub(crate) fn collect_html_extras(
    item: &Bound<'_, PyAny>,
    nodeid: &str,
    extras: &mut BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    errors: &mut BTreeMap<String, String>,
) {
    // Attachments are report inputs; invalid plug-in data must not turn a
    // completed test into an execution failure or interrupt fixture cleanup.
    match parse_html_extras(item, "_vfy_html_extras") {
        Ok(Some(attachments)) => {
            errors.remove(nodeid);
            if attachments.is_empty() {
                extras.remove(nodeid);
            } else {
                extras.insert(nodeid.to_owned(), attachments);
            }
        }
        Ok(None) => {}
        Err(error) => {
            extras.remove(nodeid);
            errors
                .entry(nodeid.to_owned())
                .or_insert_with(|| error.to_string());
        }
    }
}

pub(crate) fn parse_html_extras(
    item: &Bound<'_, PyAny>,
    field: &str,
) -> PyResult<Option<Vec<vfy_core::PytestHtmlExtra>>> {
    let Some(values) = item
        .getattr("__dict__")?
        .cast::<PyDict>()?
        .get_item(field)?
    else {
        return Ok(None);
    };
    let values = values.cast::<PyList>()?;
    let attachments = values
        .iter()
        .map(|value| {
            let value = value.cast::<PyDict>()?;
            let field = |name: &str| -> PyResult<String> {
                value
                    .get_item(name)?
                    .ok_or_else(|| PyValueError::new_err(format!("HTML attachment has no {name}")))?
                    .extract()
            };
            Ok(vfy_core::PytestHtmlExtra {
                kind: field("kind")?,
                name: field("name")?,
                content: field("content")?,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(Some(attachments))
}

pub(crate) fn publish_html_case(
    py: Python<'_>,
    reporter: &Bound<'_, PyAny>,
    case: &CaseResult,
    extras: &BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    errors: &BTreeMap<String, String>,
) -> PyResult<()> {
    // The callback retains a snapshot while the runner still owns authoritative
    // cases for cleanup, failure accounting, and the final result.
    let mut result =
        partial_native_result(vec![case.clone()], Vec::new(), None, 1, case.duration_ms);
    if let Some(attachments) = extras.get(&case.nodeid) {
        result
            .html_extras
            .insert(case.nodeid.clone(), attachments.clone());
    }
    if let Some((nodeid, error)) = errors.first_key_value() {
        result.html_errors.insert(nodeid.clone(), error.clone());
    }
    reporter.call_method1("_native_progress", (Py::new(py, result)?,))?;
    Ok(())
}
