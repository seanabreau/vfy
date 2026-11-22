//! `PyO3` adapters for semantic report aggregation and rendering.

use std::path::Path;

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;

use crate::data::CoverageData;

use super::PySemanticReport;

fn semantic_error(error: &vfy_core::CoreError) -> PyErr {
    match error {
        vfy_core::CoreError::ArtifactIo { .. } => PyOSError::new_err(error.to_string()),
        _ => PyValueError::new_err(error.to_string()),
    }
}

#[pyfunction]
#[pyo3(signature = (data, include=Vec::new(), omit=Vec::new(), contexts=Vec::new(), source_roots=Vec::new(), exclude_decisions=Vec::new()))]
pub(crate) fn build_semantic_report(
    py: Python<'_>,
    data: PyRef<'_, CoverageData>,
    include: Vec<String>,
    omit: Vec<String>,
    contexts: Vec<String>,
    source_roots: Vec<String>,
    exclude_decisions: Vec<String>,
) -> PyResult<PySemanticReport> {
    let semantic = data.semantic_data().clone();
    let optimize = data
        .metadata()
        .get("vfy_optimize")
        .is_some_and(|value| !value.is_empty() && value != "0");
    drop(data);
    let unicode_case_version =
        vfy_core::UnicodeCaseVersion::for_python_minor(py.version_info().minor);
    py.detach(move || {
        vfy_core::semantic::report::build_semantic_report(
            semantic,
            &vfy_core::semantic::report::SemanticReportOptions {
                include,
                omit,
                contexts,
                source_roots,
                exclude_decisions,
                optimize,
                unicode_case_version,
            },
        )
        .map(Into::into)
        .map_err(|error| semantic_error(&error))
    })
}

#[pyfunction]
#[pyo3(signature = (report, precision=1, history_path=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned optional strings at the boundary."
)]
pub(crate) fn render_semantic_text(
    report: PyRef<'_, PySemanticReport>,
    precision: usize,
    history_path: Option<String>,
) -> String {
    vfy_core::semantic::report::render_semantic_text(
        &report.inner,
        precision,
        history_path.as_deref().map(Path::new),
    )
}

#[pyfunction]
#[pyo3(signature = (report, pretty=false))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the report handle by value."
)]
pub(crate) fn render_semantic_json(
    report: PyRef<'_, PySemanticReport>,
    pretty: bool,
) -> PyResult<String> {
    vfy_core::semantic::report::render_semantic_json(&report.inner, pretty)
        .map_err(|error| semantic_error(&error))
}
