//! Python bindings for coverage report rendering.

use crate::data::CoverageData;
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc as Shared;
use vfy_core::report::{self as core, ReportError};
type Arc = (i32, i32);
type FileInput = (
    String,
    Vec<i32>,
    Vec<Arc>,
    Vec<(String, Vec<i32>)>,
    Vec<(String, Vec<Arc>)>,
);

fn boundary_error(error: ReportError) -> PyErr {
    match error {
        ReportError::InvalidPattern(error) => pattern_error(&error),
        ReportError::InvalidInput(message) => pyo3::exceptions::PyValueError::new_err(message),
        ReportError::Output(message) => pyo3::exceptions::PyOSError::new_err(message),
        ReportError::Serialization(error) => {
            pyo3::exceptions::PyRuntimeError::new_err(error.to_string())
        }
    }
}

pub(crate) fn pattern_error(error: &vfy_core::PathPatternError) -> PyErr {
    Python::attach(|py| {
        let exception = match error {
            vfy_core::PathPatternError::InvalidToken(_) => py
                .import("vfy.errors")
                .and_then(|module| module.getattr("ConfigError")),
            vfy_core::PathPatternError::Compile(_)
            | vfy_core::PathPatternError::VersionedCompile(_) => {
                py.import("re").and_then(|module| module.getattr("error"))
            }
        }
        .and_then(|kind| kind.call1((error.to_string(),)));
        match exception {
            Ok(exception) => PyErr::from_value(exception),
            Err(error) => error,
        }
    })
}

#[pyclass(module = "vfy._native", frozen, skip_from_py_object)]
#[derive(Clone)]
pub(crate) struct ReportCounts(core::ReportCounts);

#[pymethods]
impl ReportCounts {
    #[getter]
    fn statements(&self) -> usize {
        self.0.statements()
    }
    #[getter]
    fn covered(&self) -> usize {
        self.0.covered()
    }
    #[getter]
    fn missing(&self) -> usize {
        self.0.missing()
    }
    #[getter]
    fn excluded(&self) -> usize {
        self.0.excluded()
    }
    #[getter]
    fn branches(&self) -> usize {
        self.0.branches()
    }
    #[getter]
    fn covered_branches(&self) -> usize {
        self.0.covered_branches()
    }
    #[getter]
    fn missing_branches(&self) -> usize {
        self.0.missing_branches()
    }
    #[getter]
    fn partial_branches(&self) -> usize {
        self.0.partial_branches()
    }
    #[getter]
    fn line_percent(&self) -> f64 {
        self.0.line_percent()
    }
    #[getter]
    fn percent(&self) -> f64 {
        self.0.percent()
    }
    #[getter]
    fn branch_percent(&self) -> f64 {
        self.0.branch_percent()
    }
}

#[pyclass(module = "vfy._native", frozen, skip_from_py_object)]
#[derive(Clone)]
pub(crate) struct ReportRow(Shared<core::ReportRow>);

#[pymethods]
impl ReportRow {
    #[getter]
    fn path(&self) -> &str {
        self.0.path()
    }
    #[getter]
    fn data_filename(&self) -> &str {
        self.0.data_filename()
    }
    #[getter]
    fn source_missing(&self) -> bool {
        self.0.source_missing()
    }
    #[getter]
    fn source_error_kind(&self) -> Option<&str> {
        self.0.source_error_kind()
    }
    #[getter]
    fn source_error_detail(&self) -> Option<&str> {
        self.0.source_error_detail()
    }
    #[getter]
    fn statements(&self) -> usize {
        self.0.statements()
    }
    #[getter]
    fn covered(&self) -> usize {
        self.0.covered()
    }
    #[getter]
    fn missing(&self) -> BTreeSet<i32> {
        self.0.missing()
    }
    #[getter]
    fn executable_lines(&self) -> BTreeSet<i32> {
        self.0.executable_lines()
    }
    #[getter]
    fn covered_lines(&self) -> BTreeSet<i32> {
        self.0.covered_lines()
    }
    #[getter]
    fn excluded_lines(&self) -> BTreeSet<i32> {
        self.0.excluded_lines()
    }
    #[getter]
    fn branches(&self) -> usize {
        self.0.branches()
    }
    #[getter]
    fn covered_branches(&self) -> usize {
        self.0.covered_branches()
    }
    #[getter]
    fn possible_arcs(&self) -> BTreeSet<Arc> {
        self.0.possible_arcs()
    }
    #[getter]
    fn covered_arcs(&self) -> BTreeSet<Arc> {
        self.0.covered_arcs()
    }
    #[getter]
    fn missing_arcs(&self) -> BTreeSet<Arc> {
        self.0.missing_arcs()
    }
    #[getter]
    fn contexts_by_line(&self) -> BTreeMap<i32, Vec<String>> {
        self.0.contexts_by_line()
    }
    #[getter]
    fn line_percent(&self) -> f64 {
        self.0.line_percent()
    }
    #[getter]
    fn percent(&self) -> f64 {
        self.0.percent()
    }
    #[getter]
    fn branch_percent(&self) -> f64 {
        self.0.branch_percent()
    }
    #[getter]
    fn partial_branches(&self) -> usize {
        self.0.partial_branches()
    }
}

#[pyfunction]
#[pyo3(signature = (files, has_arcs, include, omit, excludes, contexts, partials=Vec::new(), source_roots=Vec::new(), relative_files=None, requested_paths=None, *, line_contexts=true))]
#[expect(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    reason = "PyO3 compatibility signature batches the complete report operation."
)]
fn build_report_rows(
    py: Python<'_>,
    files: Vec<FileInput>,
    has_arcs: bool,
    include: Vec<String>,
    omit: Vec<String>,
    excludes: Vec<String>,
    contexts: Vec<String>,
    partials: Vec<String>,
    source_roots: Vec<String>,
    relative_files: Option<bool>,
    requested_paths: Option<Vec<String>>,
    line_contexts: bool,
) -> PyResult<Vec<ReportRow>> {
    let semantics = crate::analysis::python_semantics(py)?;
    let unicode_case_version =
        vfy_core::UnicodeCaseVersion::for_python_minor(py.version_info().minor);
    py.detach(|| {
        core::build_report_rows(
            files,
            core::ReportOptions {
                has_arcs,
                include: &include,
                omit: &omit,
                excludes: &excludes,
                contexts: &contexts,
                partials: &partials,
                source_roots: &source_roots,
                path_semantics: relative_files.into(),
                requested_paths: requested_paths.as_deref(),
                python_semantics: semantics,
                unicode_case_version,
                context_detail: context_detail(line_contexts),
            },
        )
    })
    .map_err(boundary_error)
    .map(|rows| {
        rows.into_iter()
            .map(|row| ReportRow(Shared::new(row)))
            .collect()
    })
}

#[pyfunction]
#[pyo3(signature = (paths, include, omit, relative_files=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned path and pattern batches before releasing the interpreter."
)]
fn select_plugin_report_paths(
    py: Python<'_>,
    paths: Vec<String>,
    include: Vec<String>,
    omit: Vec<String>,
    relative_files: Option<bool>,
) -> PyResult<Vec<(String, String)>> {
    py.detach(|| core::select_plugin_report_paths(&paths, &include, &omit, relative_files.into()))
        .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (file, has_arcs, executable, excluded, possible_arcs, no_branch, contexts, source, data_filename, relative_filename, exit_counts))]
#[expect(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    reason = "Plug-in reports retain their documented batch shape."
)]
fn build_plugin_report_row(
    py: Python<'_>,
    file: FileInput,
    has_arcs: bool,
    executable: Vec<i32>,
    excluded: Vec<i32>,
    possible_arcs: Vec<Arc>,
    no_branch: Vec<i32>,
    contexts: Vec<String>,
    source: String,
    data_filename: String,
    relative_filename: String,
    exit_counts: BTreeMap<i32, i32>,
) -> PyResult<ReportRow> {
    py.detach(|| {
        core::build_plugin_report_row(
            core::PluginReportInput {
                file,
                has_arcs,
                executable,
                excluded,
                possible_arcs,
                no_branch,
                contexts: &contexts,
                source,
            }
            .with_exit_counts(&exit_counts),
        )
        .map(|row| row.with_plugin_report_filenames(data_filename, relative_filename))
    })
    .map_err(boundary_error)
    .map(|row| ReportRow(Shared::new(row)))
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn report_totals(py: Python<'_>, rows: Vec<PyRef<'_, ReportRow>>) -> ReportCounts {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    ReportCounts(py.detach(|| core::report_totals(&borrowed)))
}

#[pyfunction]
#[pyo3(signature = (rows, skip_covered=false, skip_empty=false, sort=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn select_report_rows(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    skip_covered: bool,
    skip_empty: bool,
    sort: Option<&str>,
) -> PyResult<Vec<ReportRow>> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| core::select_report_indices(&borrowed, skip_covered, skip_empty, sort))
        .map_err(boundary_error)
        .map(|selected| {
            selected
                .into_iter()
                .map(|index| ReportRow(Shared::clone(&rows[index].0)))
                .collect()
        })
}

#[pyfunction]
#[pyo3(signature = (data, include, omit, excludes, contexts, partials=Vec::new(), source_roots=Vec::new(), relative_files=None, requested_paths=None, *, line_contexts=true))]
#[expect(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    reason = "PyO3 compatibility signature batches report inputs."
)]
fn build_report_rows_from_data(
    py: Python<'_>,
    data: PyRef<'_, CoverageData>,
    include: Vec<String>,
    omit: Vec<String>,
    excludes: Vec<String>,
    contexts: Vec<String>,
    partials: Vec<String>,
    source_roots: Vec<String>,
    relative_files: Option<bool>,
    requested_paths: Option<Vec<String>>,
    line_contexts: bool,
) -> PyResult<Vec<ReportRow>> {
    let has_arcs = data.branch_mode();
    let core_data = data.core_snapshot();
    drop(data);
    let semantics = crate::analysis::python_semantics(py)?;
    let unicode_case_version =
        vfy_core::UnicodeCaseVersion::for_python_minor(py.version_info().minor);
    py.detach(|| {
        core::build_report_rows_from_data(
            &core_data,
            core::ReportOptions {
                has_arcs,
                include: &include,
                omit: &omit,
                excludes: &excludes,
                contexts: &contexts,
                partials: &partials,
                source_roots: &source_roots,
                path_semantics: relative_files.into(),
                requested_paths: requested_paths.as_deref(),
                python_semantics: semantics,
                unicode_case_version,
                context_detail: context_detail(line_contexts),
            },
        )
    })
    .map_err(boundary_error)
    .map(|rows| {
        rows.into_iter()
            .map(|row| ReportRow(Shared::new(row)))
            .collect()
    })
}

fn context_detail(line_contexts: bool) -> core::ReportContextDetail {
    if line_contexts {
        core::ReportContextDetail::Include
    } else {
        core::ReportContextDetail::Omit
    }
}

#[pyfunction]
fn format_line_ranges(py: Python<'_>, lines: Vec<i32>) -> String {
    py.detach(|| core::render::format_line_ranges(lines))
}

#[pyfunction]
fn branch_statistics(
    py: Python<'_>,
    possible: Vec<Arc>,
    observed: Vec<Arc>,
) -> BTreeMap<i32, (usize, usize)> {
    py.detach(|| core::render::branch_statistics(possible, observed))
}

#[pyfunction]
#[pyo3(signature = (rows, precision=0, show_missing=true, color=false, *, skip_covered=false, skip_empty=false, sort=None))]
#[expect(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    clippy::fn_params_excessive_bools,
    reason = "PyO3 batches report rendering and compatibility display options."
)]
fn render_terminal(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    precision: usize,
    show_missing: bool,
    color: bool,
    skip_covered: bool,
    skip_empty: bool,
    sort: Option<&str>,
) -> PyResult<String> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| {
        let report = core::ReportDisplay::selected(&borrowed, skip_covered, skip_empty, sort)?;
        Ok(core::render::render_terminal(
            &report,
            precision,
            show_missing,
            color,
        ))
    })
    .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, precision=0, show_missing=true, *, skip_covered=false, skip_empty=false, sort=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 batches report rendering and compatibility display options."
)]
fn render_markdown(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    precision: usize,
    show_missing: bool,
    skip_covered: bool,
    skip_empty: bool,
    sort: Option<&str>,
) -> PyResult<String> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| {
        let report = core::ReportDisplay::selected(&borrowed, skip_covered, skip_empty, sort)?;
        Ok(core::render::render_markdown(
            &report,
            precision,
            show_missing,
        ))
    })
    .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, precision=0, *, skip_covered=false, skip_empty=false, sort=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn render_total(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    precision: usize,
    skip_covered: bool,
    skip_empty: bool,
    sort: Option<&str>,
) -> PyResult<String> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| {
        let report = core::ReportDisplay::selected(&borrowed, skip_covered, skip_empty, sort)?;
        Ok(core::render::render_total(&report, precision))
    })
    .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, pretty=false, show_contexts=false, precision=0))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn render_json(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    pretty: bool,
    show_contexts: bool,
    precision: usize,
) -> PyResult<String> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| core::render::render_json(&borrowed, pretty, show_contexts, precision))
        .map_err(boundary_error)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn render_lcov(py: Python<'_>, rows: Vec<PyRef<'_, ReportRow>>) -> String {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    let unicode_version = core::render::UnicodeVersion::from_python_minor(py.version_info().minor);
    py.detach(|| core::render::render_lcov(&borrowed, unicode_version))
}

#[pyfunction]
#[pyo3(signature = (row, source=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts a bound pyclass reference by value."
)]
fn render_annotation(
    py: Python<'_>,
    row: PyRef<'_, ReportRow>,
    source: Option<&str>,
) -> PyResult<String> {
    let row = row.0.as_ref();
    py.detach(|| core::render::render_annotation(row, source))
        .map_err(boundary_error)
}

#[pyfunction]
fn write_report_output(py: Python<'_>, content: &str, destination: &str) -> PyResult<()> {
    py.detach(|| core::render::write_report_output(content, destination))
        .map_err(boundary_error)
}

#[pyfunction]
fn append_report_output(py: Python<'_>, content: &str, destination: &str) -> PyResult<()> {
    py.detach(|| core::render::append_report_output(content, destination))
        .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, directory, title="Coverage report", precision=0, show_contexts=false, extra_css=None, skip_covered=false, skip_empty=false))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
#[expect(
    clippy::too_many_arguments,
    reason = "PyO3 keeps the report ABI; core receives named HtmlSelection options."
)]
fn write_html_report(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    directory: &str,
    title: &str,
    precision: usize,
    show_contexts: bool,
    extra_css: Option<&str>,
    skip_covered: bool,
    skip_empty: bool,
) -> PyResult<()> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| {
        core::render::write_html_report(
            &borrowed,
            directory,
            title,
            precision,
            show_contexts,
            extra_css,
            core::render::HtmlSelection {
                skip_covered,
                skip_empty,
            },
        )
    })
    .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, directory=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn write_annotation_report(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    directory: Option<&str>,
) -> PyResult<usize> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| core::render::write_annotation_report(&borrowed, directory))
        .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, title="Coverage report"))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn render_html(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    title: &str,
) -> PyResult<Vec<(String, String)>> {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| core::render::render_html(&borrowed, title))
        .map_err(boundary_error)
}

#[pyfunction]
#[pyo3(signature = (rows, package_depth=None, source_roots=Vec::new(), relative_files=false))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned report rows at the boundary."
)]
fn render_xml(
    py: Python<'_>,
    rows: Vec<PyRef<'_, ReportRow>>,
    package_depth: Option<i64>,
    source_roots: Vec<String>,
    relative_files: bool,
) -> String {
    let borrowed: Vec<_> = rows.iter().map(|row| row.0.as_ref()).collect();
    py.detach(|| {
        core::render::render_xml_with_options(
            &borrowed,
            core::render::XmlOptions {
                package_depth,
                source_roots: &source_roots,
                relative_files,
            },
        )
    })
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(build_report_rows, module)?)?;
    module.add_function(wrap_pyfunction!(build_report_rows_from_data, module)?)?;
    module.add_function(wrap_pyfunction!(build_plugin_report_row, module)?)?;
    module.add_function(wrap_pyfunction!(select_plugin_report_paths, module)?)?;
    module.add_function(wrap_pyfunction!(report_totals, module)?)?;
    module.add_function(wrap_pyfunction!(select_report_rows, module)?)?;
    module.add_function(wrap_pyfunction!(format_line_ranges, module)?)?;
    module.add_function(wrap_pyfunction!(branch_statistics, module)?)?;
    module.add_function(wrap_pyfunction!(render_terminal, module)?)?;
    module.add_function(wrap_pyfunction!(render_markdown, module)?)?;
    module.add_function(wrap_pyfunction!(render_total, module)?)?;
    module.add_function(wrap_pyfunction!(render_json, module)?)?;
    module.add_function(wrap_pyfunction!(render_xml, module)?)?;
    module.add_function(wrap_pyfunction!(render_lcov, module)?)?;
    module.add_function(wrap_pyfunction!(render_annotation, module)?)?;
    module.add_function(wrap_pyfunction!(render_html, module)?)?;
    module.add_function(wrap_pyfunction!(write_report_output, module)?)?;
    module.add_function(wrap_pyfunction!(append_report_output, module)?)?;
    module.add_function(wrap_pyfunction!(write_html_report, module)?)?;
    module.add_function(wrap_pyfunction!(write_annotation_report, module)?)?;
    Ok(())
}
