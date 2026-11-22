//! Independent logging and warning scopes retain the shared Rust report model.

use super::global_capture::{NativeCaptureSuspension, NativeGlobalCapture, locked};
use crate::module_cache::Module;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString, PyTuple};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Mutex, MutexGuard};
use vfy_core::test_execution::{ExecutionPhase, WarningRecord};

mod hooks;
mod subtest;
pub(super) use hooks::NativeLoggingHooks;
pub(super) use subtest::SubtestLogs;
pub(super) mod warnings;
pub(super) use warnings::NativeWarningRecorder;

fn phase_index(phase: ExecutionPhase) -> usize {
    match phase {
        ExecutionPhase::Setup => 0,
        ExecutionPhase::Call => 1,
        ExecutionPhase::Teardown => 2,
    }
}

#[derive(Default)]
enum PhaseRecords {
    #[default]
    Absent,
    Empty,
    Live(Py<PyList>),
}

#[derive(Clone, Copy)]
enum RecordAccess {
    Current,
    Historical,
}

/// Original `CPython` records are materialized only for the mutable fixture API.
#[pyclass(frozen)]
struct NativeLogState {
    phase: AtomicU8,
    active: AtomicBool,
    records: Mutex<[PhaseRecords; 3]>,
}

impl NativeLogState {
    fn new() -> Self {
        Self {
            phase: AtomicU8::new(0),
            active: AtomicBool::new(true),
            records: Mutex::new(std::array::from_fn(|_| PhaseRecords::Absent)),
        }
    }

    fn begin(&self, phase: ExecutionPhase) {
        self.phase.store(
            u8::try_from(phase_index(phase)).expect("three logging phases fit in u8"),
            Ordering::Relaxed,
        );
        self.reset_records(phase_index(phase));
    }

    fn locked_records(&self) -> MutexGuard<'_, [PhaseRecords; 3]> {
        self.records
            .lock()
            .expect("native logging record lock is not poisoned")
    }

    fn reset_records(&self, index: usize) {
        let previous = std::mem::replace(&mut self.locked_records()[index], PhaseRecords::Empty);
        // Destructors on user records can reenter the newly published phase.
        drop(previous);
    }

    fn phase_records(&self, py: Python<'_>, index: usize, access: RecordAccess) -> Py<PyList> {
        let retain = {
            let records = self.locked_records();
            match &records[index] {
                PhaseRecords::Live(records) => return records.clone_ref(py),
                PhaseRecords::Absent => matches!(access, RecordAccess::Current),
                PhaseRecords::Empty => true,
            }
        };
        // An unstarted phase returns a fresh list, as caplog.get_records does.
        // Allocation may run a finalizer that publishes the same phase's list.
        let replacement = PyList::empty(py).unbind();
        if !retain {
            return replacement;
        }
        let (current, unused) = {
            let mut records = self.locked_records();
            if let PhaseRecords::Live(existing) = &records[index] {
                (existing.clone_ref(py), Some(replacement))
            } else {
                let current = replacement.clone_ref(py);
                records[index] = PhaseRecords::Live(replacement);
                (current, None)
            }
        };
        drop(unused);
        current
    }
}

#[pymethods]
impl NativeLogState {
    #[getter]
    fn active(&self) -> bool {
        self.active.load(Ordering::Relaxed)
    }

    #[getter(records)]
    fn current_records(&self, py: Python<'_>) -> Py<PyList> {
        let index = usize::from(self.phase.load(Ordering::Relaxed));
        self.phase_records(py, index, RecordAccess::Current)
    }

    fn get_records(&self, py: Python<'_>, when: &str) -> Py<PyList> {
        let phase = match when {
            "setup" => ExecutionPhase::Setup,
            "call" => ExecutionPhase::Call,
            "teardown" => ExecutionPhase::Teardown,
            _ => return PyList::empty(py).unbind(),
        };
        self.phase_records(py, phase_index(phase), RecordAccess::Historical)
    }

    fn emit(&self, py: Python<'_>, record: &Bound<'_, PyAny>) -> PyResult<()> {
        let records = self.current_records(py);
        records.bind(py).append(record)?;
        Ok(())
    }

    fn clear(&self, py: Python<'_>) -> PyResult<()> {
        let records = self.current_records(py);
        records.bind(py).call_method0("clear")?;
        Ok(())
    }

    fn reset(&self) {
        self.reset_records(usize::from(self.phase.load(Ordering::Relaxed)));
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(records) = self.records.try_lock() {
            for record in &*records {
                if let PhaseRecords::Live(record) = record {
                    visit.call(record)?;
                }
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let previous = std::mem::take(&mut *self.locked_records());
        drop(previous);
    }
}

/// Values resolved from the single builtin option/INI declaration inventory.
pub(super) struct LogCaptureSettings {
    pub level: Option<i32>,
    pub format: String,
    pub date_format: Option<String>,
    pub auto_indent: i32,
    pub live: Option<LiveLogSettings>,
    shared_live: Option<Py<NativeLiveStream>>,
}

pub(super) struct LiveLogSettings {
    pub capture: Option<Py<NativeGlobalCapture>>,
    pub level: Option<i32>,
    pub format: String,
    pub date_format: Option<String>,
    pub stream: Py<PyAny>,
}

enum LoopCapture {
    Outside,
    Active(Option<Py<NativeLiveStream>>),
}

fn option_ini<'py>(config: &Bound<'py, PyAny>, names: &[&str]) -> PyResult<Bound<'py, PyAny>> {
    for name in names {
        let option = config.call_method1("getoption", (name, config.py().None()))?;
        let value = if option.is_none() {
            config.call_method1("getini", (name,))?
        } else {
            option
        };
        if value.is_truthy()? {
            return Ok(value);
        }
    }
    Ok(config.py().None().into_bound(config.py()))
}

fn log_level(config: &Bound<'_, PyAny>, names: &[&str]) -> PyResult<Option<i32>> {
    let py = config.py();
    let value = option_ini(config, names)?;
    if value.is_none() {
        return Ok(None);
    }
    let value = if let Ok(name) = value.extract::<String>() {
        let name = name.to_uppercase();
        match py.import("logging")?.getattr(name.as_str()) {
            Ok(level) => level,
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                PyString::new(py, &name).into_any()
            }
            Err(error) => return Err(error),
        }
    } else {
        value
    };
    let result = Module::Builtins
        .get(py)?
        .call_method1("int", (&value,))
        .and_then(|value| value.extract::<i32>());
    result.map(Some).map_err(|_| {
        crate::IndependentUsageError::new_err(format!(
            "'{}' is not recognized as a logging level name for '{}'. Please consider passing the logging level num instead.",
            value.str().map_or_else(|_| String::new(), |value| value.to_string_lossy().into_owned()),
            names[0]
        ))
    })
}

impl LogCaptureSettings {
    pub(super) fn from_config(
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
        stream: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let plugin = config
            .getattr("pluginmanager")?
            .call_method1("get_plugin", ("logging-plugin",))?;
        let active = if plugin.is_none() {
            LoopCapture::Outside
        } else {
            plugin
                .getattr("_capture")?
                .extract::<Py<NativeLoggingHooks>>()?
                .borrow(py)
                .loop_capture(py)?
        };
        Self::resolve(py, config, stream, active)
    }

    fn for_loop(
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
        stream: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        Self::resolve(py, config, stream, LoopCapture::Outside)
    }

    fn resolve(
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
        stream: &Bound<'_, PyAny>,
        active: LoopCapture,
    ) -> PyResult<Self> {
        let format = option_ini(config, &["log_format"])?;
        let date_format = option_ini(config, &["log_date_format"])?;
        let live = if matches!(&active, LoopCapture::Active(_)) {
            None
        } else if !config
            .call_method1("getoption", ("log_cli_level", py.None()))?
            .is_none()
            || config.call_method1("getini", ("log_cli",))?.is_truthy()?
        {
            let format = option_ini(config, &["log_cli_format", "log_format"])?;
            let date_format = option_ini(config, &["log_cli_date_format", "log_date_format"])?;
            Some(LiveLogSettings {
                capture: super::global_capture::owner(config)?,
                level: log_level(config, &["log_cli_level", "log_level"])?,
                format: format.extract::<Option<String>>()?.unwrap_or_default(),
                date_format: date_format.extract()?,
                stream: stream.clone().unbind(),
            })
        } else {
            None
        };
        Ok(Self {
            level: log_level(config, &["log_level"])?,
            format: format.extract::<Option<String>>()?.unwrap_or_default(),
            date_format: date_format.extract()?,
            auto_indent: parse_auto_indent(py, &option_ini(config, &["log_auto_indent"])?),
            live,
            shared_live: match active {
                LoopCapture::Outside => None,
                LoopCapture::Active(live) => live,
            },
        })
    }
}

pub(super) fn parse_auto_indent(py: Python<'_>, value: &Bound<'_, PyAny>) -> i32 {
    if value.is_instance_of::<PyBool>() {
        return if value.is_truthy().unwrap_or(false) {
            -1
        } else {
            0
        };
    }
    if value.is_instance_of::<PyInt>() {
        return value.extract().unwrap_or(0);
    }
    if let Ok(text) = value.extract::<String>() {
        if let Ok(number) = Module::Builtins
            .get(py)
            .and_then(|m| m.call_method1("int", (&text,)))
            && let Ok(number) = number.extract()
        {
            return number;
        }
        if matches!(
            text.to_lowercase().as_str(),
            "1" | "y" | "yes" | "t" | "true" | "on"
        ) {
            return -1;
        }
    }
    0
}

#[pyclass(frozen)]
struct NativeLogStyle {
    format: String,
    auto_indent: i32,
}

#[pymethods]
impl NativeLogStyle {
    #[pyo3(name = "usesTime")]
    fn uses_time(&self) -> bool {
        self.format.contains("%(asctime)")
    }

    #[getter(_fmt)]
    fn template(&self) -> &str {
        &self.format
    }

    fn format(&self, py: Python<'_>, record: &Bound<'_, PyAny>) -> PyResult<String> {
        let attributes = record.getattr("__dict__")?.cast_into::<PyDict>()?;
        let message = record.getattr("message")?.extract::<String>()?;
        let indentation = match record.getattr("auto_indent") {
            Ok(value) => parse_auto_indent(py, &value),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyAttributeError>(py) => {
                self.auto_indent
            }
            Err(error) => return Err(error),
        };
        let template = PyString::new(py, &self.format);
        if message.contains('\n') && indentation != 0 {
            let mut lines = PyString::new(py, &message)
                .call_method0("splitlines")?
                .extract::<Vec<String>>()?;
            let values = attributes.copy()?;
            values.set_item("message", &lines[0])?;
            let formatted = format_log_attributes(py, &template, &values)?;
            let indentation = if indentation < 0 {
                let clean = py
                    .import("re")?
                    .call_method1("sub", ("\\x1b\\[[\\d;]+m", "", &formatted))?
                    .extract::<String>()?;
                clean
                    .find(&lines[0])
                    .map_or(0, |index| clean[..index].chars().count())
            } else {
                usize::try_from(indentation).unwrap_or(0)
            };
            lines[0] = formatted;
            return Ok(lines.join(&format!("\n{}", " ".repeat(indentation))));
        }
        format_log_attributes(py, &template, &attributes)
    }
}

fn format_log_attributes(
    py: Python<'_>,
    template: &Bound<'_, PyString>,
    attributes: &Bound<'_, PyDict>,
) -> PyResult<String> {
    template
        .call_method1("__mod__", (attributes,))
        .map_err(|error| {
            if error.is_instance_of::<pyo3::exceptions::PyKeyError>(py) {
                let name = error.value(py).str().map_or_else(
                    |_| String::new(),
                    |value| value.to_string_lossy().into_owned(),
                );
                pyo3::exceptions::PyValueError::new_err(format!(
                    "Formatting field not found in record: {name}"
                ))
            } else {
                error
            }
        })?
        .extract()
}

fn log_formatter<'py>(
    py: Python<'py>,
    format: &str,
    date_format: Option<&str>,
    auto_indent: i32,
) -> PyResult<Bound<'py, PyAny>> {
    let formatter = Module::Capture
        .get(py)?
        .call_method1("DatetimeFormatter", (format, date_format))?;
    formatter.setattr(
        "_style",
        Py::new(
            py,
            NativeLogStyle {
                format: formatter.getattr("_fmt")?.extract()?,
                auto_indent,
            },
        )?,
    )?;
    Ok(formatter)
}

#[pyclass(frozen)]
struct NativeLiveStream {
    capture: Option<Py<NativeGlobalCapture>>,
    stream: Py<PyAny>,
    phase: AtomicU8,
    started: AtomicBool,
    width: i64,
    level: Option<i32>,
    handler: Mutex<Option<Py<PyAny>>>,
}

impl NativeLiveStream {
    fn create(py: Python<'_>, settings: &LiveLogSettings, auto_indent: i32) -> PyResult<Py<Self>> {
        let stream = Py::new(
            py,
            Self {
                capture: settings
                    .capture
                    .as_ref()
                    .map(|capture| capture.clone_ref(py)),
                stream: settings.stream.clone_ref(py),
                phase: AtomicU8::new(0),
                started: AtomicBool::new(false),
                width: py
                    .import("shutil")?
                    .call_method0("get_terminal_size")?
                    .getattr("columns")?
                    .extract()?,
                level: settings.level,
                handler: Mutex::new(None),
            },
        )?;
        let handler = py
            .import("logging")?
            .call_method1("StreamHandler", (&stream,))?;
        handler.call_method1(
            "setFormatter",
            (log_formatter(
                py,
                &settings.format,
                settings.date_format.as_deref(),
                auto_indent,
            )?,),
        )?;
        *locked(&stream.get().handler, "live logging handler")? = Some(handler.unbind());
        Ok(stream)
    }

    fn handler(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        locked(&self.handler, "live logging handler")?
            .as_ref()
            .map(|handler| handler.clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("live logging handler has been cleared")
            })
    }

    /// Runs stream protocol calls while the owning capture yields the process streams.
    fn suspended<T>(&self, py: Python<'_>, operation: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
        let suspension = self
            .capture
            .as_ref()
            .map(|owner| NativeCaptureSuspension::start(py, owner))
            .transpose()?;
        let result = operation();
        if let Some(suspension) = suspension {
            suspension.finish(py)?;
        }
        result
    }
}

#[pymethods]
impl NativeLiveStream {
    fn write(&self, py: Python<'_>, text: &str) -> PyResult<usize> {
        self.suspended(py, || {
            if !self.started.swap(true, Ordering::Relaxed) {
                let when = match self.phase.load(Ordering::Relaxed) {
                    0 => "setup",
                    1 => "call",
                    _ => "teardown",
                };
                let title = format!("live log {when}");
                let section =
                    super::session_hooks::render_terminal_separator("-", Some(&title), self.width)?;
                let header = format!("\n{section}\n");
                self.stream.bind(py).call_method1("write", (header,))?;
            }
            self.stream.bind(py).call_method1("write", (text,))?;
            Ok(text.chars().count())
        })
    }

    fn flush(&self, py: Python<'_>) -> PyResult<()> {
        self.suspended(py, || {
            self.stream.bind(py).call_method0("flush").map(|_| ())
        })
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.stream)?;
        if let Some(capture) = &self.capture {
            visit.call(capture)?;
        }
        if let Ok(handler) = self.handler.try_lock() {
            visit.call(&*handler)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let handler = self
            .handler
            .lock()
            .ok()
            .and_then(|mut handler| handler.take());
        drop(handler);
    }
}

fn enter_logging_scope<'py>(
    py: Python<'py>,
    handlers: &Bound<'py, PyList>,
    level: Option<i32>,
) -> PyResult<Bound<'py, PyAny>> {
    let scope = Module::Capture
        .get(py)?
        .call_method1("LoggingScope", (handlers, level))?;
    if let Err(error) = scope.call_method0("__enter__") {
        let _ = scope.call_method0("close");
        return Err(error);
    }
    Ok(scope)
}

/// One test's record buffers use the logging plugin's session-owned handlers.
pub(super) struct TestCapture {
    caplog: Py<NativeLogState>,
    report: Py<NativeLogState>,
    handler: Py<PyAny>,
    report_handler: Py<PyAny>,
    level: Option<i32>,
    live: Option<Py<NativeLiveStream>>,
}

impl TestCapture {
    pub(super) fn new(py: Python<'_>, settings: &LogCaptureSettings) -> PyResult<Self> {
        let adapter = Module::Capture.get(py)?;
        let formatter = log_formatter(
            py,
            &settings.format,
            settings.date_format.as_deref(),
            settings.auto_indent,
        )?;
        let caplog = Py::new(py, NativeLogState::new())?;
        let report = Py::new(py, NativeLogState::new())?;
        let handler = adapter.call_method1("LogHandler", (&caplog, &formatter))?;
        let report_handler = adapter.call_method1("LogHandler", (&report, &formatter))?;
        Ok(Self {
            caplog,
            report,
            handler: handler.unbind(),
            report_handler: report_handler.unbind(),
            level: settings.level,
            live: settings.shared_live.as_ref().map(|live| live.clone_ref(py)),
        })
    }

    /// Keep session-owned handlers, with fresh phase buffers for this test.
    fn reuse(
        py: Python<'_>,
        handler: Py<PyAny>,
        report_handler: Py<PyAny>,
        level: Option<i32>,
        live: Option<Py<NativeLiveStream>>,
    ) -> PyResult<Self> {
        let caplog = Py::new(py, NativeLogState::new())?;
        let report = Py::new(py, NativeLogState::new())?;
        handler.bind(py).setattr("state", &caplog)?;
        report_handler.bind(py).setattr("state", &report)?;
        Ok(Self {
            caplog,
            report,
            handler,
            report_handler,
            level,
            live,
        })
    }

    pub(super) fn begin_phase(&self, py: Python<'_>, phase: ExecutionPhase) -> PyResult<LogPhase> {
        let handlers = PyList::new(py, [self.handler.bind(py), self.report_handler.bind(py)])?;
        if let Some(stream) = &self.live {
            stream.borrow(py).phase.store(
                u8::try_from(phase_index(phase)).expect("three logging phases fit in u8"),
                Ordering::Relaxed,
            );
            stream.borrow(py).started.store(false, Ordering::Relaxed);
        }
        let scope = enter_logging_scope(py, &handlers, self.level)?;
        let guard = LogPhase {
            scope: Some(scope.unbind()),
            handler: Some(self.report_handler.clone_ref(py)),
        };
        self.caplog.borrow(py).begin(phase);
        self.report.borrow(py).begin(phase);
        self.handler.bind(py).call_method0("_reset_stream")?;
        self.report_handler.bind(py).call_method0("_reset_stream")?;
        Ok(guard)
    }

    pub(super) fn caplog(&self, py: Python<'_>) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        let fixture = Module::Capture
            .get(py)?
            .call_method1("LogCaptureFixture", (&self.caplog, &self.handler))?;
        let cleanup = fixture.getattr("close")?.unbind();
        Ok((fixture.unbind(), cleanup))
    }
}

pub(super) struct LogPhase {
    scope: Option<Py<PyAny>>,
    handler: Option<Py<PyAny>>,
}

impl LogPhase {
    fn protocol(py: Python<'_>, live: Option<&Py<NativeLiveStream>>) -> PyResult<Self> {
        let handler = if let Some(live) = live {
            live.get().handler(py)?.into_bound(py)
        } else {
            py.import("logging")?.call_method0("NullHandler")?
        };
        let handlers = PyList::new(py, [handler])?;
        let level = live.and_then(|live| live.get().level);
        let scope = enter_logging_scope(py, &handlers, level)?;
        Ok(Self {
            scope: Some(scope.unbind()),
            handler: None,
        })
    }

    pub(super) fn finish(mut self, py: Python<'_>) -> PyResult<String> {
        if let Some(scope) = self.scope.take() {
            scope.bind(py).call_method0("close")?;
        }
        let Some(handler) = &self.handler else {
            return Ok(String::new());
        };
        let text = handler
            .bind(py)
            .getattr("stream")?
            .call_method0("getvalue")?
            .extract::<String>()?;
        Ok(vfy_core::test_execution::split_python_strip(&text)
            .1
            .to_owned())
    }
}

// CPython can deallocate a capture guard while unwinding a bytecode instruction.
// Keep the error indicator intact across Python cleanup callbacks.
fn drop_capture_scope(cleanup: impl FnOnce(Python<'_>)) {
    Python::attach(|py| {
        let pending = PyErr::take(py);
        cleanup(py);
        if let Some(error) = pending {
            error.restore(py);
        }
    });
}

impl Drop for LogPhase {
    fn drop(&mut self) {
        if let Some(scope) = self.scope.take() {
            drop_capture_scope(move |py| {
                let _ = scope.bind(py).call_method0("close");
            });
        }
    }
}

/// The caller retains warning classification in the shared collection/run record.
pub(super) struct WarningSettings {
    pub config_filters: Py<PyAny>,
    pub command_filters: Py<PyAny>,
}

impl WarningSettings {
    pub(super) fn from_config(config: &Bound<'_, PyAny>) -> PyResult<Self> {
        let config_filters = config.call_method1("getini", ("filterwarnings",))?;
        let command = config
            .getattr("known_args_namespace")?
            .getattr("pythonwarnings")?;
        let command_filters = if command.is_truthy()? {
            command.unbind()
        } else {
            PyTuple::empty(config.py()).into_any().unbind()
        };
        Ok(Self {
            config_filters: config_filters.unbind(),
            command_filters,
        })
    }
}

#[derive(Clone, Copy)]
enum WarningRecording {
    Capture,
    Publish,
}

pub(super) struct WarningScope {
    context: Option<Py<PyAny>>,
    messages: Option<Py<PyList>>,
}

impl WarningScope {
    pub(super) fn start(
        py: Python<'_>,
        settings: &WarningSettings,
        item: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        Self::start_recording(py, settings, item, WarningRecording::Capture)
    }

    pub(super) fn publish(py: Python<'_>, settings: &WarningSettings) -> PyResult<Self> {
        Self::start_recording(py, settings, None, WarningRecording::Publish)
    }

    fn start_recording(
        py: Python<'_>,
        settings: &WarningSettings,
        item: Option<&Bound<'_, PyAny>>,
        recording: WarningRecording,
    ) -> PyResult<Self> {
        let warnings = Module::Warnings.get(py)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("record", matches!(recording, WarningRecording::Capture))?;
        let context = warnings.call_method("catch_warnings", (), Some(&kwargs))?;
        let messages = context.call_method0("__enter__")?;
        let messages = match recording {
            WarningRecording::Capture => Some(messages.cast_into::<PyList>()?.unbind()),
            WarningRecording::Publish => None,
        };
        let scope = Self {
            context: Some(context.unbind()),
            messages,
        };
        if !Module::Sys.get(py)?.getattr("warnoptions")?.is_truthy()? {
            let builtins = Module::Builtins.get(py)?;
            for name in ["DeprecationWarning", "PendingDeprecationWarning"] {
                let kwargs = PyDict::new(py);
                kwargs.set_item("category", builtins.getattr(name)?)?;
                warnings.call_method("filterwarnings", ("always",), Some(&kwargs))?;
            }
        }
        for (filters, source) in [
            (&settings.config_filters, WarningFilterSource::Config),
            (&settings.command_filters, WarningFilterSource::CommandLine),
        ] {
            for filter in filters.bind(py).try_iter()? {
                let filter = filter?;
                match apply_warning_filter(py, &filter, source) {
                    Err(error) if error.is_instance_of::<pyo3::exceptions::PyImportError>(py) => {
                        let name = error.value(py).getattr("name")?;
                        let message = format!(
                            "Failed to import filter module '{}': {}",
                            name.str()?.to_string_lossy(),
                            filter.str()?.to_string_lossy()
                        );
                        let category = py.import("pytest")?.getattr("PytestConfigWarning")?;
                        warnings.call_method1("warn", (message, category))?;
                    }
                    result => result?,
                }
            }
        }
        if let Some(item) = item {
            for marker in item
                .call_method1("iter_markers", ("filterwarnings",))?
                .try_iter()?
            {
                for filter in marker?.getattr("args")?.try_iter()? {
                    apply_warning_filter(py, &filter?, WarningFilterSource::Config)?;
                }
            }
        }
        Ok(scope)
    }

    /// Dispatch original warning objects while their enclosing catch scope is live.
    pub(super) fn dispatch(
        mut self,
        py: Python<'_>,
        nodeid: &str,
        mut recorded: impl FnMut(&Bound<'_, PyAny>, &str) -> PyResult<()>,
    ) -> PyResult<()> {
        let result = (|| {
            let Some(messages) = &self.messages else {
                return Ok(());
            };
            let messages = messages.bind(py);
            let mut index = 0;
            while index < messages.len() {
                let message = messages.get_item(index)?;
                index += 1;
                recorded(&message, nodeid)?;
            }
            Ok(())
        })();
        result.and(self.close(py))
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(context) = self.context.take() {
            context
                .bind(py)
                .call_method1("__exit__", (py.None(), py.None(), py.None()))?;
        }
        Ok(())
    }
}

fn warning_record(
    py: Python<'_>,
    message: &Bound<'_, PyAny>,
    nodeid: &str,
) -> PyResult<WarningRecord> {
    let warning = message.getattr("message")?;
    let formatted_message = warning.str()?;
    let category = message.getattr("category")?;
    let text = Module::Warnings.get(py)?.call_method1(
        "formatwarning",
        (
            formatted_message,
            &category,
            message.getattr("filename")?,
            message.getattr("lineno")?,
            message.getattr("line")?,
        ),
    )?;
    let raw_message = if category.is(&Module::Facade
        .get(py)?
        .getattr("PytestRemovedIn10Warning")?)
    {
        let args = warning
            .getattr("args")?
            .cast_into::<pyo3::types::PyTuple>()?;
        if args.len() == 1 {
            args.get_item(0)?.extract::<String>().ok().filter(|text| {
                text.starts_with(
                    "Passing a non-Collection iterable to parametrize is deprecated.\n",
                ) && text.ends_with(
                    "https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators",
                )
            })
        } else {
            None
        }
    } else {
        None
    };
    Ok(WarningRecord {
        message: text.extract::<String>()? + &warning_source_traceback(py, message)?,
        nodeid: nodeid.to_owned(),
        filename: message.getattr("filename")?.extract()?,
        lineno: message.getattr("lineno")?.extract()?,
        category: raw_message
            .as_ref()
            .map(|_| "PytestRemovedIn10Warning".to_owned()),
        raw_message,
    })
}

impl Drop for WarningScope {
    fn drop(&mut self) {
        if self.context.is_some() {
            drop_capture_scope(|py| {
                let _ = self.close(py);
            });
        }
    }
}

fn warning_filter_error(filter: &str, error: impl std::fmt::Display) -> PyErr {
    crate::IndependentUsageError::new_err(format!(
        "while parsing the following warning configuration:\n\n  {filter}\n\nThis error occurred:\n\n{error}"
    ))
}

/// Where a warning filter came from; command-line filters escape their regular expressions.
#[derive(Clone, Copy)]
enum WarningFilterSource {
    Config,
    CommandLine,
}

fn apply_warning_filter(
    py: Python<'_>,
    filter: &Bound<'_, PyAny>,
    source: WarningFilterSource,
) -> PyResult<()> {
    let escape = matches!(source, WarningFilterSource::CommandLine);
    let parsed = Module::Capture
        .get(py)?
        .call_method1("parse_warning_filter", (filter, escape))?;
    Module::Warnings
        .get(py)?
        .getattr("filterwarnings")?
        .call1(parsed.cast_into::<PyTuple>()?)?;
    Ok(())
}

#[pyfunction]
fn independent_parse_warning_filter<'py>(
    py: Python<'py>,
    filter_text: &str,
    escape: bool,
) -> PyResult<Bound<'py, PyTuple>> {
    let fields = filter_text
        .split(':')
        .map(|field| vfy_core::test_execution::split_python_strip(field).1)
        .collect::<Vec<_>>();
    if fields.len() > 5 {
        return Err(warning_filter_error(
            filter_text,
            format!(
                "Too many fields ({}), expected at most 5 separated by colons",
                fields.len()
            ),
        ));
    }
    let field = |index| fields.get(index).copied().unwrap_or("");
    let action = match field(0) {
        "" => "default",
        "all" => "always",
        name => ["default", "always", "ignore", "module", "once", "error"]
            .into_iter()
            .find(|action| action.starts_with(name))
            .ok_or_else(|| {
                warning_filter_error(filter_text, format!("invalid action: {name:?}"))
            })?,
    };
    let builtins = Module::Builtins.get(py)?;
    let category_name = field(2);
    let category = resolve_warning_category(py, category_name).map_err(|error| {
        if error.is_instance_of::<pyo3::exceptions::PyImportError>(py) {
            error
        } else {
            warning_filter_error(filter_text, error)
        }
    })?;
    let regex = py.import("re")?;
    let mut message = field(1).to_owned();
    let mut module = field(3).to_owned();
    if escape {
        if !message.is_empty() {
            message = regex.call_method1("escape", (message,))?.extract()?;
        }
        if !module.is_empty() {
            module = regex
                .call_method1("escape", (module,))?
                .extract::<String>()?
                + "\\Z";
        }
    }
    let lineno = if field(4).is_empty() {
        builtins.call_method1("int", (0,))?
    } else {
        let lineno = builtins.call_method1("int", (field(4),)).map_err(|error| {
            warning_filter_error(
                filter_text,
                format!("invalid lineno {:?}: {error}", field(4)),
            )
        })?;
        if lineno.lt(0)? {
            return Err(warning_filter_error(
                filter_text,
                format!("invalid lineno {:?}: number is negative", field(4)),
            ));
        }
        lineno
    };
    for pattern in [&message, &module] {
        regex.call_method1("compile", (pattern,)).map_err(|error| {
            warning_filter_error(filter_text, format!("Invalid regex {pattern:?}: {error}"))
        })?;
    }
    (action, message, category, module, lineno).into_pyobject(py)
}

pub(crate) fn register_warning_filter(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(independent_parse_warning_filter, module)?)
}

fn resolve_warning_category<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    if name.is_empty() {
        return Module::Builtins.get(py)?.getattr("Warning");
    }
    let (module, name) = name.rsplit_once('.').unwrap_or(("builtins", name));
    let category = py.import(module)?.getattr(name)?;
    if !Module::Builtins
        .get(py)?
        .call_method1(
            "issubclass",
            (&category, Module::Builtins.get(py)?.getattr("Warning")?),
        )?
        .is_truthy()?
    {
        return Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "{name} is not a Warning subclass"
        )));
    }
    Ok(category)
}

fn warning_source_traceback(py: Python<'_>, message: &Bound<'_, PyAny>) -> PyResult<String> {
    let source = message.getattr("source")?;
    if source.is_none() {
        return Ok(String::new());
    }
    let tracemalloc = match py.import("tracemalloc") {
        Ok(module) => module,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyImportError>(py) => {
            return Ok(String::new());
        }
        Err(error) => return Err(error),
    };
    let traceback = tracemalloc.call_method1("get_object_traceback", (source,))?;
    if traceback.is_none() {
        return Ok("Enable tracemalloc to get traceback where the object was allocated.\nSee https://docs.pytest.org/en/stable/how-to/capture-warnings.html#resource-warnings for more info.".to_owned());
    }
    let lines = traceback.call_method0("format")?.extract::<Vec<String>>()?;
    Ok(format!("\nObject allocated at:\n{}", lines.join("\n")))
}

#[cfg(test)]
mod tests;
