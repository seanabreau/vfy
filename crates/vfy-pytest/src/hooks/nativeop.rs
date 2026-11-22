//! Immutable program definition for public `CPython` skipping generators.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyCode, PyCodeInput, PyCodeMethods, PyModule};

struct Programs {
    skipping: Py<PyAny>,
    hypothesis_report: Py<PyAny>,
    method_type: Py<PyAny>,
}

static PROGRAMS: PyOnceLock<Programs> = PyOnceLock::new();

const SOURCE: &std::ffi::CStr = cr"
def make(start, finish, report):
    def call(item):
        __tracebackhide__ = True
        start(item)
        try:
            return (yield)
        finally:
            finish(item)

    def makereport(item, call):
        __tracebackhide__ = True
        result = yield
        report(item, call, result)
        return result

    return call, makereport

def make_hypothesis_report(finish):
    def makereport(item, call):
        report = (yield).get_result()
        finish(item, report)
    return makereport
";

/// Install the private program before user plugins can register.
pub(crate) fn initialize(py: Python<'_>) -> PyResult<()> {
    PROGRAMS.get_or_try_init(py, || -> PyResult<Programs> {
        // A private namespace avoids sharing temporary sys.modules state with
        // concurrent initializers and import observers.
        let module = PyModule::new(py, "_vfy_skipping_program")?;
        module.setattr("__file__", "<vfy skipping program>")?;
        PyCode::compile(py, SOURCE, c"<vfy skipping program>", PyCodeInput::File)?
            .run(Some(&module.dict()), None)?;
        let skipping = module.getattr("make")?;
        let method_type = skipping
            .getattr("__get__")?
            .call1((true,))?
            .get_type()
            .into_any()
            .unbind();
        let program = Programs {
            skipping: skipping.unbind(),
            hypothesis_report: module.getattr("make_hypothesis_report")?.unbind(),
            method_type,
        };
        module.delattr("make")?;
        module.delattr("make_hypothesis_report")?;
        Ok(program)
    })?;
    Ok(())
}

/// The real `CPython` bound-method type obtained from the owned program definition.
pub(crate) fn method_type(py: Python<'_>) -> PyResult<Py<PyAny>> {
    PROGRAMS
        .get(py)
        .map(|program| program.method_type.clone_ref(py))
        .ok_or_else(|| PyRuntimeError::new_err("native hook program is not initialized"))
}

pub(crate) fn bound_method_type(py: Python<'_>) -> Option<&Bound<'_, PyAny>> {
    PROGRAMS.get(py).map(|program| program.method_type.bind(py))
}

pub(crate) fn skipping_factories<'py>(
    py: Python<'py>,
    start: &Bound<'py, PyAny>,
    finish: &Bound<'py, PyAny>,
    report: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    PROGRAMS
        .get(py)
        .ok_or_else(|| PyRuntimeError::new_err("skipping program is not initialized"))?
        .skipping
        .bind(py)
        .call1((start, finish, report))
}

pub(crate) fn hypothesis_report_factory<'py>(
    py: Python<'py>,
    finish: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    PROGRAMS
        .get(py)
        .ok_or_else(|| PyRuntimeError::new_err("Hypothesis report program is not initialized"))?
        .hypothesis_report
        .bind(py)
        .call1((finish,))
}
