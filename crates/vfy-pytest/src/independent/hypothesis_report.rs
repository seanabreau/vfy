//! The Hypothesis report wrapper reads live item state and publishes only when needed.

use super::reporting::terminal::{ReportExtras, ReportSource};
use super::reporting::{NativeSessionReports, ReportHandle};
use pyo3::exceptions::{PyImportError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::{Mutex, PoisonError};

const INFORMATION: &str = "hypothesis_report_information";
const STATISTICS: &str = "hypothesis_statistics";

#[derive(Clone, Copy)]
enum Report<'a, 'py> {
    Native(&'a ReportHandle),
    Python(&'a Bound<'py, PyAny>),
}

impl<'py> Report<'_, 'py> {
    fn object(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Native(report) => Ok(report.to_python(py)?.into_bound(py).into_any()),
            Self::Python(report) => Ok((*report).clone()),
        }
    }

    fn attribute(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Native(report) => Ok(report.getattr(py, name)?.into_bound(py)),
            Self::Python(report) => report.getattr(name),
        }
    }
}

fn global<'py>(globals: &Bound<'py, PyDict>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    globals.get_item(name)?.ok_or_else(|| {
        pyo3::exceptions::PyNameError::new_err(format!("name '{name}' is not defined"))
    })
}

fn finish<'py>(
    globals: &Bound<'py, PyDict>,
    item: &Bound<'py, PyAny>,
    report: Report<'_, 'py>,
) -> PyResult<()> {
    let py = item.py();
    if item.hasattr(INFORMATION)? {
        let object = report.object(py)?;
        let append = object.getattr("sections")?.getattr("append")?;
        let information = item.getattr(INFORMATION)?;
        append.call1((("Hypothesis", information),))?;
    }
    if report.attribute(py, "when")?.ne("teardown")? {
        return Ok(());
    }
    let terminal = item
        .getattr("config")?
        .getattr("pluginmanager")?
        .call_method1("getplugin", ("terminalreporter",))?;
    let statistics = item
        .getattr_opt(STATISTICS)?
        .unwrap_or_else(|| py.None().into_bound(py));
    if item.hasattr(STATISTICS)? {
        let junit = item
            .getattr("config")?
            .getattr_opt("_vfy_junit_writer")?
            .unwrap_or_else(|| py.None().into_bound(py));
        if !junit.is_none() {
            let encode = global(globals, "base64")?.getattr("b64encode")?;
            let encoded = encode
                .call1((statistics.call_method0("encode")?,))?
                .call_method0("decode")?;
            let method = junit.getattr("add_global_property")?;
            let key = PyString::new(py, "hypothesis-statistics-").add(item.getattr("nodeid")?)?;
            method.call1((key, encoded))?;
        }
        if !terminal.is_none() {
            let object = report.object(py)?;
            let key = global(globals, "_STATS")?;
            object.getattr("__dict__")?.set_item(key, &statistics)?;
        }
        let html = item
            .getattr("config")?
            .getattr("pluginmanager")?
            .call_method1("getplugin", ("html",))?;
        if !html.is_none() {
            let object = report.object(py)?;
            let previous = object
                .getattr_opt("extra")?
                .unwrap_or_else(|| PyList::empty(py).into_any());
            let extra = PyList::new(py, previous.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
            let text = html.getattr("extras")?.getattr("text")?;
            let kwargs = PyDict::new(py);
            kwargs.set_item("name", "Hypothesis stats")?;
            extra.append(text.call((statistics.clone(),), Some(&kwargs))?)?;
            object.setattr("extra", extra)?;
        }
    }
    let examples_name = global(globals, "_EXAMPLES")?;
    let examples = item
        .getattr_opt(examples_name.cast::<PyString>()?)?
        .unwrap_or_else(|| py.None().into_bound(py));
    if !examples.is_truthy()? || terminal.is_none() {
        return Ok(());
    }
    let patching = match py.import("hypothesis.extra._patching") {
        Ok(module) => module,
        Err(error) if error.is_instance_of::<PyImportError>(py) => return Ok(()),
        Err(error) => return Err(error),
    };
    let fail_message = patching.getattr("FAIL_MSG")?;
    let get_patch = patching.getattr("get_patch_for")?;
    let function = item.getattr("obj")?;
    let pairs = PyList::empty(py);
    for example in examples.try_iter()? {
        pairs.append(PyTuple::new(py, [example?, fail_message.clone()])?)?;
    }
    let patch = get_patch.call1((function, pairs))?;
    if !patch.is_none() {
        let json = global(globals, "json")?.call_method1("dumps", (patch,))?;
        let object = report.object(py)?;
        let key = global(globals, "_EXAMPLES")?;
        object.getattr("__dict__")?.set_item(key, json)?;
    }
    Ok(())
}

/// Read one report's dictionary entry, retaining the original global lookup.
fn summary_value<'py>(
    source: &ReportSource,
    namespace: &Bound<'py, PyDict>,
    global_name: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = namespace.py();
    let extras = source.extras(py)?;
    match extras {
        ReportExtras::Python(mapping) => {
            let getter = mapping.bind(py).getattr("get")?;
            let key = namespace.get_item(global_name)?.ok_or_else(|| {
                pyo3::exceptions::PyNameError::new_err(format!(
                    "name '{}' is not defined",
                    global_name.to_string_lossy()
                ))
            })?;
            getter.call1((key,))
        }
        ReportExtras::Native(extras) => {
            let key = namespace.get_item(global_name)?.ok_or_else(|| {
                pyo3::exceptions::PyNameError::new_err(format!(
                    "name '{}' is not defined",
                    global_name.to_string_lossy()
                ))
            })?;
            // A custom hash or equality method can reenter and mutate the
            // report dictionary, so give that protocol the actual object.
            if !key.is_exact_instance_of::<PyString>() {
                let report = source.to_python(py)?;
                return report
                    .bind(py)
                    .getattr("__dict__")?
                    .call_method1("get", (key,));
            }
            match extras {
                Some(extras) => Ok(extras
                    .get_item(py, key.cast::<PyString>()?)?
                    .map_or_else(|| py.None().into_bound(py), |value| value.into_bound(py))),
                None => Ok(py.None().into_bound(py)),
            }
        }
    }
}

/// Scan the terminal's current ordered report snapshot without publishing raw rows.
pub(crate) fn summary(
    history: &Bound<'_, NativeSessionReports>,
    reporter: &Bound<'_, PyAny>,
    namespace: &Bound<'_, PyDict>,
    print_statistics: &Bound<'_, PyAny>,
) -> PyResult<Py<PyList>> {
    let py = history.py();
    let examples = PyList::empty(py);
    let reports = super::reporting::terminal::report_sources(history)?;
    let stats_name = PyString::new(py, "_STATS");
    let examples_name = PyString::new(py, "_EXAMPLES");
    for report in &reports {
        let statistics = summary_value(report, namespace, &stats_name)?;
        if statistics.is_truthy()? && print_statistics.is_truthy()? {
            let write_line = reporter.getattr("write_line")?;
            let text = statistics.add("\n\n")?;
            write_line.call1((text,))?;
        }
        let patch = summary_value(report, namespace, &examples_name)?;
        if patch.is_instance_of::<PyString>() {
            let loads = global(namespace, "json")?.getattr("loads")?;
            examples.append(loads.call1((patch,))?)?;
        }
    }
    Ok(examples.unbind())
}

struct OwnerObjects {
    factory: Py<PyAny>,
    globals: Py<PyDict>,
}

/// The public bound C method owns the same legacy generator as hook dispatch.
#[pyclass(frozen)]
pub(crate) struct NativeHypothesisReport {
    objects: Mutex<Option<OwnerObjects>>,
}

impl NativeHypothesisReport {
    fn globals(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        self.objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|objects| objects.globals.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("Hypothesis report owner has been cleared"))
    }
}

pub(crate) fn finish_handle(
    owner: &Bound<'_, NativeHypothesisReport>,
    item: &Bound<'_, PyAny>,
    report: &ReportHandle,
) -> PyResult<()> {
    let globals = owner.get().globals(item.py())?;
    finish(globals.bind(item.py()), item, Report::Native(report))
}

pub(crate) fn finish_python(
    owner: &Bound<'_, NativeHypothesisReport>,
    item: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let globals = owner.get().globals(item.py())?;
    finish(globals.bind(item.py()), item, Report::Python(report))
}

#[pymethods]
impl NativeHypothesisReport {
    fn pytest_runtest_makereport(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        call: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let factory = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|objects| objects.factory.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("Hypothesis report owner has been cleared"))?;
        Ok(factory.bind(py).call1((item, call))?.unbind())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_lock()
            && let Some(objects) = objects.as_ref()
        {
            visit.call(&objects.factory)?;
            visit.call(&objects.globals)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(removed);
    }
}

#[pyclass(frozen)]
struct NativeHypothesisFinisher {
    globals: Mutex<Option<Py<PyDict>>>,
}

#[pymethods]
impl NativeHypothesisFinisher {
    fn finish(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let globals = self
            .globals
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|globals| globals.clone_ref(py))
            .ok_or_else(|| {
                PyRuntimeError::new_err("Hypothesis report finisher has been cleared")
            })?;
        finish(globals.bind(py), item, Report::Python(report))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(globals) = self.globals.try_lock() {
            visit.call(&*globals)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .globals
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(removed);
    }
}

#[pyfunction]
fn independent_hypothesis_report(namespace: &Bound<'_, PyDict>) -> PyResult<Py<PyAny>> {
    let py = namespace.py();
    let finisher = Bound::new(
        py,
        NativeHypothesisFinisher {
            globals: Mutex::new(Some(namespace.clone().unbind())),
        },
    )?;
    let finish = finisher.getattr("finish")?;
    let factory = crate::hooks::nativeop::hypothesis_report_factory(py, &finish)?;
    let owner = Bound::new(
        py,
        NativeHypothesisReport {
            objects: Mutex::new(Some(OwnerObjects {
                factory: factory.unbind(),
                globals: namespace.clone().unbind(),
            })),
        },
    )?;
    Ok(owner.getattr("pytest_runtest_makereport")?.unbind())
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(independent_hypothesis_report, module)?)
}
