//! Preserve owned HTML inputs in the native worker result and publish in its controller.

use super::NativeSessionReports;
use crate::independent::lifecycle::Context;
use crate::results::{NativeRunResult, parse_html_extras};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::BTreeMap;

pub(in crate::independent) fn complete(
    py: Python<'_>,
    context: &Context,
    result: &Py<NativeRunResult>,
) -> PyResult<()> {
    let config = context.config.bind(py);
    if !config
        .getattr("pluginmanager")?
        .call_method1("hasplugin", ("vfy.pytest_reporting",))?
        .is_truthy()?
    {
        return Ok(());
    }
    if !config.getattr("option")?.getattr("htmlpath")?.is_truthy()? {
        return Ok(());
    }
    let history = context
        .session
        .bind(py)
        .getattr("_vfy_report")?
        .cast_into::<NativeSessionReports>()?;
    let reports = NativeSessionReports::published_reports(&history)?;
    let mut extras: BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>> = BTreeMap::new();
    let mut errors = BTreeMap::new();
    for report in reports {
        let nodeid = report.borrow(py).nodeid.clone();
        match parse_html_extras(report.bind(py).as_any(), "vfy_html_extras") {
            Ok(Some(attachments)) if !attachments.is_empty() => {
                extras
                    .entry(nodeid.to_string())
                    .or_default()
                    .extend(attachments);
            }
            Ok(_) => {}
            Err(error) => {
                errors
                    .entry(nodeid.to_string())
                    .or_insert_with(|| error.to_string());
            }
        }
    }
    let metadata = if context.invocation.is_worker() {
        let key = py.import("vfy.pytest_metadata")?.getattr("metadata_key")?;
        let values = config
            .getattr("stash")?
            .get_item(key)?
            .cast_into::<PyDict>()?;
        Some(
            values
                .iter()
                .map(|(key, value)| {
                    Ok((
                        key.str()?.to_str()?.to_owned(),
                        value.str()?.to_str()?.to_owned(),
                    ))
                })
                .collect::<PyResult<BTreeMap<_, _>>>()?,
        )
    } else {
        None
    };
    {
        let mut result = result.borrow_mut(py);
        result.set_html_extras(extras, errors);
        if let Some(metadata) = metadata {
            result.set_worker_metadata(metadata);
        }
    }
    if !context.invocation.is_worker() {
        bind_result(py, context, result)?;
    }
    Ok(())
}

pub(in crate::independent) fn bind_result(
    py: Python<'_>,
    context: &Context,
    result: &Py<NativeRunResult>,
) -> PyResult<()> {
    if !result.borrow(py).record().internal_errors.is_empty() {
        return Ok(());
    }
    let reporter = context
        .config
        .bind(py)
        .getattr("pluginmanager")?
        .call_method1("getplugin", ("_vfy_html_reporter",))?;
    if !reporter.is_none() {
        reporter.call_method1("_independent_complete", (result,))?;
    }
    Ok(())
}
