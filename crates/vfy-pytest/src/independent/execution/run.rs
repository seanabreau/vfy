//! Case execution and the session run loop.

use super::super::CollectedSuite;
use super::super::global_capture::Capture;
use super::super::reporting::call::PhaseCompletion;
use super::super::reporting::origin::ReportBoundary;
use super::super::reporting::{NativeSessionReports, PhaseProjection, PhasePublication};
use super::callbacks::{FixtureCaptureAction, PhaseOperation};
use super::capture::ExecutionCapture;
use super::executor::Executor;
use super::phase_dispatch::run_phase;
use super::protocol::SessionFailure;
use super::report_sections::ItemReportSections;
use super::state::{ExecutionState, FixtureSlot};
use super::{collected_item, protocol, provider_scopes};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyWeakrefReference;
use std::cell::RefCell;
use std::ops::Range;
use std::rc::Rc;
use std::time::Instant;
use vfy_core::test_execution::{CasePhases, CaseResult, PhaseResult};
use vfy_core::test_plan::TestPlan;

pub(super) fn execute_case(
    executor: &Rc<Executor>,
    py: Python<'_>,
    dispatch: protocol::CaseDispatch,
    test: &TestPlan,
    history: &Bound<'_, NativeSessionReports>,
    report_start: usize,
    projection: PhaseProjection,
) -> PyResult<CaseResult> {
    let protocol::CaseDispatch { index, next } = dispatch;
    let case_started = Instant::now();
    let shared_nodeid = std::sync::Arc::<str>::from(test.nodeid.as_str());
    let suite = executor.suite()?;
    let protocol_capture = ExecutionCapture::start(py, &suite, index)?;
    drop(executor.capture.replace(protocol_capture.logs.clone()));
    let mut head_line = None;
    let mut section_owner = ItemReportSections::default();
    let phases = (|| -> PyResult<CasePhases> {
        let protocol = &suite.context;
        let item = protocol
            .items
            .get(index)
            .ok_or_else(|| PyValueError::new_err("test hook item is unavailable"))?
            .bind(py);
        let location = std::sync::Arc::new(super::super::reporting::location(item)?);
        // pytest's TestReport.head_line is the domain of Item.location.
        head_line = Some(location.2.clone());
        super::super::reporting::boundary(
            py,
            protocol,
            item,
            "pytest_runtest_logstart",
            &location,
        )?;
        let capture = Capture::start(py, Some(&suite.context))?;
        let context = executor.begin_test(py, index)?;
        let run = |operation, capture| {
            run_phase(
                executor,
                py,
                &protocol_capture,
                &context,
                operation,
                capture,
            )
        };
        let mut publish = |completion: PhaseCompletion| -> PyResult<PhaseResult> {
            section_owner.append(item, completion.phase(), &completion.capture)?;
            let item_sections = super::super::reporting::item_sections(item)?;
            let boundary = ReportBoundary::bound(py, protocol, index)?;
            super::super::reporting::publish_phase(
                py,
                protocol,
                item,
                &location,
                completion,
                item_sections,
                PhasePublication {
                    shared_nodeid: Some(&shared_nodeid),
                    boundary,
                    projection,
                    history,
                    report_start,
                },
            )
        };
        let setup = publish(run(PhaseOperation::Setup, capture)?)?;
        let ready = setup.outcome.report_outcome() == vfy_core::selection::TestOutcome::Passed;
        let mut phases = CasePhases::new();
        phases.push(setup);
        if ready {
            let capture = Capture::start(py, Some(&suite.context))?;
            executor.fixture_capture(py, FixtureCaptureAction::Start)?;
            phases.push(publish(run(PhaseOperation::Call, capture)?)?);
        }
        let next = protocol::next_test(py, executor, next)?;
        let capture = Capture::start(py, Some(&suite.context))?;
        executor.fixture_capture(py, FixtureCaptureAction::Start)?;
        phases.push(publish(run(PhaseOperation::Teardown { next }, capture)?)?);
        executor.end_test(py, index)?;
        super::super::reporting::boundary(
            py,
            protocol,
            item,
            "pytest_runtest_logfinish",
            &location,
        )?;
        Ok(phases)
    })();
    let finished = protocol_capture.finish(py, &suite.plan.tests()[index].nodeid);
    let cleared_logs = collected_item(&suite, index)
        .ok_or_else(|| PyValueError::new_err("test hook item is unavailable"))
        .and_then(|item| section_owner.release_logs(item.bind(py)));
    drop(executor.capture.take());
    finished?;
    let phases = phases?;
    cleared_logs?;
    let mut case = CaseResult::new(
        test.nodeid.clone(),
        case_started.elapsed().as_secs_f64() * 1000.0,
        phases,
    );
    case.set_head_line(head_line);
    Ok(case)
}

/// Session-finish cleanup releases the executor with its active setup owners.
/// The owning Context explicitly consumes or releases this temporary callback;
/// retained hook-monitor callbacks therefore cannot retain fixture state afterward.
#[pyclass(frozen, unsendable)]
pub(in crate::independent) struct NativeDeferredCleanup {
    pub(super) executor: RefCell<Option<Rc<Executor>>>,
}

#[pymethods]
impl NativeDeferredCleanup {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let executor = self.executor.borrow_mut().take();
        if let Some(executor) = executor {
            let invocation = Rc::clone(&executor.suite()?.context.invocation);
            let result = executor.finalize(py, None);
            if let Err(error) = &result {
                let previous = invocation
                    .fixture_cleanup_error
                    .replace(Some(error.value(py).clone().into_any().unbind()));
                drop(previous);
            }
            executor.close();
            result?;
        }
        Ok(())
    }
}

impl NativeDeferredCleanup {
    pub(in crate::independent) fn release(&self) {
        let executor = self.executor.borrow_mut().take();
        if let Some(executor) = executor {
            executor.close();
        }
    }
}

/// Choose one owned case and freeze its successor before protocol hooks run.
fn next_dispatch(
    py: Python<'_>,
    worker: Option<&crate::parallel::NativeParallelWorker>,
    demand: &mut Option<crate::parallel::DemandCaseCursor<'_>>,
    static_indices: &mut Range<usize>,
) -> PyResult<Option<protocol::CaseDispatch>> {
    let index = if let Some(cursor) = demand.as_mut() {
        cursor.next(py)?
    } else {
        static_indices.next()
    };
    let Some(index) = index else {
        return Ok(None);
    };
    if let Some(worker) = worker
        && !worker.claim_test()?
    {
        return Ok(None);
    }
    let next = if let Some(cursor) = demand.as_mut() {
        cursor.peek(py)?
    } else {
        static_indices.clone().next()
    };
    Ok(Some(protocol::CaseDispatch { index, next }))
}

pub(in crate::independent) fn run_cases(
    py: Python<'_>,
    mut suite: CollectedSuite,
    root: &str,
    cases: &mut Vec<CaseResult>,
    session_failure: &mut Option<SessionFailure>,
) -> PyResult<()> {
    let provider_scopes = provider_scopes::build(py, &mut suite)?;
    let suite = Rc::new(suite);
    let executor = Rc::new(Executor {
        suite: RefCell::new(Some(Rc::clone(&suite))),
        root: root.to_owned(),
        state: RefCell::new(ExecutionState {
            cache: std::iter::repeat_with(|| FixtureSlot::Empty)
                .take(suite.plan.fixtures().len())
                .collect(),
            ..ExecutionState::default()
        }),
        node_state: PyWeakrefReference::new(
            &suite
                .context
                .session
                .bind(py)
                .getattr("_vfy_native_setupstate")?,
        )?
        .unbind(),
        capture: RefCell::new(None),
        provider_scopes: RefCell::new(provider_scopes),
    });
    let result = (|| {
        let junit_observer = !suite
            .context
            .config
            .bind(py)
            .getattr(pyo3::intern!(py, "_vfy_junit_writer"))?
            .is_none();
        let worker = suite.context.invocation.worker(py);
        let worker = worker.as_ref().map(|worker| worker.borrow(py));
        let mut demand_cursor = worker
            .as_deref()
            .and_then(crate::parallel::NativeParallelWorker::demand_cursor);
        let mut static_indices = 0..suite.plan.tests().len();
        while let Some(dispatch) = next_dispatch(
            py,
            worker.as_deref(),
            &mut demand_cursor,
            &mut static_indices,
        )? {
            let first_case = cases.len();
            let projection = if junit_observer || worker.is_some() {
                PhaseProjection::Full
            } else {
                PhaseProjection::ExecutionCheckpoint
            };
            let completed = protocol::run(&executor, py, dispatch, cases, projection);
            let recorded = suite
                .context
                .invocation
                .synchronize_worker_failures(py, suite.context.session.bind(py));
            let progress = (|| {
                if let Some(worker) = &worker {
                    for case in &cases[first_case..] {
                        worker.record_progress(case)?;
                    }
                }
                Ok(())
            })();
            let recorded = recorded.and(progress);
            if let Err(primary) = completed {
                if let Err(secondary) = recorded {
                    crate::parallel::retain_parent_context(py, &primary, secondary);
                }
                return Err(primary);
            }
            recorded?;
            if let Some(cursor) = demand_cursor.as_mut() {
                cursor.case_completed(dispatch.index)?;
            }
            protocol::check_stop(py, &executor, session_failure)?;
        }
        Ok(())
    })();
    let defer = match &result {
        Err(error) => {
            super::super::controls::is_control_error(py, error)?
                || !super::super::session_hooks::propagates_loop_error(py, error)?
        }
        // Protocol wrappers can leave setup owners active without propagating
        // an error. The same chain then belongs to session-finish cleanup.
        Ok(()) => !executor.node_state(py)?.get().locked()?.is_empty(),
    };
    if defer {
        suite.context.session.bind(py).setattr(
            "_vfy_deferred_cleanup",
            Py::new(
                py,
                NativeDeferredCleanup {
                    executor: RefCell::new(Some(Rc::clone(&executor))),
                },
            )?,
        )?;
        return result;
    }
    let finalized = executor.finalize(py, None);
    executor.close();
    match result {
        Err(error) => Err(error),
        Ok(()) => finalized,
    }
}
