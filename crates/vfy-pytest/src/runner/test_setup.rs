//! Owns test argument resolution, setup failure reports, and test call preparation.

use super::capture::{
    CaptureBuffers, CaptureHandle, finish_capture, start_capture, uses_capture_fixture,
};
use super::compile::case_item;
use super::fixture_lookup::{prepare_unittest_item, refresh_xfail, setup_package_owners};
use super::fixture_setup::{FixtureResolution, live_callspec_parameter, resolve_named_fixture};
use super::model::{FixtureStack, XfailMode, XfailPlan};
use super::native_session::PytestReporter;
use super::report::{
    PhaseClock, PhaseTiming, canonical_pytest_exception, format_skip_diagnostic_with_location,
    is_control_error, is_skip_error, is_xfail_error, normalize_unittest_skip, phase_result,
    record_pytest_capture, render_item_failure, xfail_matches_error,
};
use super::request::NativeRequest;
use super::runtime::SharedRuntime;
use super::test_call::{ExecutedTest, PhasePublication, enforce_xfail_run, publish_pytest_phase};
use crate::lifecycle::{LifecycleService, PhaseAnchor};
use crate::logging::{LoggingService, PhaseCompletion};
use pyo3::exceptions::{PyAssertionError, PyAttributeError, PyRuntimeError};
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, Python};
use pyo3::types::{PyAny, PyDict, PyTuple};
use std::cell::Cell;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;
use vfy_core::AsyncioLoopScope;
use vfy_core::test_execution::{CaseResult, ExecutionOutcome, ExecutionPhase, PhaseResult};
use vfy_core::test_plan::{TestBody, TestId};

pub(super) fn execute_async_test(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    scope: AsyncioLoopScope,
    callable: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<()> {
    let runner_name = format!("_{}_scoped_runner", scope.as_str());
    let runner = resolve_named_fixture(
        py,
        runtime,
        test_index,
        &runner_name,
        None,
        None,
        FixtureResolution::DECLARED,
    )?;
    py.import("vfy.asyncio_adapter")?
        .getattr("run_async_test")?
        .call1((runner, callable.bind(py), PyTuple::empty(py), kwargs))?;
    Ok(())
}

pub(super) struct TestExecutionPlan {
    pub(super) nodeid: String,
    pub(super) body: TestBody,
    async_loop_scope: Option<AsyncioLoopScope>,
    argument_names: Arc<[String]>,
    direct_parameters: Option<Arc<HashMap<String, Py<PyAny>>>>,
    pub(super) xfail: Option<XfailPlan>,
    pub(super) no_cover: bool,
    pub(super) live_arguments: bool,
    skipping_enabled: bool,
}

pub(super) fn test_execution_plan(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> TestExecutionPlan {
    let state = runtime.lock();
    let test = &state.suite.tests()[test_index];
    let bindings = &state.suite.test_bindings[test_index];
    TestExecutionPlan {
        nodeid: test.nodeid.clone(),
        body: test.body,
        async_loop_scope: test.async_loop_scope,
        argument_names: test.argument_names.clone(),
        direct_parameters: bindings
            .fixture_state
            .as_ref()
            .and_then(|state| state.direct_parameters.clone()),
        xfail: bindings.xfail.as_ref().map(|value| value.clone_ref(py)),
        live_arguments: false,
        skipping_enabled: bindings.xfail_mode != XfailMode::Disabled,
        no_cover: state
            .suite
            .no_cover_cases()
            .as_ref()
            .is_some_and(|cases| cases.contains(&test_index)),
    }
}

fn prepare_doctest_arguments(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    item: &Bound<'_, PyAny>,
) -> PyResult<Py<PyDict>> {
    let funcargs = item.getattr("funcargs")?.cast_into::<PyDict>()?;
    let request = Py::new(
        py,
        NativeRequest::for_test(
            runtime,
            item.clone().unbind(),
            test_index,
            FixtureStack::default(),
        ),
    )?;
    let globs = PyDict::new(py);
    let getfixture = request.bind(py).getattr("getfixturevalue")?;
    globs.set_item("getfixture", &getfixture)?;
    // Like DoctestItem.setup(), request the session-scoped namespace even when
    // no fixture in this item's closure depends on it, so names added by
    // earlier tests' autouse fixtures reach later doctests.
    let namespace = match funcargs.get_item("doctest_namespace")? {
        Some(namespace) => namespace,
        None => getfixture.call1(("doctest_namespace",))?,
    };
    globs.call_method1("update", (namespace.call_method0("items")?,))?;
    item.getattr("dtest")?
        .getattr("globs")?
        .call_method1("update", (&globs,))?;
    Ok(PyDict::new(py).unbind())
}

fn prepare_function_arguments(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &TestExecutionPlan,
) -> PyResult<Option<Py<PyDict>>> {
    let item = case_item(py, runtime, test_index)?;
    let funcargs = item
        .bind(py)
        .getattr(intern!(py, "funcargs"))?
        .cast_into::<PyDict>()?;
    if plan.argument_names.is_empty() && plan.async_loop_scope.is_none() {
        return Ok(None);
    }
    let kwargs = if plan.live_arguments {
        None
    } else {
        let kwargs = PyDict::new(py);
        for name in plan.argument_names.iter() {
            kwargs.set_item(
                name,
                funcargs.get_item(name)?.ok_or_else(|| {
                    PyRuntimeError::new_err(format!("test argument {name:?} has no fixture value"))
                })?,
            )?;
        }
        Some(kwargs.unbind())
    };
    if let Some(scope) = plan.async_loop_scope {
        let runner_name = format!("_{}_scoped_runner", scope.as_str());
        resolve_named_fixture(
            py,
            runtime,
            test_index,
            &runner_name,
            None,
            None,
            FixtureResolution::DECLARED,
        )?;
    }
    Ok(kwargs)
}

pub(super) fn live_function_arguments(item: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    let py = item.py();
    let kwargs = PyDict::new(py);
    let funcargs = item.getattr("funcargs")?;
    for name in item
        .getattr("_fixtureinfo")?
        .getattr("argnames")?
        .try_iter()?
    {
        let name = name?;
        kwargs.set_item(&name, funcargs.get_item(&name)?)?;
    }
    Ok(kwargs.unbind())
}

pub(super) fn resolve_test_arguments(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    item: &Bound<'_, PyAny>,
    plan: &TestExecutionPlan,
) -> PyResult<Option<Py<PyDict>>> {
    let (eager_fixtures, live_direct_names, live_item, scope_lookup, callspec_reader) = {
        let state = runtime.lock();
        (
            state.suite.tests()[test_index].eager_fixtures.clone(),
            state.suite.test_bindings[test_index]
                .compact
                .is_none()
                .then(|| {
                    state.suite.tests()[test_index]
                        .fixture_state
                        .as_ref()
                        .map(|state| state.direct_parameter_names.clone())
                        .unwrap_or_default()
                }),
            state.suite.test_bindings[test_index]
                .compact
                .is_none()
                .then_some(item),
            state.compatibility.scope_override_lookup,
            state.suite.callspec_reader.clone(),
        )
    };
    let mut live_names = if plan.live_arguments {
        Some(item.getattr("fixturenames")?.try_iter()?)
    } else {
        None
    };
    let mut compiled_names = eager_fixtures.iter();
    let funcargs = item
        .getattr(intern!(py, "funcargs"))?
        .cast_into::<PyDict>()?;
    loop {
        let name = if let Some(names) = &mut live_names {
            let Some(name) = names.next() else {
                break;
            };
            std::borrow::Cow::Owned(name?.extract::<String>()?)
        } else {
            let Some(name) = compiled_names.next() else {
                break;
            };
            std::borrow::Cow::Borrowed(name.as_str())
        };
        let name = name.as_ref();
        if funcargs.contains(name)? {
            continue;
        }
        let value = if let Some(item) = live_item
            && live_direct_names
                .as_ref()
                .is_some_and(|names| names.contains(name))
        {
            live_callspec_parameter(item, name, scope_lookup, callspec_reader.as_deref())?
                .map(|parameter| parameter.value)
                .ok_or_else(|| PyAttributeError::new_err("param"))?
        } else if let Some(value) = plan
            .direct_parameters
            .as_ref()
            .and_then(|parameters| parameters.get(name))
        {
            value.clone_ref(py)
        } else if name == "request" && plan.live_arguments {
            item.getattr("_request")?.unbind()
        } else if name == "request" {
            Py::new(
                py,
                NativeRequest::for_test(
                    runtime,
                    item.clone().unbind(),
                    test_index,
                    FixtureStack::default(),
                ),
            )?
            .into_any()
        } else {
            resolve_named_fixture(
                py,
                runtime,
                test_index,
                name,
                None,
                None,
                FixtureResolution::DECLARED,
            )?
        };
        // Match pytest's incremental fill so later fixtures can inspect earlier values.
        funcargs.set_item(name, value)?;
    }
    if plan.body == TestBody::Doctest {
        prepare_doctest_arguments(py, runtime, test_index, item).map(Some)
    } else {
        prepare_function_arguments(py, runtime, test_index, plan)
    }
}

struct SetupFailure {
    started: Instant,
    setup_timing: PhaseTiming,
    stdout: String,
    stderr: String,
    log: String,
    error: PyErr,
}

pub(super) struct TestPhaseServices<'a> {
    pub(super) logging: &'a mut LoggingService,
    pub(super) lifecycle: &'a LifecycleService,
    pub(super) setup_state_started: &'a mut bool,
    pub(super) previous_setup_unfinished: bool,
    pub(super) pending_setup_nodes: Option<Vec<String>>,
    pub(super) pytest_reporter: Option<&'a PytestReporter>,
}

pub(super) struct PreparedTestCall {
    pub(super) item: Py<PyAny>,
    pub(super) kwargs: Option<Py<PyDict>>,
    pub(super) setup_phase: PhaseResult,
    pub(super) setup_timing: PhaseTiming,
}

pub(super) struct ItemRequestCleanup<'a> {
    item: Option<Py<PyAny>>,
    capture: Option<CaptureHandle>,
    error: &'a Cell<Option<PyErr>>,
}

impl<'a> ItemRequestCleanup<'a> {
    pub(super) fn new(
        py: Python<'_>,
        item: Py<PyAny>,
        buffers: Option<&CaptureBuffers>,
        error: &'a Cell<Option<PyErr>>,
    ) -> PyResult<Self> {
        let has_request = item.bind(py).hasattr(intern!(py, "_request"))?;
        Ok(Self {
            item: has_request.then_some(item),
            capture: buffers.and_then(|buffers| buffers.capture_handle(py)),
            error,
        })
    }

    pub(super) fn finish(&mut self, py: Python<'_>) -> PyResult<()> {
        let Some(item) = self.item.take() else {
            return Ok(());
        };
        let capture = self.capture.as_ref();
        let active = capture
            .map(|capture| capture.is_started(py))
            .transpose()?
            .unwrap_or(false);
        if let Some(capture) = capture.filter(|_| active) {
            capture.suspend_capturing(py)?;
        }
        let cleared = item
            .bind(py)
            .setattr(intern!(py, "_request"), false)
            .and_then(|()| item.bind(py).setattr(intern!(py, "funcargs"), py.None()));
        let resumed = match capture.filter(|_| active) {
            Some(capture) => capture.resume_capturing(py),
            None => Ok(()),
        };
        cleared.and(resumed)
    }
}

impl Drop for ItemRequestCleanup<'_> {
    fn drop(&mut self) {
        if self.item.is_none() {
            return;
        }
        Python::attach(|py| {
            if let Err(error) = self.finish(py) {
                self.error.set(Some(error));
            }
        });
    }
}

pub(super) enum TestSetupOutcome {
    Prepared(PreparedTestCall),
    Failed(FailedSetup),
}

pub(super) struct FailedSetup {
    case: CaseResult,
    timing: PhaseTiming,
    representation: Option<Py<PyAny>>,
}

impl FailedSetup {
    pub(super) fn publish(
        self,
        py: Python<'_>,
        reporter: Option<&PytestReporter>,
        buffers: Option<&CaptureBuffers>,
    ) -> PyResult<ExecutedTest> {
        let stop_requested = publish_pytest_phase(
            py,
            reporter,
            PhasePublication {
                case: &self.case,
                timing: self.timing,
                buffers,
                html: None,
                representation: self.representation.as_ref(),
            },
        )?;
        Ok(ExecutedTest {
            case: self.case,
            stop_requested,
        })
    }
}

fn setup_failure_case(
    py: Python<'_>,
    plan: &TestExecutionPlan,
    item: &Bound<'_, PyAny>,
    failure: SetupFailure,
) -> PyResult<FailedSetup> {
    let outcome = if plan.skipping_enabled && is_xfail_error(py, &failure.error) {
        ExecutionOutcome::XFailed
    } else if is_skip_error(py, &failure.error) {
        ExecutionOutcome::Skipped
    } else if let Some(xfail) = plan.xfail.as_ref() {
        if xfail_matches_error(py, &failure.error, xfail)? {
            ExecutionOutcome::XFailed
        } else {
            ExecutionOutcome::Error
        }
    } else {
        ExecutionOutcome::Error
    };
    let not_run =
        outcome == ExecutionOutcome::XFailed && plan.xfail.as_ref().is_some_and(|xfail| !xfail.run);
    let formatted = if outcome == ExecutionOutcome::Skipped || not_run {
        None
    } else {
        Some(render_item_failure(py, item, &failure.error)?)
    };
    let skipped = if outcome == ExecutionOutcome::Skipped {
        Some(format_skip_diagnostic_with_location(
            py,
            item,
            &failure.error,
        )?)
    } else {
        None
    };
    let xfail_reason =
        if outcome == ExecutionOutcome::XFailed && !is_xfail_error(py, &failure.error) {
            plan.xfail.as_ref().map(|xfail| xfail.reason.as_str())
        } else {
            None
        };
    let exception = if let Some(skipped) = skipped.as_ref() {
        Some(skipped.exception.clone())
    } else {
        canonical_pytest_exception(
            py,
            item,
            &failure.error,
            outcome,
            xfail_reason,
            formatted.as_ref(),
        )?
    };
    let mut terminal_lead = String::new();
    let (detail, skip_location, representation) = if let Some(skipped) = skipped {
        (skipped.detail, skipped.location, None)
    } else if not_run {
        (
            format!(
                "[NOTRUN] {}",
                plan.xfail.as_ref().expect("xfail plan").reason
            ),
            None,
            None,
        )
    } else {
        let formatted = formatted.expect("failed setup has a diagnostic");
        terminal_lead = formatted.terminal_lead;
        (formatted.detail, None, formatted.representation)
    };
    let mut phase = phase_result(
        outcome,
        ExecutionPhase::Setup,
        failure.setup_timing,
        failure.stdout,
        failure.stderr,
        detail,
    );
    phase.set_skip_location(skip_location);
    phase.set_exception(exception);
    phase.set_log(failure.log);
    phase.set_terminal_lead(terminal_lead);
    let mut phases = Vec::with_capacity(2);
    phases.push(phase);
    Ok(FailedSetup {
        case: CaseResult::new(
            plan.nodeid.clone(),
            failure.started.elapsed().as_secs_f64() * 1_000.0,
            phases,
        ),
        timing: failure.setup_timing,
        representation,
    })
}

fn validate_pending_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    item: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let active = {
        let state = runtime.lock();
        state
            .active_nodes
            .iter()
            .filter_map(|owner| state.owner_nodes.get(owner).map(|node| node.clone_ref(py)))
            .collect::<Vec<_>>()
    };
    let needed = item.call_method0("listchain")?;
    for node in active {
        if !needed.contains(node.bind(py))? {
            return Err(PyAssertionError::new_err(
                "previous item was not torn down properly",
            ));
        }
    }
    Ok(())
}

fn enforce_skip_marks(item: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = item.py();
    let skip = py
        .import("_pytest.skipping")?
        .call_method1(intern!(py, "evaluate_skip_marks"), (item,))?;
    if !skip.is_none() {
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "_use_item_location"), true)?;
        let exception = py
            .import("_pytest.outcomes")?
            .getattr(intern!(py, "Skipped"))?
            .call((skip.getattr(intern!(py, "reason"))?,), Some(&kwargs))?;
        return Err(PyErr::from_value(exception));
    }
    Ok(())
}

pub(super) fn prepare_test_call(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    capture_buffers: &mut Option<CaptureBuffers>,
    services: &mut TestPhaseServices<'_>,
    plan: &mut TestExecutionPlan,
    started: Instant,
) -> PyResult<TestSetupOutcome> {
    let item = case_item(py, runtime, test_index)?;
    let setup_started = PhaseClock::start();
    let lifecycle = services.lifecycle;
    plan.live_arguments = lifecycle.has_project_phases();
    if plan.live_arguments {
        let request = NativeRequest::for_test(
            runtime,
            item.clone_ref(py),
            test_index,
            FixtureStack::default(),
        );
        item.bind(py).setattr("_request", Py::new(py, request)?)?;
    }
    let completion = services.logging.with_phase(py, item.bind(py), "setup", || {
        let capture_state = start_capture(py, capture_buffers, false)?;
        let setup = lifecycle.with_phase(item.bind(py), ExecutionPhase::Setup, |phase_context| {
            let item = item.bind(py);
            phase_context.before(item, PhaseAnchor::Skipping)?;
            if plan.skipping_enabled {
                enforce_skip_marks(item)?;
            }
            plan.xfail =
                refresh_xfail(runtime, test_index, item, lifecycle, ExecutionPhase::Setup)?;
            enforce_xfail_run(py, plan.xfail.as_ref())?;
            phase_context.before(item, PhaseAnchor::Runner)?;
            lifecycle.update_current_test(item, Some("setup"))?;
            if services.previous_setup_unfinished {
                validate_pending_setup(py, runtime, item)?;
            }
            if let Some(nodes) = services.pending_setup_nodes.take() {
                let mut state = runtime.lock();
                state.active_nodes = nodes;
                state.active_function = Some(TestId::from_index(test_index));
            }
            *services.setup_state_started = true;
            setup_package_owners(py, runtime, item)?;
            if plan.body == TestBody::Unittest {
                prepare_unittest_item(py, item)?;
            }
            resolve_test_arguments(py, runtime, test_index, item, plan)
        });
        let (stdout, stderr) = finish_capture(
            py,
            capture_buffers,
            capture_state,
            uses_capture_fixture(runtime, test_index),
        )?;
        if services.pytest_reporter.is_some() {
            record_pytest_capture(item.bind(py), ExecutionPhase::Setup, &stdout, &stderr)?;
        }
        Ok((setup, stdout, stderr))
    });
    let ((setup, stdout, stderr), log) = match completion {
        PhaseCompletion::Completed { value, log, error } => {
            let (setup, stdout, stderr) = value;
            ((error.map_or(setup, Err), stdout, stderr), log)
        }
        PhaseCompletion::NotStarted(error) | PhaseCompletion::Interrupted(error) => {
            ((Err(error), String::new(), String::new()), String::new())
        }
    };
    let setup_timing = setup_started.finish();
    let kwargs = match setup {
        Ok(kwargs) => kwargs,
        Err(error) => {
            if is_control_error(py, &error) {
                return Err(error);
            }
            let error = normalize_unittest_skip(py, item.bind(py), &error)?;
            return setup_failure_case(
                py,
                plan,
                item.bind(py),
                SetupFailure {
                    started,
                    setup_timing,
                    stdout,
                    stderr,
                    log,
                    error,
                },
            )
            .map(TestSetupOutcome::Failed);
        }
    };
    let mut phase = phase_result(
        ExecutionOutcome::Passed,
        ExecutionPhase::Setup,
        setup_timing,
        stdout,
        stderr,
        String::new(),
    );
    phase.set_log(log);
    Ok(TestSetupOutcome::Prepared(PreparedTestCall {
        item,
        kwargs,
        setup_phase: phase,
        setup_timing,
    }))
}
