//! Owns the per-session test execution loop.

use super::capture::{
    close_capture_buffers, create_capture_buffers, finish_capture, start_capture,
    uses_capture_fixture, with_uncaptured_output,
};
use super::compile::{
    case_item, case_user_properties, case_xml_attributes, prepare_builtin_teardown,
};
use super::fixture_lookup::executed_case_report_location;
use super::native_session::{
    DEMAND_INITIAL_CASES, ExecutionOutput, ExecutionRequest, ExecutionSchedule,
    MAXFAIL_INITIAL_CASE_CAPACITY, NativeExecutionState, PartialReportSnapshot, ReportProtocol,
    io_error, report_native_partial, require_serial_protocol_recovery,
    retain_session_cleanup_failure,
};
use super::project_session_stopped;
use super::report::{
    PhaseClock, canonical_pytest_exception, discard_success_output,
    format_skip_diagnostic_with_location, item_head_line, normalize_teardown_skip, phase_result,
    record_pytest_capture, render_item_failure, reports_failure,
};
use super::request::SetupStateGuard;
use super::runtime::{StageTiming, TeardownFailure, TeardownGrouping};
use super::teardown::{
    plan_teardown_for_next, run_item_teardown, run_test_finalizers, teardown_all,
    teardown_failed_item, teardown_owner_nodes, teardown_report_outcome,
};
use super::test_call::{ExecutedTest, PhaseHtml, PhasePublication, publish_pytest_phase, run_test};
use super::test_setup::TestPhaseServices;
use crate::lifecycle::{PhaseAnchor, ProtocolContext};
use crate::logging::PhaseCompletion;
use crate::parallel::NativeParallelWorker;
use crate::results::{NativeRunResult, collect_html_extras, native_run_result, publish_html_case};
use pyo3::exceptions::{PyBaseExceptionGroup, PyRuntimeError};
use pyo3::intern;
use pyo3::prelude::{Bound, IntoPyObject, PyAnyMethods, PyErr, PyResult, Python};
use pyo3::types::PyList;
use std::cell::Cell;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::Instant;
use vfy_core::test_execution::{
    Counts, ExecutionOutcome, ExecutionPhase, RunRecord, compute_counts,
};
use vfy_core::test_plan::{TestBody, TestId};

#[expect(
    clippy::too_many_lines,
    reason = "setup, call, and teardown share cleanup state and partial reports"
)]
pub(super) fn execute_native_session_output(
    py: Python<'_>,
    state: &mut NativeExecutionState,
    request: ExecutionRequest,
    maxfail: usize,
    initial_failures: usize,
    parallel_worker: Option<&NativeParallelWorker>,
) -> PyResult<Option<NativeRunResult>> {
    let ExecutionRequest { schedule, output } = request;
    let NativeExecutionState {
        items,
        vfy_coverage,
        vfy_results,
        pytest_reporter,
        pytest_protocol_reporter,
        runtime,
        owner_nodes_buffer,
        warning_service,
        logging_service,
        lifecycle_service,
        report_locations,
        capture_summary,
        summary_locations,
        cases,
        case_indices,
        last_test,
        unfinished_setup: unfinished_setup_state,
        html_extras,
        html_errors,
        started,
        compile_ms,
        warning_setup_ms,
        capture,
        retain_success_output,
        retain_html_extras,
    } = state;
    let items = items.bind(py);
    let vfy_coverage = vfy_coverage.as_ref().map(|value| value.bind(py));
    let vfy_results = vfy_results.as_ref().map(|value| value.bind(py));
    let collected = runtime.lock().suite.tests().len();
    let capture = *capture;
    let retain_success_output = *retain_success_output;
    let retain_html_extras = *retain_html_extras;
    let retain_for_vfy = match vfy_results {
        Some(results) => results
            .call_method0("_native_retain_success_output")?
            .extract::<bool>()?,
        None => false,
    };
    let retain_case_output = retain_success_output
        || retain_for_vfy
        || std::env::var_os("PYTEST_NATIVE_RESULT").is_some_and(|path| !path.is_empty());
    let started = *started;
    let compile_ms = *compile_ms;
    let warning_setup_ms = *warning_setup_ms;
    let execute_started = Instant::now();
    let html_reporter = items
        .try_iter()?
        .next()
        .transpose()?
        .map(|item| {
            item.getattr(intern!(py, "config"))?
                .getattr(intern!(py, "pluginmanager"))?
                .call_method1(intern!(py, "getplugin"), ("_vfy_html_reporter",))
        })
        .transpose()?
        .filter(|reporter| !reporter.is_none());
    let streaming_html = match &html_reporter {
        Some(reporter) => reporter
            .getattr(intern!(py, "config"))?
            .call_method1(intern!(py, "getini"), ("generate_report_on_test",))?
            .is_truthy()?,
        None => false,
    };
    let collect_html_attachments = retain_html_extras || html_reporter.is_some();
    let mut published_html_cases = 0;
    let mut setup_state = SetupStateGuard::install(py, runtime)?;
    let mut progress = if matches!(schedule, ExecutionSchedule::Suite) && pytest_reporter.is_none()
    {
        crate::progress::Progress::new(items, collected)?
    } else {
        None
    };
    let capture_protocol = if schedule.enters_protocols() {
        ReportProtocol::Native
    } else {
        ReportProtocol::Pytest
    };
    let mut capture_buffers = create_capture_buffers(py, runtime, capture, capture_protocol)?;
    let initial_case_capacity = match schedule {
        ExecutionSchedule::Case { .. } => 1,
        ExecutionSchedule::Finish => 0,
        ExecutionSchedule::Suite
            if parallel_worker.is_some_and(|worker| worker.demand_cursor().is_some()) =>
        {
            collected.min(DEMAND_INITIAL_CASES)
        }
        ExecutionSchedule::Suite | ExecutionSchedule::Protocols if maxfail == 0 => collected,
        ExecutionSchedule::Suite | ExecutionSchedule::Protocols => {
            collected.min(MAXFAIL_INITIAL_CASE_CAPACITY)
        }
    };
    cases.reserve(initial_case_capacity);
    let first_case = cases.len();
    let first_warning = if schedule.enters_protocols() {
        0
    } else {
        warning_service.records().len()
    };
    let mut failure_count = initial_failures;
    macro_rules! report_partial {
        ($error:expr) => {
            report_native_partial(
                py,
                items,
                vfy_results,
                PartialReportSnapshot {
                    cases,
                    case_indices,
                    warnings: warning_service.records(),
                    report_locations: report_locations.as_deref(),
                    summary_locations,
                    html_extras,
                    html_errors,
                    collected,
                    started,
                },
                $error,
            );
        };
    }
    let mut active_test = None;
    let mut unfinished_setup = unfinished_setup_state.take();
    let request_cleanup_error = Cell::new(None);
    let execution = (|| -> PyResult<()> {
        let mut static_indices = schedule.indices(collected);
        let mut demand_cursor = parallel_worker.and_then(NativeParallelWorker::demand_cursor);
        let mut omitted_protocol_tail = false;
        loop {
            let dispatch = if let Some(cursor) = demand_cursor.as_mut() {
                cursor.next(py)?.map(|index| (index, None))
            } else {
                static_indices.next()
            };
            let Some((test_index, mut scheduled_next)) = dispatch else {
                break;
            };
            let case_index = cases.len();
            if let Some(worker) = &parallel_worker
                && !worker.claim_test()?
            {
                break;
            }
            // Publish only once the preceding case's teardown is complete. The
            // last case waits for suite cleanup, including --maxfail finalizers.
            if streaming_html
                && let Some(reporter) = &html_reporter
                && let Some(case) = cases.last()
            {
                publish_html_case(py, reporter, case, html_extras, html_errors)?;
                published_html_cases = cases.len();
            }
            let item = case_item(py, runtime, test_index)?;
            let mut protocol_context = ProtocolContext::default();
            let mut protocol_report_started = false;
            let test_result = (|| -> PyResult<bool> {
                let (nodeid, prior_profile) = {
                    let state = runtime.lock();
                    let test = &state.suite.tests()[test_index];
                    (
                        test.nodeid.clone(),
                        state.suite.test_bindings[test_index].warning_profile,
                    )
                };
                let (template, callspec_marks) = {
                    let state = runtime.lock();
                    let bindings = &state.suite.test_bindings[test_index];
                    (
                        bindings.item.clone_ref(py),
                        bindings.compact.as_ref().map(|compact| {
                            compact
                                .callspec
                                .marks
                                .iter()
                                .map(|mark| mark.clone_ref(py))
                                .collect::<Vec<_>>()
                        }),
                    )
                };
                let node_objects = template
                    .bind(py)
                    .call_method0(intern!(py, "listchain"))?
                    .try_iter()?
                    .map(|node| node.map(Bound::unbind))
                    .collect::<PyResult<Vec<_>>>()?;
                let mut nodes = node_objects
                    .iter()
                    .map(|node| {
                        node.bind(py)
                            .getattr(intern!(py, "nodeid"))?
                            .extract::<String>()
                    })
                    .collect::<PyResult<Vec<_>>>()?;
                if let Some(last) = nodes.last_mut() {
                    last.clone_from(&nodeid);
                }
                let mut profile = prior_profile;
                logging_service.start_test(py)?;
                if schedule.enters_protocols() {
                    // Demand workers may not know the next scheduled item yet.
                    let nextitem = scheduled_next
                        .map(|next| case_item(py, runtime, next))
                        .transpose()?;
                    let Some(context) = lifecycle_service.enter(
                        item.bind(py),
                        nextitem.as_ref().map(|next| next.bind(py)),
                        warning_service,
                        &mut profile,
                    )?
                    else {
                        require_serial_protocol_recovery(parallel_worker)?;
                        omitted_protocol_tail = true;
                        return Ok(false);
                    };
                    omitted_protocol_tail = false;
                    protocol_context = context;
                    if let Some(reporter) = pytest_protocol_reporter.as_ref() {
                        with_uncaptured_output(py, capture_buffers.as_ref(), || {
                            reporter.bind(py).call1((item.bind(py), "start"))
                        })?;
                        protocol_report_started = true;
                    }
                } else {
                    profile = warning_service.refresh_profile_for_case(
                        py,
                        template.bind(py),
                        prior_profile,
                        callspec_marks.as_deref(),
                    )?;
                    warning_service.enter_profile(py, &nodeid, profile)?;
                }
                active_test = Some(test_index);
                *last_test = Some(test_index);
                let mut pending_setup_nodes = None;
                let mut previous_nodes = {
                    let mut state = runtime.lock();
                    let mut owner_nodes = owner_nodes_buffer.take().unwrap_or_default();
                    owner_nodes.extend(nodes.iter().cloned().zip(node_objects));
                    state.suite.test_bindings[test_index].warning_profile = profile;
                    if unfinished_setup.is_some() {
                        owner_nodes
                    } else {
                        if lifecycle_service.has_project_phases() {
                            // Project setup callbacks run before pytest's runner activates owners.
                            pending_setup_nodes = Some(nodes);
                        } else {
                            state.active_nodes = nodes;
                            state.active_function = Some(TestId::from_index(test_index));
                        }
                        std::mem::replace(&mut state.owner_nodes, owner_nodes)
                    }
                };
                previous_nodes.clear();
                *owner_nodes_buffer = Some(previous_nodes);
                if let Some(progress) = progress.as_mut() {
                    if progress.start_writes() {
                        with_uncaptured_output(py, capture_buffers.as_ref(), || {
                            progress.start(py, &nodeid)
                        })?;
                    } else {
                        progress.start(py, &nodeid)?;
                    }
                }
                if let Some(coverage) = vfy_coverage
                    && let Err(error) = coverage
                        .call_method1(intern!(py, "_native_phase"), (&nodeid, "setup", false))
                {
                    return Err(error);
                }
                let mut setup_state_started = false;
                let (
                    ExecutedTest {
                        case,
                        stop_requested,
                    },
                    mut request_cleanup,
                ) = run_test(
                    py,
                    runtime,
                    test_index,
                    &mut capture_buffers,
                    TestPhaseServices {
                        logging: logging_service,
                        lifecycle: lifecycle_service,
                        setup_state_started: &mut setup_state_started,
                        previous_setup_unfinished: unfinished_setup.is_some(),
                        pending_setup_nodes,
                        pytest_reporter: pytest_reporter.as_ref(),
                    },
                    vfy_coverage,
                    &request_cleanup_error,
                )?;
                if let Some(coverage) = vfy_coverage
                    && let Err(error) = coverage
                        .call_method1(intern!(py, "_native_phase"), (&nodeid, "teardown", false))
                {
                    cases.push(case);
                    case_indices.push(test_index);
                    return Err(error);
                }
                cases.push(case);
                case_indices.push(test_index);
                // The call phase decides summary membership; record before teardown
                // so an interrupted run still renders its completed passing case.
                if report_locations.is_none() && capture_summary.includes(&cases[case_index]) {
                    summary_locations.insert(
                        case_index,
                        executed_case_report_location(py, runtime, test_index)?,
                    );
                }
                let pre_teardown_failures = cases[case_index].failure_count();
                let stop_after_call = stop_requested
                    || if maxfail == 0 {
                        false
                    } else if let Some(worker) = &parallel_worker {
                        worker.record_failures(pre_teardown_failures)?;
                        !worker.claim_test()?
                    } else {
                        failure_count.saturating_add(pre_teardown_failures) >= maxfail
                    };
                prepare_builtin_teardown(py, runtime, test_index, &cases[case_index])?;
                let teardown_started = PhaseClock::start();
                let teardown_item = case_item(py, runtime, test_index)?;
                let stop_after_call = stop_after_call
                    || (lifecycle_service.can_stop_session()
                        && project_session_stopped(item.bind(py))?);
                if !stop_after_call && let Some(cursor) = demand_cursor.as_mut() {
                    // Lease the real next case before determining broad fixture owner lifetimes.
                    scheduled_next = cursor.peek(py)?;
                }
                let mut teardown_core_started = false;
                let teardown =
                    logging_service.with_phase(py, teardown_item.bind(py), "teardown", || {
                        let teardown_capture = start_capture(
                            py,
                            &mut capture_buffers,
                            uses_capture_fixture(runtime, test_index),
                        )?;
                        let mut phase_context = lifecycle_service
                            .enter_phase(teardown_item.bind(py), ExecutionPhase::Teardown)?;
                        if phase_context.needs_nextitem() {
                            phase_context.set_nextitem(
                                if stop_after_call || scheduled_next.is_none() {
                                    None
                                } else {
                                    scheduled_next
                                        .map(|next| case_item(py, runtime, next))
                                        .transpose()?
                                },
                            );
                        }
                        let core = (|| -> PyResult<Option<TeardownFailure>> {
                            phase_context.before(teardown_item.bind(py), PhaseAnchor::Runner)?;
                            teardown_core_started = true;
                            lifecycle_service
                                .update_current_test(teardown_item.bind(py), Some("teardown"))?;
                            let body = runtime.lock().suite.tests()[test_index].body;
                            let mut teardown_error = if body == TestBody::Doctest {
                                run_item_teardown(py, runtime, test_index)?
                            } else {
                                None
                            };
                            teardown_error = merge_errors(
                                py,
                                teardown_error,
                                run_test_finalizers(py, runtime, test_index),
                            );
                            // A call/setup failure can stop the run before teardown begins.
                            // Unwind all owners together so xfail matching sees one exception group.
                            let next_test = if stop_after_call {
                                collected
                            } else {
                                scheduled_next.unwrap_or(collected)
                            };
                            let finished_owners = plan_teardown_for_next(py, runtime, next_test)?;
                            if body == TestBody::Unittest {
                                teardown_error = merge_errors(
                                    py,
                                    teardown_error,
                                    run_item_teardown(py, runtime, test_index)?,
                                );
                            }
                            teardown_error = merge_errors(
                                py,
                                teardown_error,
                                teardown_owner_nodes(py, runtime, finished_owners),
                            );
                            if teardown_error.is_none() {
                                lifecycle_service
                                    .update_current_test(teardown_item.bind(py), None)?;
                            }
                            Ok(teardown_error)
                        })();
                        let mut teardown_error = match core {
                            Ok(failure) => failure,
                            Err(error) => Some(TeardownFailure::from_pyerr(py, &error)),
                        };
                        if let Err(error) = phase_context.finish(
                            teardown_item.bind(py),
                            teardown_error.as_ref().and_then(|failure| {
                                failure.exception.as_ref().or(failure.control.as_ref())
                            }),
                        ) {
                            teardown_error = Some(TeardownFailure::from_pyerr(py, &error));
                        }
                        let (stdout, stderr) = finish_capture(
                            py,
                            &mut capture_buffers,
                            teardown_capture,
                            uses_capture_fixture(runtime, test_index),
                        )?;
                        if pytest_reporter.is_some() {
                            record_pytest_capture(
                                teardown_item.bind(py),
                                ExecutionPhase::Teardown,
                                &stdout,
                                &stderr,
                            )?;
                        }
                        let properties = case_user_properties(teardown_item.bind(py))?;
                        let attributes = case_xml_attributes(teardown_item.bind(py))?;
                        Ok((teardown_error, stdout, stderr, properties, attributes))
                    });
                let ((mut teardown_error, stdout, stderr, properties, attributes), log) =
                    match teardown {
                        PhaseCompletion::Completed { value, log, error } => {
                            let (failure, stdout, stderr, properties, attributes) = value;
                            unfinished_setup = if teardown_core_started || !setup_state_started {
                                None
                            } else {
                                Some(test_index)
                            };
                            (
                                (
                                    error.as_ref().map_or(failure, |error| {
                                        Some(TeardownFailure::from_pyerr(py, error))
                                    }),
                                    stdout,
                                    stderr,
                                    properties,
                                    attributes,
                                ),
                                log,
                            )
                        }
                        PhaseCompletion::NotStarted(error)
                        | PhaseCompletion::Interrupted(error) => {
                            if setup_state_started {
                                unfinished_setup = Some(test_index);
                            }
                            (
                                (
                                    Some(TeardownFailure::from_pyerr(py, &error)),
                                    String::new(),
                                    String::new(),
                                    case_user_properties(teardown_item.bind(py))?,
                                    case_xml_attributes(teardown_item.bind(py))?,
                                ),
                                String::new(),
                            )
                        }
                    };
                let teardown_timing = teardown_started.finish();
                normalize_teardown_skip(py, teardown_item.bind(py), &mut teardown_error)?;
                if let Some(error) = teardown_error
                    .as_mut()
                    .and_then(|failure| failure.control.take())
                {
                    return Err(error);
                }
                let teardown_representation = if let Some(result) = cases.last_mut() {
                    result.duration_ms += teardown_timing.duration_ms;
                    let (teardown_outcome, xfail_reason) =
                        teardown_report_outcome(py, runtime, test_index, teardown_error.as_ref())?;
                    let formatted = if teardown_outcome == ExecutionOutcome::Skipped {
                        None
                    } else {
                        teardown_error
                            .as_ref()
                            .and_then(|failure| failure.exception.as_ref())
                            .map(|error| render_item_failure(py, teardown_item.bind(py), error))
                            .transpose()?
                    };
                    let skipped = if teardown_outcome == ExecutionOutcome::Skipped {
                        teardown_error
                            .as_ref()
                            .and_then(|failure| failure.exception.as_ref())
                            .map(|error| {
                                format_skip_diagnostic_with_location(
                                    py,
                                    teardown_item.bind(py),
                                    error,
                                )
                            })
                            .transpose()?
                    } else {
                        None
                    };
                    let exception = if let Some(skipped) = skipped.as_ref() {
                        Some(skipped.exception.clone())
                    } else {
                        teardown_error
                            .as_ref()
                            .and_then(|failure| failure.exception.as_ref())
                            .map(|error| {
                                canonical_pytest_exception(
                                    py,
                                    teardown_item.bind(py),
                                    error,
                                    teardown_outcome,
                                    xfail_reason.as_deref(),
                                    formatted.as_ref(),
                                )
                            })
                            .transpose()?
                            .flatten()
                    };
                    let mut terminal_lead = String::new();
                    let (detail, skip_location, representation) =
                        match (skipped, formatted, teardown_error.as_ref()) {
                            (Some(skipped), _, _) => (skipped.detail, skipped.location, None),
                            (None, Some(failure), _) => {
                                terminal_lead = failure.terminal_lead;
                                (failure.detail, None, failure.representation)
                            }
                            (None, None, Some(failure)) => (failure.detail.clone(), None, None),
                            (None, None, None) => (String::new(), None, None),
                        };
                    let mut phase = phase_result(
                        teardown_outcome,
                        ExecutionPhase::Teardown,
                        teardown_timing,
                        stdout,
                        stderr,
                        detail,
                    );
                    phase.set_exception(exception);
                    phase.set_log(log);
                    phase.set_skip_location(skip_location);
                    phase.set_terminal_lead(terminal_lead);
                    result.phases.push(phase);
                    representation
                } else {
                    None
                };
                if reports_failure(&cases[case_index]) {
                    let head_line = item_head_line(teardown_item.bind(py))?;
                    cases[case_index].set_head_line(Some(head_line));
                }
                with_uncaptured_output(py, capture_buffers.as_ref(), || {
                    if let Some(progress) = progress.as_mut() {
                        progress.case(py, &cases[case_index])?;
                    }
                    if schedule.enters_protocols() {
                        request_cleanup.finish(py)
                    } else {
                        Ok(())
                    }
                })?;
                let mut state = runtime.lock();
                if !state.retained_request_counts.contains_key(&test_index) {
                    state.resolved_fixture_keys.remove(&test_index);
                }
                drop(state);
                if !retain_case_output && test_index + 1 < collected {
                    discard_success_output(&mut cases[case_index]);
                }
                cases[case_index].set_user_properties(properties.into_boxed_slice());
                cases[case_index].set_xml_attributes(attributes.into_boxed_slice());
                if collect_html_attachments {
                    collect_html_extras(teardown_item.bind(py), &nodeid, html_extras, html_errors);
                }
                publish_pytest_phase(
                    py,
                    pytest_reporter.as_ref(),
                    PhasePublication {
                        case: &cases[case_index],
                        timing: teardown_timing,
                        buffers: capture_buffers.as_ref(),
                        html: Some(PhaseHtml {
                            extras: html_extras,
                            errors: html_errors,
                        }),
                        representation: teardown_representation.as_ref(),
                    },
                )?;
                request_cleanup.finish(py)?;
                if let Some(worker) = &parallel_worker {
                    worker.record_progress(&cases[case_index])?;
                }
                if maxfail > 0 {
                    let case_failures = cases[case_index].failure_count();
                    if let Some(worker) = &parallel_worker {
                        worker
                            .record_failures(case_failures.saturating_sub(pre_teardown_failures))?;
                    } else {
                        failure_count = failure_count.saturating_add(case_failures);
                        if failure_count >= maxfail {
                            return Ok(true);
                        }
                    }
                }
                // Requests retain the exact item they expose; release the suite cache
                // so completed compact-family cases do not accumulate materialized items.
                let completed_item = if unfinished_setup == Some(test_index) {
                    None
                } else {
                    runtime.lock().suite.test_bindings[test_index]
                        .compact
                        .as_mut()
                        .and_then(|compact| compact.materialized_item.take())
                };
                drop(completed_item);
                if unfinished_setup.is_none()
                    && let Some(cursor) = demand_cursor.as_mut()
                {
                    cursor.case_completed(test_index)?;
                }
                active_test = None;
                Ok(false)
            })();
            let test_result = if protocol_report_started && test_result.is_ok() {
                let finish = with_uncaptured_output(py, capture_buffers.as_ref(), || {
                    if let Some(reporter) = pytest_protocol_reporter.as_ref() {
                        reporter.bind(py).call1((item.bind(py), "finish"))?;
                    }
                    Ok(())
                });
                finish.and(test_result)
            } else {
                test_result
            };
            let outcome = match &test_result {
                Ok(_) => Ok(true.into_pyobject(py)?.to_owned().into_any().unbind()),
                Err(error) => Err(error.clone_ref(py)),
            };
            let protocol_result = protocol_context.finish(py, outcome, warning_service);
            let warning_exit = warning_service.exit_item(py);
            let logging_exit = logging_service.finish_test(py);
            protocol_result?;
            warning_exit?;
            logging_exit?;
            if test_result.is_err() {
                require_serial_protocol_recovery(parallel_worker)?;
                // Suppressing an exception does not tear down pytest's setup
                // stack. Preserve its owners for the next setup's validation.
                unfinished_setup = Some(test_index);
            }
            if matches!(test_result, Ok(true))
                || (lifecycle_service.can_stop_session() && project_session_stopped(item.bind(py))?)
            {
                break;
            }
        }
        let mut retained_cleanup_failure = false;
        if schedule.finishes_suite() && (unfinished_setup.is_some() || omitted_protocol_tail) {
            // Protocols that leave owners active defer their finalizers to session cleanup.
            close_capture_buffers(py, &mut capture_buffers)?;
            warning_service.enter_config(py)?;
            let cleanup_failure = teardown_all(py, runtime);
            let warning_exit = warning_service.exit_item(py);
            if let Some(failure) = cleanup_failure {
                retain_session_cleanup_failure(py, items, failure.into_pyerr(py))?;
                retained_cleanup_failure = true;
            }
            warning_exit?;
        }
        if schedule.finishes_suite()
            && !retained_cleanup_failure
            && !runtime.lock().active_nodes.is_empty()
            && !cases.is_empty()
        {
            let last_case_index = cases.len() - 1;
            let last_test_index = last_test.ok_or_else(|| {
                PyRuntimeError::new_err("native session has no last executed test")
            })?;
            let item = case_item(py, runtime, last_test_index)?;
            let (nodeid, profile) = {
                let state = runtime.lock();
                (
                    state.suite.tests()[last_test_index].nodeid.clone(),
                    state.suite.test_bindings[last_test_index].warning_profile,
                )
            };
            logging_service.start_test(py)?;
            if matches!(schedule, ExecutionSchedule::Finish) {
                warning_service.enter_config(py)?;
            } else {
                warning_service.enter_profile(py, &nodeid, profile)?;
            }
            let teardown_started = Instant::now();
            let cleanup = logging_service.with_phase(py, item.bind(py), "teardown", || {
                let capture = start_capture(py, &mut capture_buffers, false)?;
                let mut phase_context =
                    lifecycle_service.enter_phase(item.bind(py), ExecutionPhase::Teardown)?;
                phase_context.builtin_cleanup();
                let mut failure = teardown_all(py, runtime);
                if let Err(error) = phase_context.finish(
                    item.bind(py),
                    failure.as_ref().and_then(|failure| {
                        failure.exception.as_ref().or(failure.control.as_ref())
                    }),
                ) {
                    failure = Some(TeardownFailure::from_pyerr(py, &error));
                }
                let (stdout, stderr) = finish_capture(py, &mut capture_buffers, capture, false)?;
                let properties = case_user_properties(item.bind(py))?;
                let attributes = case_xml_attributes(item.bind(py))?;
                Ok((failure, stdout, stderr, properties, attributes))
            });
            let warning_exit = warning_service.exit_item(py);
            let logging_exit = logging_service.finish_test(py);
            let ((mut failure, stdout, stderr, properties, attributes), log) = match cleanup {
                PhaseCompletion::Completed { value, log, error } => {
                    let (failure, stdout, stderr, properties, attributes) = value;
                    (
                        (
                            error.as_ref().map_or(failure, |error| {
                                Some(TeardownFailure::from_pyerr(py, error))
                            }),
                            stdout,
                            stderr,
                            properties,
                            attributes,
                        ),
                        log,
                    )
                }
                PhaseCompletion::NotStarted(error) | PhaseCompletion::Interrupted(error) => (
                    (
                        Some(TeardownFailure::from_pyerr(py, &error)),
                        String::new(),
                        String::new(),
                        case_user_properties(item.bind(py))?,
                        case_xml_attributes(item.bind(py))?,
                    ),
                    String::new(),
                ),
            };
            warning_exit?;
            logging_exit?;
            if matches!(schedule, ExecutionSchedule::Finish)
                && let Some(failure) = failure.take()
            {
                // Upstream sessionfinish propagates late finalizer errors without
                // rewriting a test report that has already left the worker.
                return Err(failure
                    .control
                    .or(failure.exception)
                    .unwrap_or_else(|| PyRuntimeError::new_err(failure.detail)));
            }
            normalize_teardown_skip(py, item.bind(py), &mut failure)?;
            if let Some(control) = failure.as_mut().and_then(|failure| failure.control.take()) {
                return Err(control);
            }
            let (outcome, xfail_reason) =
                teardown_report_outcome(py, runtime, last_test_index, failure.as_ref())?;
            if collect_html_attachments {
                collect_html_extras(item.bind(py), &nodeid, html_extras, html_errors);
            }
            let result = &mut cases[last_case_index];
            result.set_user_properties(properties.into_boxed_slice());
            result.append_xml_attributes(attributes);
            let duration_ms = teardown_started.elapsed().as_secs_f64() * 1_000.0;
            result.duration_ms += duration_ms;
            if let Some(teardown) = result
                .phases
                .iter_mut()
                .rev()
                .find(|phase| phase.phase == ExecutionPhase::Teardown)
            {
                teardown.duration_ms += duration_ms;
                teardown.append_capture(&stdout, &stderr, &log);
                if let Some(failure) = failure {
                    let skipped = if outcome == ExecutionOutcome::Skipped {
                        failure
                            .exception
                            .as_ref()
                            .map(|error| {
                                format_skip_diagnostic_with_location(py, item.bind(py), error)
                            })
                            .transpose()?
                    } else {
                        None
                    };
                    let exception = if let Some(skipped) = skipped.as_ref() {
                        Some(skipped.exception.clone())
                    } else {
                        failure
                            .exception
                            .as_ref()
                            .map(|error| {
                                canonical_pytest_exception(
                                    py,
                                    item.bind(py),
                                    error,
                                    outcome,
                                    xfail_reason.as_deref(),
                                    None,
                                )
                            })
                            .transpose()?
                            .flatten()
                    };
                    teardown.outcome = if teardown.detail.is_empty() {
                        outcome
                    } else {
                        ExecutionOutcome::Error
                    };
                    if !teardown.detail.is_empty() {
                        teardown.append_detail("\n");
                    }
                    if let Some(skipped) = skipped {
                        teardown.append_detail(&skipped.detail);
                        teardown.set_skip_location(skipped.location);
                    } else {
                        teardown.append_detail(&failure.detail);
                    }
                    teardown.set_exception(exception);
                }
            }
        }
        if streaming_html
            && cases.len() > published_html_cases
            && let Some(reporter) = &html_reporter
            && let Some(case) = cases.last()
        {
            publish_html_case(py, reporter, case, html_extras, html_errors)?;
        }
        if !retain_case_output && let Some(last) = cases.last_mut() {
            discard_success_output(last);
        }
        close_capture_buffers(py, &mut capture_buffers)?;
        if let Some(progress) = progress.as_ref()
            && let Err(error) = progress.finish(py)
        {
            return Err(error);
        }
        Ok(())
    })();
    *unfinished_setup_state = unfinished_setup;
    if let Err(error) = execution {
        let error = request_cleanup_error.take().unwrap_or(error);
        // Reporting and plug-in callbacks can fail after case teardown. Complete
        // all remaining finalizers before restoring temporary process state.
        if schedule.enters_protocols() {
            // Protocol filters have already unwound; session finalizers use
            // configuration filters, as in pytest's sessionfinish wrapper.
            let _ = warning_service.enter_config(py);
        }
        let mut cleanup_failure = None;
        if let Some(test_index) = active_test {
            if let Some(coverage) = vfy_coverage {
                let nodeid = runtime.lock().suite.tests()[test_index].nodeid.clone();
                let _ = coverage
                    .call_method1(intern!(py, "_native_phase"), (&nodeid, "teardown", false));
            }
            cleanup_failure = match teardown_failed_item(py, runtime, test_index) {
                Ok(failure) => failure,
                Err(error) => Some(TeardownFailure::from_pyerr(py, &error)),
            };
        }
        cleanup_failure = merge_errors(py, cleanup_failure, teardown_all(py, runtime));
        let _ = warning_service.exit_item(py);
        let _ = logging_service.finish_test(py);
        let _ = close_capture_buffers(py, &mut capture_buffers);
        if let Some(setup_state) = setup_state.as_mut() {
            let _ = setup_state.restore(py);
        }
        if !matches!(schedule, ExecutionSchedule::Finish) {
            report_partial!(&error);
        }
        if schedule.enters_protocols()
            && let Some(failure) = cleanup_failure
        {
            // Pytest propagates protocol errors before sessionfinish tears down
            // fixtures. Retain cleanup failures for that same session boundary.
            retain_session_cleanup_failure(py, items, failure.into_pyerr(py))?;
        }
        return Err(error);
    }
    if let Some(setup_state) = setup_state.as_mut() {
        setup_state.restore(py)?;
    }
    let execute_ms = execute_started.elapsed().as_secs_f64() * 1_000.0;
    let aggregate_started = Instant::now();

    let counts = match output {
        ExecutionOutput::Snapshot => compute_counts(&cases[first_case..]),
        ExecutionOutput::PhaseWarnings => Counts::default(),
    };
    let duration_ms = started.elapsed().as_secs_f64() * 1_000.0;
    if let Some(destination) = std::env::var_os("PYTEST_NATIVE_STAGE_OUTPUT")
        && let Err(error) = write_stage_timing(
            Path::new(&destination),
            &StageTiming {
                compile_ms,
                warning_setup_ms,
                execute_ms,
                aggregate_ms: aggregate_started.elapsed().as_secs_f64() * 1_000.0,
                total_ms: started.elapsed().as_secs_f64() * 1_000.0,
            },
        )
    {
        report_partial!(&error);
        return Err(error);
    }
    if matches!(output, ExecutionOutput::PhaseWarnings) {
        // Phase callbacks already published the case; retain its authoritative
        // record for finish() without cloning a discarded dispatch snapshot.
        return Ok(None);
    }
    let result_locations = report_locations.as_ref().map(|locations| {
        case_indices[first_case..]
            .iter()
            .filter_map(|index| locations.get(*index).cloned())
            .collect()
    });
    let warning_records = warning_service.records()[first_warning..].to_vec();
    let record = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected,
        counts,
        duration_ms,
        warnings: warning_records,
        cases: if matches!(schedule, ExecutionSchedule::Suite) {
            std::mem::take(cases)
        } else {
            cases[first_case..].to_vec()
        },
    };
    let mut result = native_run_result(record, result_locations);
    result.set_warning_messages(warning_service.take_messages());
    if matches!(schedule, ExecutionSchedule::Suite) {
        result.set_summary_locations(std::mem::take(summary_locations));
        result.set_html_extras(std::mem::take(html_extras), std::mem::take(html_errors));
    } else {
        let extras = cases[first_case..]
            .iter()
            .filter_map(|case| html_extras.get_key_value(&case.nodeid))
            .map(|(nodeid, value)| (nodeid.clone(), value.clone()))
            .collect();
        let errors = cases[first_case..]
            .iter()
            .filter_map(|case| html_errors.get_key_value(&case.nodeid))
            .map(|(nodeid, value)| (nodeid.clone(), value.clone()))
            .collect();
        result.set_html_extras(extras, errors);
    }
    Ok(Some(result))
}

fn write_stage_timing(destination: &Path, timing: &StageTiming) -> PyResult<()> {
    let parent = destination
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|error| io_error(&error))?;
    let mut temporary =
        tempfile::NamedTempFile::new_in(parent).map_err(|error| io_error(&error))?;
    serde_json::to_writer(&mut temporary, timing)
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
    temporary
        .write_all(b"\n")
        .map_err(|error| io_error(&error))?;
    temporary
        .persist(destination)
        .map_err(|error| io_error(&error.error))?;
    Ok(())
}

pub(super) fn merge_errors(
    py: Python<'_>,
    first: Option<TeardownFailure>,
    second: Option<TeardownFailure>,
) -> Option<TeardownFailure> {
    if first
        .as_ref()
        .is_some_and(|failure| failure.escapes_owner(py))
    {
        return first;
    }
    if second
        .as_ref()
        .is_some_and(|failure| failure.escapes_owner(py))
    {
        return second;
    }
    match (first, second) {
        (Some(first), Some(second)) => {
            let error = (|| -> PyResult<PyErr> {
                let mut members = second.aggregate_members(py)?;
                members.extend(first.aggregate_members(py)?);
                Ok(PyBaseExceptionGroup::new_err((
                    "errors during test teardown",
                    PyList::new(py, members)?.unbind(),
                )))
            })()
            .unwrap_or_else(|error| error);
            let mut failure = TeardownFailure::from_pyerr(py, &error);
            // Only this outer aggregation may flatten its members on the next merge.
            failure.grouping = TeardownGrouping::Run;
            Some(failure)
        }
        (Some(error), None) | (None, Some(error)) => Some(error),
        (None, None) => None,
    }
}
