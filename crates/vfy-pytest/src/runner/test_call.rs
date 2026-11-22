//! Owns test body invocation, call outcome classification, and phase publication.

use super::capture::{
    CaptureBuffers, finish_capture, start_capture, uses_capture_fixture, with_uncaptured_output,
};
use super::compile::{case_item, prepare_case_callable};
use super::fixture_lookup::refresh_xfail;
use super::model::{XfailPlan, asyncio_mode, test_async_loop_scope};
use super::native_session::PytestReporter;
use super::report::{
    PhaseClock, PhaseTiming, canonical_pytest_exception, check_test_result,
    format_skip_diagnostic_with_location, handle_unhandled_async_test, is_control_error,
    is_skip_error, is_xfail_error, normalize_unittest_skip, phase_result, record_pytest_capture,
    reject_anyio_test, render_item_failure, take_unittest_exception, xfail_matches_error,
};
use super::runtime::SharedRuntime;
use super::skipping_plugin_enabled;
use super::subtest::NativeSubtestCollector;
use super::test_setup::{
    ItemRequestCleanup, PreparedTestCall, TestExecutionPlan, TestPhaseServices, TestSetupOutcome,
    execute_async_test, live_function_arguments, prepare_test_call, test_execution_plan,
};
use crate::lifecycle::{LifecycleService, PhaseAnchor};
use crate::logging::{LoggingService, PhaseCompletion};
use crate::results::{native_run_result, pytest_phase_report};
use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyErr, PyResult, Python};
use pyo3::types::{PyAny, PyDict, PyFunction};
use std::cell::Cell;
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Instant;
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, ExecutionPhase, PhaseResult, RunRecord, SkipLocation,
    SubtestResult, compute_counts,
};
use vfy_core::test_plan::TestBody;

fn publish_unittest_xfail(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    xfail: Option<&XfailPlan>,
) -> PyResult<()> {
    // The retained builtin addSubTest dispatches makereport, whose skipping
    // wrapper reads this stash instead of reevaluating the parent marker.
    let skipping = py.import("_pytest.skipping")?;
    let upstream_xfail = match xfail {
        Some(xfail) => skipping
            .getattr("Xfail")?
            .call1((
                &xfail.reason,
                xfail.run,
                xfail.strict,
                xfail
                    .raises
                    .as_ref()
                    .map_or_else(|| py.None(), |raises| raises.clone_ref(py)),
            ))?
            .unbind(),
        None => py.None(),
    };
    item.getattr("stash")?
        .set_item(skipping.getattr("xfailed_key")?, upstream_xfail)
}

fn invoke_unittest_test(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    callable: &Py<PyAny>,
    xfail: Option<&XfailPlan>,
    subtests: &mut Vec<SubtestResult>,
) -> PyResult<()> {
    if !item.hasattr("addSubTest")? {
        callable.bind(py).call0()?;
        return match take_unittest_exception(item)? {
            Some(error) => Err(error),
            None => Ok(()),
        };
    }
    // Both the retained makereport hook and native classification consume each
    // subtest's match verdict, without calling user checks twice. The parent
    // call retains its original matcher.
    let mut subtest_xfail = xfail.map(|xfail| xfail.clone_ref(py));
    let reset_xfail_match = if let Some(xfail) = &mut subtest_xfail
        && let Some(raises) = &xfail.raises
    {
        let (matcher, reset) = py
            .import("vfy._pytest.native_pytest")?
            .call_method1("subtest_xfail_matcher", (raises.bind(py),))?
            .extract::<(Py<PyAny>, Option<Py<PyAny>>)>()?;
        xfail.raises = Some(matcher);
        reset
    } else {
        None
    };
    publish_unittest_xfail(py, item, subtest_xfail.as_ref())?;
    let original = item.getattr("addSubTest")?.unbind();
    let report_callback = py
        .import("vfy._pytest.native_pytest")?
        .call_method1(
            "unittest_subtest_report_callback",
            (item.getattr("config")?, original.bind(py)),
        )?
        .unbind();
    let collector = Py::new(
        py,
        NativeSubtestCollector {
            item: item.clone().unbind(),
            original: report_callback,
            xfail: subtest_xfail,
            reset_xfail_match,
            reports: Mutex::new(Vec::new()),
        },
    )?;
    let add_subtest = collector.bind(py).getattr("addSubTest")?;
    item.setattr("addSubTest", add_subtest)?;
    let result = callable.bind(py).call0().map(|_| ());
    let restore = item.setattr("addSubTest", original.bind(py));
    let restore_xfail = publish_unittest_xfail(py, item, xfail);
    subtests.extend(collector.borrow(py).take_reports());
    if let Err(error) = result {
        let _ = restore;
        let _ = restore_xfail;
        return Err(error);
    }
    restore?;
    restore_xfail?;
    match take_unittest_exception(item)? {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

pub(super) fn invoke_test_body(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    item: &Bound<'_, PyAny>,
    kwargs: Option<&Bound<'_, PyDict>>,
    plan: &TestExecutionPlan,
    subtests: &mut Vec<SubtestResult>,
) -> PyResult<()> {
    clear_last_exception(py)?;
    let result = run_test_body(py, runtime, test_index, item, kwargs, plan, subtests);
    if let Err(error) = &result {
        store_last_exception(py, error)?;
    }
    result
}

fn run_test_body(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    item: &Bound<'_, PyAny>,
    kwargs: Option<&Bound<'_, PyDict>>,
    plan: &TestExecutionPlan,
    subtests: &mut Vec<SubtestResult>,
) -> PyResult<()> {
    match plan.body {
        TestBody::Unittest => {
            let callable = item.getattr(intern!(py, "runtest"))?.unbind();
            return invoke_unittest_test(py, item, &callable, plan.xfail.as_ref(), subtests);
        }
        TestBody::Doctest | TestBody::Item => {
            return item.call_method0(intern!(py, "runtest")).map(|_| ());
        }
        TestBody::Function => {}
    }
    let live_kwargs = if plan.live_arguments {
        Some(live_function_arguments(item)?)
    } else {
        None
    };
    let kwargs = live_kwargs
        .as_ref()
        .map(|kwargs| kwargs.bind(py))
        .or(kwargs);
    // Fixture setup can replace the test or its code. Read the live item here,
    // after setup, and reuse asyncio's code-aware callable classification.
    let callable = item.getattr(intern!(py, "obj"))?;
    let config = item.getattr(intern!(py, "config"))?;
    let asyncio = py.import("vfy.asyncio_plugin")?;
    asyncio.call_method1(intern!(py, "warn_asyncio_test"), (item,))?;
    let kind: String = asyncio
        .call_method1(intern!(py, "_callable_kind"), (&config, &callable))?
        .extract()?;
    if kind == "coroutine" {
        if !item
            .call_method1(intern!(py, "get_closest_marker"), ("anyio",))?
            .is_none()
            || item
                .getattr(intern!(py, "fixturenames"))?
                .contains("anyio_backend")?
        {
            return reject_anyio_test(py);
        }
        // Auto mode adds the marker during collection. A coroutine introduced
        // by setup must still enter the same marked adapter as stock pytest.
        if !item
            .call_method1(intern!(py, "get_closest_marker"), ("asyncio",))?
            .is_none()
            && let Some(scope) = test_async_loop_scope(item, None, &config, asyncio_mode(&config)?)?
        {
            let kwargs = kwargs.cloned().unwrap_or_else(|| PyDict::new(py));
            return execute_async_test(py, runtime, test_index, scope, &callable.unbind(), &kwargs);
        }
        return handle_unhandled_async_test(py, &plan.nodeid);
    }
    let returned_test_value = runtime.lock().compatibility.returned_test_value;
    match kwargs {
        Some(kwargs) => callable.call((), Some(kwargs)),
        None if callable.is_exact_instance_of::<PyFunction>() => callable.call0(),
        // Opaque C callables can distinguish NULL kwargs from an empty dict.
        None => callable.call((), Some(&PyDict::new(py))),
    }
    .and_then(|result| check_test_result(py, &result, &plan.nodeid, returned_test_value))
}

struct TestCallContext<'a, 'py> {
    runtime: &'a SharedRuntime,
    test_index: usize,
    capture_buffers: &'a mut Option<CaptureBuffers>,
    logging_service: &'a mut LoggingService,
    lifecycle_service: &'a LifecycleService,
    pytest_reporter: Option<&'a PytestReporter>,
    coverage: Option<&'a Bound<'py, PyAny>>,
    plan: &'a mut TestExecutionPlan,
    item: &'a Bound<'py, PyAny>,
    kwargs: Option<&'a Bound<'py, PyDict>>,
}

struct TestCallResult {
    result: PyResult<()>,
    stdout: String,
    stderr: String,
    log: String,
    timing: PhaseTiming,
    subtests: Vec<SubtestResult>,
}

pub(super) fn enforce_xfail_run(py: Python<'_>, xfail: Option<&XfailPlan>) -> PyResult<()> {
    if let Some(xfail) = xfail
        && !xfail.run
    {
        py.import("_pytest.outcomes")?
            .getattr("xfail")?
            .call1((format!("[NOTRUN] {}", xfail.reason),))?;
    }
    Ok(())
}

fn execute_test_call(py: Python<'_>, context: TestCallContext<'_, '_>) -> TestCallResult {
    let TestCallContext {
        runtime,
        test_index,
        capture_buffers,
        logging_service,
        lifecycle_service: lifecycle,
        pytest_reporter,
        coverage,
        plan,
        item,
        kwargs,
    } = context;
    let call_started = PhaseClock::start();
    let mut subtests = Vec::new();
    let completion = logging_service.with_phase(py, item, "call", || {
        let capture_state = start_capture(
            py,
            capture_buffers,
            uses_capture_fixture(runtime, test_index),
        )?;
        if plan.live_arguments && coverage.is_some() {
            plan.no_cover = !item
                .call_method1("get_closest_marker", ("no_cover",))?
                .is_none()
                || item
                    .getattr("fixturenames")?
                    .extract::<Vec<String>>()?
                    .iter()
                    .any(|name| name == "no_cover");
        }
        if let Some(coverage) = coverage
            && let Err(error) = coverage.call_method1(
                intern!(py, "_native_phase"),
                (&plan.nodeid, "run", plan.no_cover),
            )
        {
            if plan.no_cover {
                let _ = coverage.call_method1(intern!(py, "_native_phase_complete"), ("run",));
            }
            let _ = finish_capture(
                py,
                capture_buffers,
                capture_state,
                uses_capture_fixture(runtime, test_index),
            );
            return Err(error);
        }
        let mut result = lifecycle.with_phase(item, ExecutionPhase::Call, |phase_context| {
            plan.xfail = refresh_xfail(runtime, test_index, item, lifecycle, ExecutionPhase::Call)?;
            enforce_xfail_run(py, plan.xfail.as_ref())?;
            phase_context.before(item, PhaseAnchor::Runner)?;
            lifecycle.update_current_test(item, Some("call"))?;
            invoke_test_body(py, runtime, test_index, item, kwargs, plan, &mut subtests)
        });
        match refresh_xfail(runtime, test_index, item, lifecycle, ExecutionPhase::Call) {
            Ok(xfail) => plan.xfail = xfail,
            Err(error) => result = Err(error),
        }
        let resume_coverage = if plan.no_cover {
            coverage
                .map(|coverage| {
                    coverage.call_method1(intern!(py, "_native_phase_complete"), ("run",))
                })
                .transpose()
        } else {
            Ok(None)
        };
        let capture = finish_capture(
            py,
            capture_buffers,
            capture_state,
            uses_capture_fixture(runtime, test_index),
        );
        let (stdout, stderr) = capture?;
        if pytest_reporter.is_some() {
            record_pytest_capture(item, ExecutionPhase::Call, &stdout, &stderr)?;
        }
        resume_coverage?;
        Ok((result, stdout, stderr))
    });
    let ((result, stdout, stderr), log) = match completion {
        PhaseCompletion::Completed { value, log, error } => {
            let (result, stdout, stderr) = value;
            ((error.map_or(result, Err), stdout, stderr), log)
        }
        PhaseCompletion::NotStarted(error) | PhaseCompletion::Interrupted(error) => {
            ((Err(error), String::new(), String::new()), String::new())
        }
    };
    TestCallResult {
        result,
        stdout,
        stderr,
        log,
        timing: call_started.finish(),
        subtests,
    }
}

/// Clear the `sys.last_*` failure that pytest's call phase resets.
fn clear_last_exception(py: Python<'_>) -> PyResult<()> {
    let sys = py.import("sys")?;
    for name in ["last_type", "last_value", "last_traceback", "last_exc"] {
        if sys.hasattr(name)? {
            sys.delattr(name)?;
        }
    }
    Ok(())
}

/// Store a failing call for post-mortem debugging, as pytest's call phase does.
///
/// The stored traceback keeps the failing frames, and the objects they
/// reference, alive until the next test call, as under pytest.
fn store_last_exception(py: Python<'_>, error: &PyErr) -> PyResult<()> {
    // Pytest's skip, fail, and exit outcomes derive from BaseException.
    if !error.is_instance_of::<pyo3::exceptions::PyException>(py) {
        return Ok(());
    }
    let sys = py.import("sys")?;
    let value = error.value(py);
    sys.setattr("last_type", value.get_type())?;
    sys.setattr("last_value", value)?;
    sys.setattr("last_exc", value)?;
    sys.setattr("last_traceback", value.getattr("__traceback__")?)?;
    Ok(())
}

pub(super) struct CallOutcome {
    pub(super) outcome: ExecutionOutcome,
    pub(super) detail: String,
    pub(super) terminal_lead: String,
    pub(super) exception: Option<String>,
    pub(super) skip_location: Option<SkipLocation>,
    representation: Option<Py<PyAny>>,
}

pub(super) fn classify_call_result(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    result: PyResult<()>,
    xfail: Option<&XfailPlan>,
) -> PyResult<CallOutcome> {
    let mut terminal_lead = String::new();
    let (outcome, detail, exception, skip_location, representation) = match result {
        Ok(()) => {
            if let Some(xfail) = xfail {
                let outcome = if xfail.strict {
                    ExecutionOutcome::Failed
                } else {
                    ExecutionOutcome::XPassed
                };
                let detail = if xfail.strict {
                    format!("[XPASS(strict)] {}", xfail.reason)
                } else {
                    xfail.reason.clone()
                };
                let exception = xfail.strict.then(|| detail.clone());
                (outcome, detail, exception, None, None)
            } else {
                (ExecutionOutcome::Passed, String::new(), None, None, None)
            }
        }
        Err(error) => {
            if is_control_error(py, &error) {
                return Err(error);
            }
            let error = normalize_unittest_skip(py, item, &error)?;
            let (outcome, expected_xfail_reason) =
                if is_xfail_error(py, &error) && skipping_plugin_enabled(item)? {
                    (ExecutionOutcome::XFailed, None)
                } else if is_skip_error(py, &error) {
                    (ExecutionOutcome::Skipped, None)
                } else if let Some(xfail) = xfail {
                    if xfail_matches_error(py, &error, xfail)? {
                        (ExecutionOutcome::XFailed, Some(xfail.reason.as_str()))
                    } else {
                        (ExecutionOutcome::Failed, None)
                    }
                } else {
                    (ExecutionOutcome::Failed, None)
                };
            let (detail, exception, skip_location, representation) =
                if outcome == ExecutionOutcome::Skipped {
                    let skipped = format_skip_diagnostic_with_location(py, item, &error)?;
                    (
                        skipped.detail,
                        Some(skipped.exception),
                        skipped.location,
                        None,
                    )
                } else {
                    let formatted = render_item_failure(py, item, &error)?;
                    let exception = canonical_pytest_exception(
                        py,
                        item,
                        &error,
                        outcome,
                        expected_xfail_reason,
                        Some(&formatted),
                    )?;
                    terminal_lead = formatted.terminal_lead;
                    (formatted.detail, exception, None, formatted.representation)
                };
            (outcome, detail, exception, skip_location, representation)
        }
    };
    Ok(CallOutcome {
        outcome,
        detail,
        terminal_lead,
        exception,
        skip_location,
        representation,
    })
}

struct CallPhase {
    phase: PhaseResult,
    subtests: Vec<SubtestResult>,
    timing: PhaseTiming,
    representation: Option<Py<PyAny>>,
}

fn call_result_phase(
    py: Python<'_>,
    plan: &TestExecutionPlan,
    item: &Bound<'_, PyAny>,
    call: TestCallResult,
) -> PyResult<CallPhase> {
    let TestCallResult {
        result,
        stdout,
        stderr,
        log,
        timing,
        subtests,
    } = call;
    let report = classify_call_result(py, item, result, plan.xfail.as_ref())?;
    let mut phase = phase_result(
        report.outcome,
        ExecutionPhase::Call,
        timing,
        stdout,
        stderr,
        report.detail,
    );
    phase.set_exception(report.exception);
    phase.set_log(log);
    phase.set_skip_location(report.skip_location);
    phase.set_terminal_lead(report.terminal_lead);
    Ok(CallPhase {
        phase,
        subtests,
        timing,
        representation: report.representation,
    })
}

pub(super) struct ExecutedTest {
    pub(super) case: CaseResult,
    pub(super) stop_requested: bool,
}

#[derive(Clone, Copy)]
pub(super) struct PhaseHtml<'a> {
    pub(super) extras: &'a BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    pub(super) errors: &'a BTreeMap<String, String>,
}

#[derive(Clone, Copy)]
pub(super) struct PhasePublication<'a> {
    pub(super) case: &'a CaseResult,
    pub(super) timing: PhaseTiming,
    pub(super) buffers: Option<&'a CaptureBuffers>,
    pub(super) html: Option<PhaseHtml<'a>>,
    pub(super) representation: Option<&'a Py<PyAny>>,
}

/// Emit the last completed phase through the same Rust report projection used by batches.
pub(super) fn publish_pytest_phase(
    py: Python<'_>,
    reporter: Option<&PytestReporter>,
    publication: PhasePublication<'_>,
) -> PyResult<bool> {
    let reporter = match reporter {
        None => return Ok(false),
        Some(PytestReporter::Phase(reporter)) => {
            let phase =
                publication.case.phases.last().ok_or_else(|| {
                    PyValueError::new_err("native result contains no pytest phase")
                })?;
            return with_uncaptured_output(py, publication.buffers, || {
                reporter
                    .bind(py)
                    .call1((
                        pytest_phase_report(
                            py,
                            &publication.case.nodeid,
                            phase,
                            Vec::new(),
                            publication.representation,
                        )?,
                        publication.timing.start,
                        publication.timing.stop,
                    ))?
                    .extract()
            });
        }
        Some(PytestReporter::Result(reporter)) => reporter,
    };
    let cases = vec![publication.case.clone()];
    let record = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: compute_counts(&cases),
        duration_ms: publication.case.duration_ms,
        warnings: Vec::new(),
        cases,
    };
    let mut result = native_run_result(record, None);
    result.set_last_phase_representation(
        publication
            .representation
            .map(|representation| representation.clone_ref(py)),
    );
    if let Some(html) = publication.html {
        let nodeid = &publication.case.nodeid;
        let extras = html
            .extras
            .get_key_value(nodeid)
            .map(|(key, value)| (key.clone(), value.clone()));
        let errors = html
            .errors
            .get_key_value(nodeid)
            .map(|(key, value)| (key.clone(), value.clone()));
        result.set_html_extras(extras.into_iter().collect(), errors.into_iter().collect());
    }
    with_uncaptured_output(py, publication.buffers, || {
        reporter
            .bind(py)
            .call1((
                Py::new(py, result)?,
                publication.timing.start,
                publication.timing.stop,
            ))?
            .extract()
    })
}

pub(super) fn run_test<'a>(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    capture_buffers: &mut Option<CaptureBuffers>,
    mut services: TestPhaseServices<'_>,
    vfy_coverage: Option<&Bound<'_, PyAny>>,
    request_cleanup_error: &'a Cell<Option<PyErr>>,
) -> PyResult<(ExecutedTest, ItemRequestCleanup<'a>)> {
    let started = Instant::now();
    prepare_case_callable(py, runtime, test_index)?;
    let request_cleanup = ItemRequestCleanup::new(
        py,
        case_item(py, runtime, test_index)?,
        capture_buffers.as_ref(),
        request_cleanup_error,
    )?;
    let mut plan = test_execution_plan(py, runtime, test_index);
    let PreparedTestCall {
        item,
        kwargs,
        setup_phase,
        setup_timing,
    } = match prepare_test_call(
        py,
        runtime,
        test_index,
        capture_buffers,
        &mut services,
        &mut plan,
        started,
    )? {
        TestSetupOutcome::Prepared(prepared) => prepared,
        TestSetupOutcome::Failed(failed) => {
            let executed =
                failed.publish(py, services.pytest_reporter, capture_buffers.as_ref())?;
            return Ok((executed, request_cleanup));
        }
    };
    let mut phases = Vec::with_capacity(3);
    phases.push(setup_phase);
    let mut case = CaseResult::new(
        plan.nodeid.clone(),
        started.elapsed().as_secs_f64() * 1_000.0,
        phases,
    );
    publish_pytest_phase(
        py,
        services.pytest_reporter,
        PhasePublication {
            case: &case,
            timing: setup_timing,
            buffers: capture_buffers.as_ref(),
            html: None,
            representation: None,
        },
    )?;
    let call = execute_test_call(
        py,
        TestCallContext {
            runtime,
            test_index,
            capture_buffers,
            logging_service: services.logging,
            lifecycle_service: services.lifecycle,
            pytest_reporter: services.pytest_reporter,
            coverage: vfy_coverage,
            plan: &mut plan,
            item: item.bind(py),
            kwargs: kwargs.as_ref().map(|kwargs| kwargs.bind(py)),
        },
    );
    let CallPhase {
        phase: call_phase,
        subtests,
        timing: call_timing,
        representation,
    } = call_result_phase(py, &plan, item.bind(py), call)?;
    case.phases.push(call_phase);
    case.set_subtests(subtests.into_boxed_slice());
    case.duration_ms = started.elapsed().as_secs_f64() * 1_000.0;
    let stop_requested = publish_pytest_phase(
        py,
        services.pytest_reporter,
        PhasePublication {
            case: &case,
            timing: call_timing,
            buffers: capture_buffers.as_ref(),
            html: None,
            representation: representation.as_ref(),
        },
    )?;
    Ok((
        ExecutedTest {
            case,
            stop_requested,
        },
        request_cleanup,
    ))
}
