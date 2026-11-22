//! The Item failure protocol returns the report's retained representation.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use vfy_core::test_execution::{ExecutionPhase, PhasePayload, split_python_strip};

/// Existing native traceback rendering keeps its metadata beside its text.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeDiagnosticRepr {
    payload: PhasePayload,
}

#[pymethods]
impl NativeDiagnosticRepr {
    fn __str__(&self) -> &str {
        &self.payload.detail
    }

    fn toterminal(slf: &Bound<'_, Self>, writer: &Bound<'_, PyAny>) -> PyResult<()> {
        let text = {
            let representation = slf.borrow();
            format!(
                "{}{}\n",
                representation.payload.terminal_lead, representation.payload.detail
            )
        };
        writer.call_method1("write", (text,))?;
        Ok(())
    }
}

pub(super) fn diagnostic(
    py: Python<'_>,
    payload: PhasePayload,
) -> PyResult<Py<NativeDiagnosticRepr>> {
    Py::new(py, NativeDiagnosticRepr { payload })
}

pub(super) fn preserve_diagnostic(
    representation: &Bound<'_, PyAny>,
    payload: &mut PhasePayload,
) -> PyResult<()> {
    let native = representation.cast::<NativeDiagnosticRepr>().is_ok();
    let diagnostic = if native {
        representation.clone()
    } else {
        let kind = representation
            .py()
            .import("vfy.independent.diagnostics")?
            .getattr("ExceptionChainRepr")?;
        if !representation.is_instance(&kind)? {
            return Ok(());
        }
        representation.getattr("_vfy_diagnostic")?
    };
    if let Ok(diagnostic) = diagnostic.cast::<NativeDiagnosticRepr>() {
        let diagnostic = diagnostic.borrow();
        payload.exception.clone_from(&diagnostic.payload.exception);
        payload.detail.clone_from(&diagnostic.payload.detail);
        if native {
            payload
                .terminal_lead
                .clone_from(&diagnostic.payload.terminal_lead);
        } else {
            payload.terminal_lead = structured_lead(representation)?;
        }
    }
    Ok(())
}

/// The blank line and indentation that pytest's terminal summary prints before a chain.
///
/// The captured diagnostic text always uses the automatic frame presentation, so its lead can
/// differ from the structured representation that the summary writes for `--tb=short` and
/// `--tb=long`. The representation's own unstripped `toterminal` output gives the lead for the
/// requested style.
fn structured_lead(representation: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = representation.py();
    let stream = py.import("io")?.getattr("StringIO")?.call0()?;
    let writer = py
        .import("vfy.independent.terminal")?
        .call_method1("report_writer", (&stream,))?;
    representation.call_method1("toterminal", (writer,))?;
    let text = stream.call_method0("getvalue")?.extract::<String>()?;
    Ok(split_python_strip(&text).0.to_owned())
}

pub(super) fn dispatch(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    when: ExecutionPhase,
) -> PyResult<Py<PyAny>> {
    if when == ExecutionPhase::Call {
        return Ok(item.call_method1("repr_failure", (info,))?.unbind());
    }
    let style = item
        .getattr("config")?
        .call_method1("getoption", ("tbstyle", "auto"))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("style", style)?;
    Ok(item
        .call_method("_repr_failure_py", (info,), Some(&kwargs))?
        .unbind())
}

#[pyfunction]
pub(crate) fn independent_item_repr_failure(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    excinfo: &Bound<'_, PyAny>,
    style: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    // Plugin items have no test function to anchor the traceback.
    let owner = if item.is_instance(
        &py.import("vfy.independent.collectors")?
            .getattr("Function")?,
    )? {
        super::diagnostics::TracebackOwner::Function
    } else {
        super::diagnostics::TracebackOwner::Collector
    };
    repr_failure(py, item, excinfo, style, owner)
}

fn repr_failure(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    excinfo: &Bound<'_, PyAny>,
    style: &Bound<'_, PyAny>,
    owner: super::diagnostics::TracebackOwner,
) -> PyResult<Py<PyAny>> {
    let value = excinfo.getattr("value")?;
    let conftest = py
        .import("vfy.independent.errors")?
        .getattr("ConftestImportFailure")?;
    let original = if value.is_instance(&conftest)? {
        let cause = value.getattr("cause")?;
        Some(super::diagnostics::named_info(
            py,
            &cause,
            &cause.getattr("__traceback__")?,
        )?)
    } else {
        None
    };
    let excinfo = original.as_ref().unwrap_or(excinfo);
    let value = excinfo.getattr("value")?;
    let lookup_error = py
        .import("vfy.independent.fixture_lookup")?
        .getattr("FixtureLookupError")?;
    if value.is_instance(&lookup_error)? {
        return Ok(value.call_method0("formatrepr")?.unbind());
    }
    let failed = py.import("vfy.independent.facade")?.getattr("Failed")?;
    if value.is_instance(&failed)?
        && !value.getattr("pytrace")?.is_truthy()?
        && !excinfo.getattr("tb")?.is_none()
        && !value.is_instance(&py.import("builtins")?.getattr("BaseExceptionGroup")?)?
    {
        return super::value::create(py, item, excinfo, owner);
    }
    let configured = item
        .getattr("config")?
        .call_method1("getoption", ("tbstyle", "auto"))?
        .extract::<String>()?;
    let selected = if style.is_none() {
        if configured == "short" {
            "short".to_owned()
        } else {
            "long".to_owned()
        }
    } else {
        style.extract::<String>()?
    };
    let presentation = match selected.as_str() {
        "auto" | "long" if configured == "auto" => {
            vfy_core::test_diagnostics::DiagnosticStyle::Auto
        }
        "auto" | "long" => vfy_core::test_diagnostics::DiagnosticStyle::Long,
        "short" => vfy_core::test_diagnostics::DiagnosticStyle::Short,
        "native" => return native(py, item, excinfo, owner),
        "line" | "no" => return crash_line(py, excinfo),
        _ => {
            return Err(crate::UnsupportedFeatureError::new_err(format!(
                "unsupported traceback style {selected:?}"
            )));
        }
    };
    super::graph::create(py, item, excinfo, presentation, owner)
}

/// Python's own traceback from the test function's frame, as pytest's native
/// style prints it, with the crash location the short summary reports.
fn native(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    excinfo: &Bound<'_, PyAny>,
    owner: super::diagnostics::TracebackOwner,
) -> PyResult<Py<PyAny>> {
    let mut traceback = excinfo.getattr("tb")?;
    if matches!(owner, super::diagnostics::TracebackOwner::Function) {
        let code = super::function_code(py, item)?;
        let mut entry = traceback.clone();
        while !entry.is_none() {
            if entry.getattr("tb_frame")?.getattr("f_code")?.is(&code) {
                traceback = entry;
                break;
            }
            entry = entry.getattr("tb_next")?;
        }
    }
    let lines = py.import("traceback")?.call_method1(
        "format_exception",
        (
            excinfo.getattr("type")?,
            excinfo.getattr("value")?,
            traceback,
        ),
    )?;
    let types = py.import("vfy.independent.diagnostics")?;
    let (path, line, message) = crash(excinfo)?;
    let location = types
        .getattr("ReprFileLocation")?
        .call1((path, line, message))?;
    let traceback = types.getattr("ReprTracebackNative")?.call1((lines,))?;
    Ok(types
        .getattr("ReprExceptionInfo")?
        .call1((traceback, location))?
        .unbind())
}

/// The last traceback entry and the exception text, pytest's `reprcrash`.
fn crash(excinfo: &Bound<'_, PyAny>) -> PyResult<(String, i64, String)> {
    let mut entry = excinfo.getattr("tb")?;
    let mut last = None;
    while !entry.is_none() {
        let next = entry.getattr("tb_next")?;
        last = Some(entry);
        entry = next;
    }
    let message = excinfo.call_method0("exconly")?.extract::<String>()?;
    let Some(last) = last else {
        return Ok((String::new(), 0, message));
    };
    let path = last
        .getattr("tb_frame")?
        .getattr("f_code")?
        .getattr("co_filename")?
        .extract::<String>()?;
    Ok((path, last.getattr("tb_lineno")?.extract()?, message))
}

/// pytest's `--tb=line` output: the exception text, then the crash location.
fn crash_line(py: Python<'_>, excinfo: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let (path, line, message) = crash(excinfo)?;
    let mut lines = message
        .lines()
        .map(|exception_line| format!("E   {exception_line}"))
        .collect::<Vec<_>>();
    let first = message.lines().next().unwrap_or_default();
    lines.push(format!("{path}:{line}: {first}"));
    let types = py.import("vfy.independent.diagnostics")?;
    let entry = types.getattr("ReprEntry")?.call1((
        PyList::new(py, lines)?,
        py.None(),
        py.None(),
        py.None(),
        "value",
    ))?;
    let traceback =
        types
            .getattr("ReprTraceback")?
            .call1((PyList::new(py, [entry])?, py.None(), "line"))?;
    let location = types
        .getattr("ReprFileLocation")?
        .call1((path, line, message))?;
    Ok(types
        .getattr("ReprExceptionInfo")?
        .call1((traceback, location))?
        .unbind())
}

/// pytest's `importtestmodule` text for a failed module import, rendered by the short graph.
///
/// The Python loader wraps the original `ImportError` or `SyntaxError`. The result is a
/// plain text representation with no `reprcrash`, as pytest's `CollectErrorRepr` is, so the
/// short summary shows only the node id.
fn module_import_failure(
    py: Python<'_>,
    collector: &Bound<'_, PyAny>,
    failure: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let cause = failure.getattr("cause")?;
    let info = super::diagnostics::named_info(py, &cause, &cause.getattr("__traceback__")?)?;
    let representation = super::graph::create(
        py,
        collector,
        &info,
        vfy_core::test_diagnostics::DiagnosticStyle::Short,
        super::diagnostics::TracebackOwner::ModuleImport,
    )?;
    // pytest renders the chain with `str()`, which strips the surrounding whitespace.
    let rendered = representation.bind(py).str()?;
    let body = rendered.to_str()?.trim();
    let text = if cause.is_instance_of::<pyo3::exceptions::PySyntaxError>() {
        body.to_owned()
    } else {
        let path = failure.getattr("path")?.str()?;
        let path = path.to_str()?;
        format!(
            "ImportError while importing test module '{path}'.\n\
             Hint: make sure your test modules/packages have valid Python names.\n\
             Traceback:\n\
             {body}"
        )
    };
    let payload = PhasePayload {
        detail: text,
        ..PhasePayload::default()
    };
    Ok(diagnostic(py, payload)?.into_any())
}

/// Builtin collectors select their short default before graph projection.
#[pyfunction]
pub(crate) fn independent_collector_repr_failure(
    py: Python<'_>,
    collector: &Bound<'_, PyAny>,
    excinfo: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let config = collector.getattr("config")?;
    let value = excinfo.getattr("value")?;
    if value.is_instance(
        &py.import("vfy.independent.errors")?
            .getattr("TestModuleImportFailure")?,
    )? {
        return module_import_failure(py, collector, &value);
    }
    if value.is_instance(&collector.getattr("CollectError")?)?
        && !config
            .call_method1("getoption", ("fulltrace", false))?
            .is_truthy()?
    {
        return Ok(value
            .getattr("args")?
            .get_item(0)?
            .str()?
            .into_any()
            .unbind());
    }
    let configured = config
        .call_method1("getoption", ("tbstyle", "auto"))?
        .extract::<String>()?;
    let style = if configured == "auto" {
        "short"
    } else {
        &configured
    };
    repr_failure(
        py,
        collector,
        excinfo,
        &pyo3::types::PyString::new(py, style).into_any(),
        super::diagnostics::TracebackOwner::Collector,
    )
}
