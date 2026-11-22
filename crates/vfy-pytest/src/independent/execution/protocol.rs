//! Item protocol dispatch around the single native executor.

use super::executor::Executor;
use super::run::execute_case;
use crate::hooks::{NativeHookDefault, NativeHookRegistry};
use crate::independent::lifecycle::Context;
use crate::independent::reporting::{
    NativeSessionReports, PhaseProjection, case_from_snapshots,
    case_snapshots_since as emitted_snapshots, replace_case_snapshots,
};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyWeakrefMethods, PyWeakrefReference};
use std::cell::RefCell;
use std::rc::Rc;
use std::time::Instant;
use vfy_core::test_execution::CaseResult;

#[derive(Clone, Copy)]
pub(super) struct CaseDispatch {
    pub(super) index: usize,
    pub(super) next: Option<usize>,
}

/// Only the native loop's own shouldfail check creates this session stop token.
pub(in crate::independent) struct SessionFailure {
    error: PyErr,
}

impl SessionFailure {
    pub(in crate::independent) fn retain_worker_stop(&self, py: Python<'_>, context: &Context) {
        if context.invocation.is_worker() {
            context.invocation.reported(
                py,
                &self.error,
                crate::independent::LifecycleStage::Running,
                crate::independent::Disposition::Return(1),
                crate::independent::FailureOrigin::SessionFailure,
            );
        }
    }

    pub(in crate::independent) fn matches(&self, py: Python<'_>, error: &PyErr) -> bool {
        self.error.value(py).is(error.value(py))
    }
}

fn parallel(context: &Context, py: Python<'_>) -> PyResult<bool> {
    Ok(
        (context.invocation.is_worker() && !context.invocation.is_fresh_serial())
            || context
                .config
                .bind(py)
                .call_method1("getoption", ("workers", 1))?
                .extract::<usize>()?
                > 1,
    )
}

/// Apply stop flags to the owned lookahead frozen before protocol dispatch.
pub(super) fn next_test(
    py: Python<'_>,
    executor: &Executor,
    next: Option<usize>,
) -> PyResult<Option<usize>> {
    let suite = executor.suite()?;
    let session = suite.context.session.bind(py);
    suite
        .context
        .invocation
        .synchronize_worker_failures(py, session)?;
    if session
        .getattr(pyo3::intern!(py, "shouldfail"))?
        .is_truthy()?
        || session
            .getattr(pyo3::intern!(py, "shouldstop"))?
            .is_truthy()?
    {
        return Ok(None);
    }
    if let Some(worker) = suite.context.invocation.worker(py)
        && !worker.borrow(py).claim_test()?
    {
        return Ok(None);
    }
    Ok(next)
}

/// Preserve the exception identity through surrounding runtestloop wrappers.
pub(super) fn check_stop(
    py: Python<'_>,
    executor: &Executor,
    failure: &mut Option<SessionFailure>,
) -> PyResult<()> {
    let suite = executor.suite()?;
    let context = &suite.context;
    let session = context.session.bind(py);
    if session
        .getattr(pyo3::intern!(py, "shouldfail"))?
        .is_truthy()?
    {
        let budget_exhausted = context
            .invocation
            .worker(py)
            .map(|worker| worker.borrow(py).claim_test().map(|available| !available))
            .transpose()?
            .unwrap_or(false);
        if parallel(context, py)? && !budget_exhausted {
            return Err(crate::UnsupportedFeatureError::new_err(
                "independent pytest requires --workers=1 with session.shouldfail; worker session stops are unsupported",
            ));
        }
        let error = PyErr::from_value(
            session
                .getattr("Failed")?
                .call1((session.getattr(pyo3::intern!(py, "shouldfail"))?,))?,
        );
        *failure = Some(SessionFailure {
            error: error.clone_ref(py),
        });
        return Err(error);
    }
    if session
        .getattr(pyo3::intern!(py, "shouldstop"))?
        .is_truthy()?
    {
        return Err(PyErr::from_value(
            session
                .getattr("Interrupted")?
                .call1((session.getattr(pyo3::intern!(py, "shouldstop"))?,))?,
        ));
    }
    Ok(())
}

enum CallbackState {
    Pending {
        executor: Rc<Executor>,
        dispatch: CaseDispatch,
        history: Py<NativeSessionReports>,
        report_start: usize,
        projection: PhaseProjection,
    },
    Running,
    Complete(CaseResult),
    Failed(CallbackFailure),
    Closed,
}

enum CallbackFailure {
    Control,
    Ordinary,
}

#[derive(Clone, Copy)]
enum PartialCasePolicy {
    EmittedPrefix,
    ReportedResult,
}

// The Rust dispatch scope is the only persistent strong owner. Retained Python
// callbacks hold an exact no-callback weakref, so they cannot retain this Rc state.
#[pyclass(frozen, unsendable, weakref)]
struct ProtocolCallbackOwner {
    state: Rc<RefCell<CallbackState>>,
}

#[pyclass(frozen)]
struct ProtocolCallback {
    owner: Py<PyWeakrefReference>,
    thread: std::thread::ThreadId,
}

impl ProtocolCallbackOwner {
    fn create(
        py: Python<'_>,
        pending: CallbackState,
    ) -> PyResult<(Py<Self>, Py<ProtocolCallback>)> {
        let owner = Py::new(
            py,
            Self {
                state: Rc::new(RefCell::new(pending)),
            },
        )?;
        let callback = Py::new(
            py,
            ProtocolCallback {
                owner: PyWeakrefReference::new(owner.bind(py))?.unbind(),
                thread: std::thread::current().id(),
            },
        )?;
        Ok((owner, callback))
    }
}

#[pymethods]
impl ProtocolCallback {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<bool> {
        if self.thread != std::thread::current().id() {
            return Err(PyRuntimeError::new_err(
                "native test protocol callback belongs to another thread",
            ));
        }
        let owner = self.owner.bind(py).upgrade().ok_or_else(|| {
            PyAssertionError::new_err("native test protocol callback is no longer available")
        })?;
        let owner = owner.cast_into::<ProtocolCallbackOwner>()?;
        let state = Rc::clone(&owner.borrow().state);
        invoke_protocol_state(py, &state)
    }
}

fn invoke_protocol_state(py: Python<'_>, state: &RefCell<CallbackState>) -> PyResult<bool> {
    let previous_state = state.replace(CallbackState::Running);
    let (executor, dispatch, history, report_start, projection) = match previous_state {
        CallbackState::Pending {
            executor,
            dispatch,
            history,
            report_start,
            projection,
        } => (executor, dispatch, history, report_start, projection),
        previous => {
            state.replace(previous);
            return Err(PyAssertionError::new_err(
                "native test protocol callback is no longer available",
            ));
        }
    };
    let result = (|| {
        let suite = executor.suite()?;
        let test = suite
            .plan
            .tests()
            .get(dispatch.index)
            .ok_or_else(|| PyValueError::new_err("test protocol plan is unavailable"))?;
        execute_case(
            &executor,
            py,
            dispatch,
            test,
            history.bind(py),
            report_start,
            projection,
        )
    })();
    match result {
        Ok(case) => {
            state.replace(CallbackState::Complete(case));
            Ok(true)
        }
        Err(error) => {
            let failure = if crate::independent::controls::is_control_error(py, &error)? {
                CallbackFailure::Control
            } else {
                CallbackFailure::Ordinary
            };
            state.replace(CallbackState::Failed(failure));
            Err(error)
        }
    }
}

fn emitted_case(
    history: &Bound<'_, NativeSessionReports>,
    start: usize,
    nodeid: &str,
    elapsed_ms: f64,
    policy: PartialCasePolicy,
    origin: &crate::independent::reporting::origin::ReportOrigin,
) -> PyResult<Option<CaseResult>> {
    if matches!(policy, PartialCasePolicy::ReportedResult)
        && !history
            .borrow()
            .has_reported_result_since(history.py(), start, nodeid)?
    {
        return Ok(None);
    }
    let snapshots = emitted_snapshots(history, start, nodeid, None, origin)?;
    Ok((!snapshots.is_empty()).then(|| {
        let mut case = case_from_snapshots(nodeid.to_owned(), elapsed_ms, snapshots);
        case.set_test_id(origin.observed_test());
        case
    }))
}

/// Preserve completed reports before propagating an outer wrapper's error.
pub(super) fn run(
    executor: &Rc<Executor>,
    py: Python<'_>,
    dispatch: CaseDispatch,
    cases: &mut Vec<CaseResult>,
    projection: PhaseProjection,
) -> PyResult<()> {
    let CaseDispatch { index, next } = dispatch;
    let suite = executor.suite()?;
    let context = &suite.context;
    let origin = crate::independent::reporting::origin::ReportOrigin::bound(py, context, index)?;
    let item = context
        .items
        .get(index)
        .ok_or_else(|| PyValueError::new_err("test protocol item is unavailable"))?
        .bind(py);
    let nodeid = item
        .getattr(pyo3::intern!(py, "nodeid"))?
        .extract::<String>()?;
    let history = context
        .session
        .bind(py)
        .getattr(pyo3::intern!(py, "_vfy_report"))?
        .cast_into::<NativeSessionReports>()?;
    let report_start = history.borrow().report_count();
    let started = Instant::now();
    let state = RefCell::new(CallbackState::Pending {
        executor: Rc::clone(executor),
        dispatch,
        history: history.clone().unbind(),
        report_start,
        projection,
    });
    // The owner and weak callback exist only when hook monitoring or a
    // hookspec warning can observe the native default.
    let callback_owner = RefCell::new(None);
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "item"), item)?;
    kwargs.set_item(
        pyo3::intern!(py, "nextitem"),
        next.and_then(|index| context.items.get(index)),
    )?;
    let exclusions = NativeHookRegistry::item_exclusions(context.registry.bind(py), item)?;
    let assertion_scope = crate::assertions::AssertionItemGuard::bind(item, &exclusions);
    let outcome = NativeHookRegistry::call_subset_with_lazy_default(
        context.registry.bind(py),
        "pytest_runtest_protocol",
        &kwargs,
        &exclusions,
        true,
        "runner",
        NativeHookDefault {
            execute: || {
                invoke_protocol_state(py, &state)?;
                Ok(true.into_pyobject(py)?.to_owned().into_any().unbind())
            },
            materialize: || {
                let (owner, callback) =
                    ProtocolCallbackOwner::create(py, state.replace(CallbackState::Closed))?;
                let result = callback.clone_ref(py).into_any();
                *callback_owner.borrow_mut() = Some((owner, callback));
                Ok(result)
            },
        },
    );
    drop(assertion_scope);
    drop(kwargs);
    let state = match callback_owner.borrow().as_ref() {
        Some((owner, _)) => owner.borrow(py).state.replace(CallbackState::Closed),
        None => state.replace(CallbackState::Closed),
    };
    // Wrappers can replace or suppress the error after the native callback failed.
    let policy = match &state {
        CallbackState::Failed(CallbackFailure::Control) => PartialCasePolicy::ReportedResult,
        _ => PartialCasePolicy::EmittedPrefix,
    };
    if let CallbackState::Complete(mut case) = state {
        case.set_test_id(origin.observed_test());
        let reports = emitted_snapshots(&history, report_start, &nodeid, Some(&case), &origin).map(
            |snapshots| {
                if !snapshots.is_empty() {
                    replace_case_snapshots(&mut case, snapshots);
                }
            },
        );
        case.nodeid = nodeid;
        cases.push(case);
        outcome?;
        return reports;
    }
    let emitted = emitted_case(
        &history,
        report_start,
        &nodeid,
        started.elapsed().as_secs_f64() * 1000.0,
        policy,
        &origin,
    );
    if let Err(error) = outcome {
        if let Ok(Some(case)) = emitted {
            cases.push(case);
        }
        return Err(error);
    }
    if let Some(case) = emitted? {
        cases.push(case);
    }
    Ok(())
}
