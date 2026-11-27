//! `PyO3` adapters for test selection, history, and replay artifacts.

use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};

use crate::analysis::python_semantics;
use crate::data::CoverageData;
use vfy_core::selection::{SelectionInput, SelectionMode, SelectionPlan, TestAttempt};

#[expect(
    clippy::needless_pass_by_value,
    reason = "map_err transfers the owned core error"
)]
fn core_error(error: vfy_core::CoreError) -> PyErr {
    match error {
        vfy_core::CoreError::InvalidInput(_) => PyValueError::new_err(error.to_string()),
        _ => PyRuntimeError::new_err(error.to_string()),
    }
}

fn ci_error(error: vfy_core::CoreError) -> PyErr {
    match error {
        vfy_core::CoreError::ArtifactIo { .. } => PyOSError::new_err(error.to_string()),
        _ => core_error(error),
    }
}

pub(crate) fn read_history(path: &Path) -> vfy_core::TestHistory {
    vfy_core::read_test_history(path)
}
pub(crate) fn record_durations(
    path: &str,
    records: Vec<(String, f64)>,
) -> Result<(), vfy_core::CoreError> {
    vfy_core::write_test_history(Path::new(path), records)
}

#[pyclass(module = "vfy._native")]
struct TestHistoryLease {
    lease: Option<vfy_core::selection::TestHistoryLease>,
}

#[pymethods]
impl TestHistoryLease {
    #[new]
    fn new(py: Python<'_>, path: &str) -> PyResult<Self> {
        let lease = py
            .detach(|| vfy_core::selection::TestHistoryLease::acquire(Path::new(path)))
            .map_err(core_error)?;
        Ok(Self { lease: Some(lease) })
    }

    fn __enter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __exit__(
        &mut self,
        _exception_type: &Bound<'_, PyAny>,
        _exception: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> bool {
        drop(self.lease.take());
        false
    }
}

#[pyclass(module = "vfy._native")]
#[derive(Default)]
pub(crate) struct TestReportAccumulator {
    inner: vfy_core::selection::TestReportAccumulator,
    conversion_error: Option<(Py<PyType>, Py<PyTuple>)>,
    received: usize,
}

#[pymethods]
impl TestReportAccumulator {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn append(
        &mut self,
        nodeid: &Bound<'_, PyString>,
        phase: &Bound<'_, PyString>,
        outcome: &Bound<'_, PyString>,
        duration_ms: f64,
    ) -> PyResult<()> {
        self.received += 1;
        if self.conversion_error.is_some() {
            return Ok(());
        }
        let converted =
            (|| Ok::<_, PyErr>((nodeid.to_str()?, phase.to_str()?, outcome.to_str()?)))();
        match converted {
            Ok((nodeid, phase, outcome)) => self.inner.append(nodeid, phase, outcome, duration_ms),
            Err(error) => {
                let py = nodeid.py();
                self.conversion_error = Some((
                    error.get_type(py).unbind(),
                    error
                        .value(py)
                        .getattr("args")?
                        .cast_into::<PyTuple>()?
                        .unbind(),
                ));
            }
        }
        Ok(())
    }

    fn __len__(&self) -> usize {
        self.received
    }

    fn write(&self, py: Python<'_>, path: &str, infrastructure_failed: bool) -> PyResult<()> {
        // Python string-conversion errors take precedence over deferred core errors.
        if let Some((kind, args)) = &self.conversion_error {
            return Err(PyErr::from_value(kind.bind(py).call1(args.bind(py))?));
        }
        self.inner
            .write(Path::new(path), infrastructure_failed)
            .map_err(core_error)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 requires this GC slot signature"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some((kind, args)) = &self.conversion_error {
            visit.call(kind)?;
            visit.call(args)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.conversion_error = None;
    }
}

#[pyfunction]
pub(crate) fn write_test_run_result(
    path: &str,
    reports: Vec<(String, String, String, f64)>,
    infrastructure_failed: bool,
) -> PyResult<()> {
    vfy_core::selection::write_test_reports(Path::new(path), reports, infrastructure_failed)
        .map_err(core_error)
}

#[pyfunction]
pub(crate) fn record_test_run(
    py: Python<'_>,
    history_path: &str,
    result_path: &str,
    run_id: &str,
    attempt: &str,
) -> PyResult<Py<PyAny>> {
    let attempt = TestAttempt::parse(attempt).map_err(core_error)?;
    let history_path = PathBuf::from(history_path);
    let result_path = PathBuf::from(result_path);
    let run_id = run_id.to_owned();
    let recorded = py
        .detach(move || {
            vfy_core::selection::record_test_run_artifact(
                &history_path,
                &result_path,
                &run_id,
                attempt,
            )
        })
        .map_err(core_error)?;
    recorded_test_run_dict(py, recorded)
}

#[pyfunction]
fn record_test_execution(
    py: Python<'_>,
    history_path: &str,
    result_path: &str,
    run_id: &str,
    attempt: &str,
) -> PyResult<Py<PyAny>> {
    let attempt = TestAttempt::parse(attempt).map_err(core_error)?;
    let history_path = PathBuf::from(history_path);
    let result_path = PathBuf::from(result_path);
    let run_id = run_id.to_owned();
    let recorded = py
        .detach(move || {
            vfy_core::selection::record_test_execution_artifact(
                &history_path,
                &result_path,
                &run_id,
                attempt,
            )
        })
        .map_err(core_error)?;
    recorded_test_run_dict(py, recorded)
}

fn recorded_test_run_dict(
    py: Python<'_>,
    recorded: vfy_core::selection::RecordedTestRun,
) -> PyResult<Py<PyAny>> {
    let outcomes = PyDict::new(py);
    for (nodeid, outcome) in recorded.outcomes {
        outcomes.set_item(nodeid, outcome.as_str())?;
    }
    let result = PyDict::new(py);
    result.set_item("failed", recorded.failed)?;
    result.set_item("outcomes", outcomes)?;
    result.set_item("infrastructure_failed", recorded.infrastructure_failed)?;
    Ok(result.into_any().unbind())
}

#[pyfunction]
#[pyo3(signature = (history_path, source_roots=Vec::new(), source_identity=""))]
fn prepare_test_import_graph(
    py: Python<'_>,
    history_path: String,
    source_roots: Vec<String>,
    source_identity: &str,
) -> Vec<String> {
    let identity = source_identity.to_owned();
    py.detach(move || {
        vfy_core::selection::prepare_import_graph(
            Path::new(&history_path),
            &source_roots.iter().map(PathBuf::from).collect::<Vec<_>>(),
            &identity,
        )
    })
}

#[pyfunction]
fn publish_test_import_graph(staged_path: &str, history_path: &str) -> PyResult<()> {
    vfy_core::selection::publish_import_graph(Path::new(staged_path), Path::new(history_path))
        .map_err(core_error)
}

fn result_dict(py: Python<'_>, plan: SelectionPlan) -> PyResult<Py<PyAny>> {
    let result = PyDict::new(py);
    result.set_item("mode", plan.mode.as_str())?;
    result.set_item("selected", plan.selected)?;
    result.set_item("all_tests", plan.all_tests)?;
    result.set_item("durations", plan.durations)?;
    result.set_item("affected_files", plan.affected_files)?;
    result.set_item("affected_functions", plan.affected_functions)?;
    result.set_item("affected_branches", plan.affected_branches)?;
    result.set_item("selected_seconds", plan.selected_seconds)?;
    result.set_item("full_seconds", plan.full_seconds)?;
    result.set_item("fallback_full", plan.fallback_full)?;
    result.set_item("reasons", plan.reasons)?;
    result.set_item("location", plan.location)?;
    let scoped_files = PyDict::new(py);
    for (path, shifts) in plan.scoped_files {
        let shifts: Vec<(i32, i32, i32)> = shifts
            .into_iter()
            .map(|shift| (shift.old_start, shift.old_count, shift.new_count))
            .collect();
        scoped_files.set_item(path, shifts)?;
    }
    result.set_item("scoped_files", scoped_files)?;
    Ok(result.into_any().unbind())
}

#[pyfunction]
#[pyo3(signature = (data, history_path, source_roots=Vec::new(), patch=None, mode="balanced", location=None, request_path=None, baseline_commit=None, target_commit=""))]
#[expect(
    clippy::needless_pass_by_value,
    clippy::too_many_arguments,
    reason = "PyO3 extracts the public selection arguments by value."
)]
pub(crate) fn plan_test_selection(
    py: Python<'_>,
    data: PyRef<'_, CoverageData>,
    history_path: &str,
    source_roots: Vec<String>,
    patch: Option<String>,
    mode: &str,
    location: Option<String>,
    request_path: Option<String>,
    baseline_commit: Option<String>,
    target_commit: &str,
) -> PyResult<Py<PyAny>> {
    let mode = SelectionMode::parse(mode).map_err(core_error)?;
    let history = read_history(Path::new(history_path));
    let (patch, submodule_change) = match (patch, location.is_some()) {
        (Some(value), true) => (value, None),
        (Some(value), false) => (
            value,
            vfy_core::selection::read_submodule_change(Path::new("."))
                .map_err(PyRuntimeError::new_err)?,
        ),
        (None, true) => (String::new(), None),
        (None, false) => {
            let state =
                vfy_core::selection::read_git_replay_state(&[]).map_err(PyRuntimeError::new_err)?;
            (state.patch, state.submodule_change)
        }
    };
    let snapshot = data.core_snapshot();
    let mut plan = vfy_core::selection::build_selection_plan_from_data(
        &snapshot,
        SelectionInput {
            mode,
            history_path: PathBuf::from(history_path),
            source_roots: source_roots.into_iter().map(PathBuf::from).collect(),
            patch,
            location,
            source_identity: data
                .metadata()
                .get("vfy_green_test_source_identity")
                .cloned()
                .unwrap_or_default(),
            branch_mode: data.branch_mode(),
            semantic_evidence: snapshot.has_semantic_data(),
            python_semantics: python_semantics(py)?,
            durations: history.durations,
        },
    )
    .map_err(core_error)?;
    if let Some(reason) = submodule_change {
        plan.fall_back_to_full(reason);
    }
    if let Some(path) = request_path {
        vfy_core::selection::ci::write_plan_request(vfy_core::selection::ci::PlanRequest {
            path: Path::new(&path),
            baseline_commit,
            target_commit: target_commit.to_owned(),
            source_identity: data
                .metadata()
                .get("vfy_green_test_source_identity")
                .cloned()
                .unwrap_or_default(),
            selection: plan.clone(),
        })
        .map_err(core_error)?;
    }
    result_dict(py, plan)
}

#[pyfunction]
pub(crate) fn update_test_history(
    py: Python<'_>,
    path: &str,
    records: Vec<(String, f64)>,
) -> PyResult<()> {
    let path = path.to_owned();
    py.detach(move || record_durations(&path, records))
        .map_err(core_error)
}

#[pyfunction]
fn snapshot_test_history(py: Python<'_>, source: &str, destination: &str) -> PyResult<()> {
    py.detach(|| {
        vfy_core::selection::snapshot_test_history(Path::new(source), Path::new(destination))
    })
    .map_err(core_error)
}

#[pyfunction]
pub(crate) fn rank_test_contexts(
    context_groups: Vec<Vec<String>>,
    history_path: &str,
) -> Vec<Vec<(String, Option<f64>)>> {
    vfy_core::selection::rank_test_contexts(
        context_groups,
        &read_history(Path::new(history_path)).durations,
    )
}

#[pyfunction]
#[pyo3(signature = (ignored=Vec::new()))]
pub(crate) fn git_replay_state(py: Python<'_>, ignored: Vec<String>) -> PyResult<Py<PyAny>> {
    let state = py
        .detach(move || vfy_core::selection::read_git_replay_state(&ignored))
        .map_err(PyRuntimeError::new_err)?;
    let result = PyDict::new(py);
    result.set_item("head", state.head)?;
    result.set_item(
        "clean",
        state.changed.is_empty() && state.submodule_change.is_none(),
    )?;
    result.set_item("changed", state.changed)?;
    result.set_item("untracked", state.untracked)?;
    result.set_item("deleted", state.deleted)?;
    result.set_item("patch", state.patch)?;
    result.set_item("submodule_change", state.submodule_change)?;
    Ok(result.into_any().unbind())
}

#[pyfunction]
pub(crate) fn green_git_head(py: Python<'_>, ignored: Vec<String>) -> PyResult<Option<String>> {
    py.detach(move || vfy_core::selection::read_green_git_head(&ignored))
        .map_err(PyRuntimeError::new_err)
}

#[pyfunction]
pub(crate) fn replay_fingerprint(values: Vec<String>) -> String {
    vfy_core::replay_fingerprint(values)
}
#[pyfunction]
fn file_digest(path: &str) -> PyResult<String> {
    vfy_core::file_digest(Path::new(path)).map_err(|error| PyOSError::new_err(error.to_string()))
}
#[pyfunction]
fn green_test_run_valid(record_path: &str, coverage_path: &str) -> PyResult<bool> {
    vfy_core::green_test_run_valid(Path::new(record_path), Path::new(coverage_path))
        .map_err(PyOSError::new_err)
}
#[pyfunction]
fn write_green_test_run(record_path: &str, coverage_path: &str) -> PyResult<()> {
    vfy_core::write_green_test_run(Path::new(record_path), Path::new(coverage_path))
        .map_err(PyOSError::new_err)
}

#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts owned lists for one complete baseline export request"
)]
fn export_ci_baseline(
    py: Python<'_>,
    destination: &str,
    coverage_path: &str,
    history_path: &str,
    collection_path: &str,
    fingerprint: &str,
    source_roots: Vec<String>,
    ignored: Vec<String>,
) -> PyResult<()> {
    py.detach(|| {
        vfy_core::selection::ci::export_baseline(vfy_core::selection::ci::BaselineExport {
            destination: Path::new(destination),
            coverage_path: Path::new(coverage_path),
            history_path: Path::new(history_path),
            collection_path: Path::new(collection_path),
            fingerprint,
            source_roots: &source_roots,
            ignored: &ignored,
        })
    })
    .map_err(ci_error)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the owned list at this batch boundary"
)]
fn import_ci_baseline(
    py: Python<'_>,
    path: &str,
    fingerprint: &str,
    ignored: Vec<String>,
    request_path: &str,
) -> PyResult<Option<CoverageData>> {
    let imported = py
        .detach(|| vfy_core::selection::ci::import_baseline(Path::new(path), fingerprint, &ignored))
        .map_err(ci_error)?;
    vfy_core::selection::ci::write_plan_request(vfy_core::selection::ci::PlanRequest {
        path: Path::new(request_path),
        baseline_commit: imported.plan.baseline_commit,
        target_commit: imported.plan.target_commit,
        source_identity: imported.plan.source_identity,
        selection: imported.plan.selection,
    })
    .map_err(ci_error)?;
    Ok(imported.data.map(CoverageData::from))
}

#[pyfunction]
fn fallback_ci_plan(request_path: &str, target_commit: &str, reason: &str) -> PyResult<()> {
    vfy_core::selection::ci::write_fallback_plan(Path::new(request_path), target_commit, reason)
        .map_err(ci_error)
}

#[pyfunction]
#[pyo3(signature = (data, history, baseline=None, export=None, plan=None))]
fn validate_ci_artifact_paths(
    data: &str,
    history: &str,
    baseline: Option<&str>,
    export: Option<&str>,
    plan: Option<&str>,
) -> PyResult<()> {
    vfy_core::selection::ci::validate_artifact_paths(data, history, baseline, export, plan)
        .map_err(ci_error)
}

#[pyfunction]
fn publish_ci_plan(source: &str, output: &str) -> PyResult<()> {
    vfy_core::selection::ci::publish_plan(Path::new(source), Path::new(output)).map_err(ci_error)
}

#[pyfunction]
fn read_ci_plan(py: Python<'_>, path: &str) -> PyResult<Py<PyAny>> {
    result_dict(
        py,
        vfy_core::selection::ci::read_plan(Path::new(path))
            .map_err(ci_error)?
            .selection,
    )
}

#[pyfunction]
#[pyo3(signature = (path, ignored=Vec::new()))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts ignored output paths as one owned batch"
)]
fn ci_plan_source_current(path: &str, ignored: Vec<String>) -> PyResult<bool> {
    vfy_core::selection::ci::plan_source_current(Path::new(path), &ignored).map_err(ci_error)
}

/// Whether a prepared request lets the selected run keep unaffected files' baseline identities.
#[pyfunction]
fn ci_plan_collection_scoped(path: &str) -> PyResult<bool> {
    vfy_core::selection::ci::collection_scope(Path::new(path))
        .map(|scope| scope.is_some())
        .map_err(ci_error)
}

#[pyfunction]
fn finalize_ci_plan(
    py: Python<'_>,
    request_path: &str,
    collected: Vec<String>,
    output_path: &str,
) -> PyResult<Py<PyAny>> {
    let plan = vfy_core::selection::ci::finalize_plan(
        Path::new(request_path),
        collected,
        Path::new(output_path),
    )
    .map_err(ci_error)?;
    result_dict(py, plan.selection)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the owned list at this batch boundary"
)]
fn write_test_collection(path: &str, collected: Vec<String>) -> PyResult<()> {
    vfy_core::selection::ci::write_collection(Path::new(path), &collected).map_err(ci_error)
}

#[pyfunction]
fn audit_ci_plan(
    py: Python<'_>,
    plan_path: &str,
    result_path: &str,
    status: i32,
    output_path: &str,
) -> PyResult<Py<PyAny>> {
    let audit = vfy_core::selection::ci::audit_plan(
        Path::new(plan_path),
        Path::new(result_path),
        status,
        Path::new(output_path),
    )
    .map_err(ci_error)?;
    let result = PyDict::new(py);
    result.set_item("complete", audit.complete)?;
    result.set_item("omitted_failures", audit.omitted_failures)?;
    Ok(result.into_any().unbind())
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts the owned list at this batch boundary"
)]
fn portable_test_fingerprint(values: Vec<String>) -> String {
    let root = vfy_core::selection::ci::repository_root()
        .unwrap_or_else(|_| vfy_core::absolute_path(Path::new(".")));
    vfy_core::selection::ci::portable_fingerprint(&values, &root.to_string_lossy())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<TestHistoryLease>()?;
    module.add_class::<TestReportAccumulator>()?;
    module.add_function(wrap_pyfunction!(export_ci_baseline, module)?)?;
    module.add_function(wrap_pyfunction!(import_ci_baseline, module)?)?;
    module.add_function(wrap_pyfunction!(fallback_ci_plan, module)?)?;
    module.add_function(wrap_pyfunction!(validate_ci_artifact_paths, module)?)?;
    module.add_function(wrap_pyfunction!(publish_ci_plan, module)?)?;
    module.add_function(wrap_pyfunction!(read_ci_plan, module)?)?;
    module.add_function(wrap_pyfunction!(ci_plan_collection_scoped, module)?)?;
    module.add_function(wrap_pyfunction!(ci_plan_source_current, module)?)?;
    module.add_function(wrap_pyfunction!(finalize_ci_plan, module)?)?;
    module.add_function(wrap_pyfunction!(write_test_collection, module)?)?;
    module.add_function(wrap_pyfunction!(audit_ci_plan, module)?)?;
    module.add_function(wrap_pyfunction!(portable_test_fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(plan_test_selection, module)?)?;
    module.add_function(wrap_pyfunction!(prepare_test_import_graph, module)?)?;
    module.add_function(wrap_pyfunction!(publish_test_import_graph, module)?)?;
    module.add_function(wrap_pyfunction!(update_test_history, module)?)?;
    module.add_function(wrap_pyfunction!(snapshot_test_history, module)?)?;
    module.add_function(wrap_pyfunction!(write_test_run_result, module)?)?;
    module.add_function(wrap_pyfunction!(record_test_run, module)?)?;
    module.add_function(wrap_pyfunction!(record_test_execution, module)?)?;
    module.add_function(wrap_pyfunction!(rank_test_contexts, module)?)?;
    module.add_function(wrap_pyfunction!(git_replay_state, module)?)?;
    module.add_function(wrap_pyfunction!(green_git_head, module)?)?;
    module.add_function(wrap_pyfunction!(replay_fingerprint, module)?)?;
    module.add_function(wrap_pyfunction!(file_digest, module)?)?;
    module.add_function(wrap_pyfunction!(green_test_run_valid, module)?)?;
    module.add_function(wrap_pyfunction!(write_green_test_run, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests;
