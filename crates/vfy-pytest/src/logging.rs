//! Logging scopes for phases executed by the native test loop.

use pyo3::exceptions::{PyRuntimeError, PyStopIteration, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyList, PyListMethods, PyString, PyTuple};
use std::collections::BTreeMap;

enum LoggingScope {
    ContextManager(Py<PyAny>),
    Generator(Py<PyAny>),
}

pub(crate) struct ActivePhase {
    plugin: Py<PyAny>,
    scope: LoggingScope,
    item: Py<PyAny>,
    phase: &'static str,
    previous_logs: BTreeMap<String, usize>,
    report_handler: Py<PyAny>,
}

pub(crate) enum PhaseCompletion<T> {
    NotStarted(PyErr),
    Interrupted(PyErr),
    Completed {
        value: T,
        log: String,
        error: Option<PyErr>,
    },
}

#[derive(Clone, Copy)]
pub(crate) enum ReportSectionRetention {
    Discard,
    Pytest,
}

pub(crate) enum LoggingService {
    Disabled,
    Enabled {
        /// Pytest calls the logging hooks only while `logging-plugin` is
        /// registered; a test may unregister it, as Flask's suite does.
        manager: Py<PyAny>,
        caplog_records_key: Py<PyAny>,
        caplog_handler_key: Py<PyAny>,
        active: Option<ActivePhase>,
        report_sections: ReportSectionRetention,
    },
}

impl LoggingService {
    pub(crate) fn new(py: Python<'_>, items: &Bound<'_, PyAny>) -> PyResult<Self> {
        let Some(first) = items.try_iter()?.next() else {
            return Ok(Self::Disabled);
        };
        let config = first?.getattr(pyo3::intern!(py, "config"))?;
        let manager = config.getattr(pyo3::intern!(py, "pluginmanager"))?;
        let logging = py.import("_pytest.logging")?;
        Ok(Self::Enabled {
            manager: manager.unbind(),
            caplog_records_key: logging.getattr("caplog_records_key")?.unbind(),
            caplog_handler_key: logging.getattr("caplog_handler_key")?.unbind(),
            active: None,
            report_sections: ReportSectionRetention::Discard,
        })
    }

    pub(crate) fn retain_pytest_sections(&mut self) {
        if let Self::Enabled {
            report_sections, ..
        } = self
        {
            *report_sections = ReportSectionRetention::Pytest;
        }
    }

    pub(crate) fn start_test(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Self::Enabled { manager, .. } = self
            && let Some(plugin) = registered_plugin(manager.bind(py))?
        {
            let handler = plugin.getattr(pyo3::intern!(py, "log_cli_handler"))?;
            handler.call_method0(pyo3::intern!(py, "reset"))?;
            handler.call_method1(pyo3::intern!(py, "set_when"), (pyo3::intern!(py, "start"),))?;
        }
        Ok(())
    }

    pub(crate) fn finish_test(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Self::Enabled { manager, .. } = self
            && let Some(plugin) = registered_plugin(manager.bind(py))?
        {
            plugin
                .getattr(pyo3::intern!(py, "log_cli_handler"))?
                .call_method1(
                    pyo3::intern!(py, "set_when"),
                    (pyo3::intern!(py, "finish"),),
                )?;
        }
        Ok(())
    }

    pub(crate) fn with_phase<T>(
        &mut self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        phase: &'static str,
        run: impl FnOnce() -> PyResult<T>,
    ) -> PhaseCompletion<T> {
        if let Err(error) = self.enter(py, item, phase) {
            return PhaseCompletion::NotStarted(error);
        }
        let result = run();
        let (log, error) = self.exit(py);
        match result {
            Ok(value) => PhaseCompletion::Completed { value, log, error },
            Err(interrupted) => PhaseCompletion::Interrupted(error.unwrap_or(interrupted)),
        }
    }

    fn enter(
        &mut self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        phase: &'static str,
    ) -> PyResult<()> {
        let Self::Enabled {
            manager,
            caplog_records_key,
            caplog_handler_key,
            active,
            ..
        } = self
        else {
            return Ok(());
        };
        let Some(plugin) = registered_plugin(manager.bind(py))? else {
            return Ok(());
        };
        let phase_argument = match phase {
            "setup" => pyo3::intern!(py, "setup").clone(),
            "call" => pyo3::intern!(py, "call").clone(),
            "teardown" => pyo3::intern!(py, "teardown").clone(),
            other => PyString::new(py, other),
        };
        let result: PyResult<()> = (|| {
            let previous_logs = if phase == "setup" {
                BTreeMap::new()
            } else {
                log_section_counts(py, item)?
            };
            plugin
                .getattr(pyo3::intern!(py, "log_cli_handler"))?
                .call_method1(pyo3::intern!(py, "set_when"), (&phase_argument,))?;
            let stash = item.getattr(pyo3::intern!(py, "stash"))?;
            let needs_phase_records = phase == "setup"
                || !stash
                    .call_method1(
                        pyo3::intern!(py, "__contains__"),
                        (caplog_records_key.bind(py),),
                    )?
                    .is_truthy()?;
            if needs_phase_records {
                stash.set_item(caplog_records_key.bind(py), PyDict::new(py))?;
            }
            let callable = plugin.getattr(pyo3::intern!(py, "_runtest_for"))?;
            let scope = callable.call1((item, &phase_argument))?;
            let scope = enter_scope(py, scope)?;
            let report_handler = match plugin.getattr(pyo3::intern!(py, "report_handler")) {
                Ok(handler) => handler.unbind(),
                Err(error) => return Err(close_scope(py, scope).err().unwrap_or(error)),
            };
            *active = Some(ActivePhase {
                plugin: plugin.clone().unbind(),
                scope,
                item: item.clone().unbind(),
                phase,
                previous_logs,
                report_handler,
            });
            Ok(())
        })();
        if result.is_err() && phase == "teardown" {
            // Pytest's teardown wrapper removes stash keys even when acquiring
            // the context fails, without executing the teardown body.
            clear_stash(py, item, caplog_records_key, caplog_handler_key)?;
        }
        result
    }

    fn exit(&mut self, py: Python<'_>) -> (String, Option<PyErr>) {
        let Self::Enabled {
            caplog_records_key,
            caplog_handler_key,
            active,
            report_sections,
            ..
        } = self
        else {
            return (String::new(), None);
        };
        let Some(ActivePhase {
            plugin,
            scope,
            item,
            phase,
            mut previous_logs,
            report_handler,
        }) = active.take()
        else {
            return (String::new(), None);
        };
        let item = item.bind(py);
        let sections: PyResult<Bound<'_, PyList>> = item
            .getattr(pyo3::intern!(py, "_report_sections"))
            .and_then(|sections| sections.cast_into::<PyList>().map_err(Into::into));
        let section_start = sections.as_ref().map_or(0, PyListMethods::len);
        let mut error = close_scope(py, scope).err().or_else(|| sections.err());
        let log = match item
            .getattr(pyo3::intern!(py, "_report_sections"))
            .and_then(|sections| sections.cast_into::<PyList>().map_err(Into::into))
            .and_then(|sections| {
                take_scope_log(
                    &sections,
                    section_start,
                    phase,
                    &mut previous_logs,
                    report_handler.bind(py),
                    *report_sections,
                )
            }) {
            Ok(log) => log,
            Err(capture_error) => {
                if error.is_none() {
                    error = Some(capture_error);
                }
                String::new()
            }
        };
        if error.is_none() {
            error = plugin
                .bind(py)
                .getattr(pyo3::intern!(py, "log_cli_handler"))
                .and_then(|handler| {
                    handler.call_method1(
                        pyo3::intern!(py, "set_when"),
                        (pyo3::intern!(py, "logreport"),),
                    )
                })
                .err();
        }
        if phase == "teardown"
            && let Err(cleanup_error) =
                clear_stash(py, item, caplog_records_key, caplog_handler_key)
        {
            error = Some(cleanup_error);
        }
        (log, error)
    }
}

fn registered_plugin<'py>(manager: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let plugin = manager.call_method1(
        pyo3::intern!(manager.py(), "get_plugin"),
        ("logging-plugin",),
    )?;
    Ok((!plugin.is_none()).then_some(plugin))
}

fn enter_scope(py: Python<'_>, scope: Bound<'_, PyAny>) -> PyResult<LoggingScope> {
    let has_enter = scope.hasattr(pyo3::intern!(py, "__enter__"))?;
    let has_exit = scope.hasattr(pyo3::intern!(py, "__exit__"))?;
    match (has_enter, has_exit) {
        (true, true) => {
            scope.call_method0(pyo3::intern!(py, "__enter__"))?;
            Ok(LoggingScope::ContextManager(scope.unbind()))
        }
        (false, false) if scope.hasattr(pyo3::intern!(py, "__next__"))? => {
            scope.call_method0(pyo3::intern!(py, "__next__"))?;
            Ok(LoggingScope::Generator(scope.unbind()))
        }
        _ => Err(PyTypeError::new_err(
            "pytest logging scope must be a context manager or generator",
        )),
    }
}

fn close_scope(py: Python<'_>, scope: LoggingScope) -> PyResult<()> {
    match scope {
        LoggingScope::ContextManager(context) => {
            context.bind(py).call_method1(
                pyo3::intern!(py, "__exit__"),
                (py.None(), py.None(), py.None()),
            )?;
        }
        LoggingScope::Generator(generator) => {
            match generator
                .bind(py)
                .call_method0(pyo3::intern!(py, "__next__"))
            {
                Err(error) if error.is_instance_of::<PyStopIteration>(py) => {}
                Ok(_) => {
                    let _ = generator.bind(py).call_method0(pyo3::intern!(py, "close"));
                    return Err(PyRuntimeError::new_err(
                        "pytest logging phase generator yielded more than once",
                    ));
                }
                Err(error) => return Err(error),
            }
        }
    }
    Ok(())
}

fn log_section_counts(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
) -> PyResult<BTreeMap<String, usize>> {
    let sections = item
        .getattr(pyo3::intern!(py, "_report_sections"))?
        .cast_into::<PyList>()?;
    let mut counts = BTreeMap::new();
    for section in sections.iter() {
        let section = section.cast_into::<PyTuple>()?;
        if section.get_item(1)?.extract::<String>()?.starts_with("log") {
            *counts
                .entry(section.get_item(2)?.extract::<String>()?)
                .or_default() += 1;
        }
    }
    Ok(counts)
}

fn take_scope_log(
    sections: &Bound<'_, PyList>,
    start: usize,
    phase: &str,
    previous: &mut BTreeMap<String, usize>,
    report_handler: &Bound<'_, PyAny>,
    retention: ReportSectionRetention,
) -> PyResult<String> {
    let mut log = String::new();
    let mut first = true;
    for section in sections.iter() {
        let section = section.cast_into::<PyTuple>()?;
        if !section.get_item(1)?.extract::<String>()?.starts_with("log") {
            continue;
        }
        let text = section.get_item(2)?.extract::<String>()?;
        if let Some(count) = previous.get_mut(&text)
            && *count > 0
        {
            *count -= 1;
            continue;
        }
        if !first {
            log.push('\n');
        }
        log.push_str(&text);
        first = false;
    }
    if matches!(retention, ReportSectionRetention::Pytest) {
        return Ok(log);
    }
    let mut matching = None;
    let mut handler_log: Option<String> = None;
    for index in start..sections.len() {
        let section = sections.get_item(index)?.cast_into::<PyTuple>()?;
        if section.get_item(0)?.extract::<String>()? == phase
            && section.get_item(1)?.extract::<String>()? == "log"
        {
            if handler_log.is_none() {
                handler_log = Some(
                    report_handler
                        .getattr(pyo3::intern!(sections.py(), "stream"))?
                        .call_method0(pyo3::intern!(sections.py(), "getvalue"))?
                        .call_method0(pyo3::intern!(sections.py(), "strip"))?
                        .extract::<String>()?,
                );
            }
            if let Some(handler_log) = &handler_log
                && section.get_item(2)?.extract::<String>()? == *handler_log
            {
                if matching.is_some() {
                    return Ok(log);
                }
                matching = Some(index);
            }
        }
    }
    if let Some(index) = matching {
        sections.del_item(index)?;
    }
    Ok(log)
}

fn clear_stash(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    records_key: &Py<PyAny>,
    handler_key: &Py<PyAny>,
) -> PyResult<()> {
    let stash = item.getattr(pyo3::intern!(py, "stash"))?;
    stash.call_method1(pyo3::intern!(py, "__delitem__"), (records_key.bind(py),))?;
    stash.call_method1(pyo3::intern!(py, "__delitem__"), (handler_key.bind(py),))?;
    Ok(())
}
