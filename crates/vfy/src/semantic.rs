//! Extension boundary for semantic decision analysis and reports.
//!
//! The collector records control-flow decisions from `CPython`'s `sys.monitoring`
//! without rewriting bytecode or testing user objects' truth values. This module
//! analyzes decision definitions and renders the observations that supplement
//! Coverage.py's line and arc data.

use pyo3::prelude::*;
mod model;
mod report;

pub(crate) use model::{PySemanticReport, PySemanticReportRow, SemanticSourceModel};
pub(crate) use vfy_core::analyze_semantic_source_data;
#[cfg(test)]
pub(crate) use vfy_core::{
    DecisionDefinition, PathDecisionDefinition, PathDecisionKind, PathOutcomeDefinition,
    PathOutcomeRole,
};
#[cfg(test)]
pub(crate) use vfy_core::{Logic, mcdc_indices, render_vector};
pub(crate) use vfy_core::{SemanticData, SemanticEvidenceRecord};

#[cfg(test)]
pub(crate) fn analyze_path_source(path: &str, source: &str) -> Vec<PathDecisionDefinition> {
    analyze_semantic_source_data(path, source)
        .expect("valid test source")
        .1
}

#[cfg(test)]
pub(crate) fn analyze_source(path: &str, source: &str) -> Vec<DecisionDefinition> {
    analyze_semantic_source_data(path, source)
        .expect("valid test source")
        .0
}
pub(crate) use report::{build_semantic_report, render_semantic_json, render_semantic_text};

#[cfg(test)]
mod tests;
#[pyfunction]
pub(crate) fn analyze_semantic_source(path: &str) -> PyResult<SemanticSourceModel> {
    let source = vfy_core::read_python_source(path).map_err(|error| {
        pyo3::exceptions::PyOSError::new_err(format!("failed to read {path}: {error}"))
    })?;
    let (definitions, path_definitions) = analyze_semantic_source_data(path, &source)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    let conditions = definitions
        .iter()
        .flat_map(|definition| {
            definition.condition_ranges().iter().enumerate().map(
                move |(index, &(line, start, end_line, end))| {
                    (
                        definition.key().to_owned(),
                        index,
                        line,
                        start,
                        end_line,
                        end,
                    )
                },
            )
        })
        .collect();
    let definition_records = definitions
        .into_iter()
        .map(|definition| {
            (
                definition.key().to_owned(),
                definition.path().to_owned(),
                definition.line(),
                definition.end_line(),
                definition.kind().to_owned(),
                definition.source_hash().to_owned(),
                definition.expression().to_owned(),
                definition.logic().render(),
            )
        })
        .collect();

    let path_outcomes = path_definitions
        .iter()
        .flat_map(|definition| {
            definition
                .outcomes()
                .iter()
                .enumerate()
                .map(move |(index, outcome)| {
                    (
                        definition.key().to_owned(),
                        index,
                        outcome.label().to_owned(),
                        outcome.range().0,
                        outcome.range().1,
                        outcome.range().2,
                        outcome.range().3,
                    )
                })
        })
        .collect();
    let path_definition_records = path_definitions
        .into_iter()
        .map(|definition| {
            (
                definition.key().to_owned(),
                definition.path().to_owned(),
                definition.line(),
                definition.end_line(),
                definition.kind().as_str().to_owned(),
                definition.source_hash().to_owned(),
                definition.expression().to_owned(),
                definition.body_start(),
                definition.body_end(),
            )
        })
        .collect();
    Ok((
        definition_records,
        conditions,
        path_definition_records,
        path_outcomes,
    ))
}
