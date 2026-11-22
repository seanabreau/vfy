//! Owns native execution sessions and the `run_native_suite` entry point.

use super::compile::compile_suite;
use super::execute::execute_native_session_output;
use super::model::{CompileOptions, PytestCompatibilityPolicy, SuiteBindings};
use super::report::is_control_error;
use super::runtime::{Runtime, SharedRuntime};
use super::teardown::teardown_all;
use crate::UnsupportedFeatureError;
use crate::lifecycle::LifecycleService;
use crate::logging::LoggingService;
use crate::parallel::NativeParallelWorker;
use crate::results::{
    CaptureSummary, NativeRunResult, ReportLocation, native_run_result, partial_native_result,
};
use crate::warnings::{WarningFilterOwnership, WarningService};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyErr, PyRef, PyResult, Python, pyclass, pyfunction, pymethods,
};
use pyo3::types::PyAny;
use rustix::io;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::Instant;
use vfy_core::test_execution::{CaseResult, RunRecord, WarningRecord, compute_counts};
use vfy_core::test_plan::TestPlan;
use vfy_core::{FixtureGeneration, FixtureScope};

// With --maxfail, avoid reserving space for a full suite that may stop early.
pub(super) const MAXFAIL_INITIAL_CASE_CAPACITY: usize = 1_024;

#[derive(Clone, Copy)]
pub(super) struct PartialReportSnapshot<'a> {
    pub(super) cases: &'a [CaseResult],
    pub(super) case_indices: &'a [usize],
    pub(super) warnings: &'a [WarningRecord],
    pub(super) report_locations: Option<&'a [(String, ReportLocation)]>,
    pub(super) summary_locations: &'a BTreeMap<usize, ReportLocation>,
    pub(super) html_extras: &'a BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    pub(super) html_errors: &'a BTreeMap<String, String>,
    pub(super) collected: usize,
    pub(super) started: Instant,
}

pub(super) fn native_execution_snapshot(snapshot: PartialReportSnapshot<'_>) -> NativeRunResult {
    let PartialReportSnapshot {
        cases,
        case_indices,
        warnings,
        report_locations,
        summary_locations,
        html_extras,
        html_errors,
        collected,
        started,
    } = snapshot;
    let report_locations = report_locations.map(|locations| {
        case_indices
            .iter()
            .filter_map(|index| locations.get(*index).cloned())
            .collect()
    });
    let mut result = partial_native_result(
        cases.to_vec(),
        warnings.to_vec(),
        report_locations,
        collected,
        started.elapsed().as_secs_f64() * 1_000.0,
    );
    result.set_html_extras(html_extras.clone(), html_errors.clone());
    result.set_summary_locations(summary_locations.clone());
    result
}

pub(super) fn report_native_partial(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    results: Option<&Bound<'_, PyAny>>,
    snapshot: PartialReportSnapshot<'_>,
    error: &PyErr,
) {
    let retain_terminal = is_control_error(py, error)
        && (!snapshot.cases.is_empty() || !snapshot.warnings.is_empty());
    if results.is_none() && !retain_terminal {
        return;
    }
    // Session cleanup still owns these records after publishing an interrupted run.
    let result = native_execution_snapshot(snapshot);
    if let Ok(result) = Py::new(py, result) {
        if retain_terminal {
            let _ = (|| -> PyResult<()> {
                let Some(first) = items.try_iter()?.next().transpose()? else {
                    return Ok(());
                };
                py.import("vfy._pytest.native_pytest")?
                    .getattr("retain_interrupted_result")?
                    .call1((first.getattr("config")?, result.bind(py)))?;
                Ok(())
            })();
        }
        if let Some(results) = results {
            let _ = results.call_method1("_native_partial_result", (result,));
        }
    }
}

pub(super) fn io_error(error: &std::io::Error) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

pub(super) fn fd_error(error: io::Errno) -> PyErr {
    let error: std::io::Error = error.into();
    io_error(&error)
}

/// Compiled worker-local fixture state shared by all dispatched test protocols.
pub(super) struct NativeExecutionState {
    pub(super) items: Py<PyAny>,
    pub(super) vfy_coverage: Option<Py<PyAny>>,
    pub(super) vfy_results: Option<Py<PyAny>>,
    pub(super) pytest_reporter: Option<PytestReporter>,
    pub(super) pytest_protocol_reporter: Option<Py<PyAny>>,
    pub(super) runtime: SharedRuntime,
    // Only empty capacity survives dispatch; Python owners are cleared outside the runtime lock.
    pub(super) owner_nodes_buffer: Option<HashMap<String, Py<PyAny>>>,
    pub(super) warning_service: WarningService,
    pub(super) logging_service: LoggingService,
    pub(super) lifecycle_service: LifecycleService,
    pub(super) report_locations: Option<Vec<(String, ReportLocation)>>,
    // Without compiled locations, record only cases the capture summary renders.
    pub(super) capture_summary: CaptureSummary,
    pub(super) summary_locations: BTreeMap<usize, ReportLocation>,
    pub(super) cases: Vec<CaseResult>,
    pub(super) case_indices: Vec<usize>,
    pub(super) last_test: Option<usize>,
    pub(super) unfinished_setup: Option<usize>,
    pub(super) html_extras: BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    pub(super) html_errors: BTreeMap<String, String>,
    pub(super) started: Instant,
    pub(super) compile_ms: f64,
    pub(super) warning_setup_ms: f64,
    pub(super) capture: bool,
    pub(super) retain_success_output: bool,
    pub(super) retain_html_extras: bool,
}

#[derive(Clone, Copy)]
pub(super) enum ExecutionSchedule {
    Suite,
    // Report callbacks can stop early; remaining fixtures belong to sessionfinish.
    Protocols,
    Case { index: usize, next: Option<usize> },
    Finish,
}

impl ExecutionSchedule {
    pub(super) fn indices(self, collected: usize) -> impl Iterator<Item = (usize, Option<usize>)> {
        let range = match self {
            Self::Suite | Self::Protocols => 0..collected,
            Self::Case { index, .. } => index..index + 1,
            Self::Finish => 0..0,
        };
        range.map(move |index| {
            (
                index,
                match self {
                    Self::Case { next, .. } => next,
                    _ => (index + 1 < collected).then_some(index + 1),
                },
            )
        })
    }

    pub(super) fn finishes_suite(self) -> bool {
        matches!(self, Self::Suite | Self::Finish)
    }

    pub(super) fn enters_protocols(self) -> bool {
        matches!(self, Self::Suite | Self::Protocols)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ExecutionSessionMode {
    Protocol,
    CollectedReady,
    CollectedRunning,
}

/// Compiled fixture and test state for pytest protocols or collected fork workers.
#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeExecutionSession {
    state: Option<NativeExecutionState>,
    result: Option<Py<NativeRunResult>>,
    mode: ExecutionSessionMode,
}

#[pymethods]
impl NativeExecutionSession {
    #[new]
    #[pyo3(signature = (items, capture = true, vfy_coverage = None, vfy_results = None, *, retain_success_output = true, retain_html_extras = false, pytest_reporter = None, pytest_phase_reporter = None, pytest_protocol_reporter = None, warning_filter_ownership = "native"))]
    #[expect(
        clippy::too_many_arguments,
        reason = "the Python-visible constructor keeps one keyword per policy and reporter"
    )]
    fn new(
        py: Python<'_>,
        items: &Bound<'_, PyAny>,
        capture: bool,
        vfy_coverage: Option<&Bound<'_, PyAny>>,
        vfy_results: Option<&Bound<'_, PyAny>>,
        retain_success_output: bool,
        retain_html_extras: bool,
        pytest_reporter: Option<&Bound<'_, PyAny>>,
        pytest_phase_reporter: Option<&Bound<'_, PyAny>>,
        pytest_protocol_reporter: Option<&Bound<'_, PyAny>>,
        warning_filter_ownership: &str,
    ) -> PyResult<Self> {
        let warning_filter_ownership = match warning_filter_ownership {
            "native" => WarningFilterOwnership::Native,
            "pytest_protocol" => WarningFilterOwnership::PytestProtocol,
            _ => {
                return Err(PyValueError::new_err(
                    "native warning filter ownership must be 'native' or 'pytest_protocol'",
                ));
            }
        };
        if pytest_phase_reporter.is_some() && (pytest_reporter.is_some() || retain_html_extras) {
            return Err(PyValueError::new_err(
                "native phase projection requires no result reporter or HTML extras",
            ));
        }
        let mut state = prepare_native_execution(
            py,
            items,
            vfy_coverage,
            vfy_results,
            NativeExecutionOptions {
                protocol: ReportProtocol::Pytest,
                warning_filter_ownership,
                retain_report_locations: false,
                retain_capture_report_locations: true,
                capture,
                retain_success_output,
                retain_html_extras,
                capture_summary: CaptureSummary::default(),
            },
        )?;
        state.pytest_reporter = pytest_reporter
            .map(|value| PytestReporter::Result(value.clone().unbind()))
            .or_else(|| {
                pytest_phase_reporter.map(|value| PytestReporter::Phase(value.clone().unbind()))
            });
        state.pytest_protocol_reporter =
            pytest_protocol_reporter.map(|value| value.clone().unbind());
        accept_native_execution(py, &state)?;
        Ok(Self {
            state: Some(state),
            result: None,
            mode: ExecutionSessionMode::Protocol,
        })
    }

    #[staticmethod]
    #[pyo3(signature = (items, capture = true, vfy_results = None, *, retain_success_output = false, retain_report_locations = false, retain_capture_report_locations = true))]
    #[expect(
        clippy::fn_params_excessive_bools,
        reason = "the Python-visible factory keeps one keyword per independent report policy"
    )]
    fn prepare_collected(
        py: Python<'_>,
        items: &Bound<'_, PyAny>,
        capture: bool,
        vfy_results: Option<&Bound<'_, PyAny>>,
        retain_success_output: bool,
        retain_report_locations: bool,
        retain_capture_report_locations: bool,
    ) -> PyResult<Self> {
        let state = prepare_native_execution(
            py,
            items,
            None,
            vfy_results,
            NativeExecutionOptions {
                protocol: ReportProtocol::Native,
                warning_filter_ownership: WarningFilterOwnership::Native,
                capture,
                retain_success_output,
                retain_html_extras: false,
                retain_report_locations,
                retain_capture_report_locations,
                capture_summary: CaptureSummary::default(),
            },
        )?;
        Ok(Self {
            state: Some(state),
            result: None,
            mode: ExecutionSessionMode::CollectedReady,
        })
    }

    fn demand_groups(&self) -> PyResult<Vec<(usize, usize)>> {
        if self.mode != ExecutionSessionMode::CollectedReady {
            return Err(PyRuntimeError::new_err(
                "native collected execution is not ready for partitioning",
            ));
        }
        let state = self.state.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("native collected execution has no compiled state")
        })?;
        let runtime = state.runtime.lock();
        Ok(collected_demand_groups(&runtime.suite))
    }

    #[pyo3(signature = (worker, *, maxfail = 0, initial_failures = 0))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 keeps the worker borrowed during execution"
    )]
    fn execute_collected(
        &mut self,
        py: Python<'_>,
        worker: PyRef<'_, NativeParallelWorker>,
        maxfail: usize,
        initial_failures: usize,
    ) -> PyResult<NativeRunResult> {
        if self.mode != ExecutionSessionMode::CollectedReady || worker.demand_cursor().is_none() {
            return Err(PyRuntimeError::new_err(
                "native collected execution requires an unused snapshot and demand worker",
            ));
        }
        let state = self.state.as_mut().ok_or_else(|| {
            PyRuntimeError::new_err("native collected execution has no compiled state")
        })?;
        self.mode = ExecutionSessionMode::CollectedRunning;
        worker.ready_and_wait(py)?;
        accept_native_execution(py, state)?;
        let mut result = execute_native_session(
            py,
            state,
            ExecutionSchedule::Suite,
            maxfail,
            initial_failures,
            Some(&worker),
        )?;
        result.restrict_collected_to_claims(worker.assigned_count())?;
        // Retain the inherited bindings until child os._exit, avoiding N unrelated decrefs.
        Ok(result)
    }

    #[pyo3(signature = (index, next_index, *, maxfail = 0, initial_failures = 0))]
    fn execute_one(
        &mut self,
        py: Python<'_>,
        index: usize,
        next_index: Option<usize>,
        maxfail: usize,
        initial_failures: usize,
    ) -> PyResult<NativeRunResult> {
        let state = self.protocol_state(index, next_index)?;
        execute_native_session(
            py,
            state,
            ExecutionSchedule::Case {
                index,
                next: next_index,
            },
            maxfail,
            initial_failures,
            None,
        )
    }

    #[pyo3(signature = (*, maxfail = 0, initial_failures = 0))]
    fn execute_suite(
        &mut self,
        py: Python<'_>,
        maxfail: usize,
        initial_failures: usize,
    ) -> PyResult<Vec<(String, Py<PyAny>)>> {
        if self.mode != ExecutionSessionMode::Protocol || self.result.is_some() {
            return Err(PyRuntimeError::new_err(
                "native whole-session execution requires an unused protocol session",
            ));
        }
        let state = self.state.as_mut().ok_or_else(|| {
            PyRuntimeError::new_err("native pytest execution session is finished")
        })?;
        if !state.cases.is_empty() {
            return Err(PyRuntimeError::new_err(
                "native whole-session execution cannot follow individual dispatches",
            ));
        }
        execute_native_session_output(
            py,
            state,
            ExecutionRequest {
                schedule: ExecutionSchedule::Protocols,
                output: ExecutionOutput::PhaseWarnings,
            },
            maxfail,
            initial_failures,
            None,
        )?;
        Ok(state.warning_service.take_messages())
    }

    #[pyo3(signature = (index, next_index, *, maxfail = 0, initial_failures = 0))]
    fn execute_protocol(
        &mut self,
        py: Python<'_>,
        index: usize,
        next_index: Option<usize>,
        maxfail: usize,
        initial_failures: usize,
    ) -> PyResult<Vec<(String, Py<PyAny>)>> {
        let state = self.protocol_state(index, next_index)?;
        execute_native_session_output(
            py,
            state,
            ExecutionRequest {
                schedule: ExecutionSchedule::Case {
                    index,
                    next: next_index,
                },
                output: ExecutionOutput::PhaseWarnings,
            },
            maxfail,
            initial_failures,
            None,
        )?;
        Ok(state.warning_service.take_messages())
    }

    fn take_warning_messages(&mut self) -> Vec<(String, Py<PyAny>)> {
        self.state
            .as_mut()
            .map_or_else(Vec::new, |state| state.warning_service.take_messages())
    }

    fn finish(&mut self, py: Python<'_>) -> PyResult<Py<NativeRunResult>> {
        if self.mode != ExecutionSessionMode::Protocol {
            return Err(PyRuntimeError::new_err(
                "native collected execution is finalized by its whole-session dispatch",
            ));
        }
        if let Some(result) = &self.result {
            return Ok(result.clone_ref(py));
        }
        let state = self.state.as_mut().ok_or_else(|| {
            PyRuntimeError::new_err("native pytest execution session has already ended")
        })?;
        let mut cleanup = execute_native_session(py, state, ExecutionSchedule::Finish, 0, 0, None)?;
        let warning_messages = cleanup.take_warning_messages();
        let state = self.state.take().ok_or_else(|| {
            PyRuntimeError::new_err("native pytest execution session disappeared during cleanup")
        })?;
        let locations = state.report_locations.as_ref().map(|locations| {
            state
                .case_indices
                .iter()
                .filter_map(|index| locations.get(*index).cloned())
                .collect()
        });
        let record = RunRecord {
            internal_errors: Vec::new(),
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
            engine: "pytest-runner-native-v1".to_owned(),
            observations: None,
            exitstatus: None,
            execution_identity: None,
            collected: state.runtime.lock().suite.tests().len(),
            counts: compute_counts(&state.cases),
            duration_ms: state.started.elapsed().as_secs_f64() * 1_000.0,
            warnings: state.warning_service.into_records(),
            cases: state.cases,
        };
        let mut result = native_run_result(record, locations);
        result.set_warning_messages(warning_messages);
        result.set_html_extras(state.html_extras, state.html_errors);
        let result = Py::new(py, result)?;
        self.result = Some(result.clone_ref(py));
        Ok(result)
    }
}

impl NativeExecutionSession {
    fn protocol_state(
        &mut self,
        index: usize,
        next_index: Option<usize>,
    ) -> PyResult<&mut NativeExecutionState> {
        if self.mode != ExecutionSessionMode::Protocol {
            return Err(PyRuntimeError::new_err(
                "native collected execution requires whole-session dispatch",
            ));
        }
        let state = self.state.as_mut().ok_or_else(|| {
            PyRuntimeError::new_err("native pytest execution session is finished")
        })?;
        let collected = state.runtime.lock().suite.tests().len();
        if index >= collected || next_index.is_some_and(|next| next >= collected) {
            return Err(PyValueError::new_err(
                "native pytest dispatch index is outside the collected suite",
            ));
        }
        Ok(state)
    }
}

impl Drop for NativeExecutionSession {
    fn drop(&mut self) {
        if let Some(state) = self.state.take() {
            let _ = Python::try_attach(|py| teardown_all(py, &state.runtime));
        }
    }
}

pub(super) const DEMAND_INITIAL_CASES: usize = 16;

fn collected_demand_groups(suite: &SuiteBindings) -> Vec<(usize, usize)> {
    let tests = suite.tests();
    let fixtures = suite.fixtures();
    let mut wide_modules = HashSet::new();
    let mut wide_classes = HashSet::new();
    for test in tests {
        for name in test.eager_fixtures.iter() {
            let Some(chain) = test.fixture_chains.by_name.get(name) else {
                continue;
            };
            for depth in 0..chain.len() {
                let Some(use_) = test
                    .resolve_fixture(fixtures, name, depth)
                    .map(|value| value.use_)
                else {
                    continue;
                };
                match use_.cache_scope {
                    FixtureScope::Module => {
                        wide_modules.insert(test.owners.module.as_ref());
                    }
                    FixtureScope::Class => {
                        if let Some(owner) = test.scope_owner(FixtureScope::Class) {
                            wide_classes.insert(owner);
                        }
                    }
                    // A package fixture can fall back to the Session collector. Without
                    // a Python parent lookup, one suite domain preserves both lifetimes.
                    FixtureScope::Package => return vec![(0, tests.len())],
                    FixtureScope::Function | FixtureScope::Session => {}
                }
            }
        }
    }
    let mut groups = Vec::new();
    let mut start = 0;
    for end in 1..=tests.len() {
        if end == tests.len()
            || (end - start >= DEMAND_INITIAL_CASES
                && !shares_collected_scope_domain(
                    &tests[end - 1],
                    &tests[end],
                    &wide_modules,
                    &wide_classes,
                ))
        {
            groups.push((start, end));
            start = end;
        }
    }
    groups
}

fn shares_collected_scope_domain(
    left: &TestPlan,
    right: &TestPlan,
    wide_modules: &HashSet<&str>,
    wide_classes: &HashSet<&str>,
) -> bool {
    (left.owners.module == right.owners.module
        && wide_modules.contains(left.owners.module.as_ref()))
        || left.scope_owner(FixtureScope::Class).is_some_and(|owner| {
            right.scope_owner(FixtureScope::Class) == Some(owner) && wide_classes.contains(owner)
        })
}

pub(super) enum PytestReporter {
    Result(Py<PyAny>),
    Phase(Py<PyAny>),
}

#[derive(Clone, Copy)]
pub(super) enum ReportProtocol {
    Native,
    Pytest,
}

#[derive(Clone, Copy)]
#[expect(
    clippy::struct_excessive_bools,
    reason = "pytest independently controls capture, passing output, HTML extras, and report locations"
)]
struct NativeExecutionOptions {
    protocol: ReportProtocol,
    warning_filter_ownership: WarningFilterOwnership,
    capture: bool,
    retain_success_output: bool,
    retain_html_extras: bool,
    retain_report_locations: bool,
    retain_capture_report_locations: bool,
    capture_summary: CaptureSummary,
}

fn accept_native_execution(py: Python<'_>, state: &NativeExecutionState) -> PyResult<()> {
    if let Some(coverage) = &state.vfy_coverage {
        coverage.bind(py).call_method0("_native_accept")?;
    }
    if let Some(results) = &state.vfy_results {
        // Publish runtime failures only after the complete suite validates.
        results.bind(py).call_method0("_native_accept")?;
    }
    Ok(())
}

fn prepare_native_execution(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    vfy_coverage: Option<&Bound<'_, PyAny>>,
    vfy_results: Option<&Bound<'_, PyAny>>,
    options: NativeExecutionOptions,
) -> PyResult<NativeExecutionState> {
    let NativeExecutionOptions {
        protocol,
        warning_filter_ownership,
        capture,
        retain_success_output,
        retain_html_extras,
        retain_report_locations,
        retain_capture_report_locations,
        capture_summary,
    } = options;
    let started = Instant::now();
    let warning_setup_started = Instant::now();
    let mut warning_service = WarningService::new(py, items, warning_filter_ownership)?;
    if matches!(protocol, ReportProtocol::Pytest) {
        warning_service.retain_pytest_messages();
    }
    let mut logging_service = LoggingService::new(py, items)?;
    if matches!(protocol, ReportProtocol::Pytest) {
        logging_service.retain_pytest_sections();
    }
    let warning_setup_ms = warning_setup_started.elapsed().as_secs_f64() * 1_000.0;
    let compile_started = Instant::now();
    let mut suite = compile_suite(
        py,
        items,
        &mut warning_service,
        CompileOptions {
            track_vfy_coverage: vfy_coverage.is_some(),
            retain_passing_report_locations: retain_report_locations
                || (retain_success_output && retain_capture_report_locations),
        },
    )?;
    warning_service.finish_compilation();
    let compile_ms = compile_started.elapsed().as_secs_f64() * 1_000.0;
    let report_locations = suite.report_locations.take();
    let lifecycle_service = LifecycleService::new(items)?;
    let compatibility = PytestCompatibilityPolicy::from_items(py, items)?;
    let runtime = SharedRuntime::new(Runtime {
        suite,
        compatibility,
        cache: HashMap::new(),
        active: Vec::new(),
        active_by_fixture: HashMap::new(),
        pending_finalizers: HashMap::new(),
        owner_finalizers: HashMap::new(),
        active_nodes: Vec::new(),
        owner_nodes: HashMap::new(),
        package_setups: HashMap::new(),
        active_function: None,
        resolved_fixture_keys: HashMap::new(),
        retained_request_counts: HashMap::new(),
        resolving: Vec::new(),
        next_cache_generation: FixtureGeneration::default(),
    });
    Ok(NativeExecutionState {
        items: items.clone().unbind(),
        vfy_coverage: vfy_coverage.map(|value| value.clone().unbind()),
        vfy_results: vfy_results.map(|value| value.clone().unbind()),
        pytest_reporter: None,
        pytest_protocol_reporter: None,
        runtime,
        owner_nodes_buffer: Some(HashMap::new()),
        warning_service,
        logging_service,
        lifecycle_service,
        report_locations,
        capture_summary,
        summary_locations: BTreeMap::new(),
        cases: Vec::new(),
        case_indices: Vec::new(),
        last_test: None,
        unfinished_setup: None,
        html_extras: BTreeMap::new(),
        html_errors: BTreeMap::new(),
        started,
        compile_ms,
        warning_setup_ms,
        capture,
        retain_success_output,
        retain_html_extras,
    })
}

#[pyfunction(signature = (items, capture = true, maxfail = 0, initial_failures = 0, vfy_coverage = None, vfy_results = None, *, retain_success_output = false, parallel_worker = None, retain_html_extras = false, retain_report_locations = false, retain_capture_report_locations = true, capture_summary = ""))]
#[expect(
    clippy::too_many_arguments,
    clippy::fn_params_excessive_bools,
    reason = "the Python-visible keywords keep one argument per independent capture and report policy"
)]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 owns the optional worker handle at the execution boundary"
)]
pub(crate) fn run_native_suite(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    capture: bool,
    maxfail: usize,
    initial_failures: usize,
    vfy_coverage: Option<&Bound<'_, PyAny>>,
    vfy_results: Option<&Bound<'_, PyAny>>,
    retain_success_output: bool,
    parallel_worker: Option<PyRef<'_, NativeParallelWorker>>,
    retain_html_extras: bool,
    retain_report_locations: bool,
    retain_capture_report_locations: bool,
    capture_summary: &str,
) -> PyResult<NativeRunResult> {
    let mut state = prepare_native_execution(
        py,
        items,
        vfy_coverage,
        vfy_results,
        NativeExecutionOptions {
            protocol: ReportProtocol::Native,
            warning_filter_ownership: WarningFilterOwnership::Native,
            capture,
            retain_success_output,
            retain_html_extras,
            retain_report_locations,
            retain_capture_report_locations,
            capture_summary: CaptureSummary::from_reportchars(capture_summary),
        },
    )?;
    if let Some(worker) = &parallel_worker {
        worker.ready_and_wait(py)?;
    }
    accept_native_execution(py, &state)?;
    execute_native_session(
        py,
        &mut state,
        ExecutionSchedule::Suite,
        maxfail,
        initial_failures,
        parallel_worker.as_deref(),
    )
}

#[derive(Clone, Copy)]
pub(super) enum ExecutionOutput {
    Snapshot,
    PhaseWarnings,
}

#[derive(Clone, Copy)]
pub(super) struct ExecutionRequest {
    pub(super) schedule: ExecutionSchedule,
    pub(super) output: ExecutionOutput,
}

fn execute_native_session(
    py: Python<'_>,
    state: &mut NativeExecutionState,
    schedule: ExecutionSchedule,
    maxfail: usize,
    initial_failures: usize,
    parallel_worker: Option<&NativeParallelWorker>,
) -> PyResult<NativeRunResult> {
    execute_native_session_output(
        py,
        state,
        ExecutionRequest {
            schedule,
            output: ExecutionOutput::Snapshot,
        },
        maxfail,
        initial_failures,
        parallel_worker,
    )?
    .ok_or_else(|| PyRuntimeError::new_err("native result execution returned no snapshot"))
}

pub(super) fn retain_session_cleanup_failure(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    error: PyErr,
) -> PyResult<()> {
    let first = items
        .try_iter()?
        .next()
        .transpose()?
        .ok_or_else(|| PyRuntimeError::new_err("native fixture cleanup has no pytest item"))?;
    if let Err(retention_error) = py.import("vfy._pytest.native_pytest")?.call_method1(
        "retain_session_cleanup_error",
        (first.getattr("config")?, error.value(py)),
    ) {
        retention_error.set_cause(py, Some(error));
        return Err(retention_error);
    }
    Ok(())
}

pub(super) fn require_serial_protocol_recovery(
    worker: Option<&NativeParallelWorker>,
) -> PyResult<()> {
    if worker.is_some() {
        return Err(UnsupportedFeatureError::new_err(
            "native parallel workers cannot continue after a pytest_runtest_protocol wrapper \
             suppressed a protocol error; use --workers=1",
        ));
    }
    Ok(())
}
