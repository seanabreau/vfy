use pyo3::prelude::*;

use vfy_core::semantic::report::{SemanticReport, SemanticReportRow};

type SemanticDefinitionRecord = (String, String, i32, i32, String, String, String, String);
type SemanticConditionRecord = (String, usize, i32, i32, i32, i32);
type PathDefinitionRecord = (String, String, i32, i32, String, String, String, i32, i32);
type PathOutcomeRecord = (String, usize, String, i32, i32, i32, i32);
pub(crate) type SemanticSourceModel = (
    Vec<SemanticDefinitionRecord>,
    Vec<SemanticConditionRecord>,
    Vec<PathDefinitionRecord>,
    Vec<PathOutcomeRecord>,
);

/// One fully aggregated semantic report row exposed to Python.
#[pyclass(
    name = "SemanticReportRow",
    module = "vfy._native",
    frozen,
    skip_from_py_object
)]
#[derive(Clone, Debug)]
pub(crate) struct PySemanticReportRow {
    inner: SemanticReportRow,
}

impl From<SemanticReportRow> for PySemanticReportRow {
    fn from(inner: SemanticReportRow) -> Self {
        Self { inner }
    }
}

#[pymethods]
impl PySemanticReportRow {
    #[getter]
    fn path(&self) -> &str {
        self.inner.path()
    }
    #[getter]
    fn line(&self) -> i32 {
        self.inner.line()
    }
    #[getter]
    fn expression(&self) -> &str {
        self.inner.expression()
    }
    #[getter]
    fn kind(&self) -> &str {
        self.inner.kind()
    }
    #[getter]
    fn decisions_covered(&self) -> usize {
        self.inner.decisions_covered()
    }
    #[getter]
    fn decisions_total(&self) -> usize {
        self.inner.decisions_total()
    }
    #[getter]
    fn conditions_covered(&self) -> usize {
        self.inner.conditions_covered()
    }
    #[getter]
    fn conditions_total(&self) -> usize {
        self.inner.conditions_total()
    }
    #[getter]
    fn mcdc_covered(&self) -> usize {
        self.inner.mcdc_covered()
    }
    #[getter]
    fn mcdc_total(&self) -> usize {
        self.inner.mcdc_total()
    }
    #[getter]
    fn missing(&self) -> Vec<String> {
        self.inner.missing().to_vec()
    }
    #[getter]
    fn key(&self) -> &str {
        self.inner.key()
    }
    #[getter]
    fn source_hash(&self) -> &str {
        self.inner.source_hash()
    }
    #[getter]
    fn logic(&self) -> &str {
        self.inner.logic()
    }
    #[getter]
    fn condition_lines(&self) -> Vec<i32> {
        self.inner.condition_lines().to_vec()
    }
    #[getter]
    fn condition_ranges(&self) -> Vec<(i32, i32, i32, i32)> {
        self.inner.condition_ranges().to_vec()
    }
    #[getter]
    fn vectors(&self) -> Vec<(String, String, bool)> {
        self.inner.vectors().to_vec()
    }
    #[getter]
    fn outcomes(&self) -> Vec<(String, bool, Vec<String>)> {
        self.inner.outcomes().to_vec()
    }
}

/// Aggregate semantic report exposed to Python.
#[pyclass(
    name = "SemanticReport",
    module = "vfy._native",
    frozen,
    skip_from_py_object
)]
#[derive(Clone, Debug)]
pub(crate) struct PySemanticReport {
    pub(crate) inner: SemanticReport,
}

impl From<SemanticReport> for PySemanticReport {
    fn from(inner: SemanticReport) -> Self {
        Self { inner }
    }
}

#[pymethods]
impl PySemanticReport {
    #[getter]
    fn rows(&self) -> Vec<PySemanticReportRow> {
        self.inner.rows().iter().cloned().map(Into::into).collect()
    }
    #[getter]
    fn decisions_covered(&self) -> usize {
        self.inner.decisions_covered()
    }
    #[getter]
    fn decisions_total(&self) -> usize {
        self.inner.decisions_total()
    }
    #[getter]
    fn conditions_covered(&self) -> usize {
        self.inner.conditions_covered()
    }
    #[getter]
    fn conditions_total(&self) -> usize {
        self.inner.conditions_total()
    }
    #[getter]
    fn mcdc_covered(&self) -> usize {
        self.inner.mcdc_covered()
    }
    #[getter]
    fn mcdc_total(&self) -> usize {
        self.inner.mcdc_total()
    }
}
