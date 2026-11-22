//! Snapshot-based call monitoring and tagged diagnostic callbacks.

use super::{Implementation, NativeHookRegistry, NativeHookResult, dispatch};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::collections::BTreeMap;
use std::fmt::Write;

pub(super) enum Monitor {
    Callbacks { before: Py<PyAny>, after: Py<PyAny> },
    Trace(Py<NativeTagTracer>),
}

impl Monitor {
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Callbacks { before, after } => Self::Callbacks {
                before: before.clone_ref(py),
                after: after.clone_ref(py),
            },
            Self::Trace(root) => Self::Trace(root.clone_ref(py)),
        }
    }
    pub(super) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            Self::Callbacks { before, after } => {
                visit.call(before)?;
                visit.call(after)
            }
            Self::Trace(root) => visit.call(root),
        }
    }
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeHookUndo {
    registry: Py<NativeHookRegistry>,
    previous: Vec<Monitor>,
}

#[pymethods]
impl NativeHookUndo {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(py
            .import("inspect")?
            .getattr("Signature")?
            .call0()?
            .unbind())
    }

    fn __call__(&self, py: Python<'_>) {
        let previous = self
            .previous
            .iter()
            .map(|monitor| monitor.clone_ref(py))
            .collect();
        let removed = std::mem::replace(&mut self.registry.borrow_mut(py).monitors, previous);
        drop(removed);
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.registry)?;
        for monitor in &self.previous {
            monitor.traverse(&visit)?;
        }
        Ok(())
    }
}

#[pyclass(module = "vfy._native._pytest")]
#[derive(Default)]
pub(crate) struct NativeTagTracer {
    writer: Option<Py<PyAny>>,
    processors: BTreeMap<Vec<String>, Py<PyAny>>,
    #[pyo3(get, set)]
    indent: isize,
}

#[pymethods]
impl NativeTagTracer {
    fn get(slf: &Bound<'_, Self>, name: String) -> NativeTagTracerSub {
        NativeTagTracerSub {
            root: slf.clone().unbind(),
            tags: vec![name],
        }
    }
    #[pyo3(signature = (writer))]
    fn setwriter(slf: &Bound<'_, Self>, writer: Option<Py<PyAny>>) {
        let removed = std::mem::replace(&mut slf.borrow_mut().writer, writer);
        drop(removed);
    }
    fn setprocessor(
        slf: &Bound<'_, Self>,
        tags: &Bound<'_, PyAny>,
        processor: Py<PyAny>,
    ) -> PyResult<()> {
        let tags = if let Ok(tags) = tags.cast::<PyString>() {
            tags.to_str()?.split(':').map(str::to_owned).collect()
        } else if tags.is_instance_of::<PyTuple>() {
            tags.extract::<Vec<String>>()?
        } else {
            return Err(PyAssertionError::new_err(""));
        };
        let removed = slf.borrow_mut().processors.insert(tags, processor);
        drop(removed);
        Ok(())
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(writer) = &self.writer {
            visit.call(writer)?;
        }
        for processor in self.processors.values() {
            visit.call(processor)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = std::mem::take(&mut *slf.borrow_mut());
        drop(removed);
    }
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeTagTracerSub {
    #[pyo3(get)]
    pub(super) root: Py<NativeTagTracer>,
    tags: Vec<String>,
}

#[pymethods]
impl NativeTagTracerSub {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let inspect = py.import("inspect")?;
        let parameter = inspect.getattr("Parameter")?;
        let args = parameter.call1(("args", parameter.getattr("VAR_POSITIONAL")?))?;
        Ok(inspect
            .getattr("Signature")?
            .call1((PyList::new(py, [args])?,))?
            .unbind())
    }

    #[getter]
    fn tags<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        PyTuple::new(py, &self.tags)
    }
    fn get(&self, py: Python<'_>, name: String) -> Self {
        let mut tags = self.tags.clone();
        tags.push(name);
        Self {
            root: self.root.clone_ref(py),
            tags,
        }
    }
    #[pyo3(signature = (*args))]
    fn __call__(slf: &Bound<'_, Self>, args: &Bound<'_, PyTuple>) -> PyResult<()> {
        let (root, tags) = {
            let tracer = slf.borrow();
            (tracer.root.clone_ref(slf.py()), tracer.tags.clone())
        };
        process(root.bind(slf.py()), &tags, args)
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.root)
    }
}

fn formatted(
    root: &Bound<'_, NativeTagTracer>,
    tags: &[String],
    args: &Bound<'_, PyTuple>,
) -> PyResult<String> {
    let (arguments, extra) = if let Ok(last) = args.get_item(args.len() - 1)?.cast_into::<PyDict>()
    {
        (args.len() - 1, Some(last))
    } else {
        (args.len(), None)
    };
    let content = args
        .iter()
        .take(arguments)
        .map(|argument| {
            argument
                .str()
                .map(|value| value.to_string_lossy().into_owned())
        })
        .collect::<PyResult<Vec<_>>>()?
        .join(" ");
    let indent = "  ".repeat(usize::try_from(root.borrow().indent).unwrap_or(0));
    let mut message = format!("{indent}{content} [{}]\n", tags.join(":"));
    if let Some(extra) = extra {
        for item in extra.call_method0("items")?.try_iter()? {
            let item = item?;
            let name = item.get_item(0)?;
            let value = item.get_item(1)?;
            let _ = writeln!(message, "{indent}    {}: {}", name.str()?, value.str()?);
        }
    }
    Ok(message)
}

fn process(
    root: &Bound<'_, NativeTagTracer>,
    tags: &[String],
    args: &Bound<'_, PyTuple>,
) -> PyResult<()> {
    let py = root.py();
    let writer = root
        .borrow()
        .writer
        .as_ref()
        .map(|writer| writer.clone_ref(py));
    if let Some(writer) = writer
        && !args.is_empty()
    {
        writer.bind(py).call1((formatted(root, tags, args)?,))?;
    }
    let processor = root
        .borrow()
        .processors
        .get(tags)
        .map(|processor| processor.clone_ref(py));
    if let Some(processor) = processor {
        processor.bind(py).call1((PyTuple::new(py, tags)?, args))?;
    }
    Ok(())
}

pub(super) fn trace(registry: &Bound<'_, NativeHookRegistry>) -> PyResult<Py<NativeTagTracerSub>> {
    let py = registry.py();
    if let Some(trace) = &registry.borrow().trace {
        return Ok(trace.clone_ref(py));
    }
    let root = Py::new(py, NativeTagTracer::default())?;
    let candidate = Py::new(
        py,
        NativeTagTracerSub {
            root,
            tags: vec!["pluginmanage".into()],
        },
    )?;
    let selected = {
        let mut registry = registry.borrow_mut();
        registry
            .trace
            .get_or_insert_with(|| candidate.clone_ref(py))
            .clone_ref(py)
    };
    Ok(selected)
}

pub(super) fn add(registry: &Bound<'_, NativeHookRegistry>, monitor: Monitor) -> NativeHookUndo {
    let py = registry.py();
    let previous = {
        let mut registry = registry.borrow_mut();
        let previous = registry
            .monitors
            .iter()
            .map(|monitor| monitor.clone_ref(py))
            .collect();
        registry.monitors.push(monitor);
        previous
    };
    NativeHookUndo {
        registry: registry.clone().unbind(),
        previous,
    }
}

/// Calls `implementations` (in call order) under every installed monitor.
pub(super) fn execute(
    registry: &Bound<'_, NativeHookRegistry>,
    name: &str,
    implementations: super::HookSelection,
    kwargs: &Bound<'_, PyDict>,
    firstresult: bool,
) -> PyResult<Py<PyAny>> {
    let py = registry.py();
    let monitors: Vec<_> = registry
        .borrow()
        .monitors
        .iter()
        .map(|monitor| monitor.clone_ref(py))
        .collect();
    if implementations.len(py) == 0 && monitors.is_empty() {
        return Ok(if firstresult {
            py.None()
        } else {
            PyList::empty(py).into_any().unbind()
        });
    }
    if monitors.is_empty() {
        let result = implementations.run(registry, name, kwargs, firstresult, None);
        implementations.release();
        return result;
    }
    // Monitors receive pluggy's mutable list in reverse call order.
    let implementations = implementations.into_owned(py);
    for implementation in &implementations {
        Implementation::options(implementation.bind(py))?;
    }
    let methods = PyList::new(py, implementations.into_iter().rev())?;
    run_monitored(registry, name, &methods, kwargs, firstresult, &monitors)
}

/// Each monitor wraps the calls below it. The most recently added monitor is outermost.
fn run_monitored(
    registry: &Bound<'_, NativeHookRegistry>,
    name: &str,
    methods: &Bound<'_, PyList>,
    kwargs: &Bound<'_, PyDict>,
    firstresult: bool,
    monitors: &[Monitor],
) -> PyResult<Py<PyAny>> {
    let py = registry.py();
    let Some((monitor, inner)) = monitors.split_last() else {
        return dispatch::run(
            registry,
            name,
            dispatch::Methods::Monitored(methods),
            kwargs,
            firstresult,
        );
    };
    match monitor {
        Monitor::Callbacks { before, after } => {
            before.bind(py).call1((name, methods, kwargs))?;
            let outcome = Py::new(
                py,
                NativeHookResult::from_outcome(
                    py,
                    run_monitored(registry, name, methods, kwargs, firstresult, inner),
                ),
            )?;
            after
                .bind(py)
                .call1((outcome.bind(py), name, methods, kwargs))?;
            NativeHookResult::outcome(&outcome, py)
        }
        Monitor::Trace(root) => {
            trace_before(py, root.bind(py), name, kwargs)?;
            let outcome = Py::new(
                py,
                NativeHookResult::from_outcome(
                    py,
                    run_monitored(registry, name, methods, kwargs, firstresult, inner),
                ),
            )?;
            let value = {
                let state = outcome.borrow(py);
                state
                    .exception
                    .is_none()
                    .then(|| state.result.clone_ref(py))
            };
            trace_after(
                py,
                root.bind(py),
                name,
                value.as_ref().map(|value| value.bind(py)),
            )?;
            NativeHookResult::outcome(&outcome, py)
        }
    }
}

fn trace_before(
    py: Python<'_>,
    root: &Bound<'_, NativeTagTracer>,
    name: &str,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<()> {
    let indent = root
        .borrow()
        .indent
        .checked_add(1)
        .ok_or_else(|| PyRuntimeError::new_err("hook trace indentation overflow"))?;
    root.borrow_mut().indent = indent;
    process(
        root,
        &["hook".into()],
        &PyTuple::new(
            py,
            [
                PyString::new(py, name).into_any(),
                kwargs.clone().into_any(),
            ],
        )?,
    )
}

fn trace_after(
    py: Python<'_>,
    root: &Bound<'_, NativeTagTracer>,
    name: &str,
    result: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    if let Some(result) = result {
        process(
            root,
            &["hook".into()],
            &PyTuple::new(
                py,
                [
                    PyString::new(py, "finish").into_any(),
                    PyString::new(py, name).into_any(),
                    PyString::new(py, "-->").into_any(),
                    result.clone(),
                ],
            )?,
        )?;
    }
    let indent = root
        .borrow()
        .indent
        .checked_sub(1)
        .ok_or_else(|| PyRuntimeError::new_err("hook trace indentation overflow"))?;
    root.borrow_mut().indent = indent;
    Ok(())
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeHookUndo>()?;
    module.add_class::<NativeTagTracer>()?;
    module.add_class::<NativeTagTracerSub>()?;
    Ok(())
}
