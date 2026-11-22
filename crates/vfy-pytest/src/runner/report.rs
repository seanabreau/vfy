//! Owns failure rendering, skip diagnostics, phase results, exception
//! classification and formatting, and test return-value checks.

use super::model::{ReturnedTestValue, XfailPlan};
use super::runtime::TeardownFailure;
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError};
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, PyTupleMethods, Python,
};
use pyo3::types::{PyAny, PyDict, PyTuple, PyType};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, SkipLocation,
    split_python_strip,
};

pub(super) struct ItemFailure {
    pub(super) detail: String,
    pub(super) terminal_lead: String,
    pub(super) representation: Option<Py<PyAny>>,
}

pub(super) fn render_item_failure(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    error: &PyErr,
) -> PyResult<ItemFailure> {
    if error.traceback(py).is_none() {
        return Ok(ItemFailure {
            detail: format_pyerr(py, error),
            terminal_lead: String::new(),
            representation: None,
        });
    }
    let exception_info = py
        .import("_pytest._code.code")?
        .getattr("ExceptionInfo")?
        .call_method1("from_exception", (error.value(py),))?;
    let representation = item.call_method1("repr_failure", (&exception_info,))?;
    let terminal = format_failure_representation(py, &representation)?;
    // Records keep pytest's stripped `str(longrepr)`; the summary also prints
    // the leading blank line and indentation that stripping removes.
    let (lead, detail) = split_python_strip(&terminal);
    Ok(ItemFailure {
        terminal_lead: lead.to_owned(),
        detail: detail.to_owned(),
        representation: Some(representation.unbind()),
    })
}

fn format_failure_representation(
    py: Python<'_>,
    representation: &Bound<'_, PyAny>,
) -> PyResult<String> {
    if let Ok(detail) = representation.extract::<String>() {
        return Ok(detail);
    }
    // Keep the unstripped terminal rendering that pytest's failure summary
    // prints; `str()` strips its leading blank line and indentation.
    if !representation.hasattr("toterminal")? {
        return Ok(representation.str()?.to_string());
    }
    let stream = py.import("io")?.getattr("StringIO")?.call0()?;
    let writer = py
        .import("_pytest._io")?
        .getattr("TerminalWriter")?
        .call1((&stream,))?;
    representation.call_method1("toterminal", (&writer,))?;
    stream.call_method0("getvalue")?.extract()
}

/// Whether pytest's terminal summary renders a section for this case.
pub(super) fn reports_failure(case: &CaseResult) -> bool {
    let failed = |outcome| {
        matches!(
            outcome,
            ExecutionOutcome::Failed | ExecutionOutcome::Error | ExecutionOutcome::XFailed
        )
    };
    case.phases.iter().any(|phase| failed(phase.outcome))
        || case
            .subtests()
            .iter()
            .any(|subtest| failed(subtest.outcome))
}

/// Return pytest's `TestReport.head_line`, the domain of `Item.location`.
pub(super) fn item_head_line(item: &Bound<'_, PyAny>) -> PyResult<String> {
    item.getattr("location")?.get_item(2)?.extract()
}

fn skip_item_location(item: &Bound<'_, PyAny>) -> PyResult<SkipLocation> {
    let location = item.call_method0("reportinfo")?;
    let location = location.cast::<PyTuple>()?;
    let path = location.get_item(0)?.str()?.to_string();
    // Match pytest: its unknown-source sentinel -1 becomes the reported line 0.
    let line = location.get_item(1)?.add(1)?.extract::<usize>()?;
    Ok(SkipLocation { path, line })
}

fn skip_item_diagnostic(
    py: Python<'_>,
    location: &SkipLocation,
    message: &str,
) -> PyResult<String> {
    py.import("builtins")?
        .getattr("repr")?
        .call1(((location.path.as_str(), location.line, message),))?
        .extract()
}

pub(super) struct SkipDiagnostic {
    pub(super) detail: String,
    pub(super) location: Option<SkipLocation>,
    pub(super) exception: String,
}

pub(super) fn format_skip_diagnostic_with_location(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    error: &PyErr,
) -> PyResult<SkipDiagnostic> {
    if error.traceback(py).is_none() {
        let detail = format_pyerr(py, error);
        if error.value(py).getattr("_use_item_location")?.is_truthy()? {
            let location = skip_item_location(item)?;
            return Ok(SkipDiagnostic {
                detail: skip_item_diagnostic(py, &location, detail.trim_end())?,
                location: Some(location),
                exception: error.to_string().trim_end().to_owned(),
            });
        }
        return Ok(SkipDiagnostic {
            detail,
            location: None,
            exception: error.to_string().trim_end().to_owned(),
        });
    }
    let exception_info = py
        .import("_pytest._code.code")?
        .getattr("ExceptionInfo")?
        .call_method1("from_exception", (error.value(py),))?;
    let crash = exception_info.call_method0("_getreprcrash")?;
    let message = crash.getattr("message")?.extract::<String>()?;
    let use_item_location = error.value(py).getattr("_use_item_location")?.is_truthy()?;
    let location = if use_item_location {
        skip_item_location(item)?
    } else {
        SkipLocation {
            path: crash.getattr("path")?.str()?.to_string(),
            line: crash.getattr("lineno")?.extract::<usize>()?,
        }
    };
    let detail = skip_item_diagnostic(py, &location, &message)?;
    Ok(SkipDiagnostic {
        detail,
        location: Some(location),
        exception: message,
    })
}

pub(super) fn discard_success_output(case: &mut CaseResult) {
    if case.phases.iter().any(|phase| {
        matches!(
            phase.outcome,
            ExecutionOutcome::Failed
                | ExecutionOutcome::Error
                | ExecutionOutcome::Skipped
                | ExecutionOutcome::XFailed
        )
    }) || case.subtests().iter().any(|subtest| {
        matches!(
            subtest.outcome,
            ExecutionOutcome::Failed
                | ExecutionOutcome::Error
                | ExecutionOutcome::Skipped
                | ExecutionOutcome::XFailed
        )
    }) {
        return;
    }
    for phase in &mut case.phases {
        phase.clear_capture();
    }
}

pub(super) fn record_pytest_capture(
    item: &Bound<'_, PyAny>,
    phase: ExecutionPhase,
    stdout: &str,
    stderr: &str,
) -> PyResult<()> {
    // Pytest's logging scope appends its own section after capture completes.
    for (stream, content) in [("stdout", stdout), ("stderr", stderr)] {
        item.call_method1("add_report_section", (phase.as_str(), stream, content))?;
    }
    Ok(())
}

pub(super) struct PhaseClock {
    monotonic: Instant,
    start: f64,
}

impl PhaseClock {
    pub(super) fn start() -> Self {
        Self {
            monotonic: Instant::now(),
            start: epoch_seconds(),
        }
    }

    pub(super) fn finish(self) -> PhaseTiming {
        PhaseTiming {
            start: self.start,
            stop: epoch_seconds(),
            duration_ms: self.monotonic.elapsed().as_secs_f64() * 1_000.0,
        }
    }
}

#[derive(Clone, Copy)]
pub(super) struct PhaseTiming {
    pub(super) start: f64,
    pub(super) stop: f64,
    pub(super) duration_ms: f64,
}

fn epoch_seconds() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or_else(
        |error| -error.duration().as_secs_f64(),
        |duration| duration.as_secs_f64(),
    )
}

pub(super) fn phase_result(
    outcome: ExecutionOutcome,
    phase: ExecutionPhase,
    timing: PhaseTiming,
    stdout: impl Into<String>,
    stderr: impl Into<String>,
    detail: String,
) -> PhaseResult {
    PhaseResult::new(
        outcome,
        phase,
        timing.duration_ms,
        PhasePayload {
            exception: None,
            stdout: stdout.into(),
            stderr: stderr.into(),
            log: String::new(),
            detail,
            skip_location: None,
            terminal_lead: String::new(),
            report_sections: None,
            report_summary: None,
        },
    )
}

pub(super) fn is_skip_error(py: Python<'_>, error: &PyErr) -> bool {
    py.import("_pytest.outcomes")
        .and_then(|module| module.getattr("Skipped"))
        .is_ok_and(|skipped| error.matches(py, &skipped).unwrap_or(false))
}

pub(super) fn normalize_unittest_skip(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    error: &PyErr,
) -> PyResult<PyErr> {
    py.import("vfy._pytest.native_pytest")?
        .call_method1("normalize_unittest_skip", (item, error.value(py)))
        .map(PyErr::from_value)
}

pub(super) fn normalize_teardown_skip(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    failure: &mut Option<TeardownFailure>,
) -> PyResult<()> {
    if let Some(failure) = failure
        && let Some(error) = failure.exception.as_ref()
    {
        let normalized = normalize_unittest_skip(py, item, error)?;
        if !normalized.value(py).is(error.value(py)) {
            *failure = TeardownFailure::from_pyerr(py, &normalized);
        }
    }
    Ok(())
}

pub(super) fn is_xfail_error(py: Python<'_>, error: &PyErr) -> bool {
    py.import("_pytest.outcomes")
        .and_then(|module| module.getattr("XFailed"))
        .is_ok_and(|xfailed| error.matches(py, &xfailed).unwrap_or(false))
}

pub(super) fn xfail_matches_error(
    py: Python<'_>,
    error: &PyErr,
    xfail: &XfailPlan,
) -> PyResult<bool> {
    let Some(raises) = &xfail.raises else {
        return Ok(true);
    };
    let raises = raises.bind(py);
    if let Ok(abstract_raises) = py
        .import("_pytest.raises")
        .and_then(|module| module.getattr("AbstractRaises"))
        && raises.is_instance(&abstract_raises)?
    {
        return raises
            .call_method1("matches", (error.value(py),))
            .and_then(|matched| matched.is_truthy());
    }
    if raises.is_instance_of::<PyType>() || raises.is_instance_of::<PyTuple>() {
        py.import("builtins")?
            .getattr("isinstance")?
            .call1((error.value(py), raises))?
            .is_truthy()
    } else {
        Ok(false)
    }
}

pub(super) fn is_control_error(py: Python<'_>, error: &PyErr) -> bool {
    if error.is_instance_of::<PyKeyboardInterrupt>(py) {
        return true;
    }
    py.import("_pytest.outcomes")
        .and_then(|module| module.getattr("Exit"))
        .is_ok_and(|exit| error.matches(py, &exit).unwrap_or(false))
}

pub(super) fn format_pyerr(py: Python<'_>, error: &PyErr) -> String {
    if let Ok(fixture_lookup_error) = py
        .import("_pytest.fixtures")
        .and_then(|module| module.getattr("FixtureLookupError"))
        && error.matches(py, &fixture_lookup_error).unwrap_or(false)
        && let Ok(formatted) = error.value(py).call_method0("formatrepr")
        && let Ok(formatted) = formatted.str()
    {
        return formatted.to_string();
    }
    if let Ok(failed) = py
        .import("_pytest.outcomes")
        .and_then(|module| module.getattr("Failed"))
        && error.matches(py, &failed).unwrap_or(false)
    {
        let value = error.value(py);
        if value
            .getattr("pytrace")
            .and_then(|pytrace| pytrace.is_truthy())
            .is_ok_and(|pytrace| !pytrace)
        {
            return value
                .str()
                .map_or_else(|_| error.to_string(), |message| message.to_string());
        }
    }
    let traceback = py.import("traceback");
    if let Ok(traceback) = traceback {
        let trace = error.traceback(py).map_or_else(|| py.None(), Py::from);
        if let Ok(lines) = traceback.call_method1(
            "format_exception",
            (error.get_type(py), error.value(py), trace),
        ) && let Ok(lines) = lines.extract::<Vec<String>>()
        {
            return lines.concat();
        }
    }
    error.to_string()
}

pub(super) fn canonical_pytest_exception(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    error: &PyErr,
    outcome: ExecutionOutcome,
    xfail_reason: Option<&str>,
    formatted: Option<&ItemFailure>,
) -> PyResult<Option<String>> {
    if matches!(
        outcome,
        ExecutionOutcome::Passed | ExecutionOutcome::XPassed
    ) {
        return Ok(None);
    }
    if outcome == ExecutionOutcome::XFailed {
        let value = match xfail_reason {
            Some(reason) => reason.to_owned(),
            None => error.value(py).str()?.to_string(),
        };
        return Ok(Some(value));
    }
    if error.traceback(py).is_none() {
        let message = error.to_string();
        return Ok(Some(if outcome == ExecutionOutcome::Skipped {
            message.trim_end().to_owned()
        } else {
            message
        }));
    }
    if outcome == ExecutionOutcome::Skipped {
        let exception_info = py
            .import("_pytest._code.code")?
            .getattr("ExceptionInfo")?
            .call_method1("from_exception", (error.value(py),))?;
        return exception_info
            .call_method0("_getreprcrash")?
            .getattr("message")?
            .extract()
            .map(Some);
    }

    // Diagnostic rendering can invoke user repr callbacks. Reuse the same
    // representation for the traceback and canonical message, like pytest.
    let representation = if let Some(representation) =
        formatted.and_then(|failure| failure.representation.as_ref())
    {
        representation.bind(py).clone()
    } else {
        let exception_info = py
            .import("_pytest._code.code")?
            .getattr("ExceptionInfo")?
            .call_method1("from_exception", (error.value(py),))?;
        item.call_method1("repr_failure", (&exception_info,))?
    };
    if let Ok(crash) = representation.getattr("reprcrash")
        && let Ok(message) = crash.getattr("message")
        && let Ok(message) = message.extract::<String>()
    {
        return Ok(Some(message));
    }
    if let Ok(tuple) = representation.cast::<PyTuple>()
        && tuple.len() == 3
        && let Ok(message) = tuple.get_item(2)?.extract::<String>()
    {
        return Ok(Some(message));
    }
    Ok(Some(match formatted {
        Some(failure) => failure.detail.clone(),
        None => representation.str()?.to_string(),
    }))
}

pub(super) fn reject_anyio_test(py: Python<'_>) -> PyResult<()> {
    let message = "vfy native execution does not support AnyIO tests.\n\
        Run them with stock pytest and vfy coverage:\n\
        pytest -p no:pytest_cov -p vfy.pytest_plugin --cov";
    let outcomes = py.import("_pytest.outcomes")?;
    if py.import("_pytest.python")?.hasattr("async_fail")? {
        let kwargs = PyDict::new(py);
        kwargs.set_item("pytrace", false)?;
        outcomes.call_method("fail", (message,), Some(&kwargs))?;
    } else {
        // Preserve older pytest's warning-and-skip outcome for unhandled coroutines.
        let warning = py
            .import("_pytest.warning_types")?
            .getattr("PytestUnhandledCoroutineWarning")?
            .call1((message,))?;
        py.import("warnings")?.call_method1("warn", (warning,))?;
        outcomes.call_method1("skip", (message,))?;
    }
    Ok(())
}

pub(super) fn handle_unhandled_async_test(py: Python<'_>, nodeid: &str) -> PyResult<()> {
    let pytest_python = py.import("_pytest.python")?;
    let helper = if pytest_python.hasattr("async_fail")? {
        "async_fail"
    } else {
        "async_warn_and_skip"
    };
    pytest_python.call_method1(helper, (nodeid,)).map(|_| ())
}

pub(super) fn check_test_result(
    py: Python<'_>,
    result: &Bound<'_, PyAny>,
    nodeid: &str,
    returned_test_value: ReturnedTestValue,
) -> PyResult<()> {
    if result.hasattr("__await__")? || result.hasattr("__aiter__")? {
        // Match pytest's async result check and its unawaited-coroutine warning.
        handle_unhandled_async_test(py, nodeid)?;
    } else if !result.is_none() {
        let message = returned_test_value.message(result, nodeid)?;
        if matches!(returned_test_value, ReturnedTestValue::FailValue) {
            let kwargs = PyDict::new(py);
            kwargs.set_item("pytrace", false)?;
            return py
                .import("_pytest.outcomes")?
                .call_method("fail", (message,), Some(&kwargs))
                .map(|_| ());
        }
        let pytest_python = py.import("_pytest.python")?;
        let pytest_pyfunc_call = pytest_python.getattr("pytest_pyfunc_call")?;
        let source = py
            .import("inspect")?
            .call_method1("getsourcelines", (&pytest_pyfunc_call,))?;
        let source_lines = source.get_item(0)?.extract::<Vec<String>>()?;
        let first_line = source.get_item(1)?.extract::<usize>()?;
        let warning_line = source_lines
            .iter()
            .position(|line| line.trim_start().starts_with("warnings.warn("))
            .map(|offset| first_line + offset)
            .ok_or_else(|| PyRuntimeError::new_err("pytest return warning source was not found"))?;
        let warning = py
            .import("_pytest.warning_types")?
            .getattr("PytestReturnNotNoneWarning")?
            .call1((message,))?;
        let warning_type = py
            .import("_pytest.warning_types")?
            .getattr("PytestReturnNotNoneWarning")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("module", pytest_python.getattr("__name__")?)?;
        let module_dict = pytest_python.getattr("__dict__")?.cast_into::<PyDict>()?;
        let registry = if let Some(registry) = module_dict.get_item("__warningregistry__")? {
            registry
        } else {
            let registry = PyDict::new(py);
            module_dict.set_item("__warningregistry__", &registry)?;
            registry.into_any()
        };
        kwargs.set_item("registry", registry)?;
        py.import("warnings")?.call_method(
            "warn_explicit",
            (
                warning,
                warning_type,
                pytest_pyfunc_call
                    .getattr("__code__")?
                    .getattr("co_filename")?,
                warning_line,
            ),
            Some(&kwargs),
        )?;
    }
    Ok(())
}

pub(super) fn take_unittest_exception(item: &Bound<'_, PyAny>) -> PyResult<Option<PyErr>> {
    let exceptions = item.getattr("_excinfo")?;
    if exceptions.is_none() || exceptions.len()? == 0 {
        return Ok(None);
    }
    let exception_info = exceptions.call_method1("pop", (0,))?;
    let exception = exception_info.getattr("value")?;
    let traceback = exception.getattr("__traceback__")?;
    let exception = exception.call_method1("with_traceback", (traceback,))?;
    Ok(Some(PyErr::from_value(exception)))
}
