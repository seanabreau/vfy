//! Native session-hook callbacks and terminal text rendering.

use super::{CollectedSuite, execution, lifecycle::Context, reporting::writer::markup_code};
use pyo3::exceptions::{PyAssertionError, PyMemoryError, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;
use vfy_core::terminal_separator::{SeparatorError, separator};
use vfy_core::test_execution::CaseResult;

#[derive(Clone, Copy)]
pub(super) enum LoopMode {
    Run,
    CollectOnly,
    CollectionFailed(usize),
}

/// Why a loop stopped without running cases.
#[derive(Clone, Copy)]
enum CollectionStop {
    Interrupted,
    Completed,
}

pub(super) struct LoopOutcome {
    pub(super) cases: Vec<CaseResult>,
    pub(super) interrupted: bool,
    pub(super) error: Option<PyErr>,
    pub(super) session_failure: Option<execution::SessionFailure>,
}

impl LoopOutcome {
    /// A loop that ran no cases, as a collection stop leaves it.
    const fn stopped(stop: CollectionStop) -> Self {
        Self {
            cases: Vec::new(),
            interrupted: matches!(stop, CollectionStop::Interrupted),
            error: None,
            session_failure: None,
        }
    }

    fn finished(
        cases: Vec<CaseResult>,
        session_failure: Option<execution::SessionFailure>,
    ) -> Self {
        Self {
            cases,
            interrupted: false,
            error: None,
            session_failure,
        }
    }
}

enum LoopState {
    Pending(Box<CollectedSuite>),
    Running,
    Complete(Vec<CaseResult>),
    Failed(Vec<CaseResult>, Option<execution::SessionFailure>),
    Interrupted,
    Closed,
}

/// The callback releases all Python-bearing suite state before dispatch returns.
/// Retaining its hook implementation cannot retain a completed or skipped suite.
#[pyclass(frozen, unsendable)]
struct LoopCallback {
    state: RefCell<LoopState>,
    root: String,
    mode: LoopMode,
}

#[pymethods]
impl LoopCallback {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "PyO3 requires a bound getter for the callable signature protocol"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<bool> {
        let suite = match self.state.replace(LoopState::Running) {
            LoopState::Pending(suite) => suite,
            previous => {
                self.state.replace(previous);
                return Err(PyAssertionError::new_err(
                    "native runtestloop callback is no longer available",
                ));
            }
        };
        let mut cases = Vec::new();
        let mut session_failure = None;
        let result = match self.mode {
            LoopMode::Run => {
                execution::run_cases(py, *suite, &self.root, &mut cases, &mut session_failure)
            }
            LoopMode::CollectOnly => {
                drop(suite);
                Ok(())
            }
            LoopMode::CollectionFailed(count) => {
                drop(suite);
                let message = format!(
                    "{count} error{} during collection",
                    if count == 1 { "" } else { "s" }
                );
                let error = PyErr::from_value(
                    py.import("vfy.independent.facade")?
                        .getattr("Interrupted")?
                        .call1((message,))?,
                );
                self.state.replace(LoopState::Interrupted);
                return Err(error);
            }
        };
        match result {
            Ok(()) => {
                self.state.replace(LoopState::Complete(cases));
                Ok(true)
            }
            Err(error) => {
                self.state
                    .replace(LoopState::Failed(cases, session_failure));
                Err(error)
            }
        }
    }
}

pub(super) fn propagates_loop_error(py: Python<'_>, error: &PyErr) -> PyResult<bool> {
    Ok(error.is_instance_of::<crate::IndependentUsageError>(py)
        || error.is_instance_of::<crate::UnsupportedFeatureError>(py)
        || super::controls::is_control_error(py, error)?)
}

pub(super) fn execute_loop(
    py: Python<'_>,
    suite: CollectedSuite,
    root: &str,
    mode: LoopMode,
) -> PyResult<LoopOutcome> {
    let context = suite.context.clone_ref(py);
    let kwargs = PyDict::new(py);
    context
        .stage
        .set(super::lifecycle::state::LifecycleStage::Running);
    kwargs.set_item("session", &context.session)?;
    let callback = Py::new(
        py,
        LoopCallback {
            state: RefCell::new(LoopState::Pending(Box::new(suite))),
            root: root.to_owned(),
            mode,
        },
    )?;
    // Once the controller releases the workers, an exiting peer can cause a SIGINT.
    // Wait inside the loop's control boundary so even a late peer retains its result.
    let ready = context
        .invocation
        .worker(py)
        .map_or(Ok(()), |worker| worker.borrow(py).ready_and_wait(py));
    let outcome = ready.and_then(|()| {
        context.registry.bind(py).call_method1(
            "call_with_default",
            ("pytest_runtestloop", kwargs, &callback),
        )
    });
    let state = callback.borrow(py).state.replace(LoopState::Closed);
    match (outcome, state) {
        (Err(error), LoopState::Interrupted)
            if error.value(py).is_instance(
                &py.import("vfy.independent.facade")?
                    .getattr("Interrupted")?,
            )? =>
        {
            // Stock wrap_session notifies every KeyboardInterrupt, including the collection stop.
            super::controls::notify(
                py,
                context.registry.bind(py),
                &error,
                super::controls::Notification::Interrupt,
            )?;
            Ok(LoopOutcome::stopped(CollectionStop::Interrupted))
        }
        (Ok(_), LoopState::Interrupted) => Ok(LoopOutcome::stopped(CollectionStop::Completed)),
        (Err(error), LoopState::Failed(cases, Some(failure))) if failure.matches(py, &error) => {
            Ok(LoopOutcome::finished(cases, Some(failure)))
        }
        (Ok(_), LoopState::Failed(cases, Some(_)) | LoopState::Complete(cases)) => {
            Ok(LoopOutcome::finished(cases, None))
        }
        (Err(error), state) => {
            if error.is_instance_of::<crate::IndependentUsageError>(py)
                || error.is_instance_of::<crate::UnsupportedFeatureError>(py)
            {
                drop(state);
                return Err(error);
            }
            let cases = match state {
                LoopState::Complete(cases) | LoopState::Failed(cases, _) => cases,
                state => {
                    drop(state);
                    Vec::new()
                }
            };
            Ok(LoopOutcome {
                cases,
                interrupted: false,
                error: Some(error),
                session_failure: None,
            })
        }
        (Ok(_), _) => Err(crate::UnsupportedFeatureError::new_err(
            "independent pytest_runtestloop must complete its native execution callback",
        )),
    }
}

struct SummaryContext {
    config: Py<PyAny>,
    session: Py<PyAny>,
    registry: Py<crate::hooks::NativeHookRegistry>,
}

#[pyclass(frozen, unsendable)]
pub(super) struct NativeTerminalSummaryCallback {
    context: RefCell<Option<SummaryContext>>,
}

#[pymethods]
impl NativeTerminalSummaryCallback {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let context = self.context.borrow_mut().take().ok_or_else(|| {
            PyAssertionError::new_err("native terminal summary callback is no longer available")
        })?;
        let exitstatus = context.session.bind(py).getattr("exitstatus")?.extract()?;
        dispatch_terminal_summary(
            py,
            &context.config,
            &context.session,
            &context.registry,
            exitstatus,
        )
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(context) = self.context.try_borrow()
            && let Some(context) = context.as_ref()
        {
            visit.call(&context.config)?;
            visit.call(&context.session)?;
            visit.call(&context.registry)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let context = self.context.borrow_mut().take();
        drop(context);
    }
}

pub(super) fn summary_callback(
    py: Python<'_>,
    context: &Context,
) -> PyResult<Py<NativeTerminalSummaryCallback>> {
    Py::new(
        py,
        NativeTerminalSummaryCallback {
            context: RefCell::new(Some(SummaryContext {
                config: context.config.clone_ref(py),
                session: context.session.clone_ref(py),
                registry: context.registry.clone_ref(py),
            })),
        },
    )
}

fn dispatch_terminal_summary(
    py: Python<'_>,
    config: &Py<PyAny>,
    session: &Py<PyAny>,
    registry: &Py<crate::hooks::NativeHookRegistry>,
    exitstatus: i32,
) -> PyResult<()> {
    let reporter = registry
        .bind(py)
        .call_method1("get_plugin", ("terminalreporter",))?;
    if reporter.is_none() {
        // Unregistering pytest's terminal reporter also removes its session summary.
        return Ok(());
    }
    if super::reporting::terminal::session::has_result(&reporter)?
        && !super::reporting::terminal::session::has_parallel_progress(&reporter)?
    {
        reporter.getattr("_tw")?.call_method1("line", ("",))?;
    }
    if matches!(exitstatus, 0 | 1 | 2 | 4 | 5 | 6)
        && !config
            .bind(py)
            .call_method1("getoption", ("no_summary", false))?
            .is_truthy()?
    {
        super::reporting::terminal::session::begin_summary(&reporter)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("config", config)?;
        kwargs.set_item("terminalreporter", &reporter)?;
        kwargs.set_item("exitstatus", exitstatus)?;
        crate::hooks::NativeHookRegistry::call(
            registry.bind(py),
            py,
            "pytest_terminal_summary",
            &kwargs,
            false,
        )?;
    }
    // Stock writes the stop reason after the summary sections and before the interrupt banner.
    let shouldfail = session.bind(py).getattr("shouldfail")?;
    if shouldfail.is_truthy()? {
        let markup = PyDict::new(py);
        markup.set_item("red", true)?;
        reporter
            .getattr("write_sep")?
            .call(("!", shouldfail.str()?), Some(&markup))?;
    }
    // Stock writes the interrupt banner after the summary sections and before the final counts.
    if exitstatus == 2 {
        reporter.call_method0("report_interrupt_at_sessionfinish")?;
    }
    super::reporting::terminal::session::totals(&reporter)?;
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (sep, title, width))]
pub(crate) fn render_terminal_separator(
    sep: &str,
    title: Option<&str>,
    width: i64,
) -> PyResult<String> {
    match separator(sep, title, width) {
        Ok(rendered) => Ok(rendered),
        Err(SeparatorError::Empty) => Python::attach(|py| {
            // CPython owns the version-specific integer-division diagnostic.
            PyInt::new(py, width)
                .floor_div(0)
                .map(|value| value.to_string())
        }),
        Err(SeparatorError::TooLarge) => {
            Err(PyOverflowError::new_err("repeated string is too long"))
        }
        Err(SeparatorError::Memory) => Err(PyMemoryError::new_err(
            "terminal separator exceeds available capacity",
        )),
    }
}

fn ansi_text(content: &str, codes: &[u8]) -> String {
    if codes.is_empty() {
        return content.to_owned();
    }
    let mut rendered = String::new();
    for code in codes {
        use std::fmt::Write as _;
        let _ = write!(rendered, "\u{1b}[{code}m");
    }
    rendered.push_str(content);
    rendered.push_str("\u{1b}[0m");
    rendered
}

#[pyfunction]
pub(crate) fn render_terminal_text(
    py: Python<'_>,
    content: &str,
    markup: &Bound<'_, PyDict>,
    hasmarkup: bool,
) -> PyResult<String> {
    let mut validated = Vec::new();
    for (name, value) in markup.iter() {
        let name = name.extract::<String>()?;
        let Some(code) = markup_code(&name) else {
            return Err(PyValueError::new_err(format!(
                "unknown markup: {}",
                PyString::new(py, &name).repr()?
            )));
        };
        validated.push((code, value));
    }
    let mut codes = Vec::new();
    if hasmarkup {
        for (code, value) in validated {
            if value.is_truthy()? {
                codes.push(code);
            }
        }
    }
    Ok(ansi_text(content, &codes))
}
