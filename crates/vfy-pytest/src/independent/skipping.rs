//! Skipping evaluates live markers at the builtin plugin's hook positions.

use super::reporting::NativeIndependentCallInfo;
use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyAttributeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple, PyType};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;

mod conditions;

struct Xfail {
    reason: Py<PyAny>,
    run: Py<PyAny>,
    strict: Py<PyAny>,
    raises: Py<PyAny>,
}

impl Xfail {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            reason: self.reason.clone_ref(py),
            run: self.run.clone_ref(py),
            strict: self.strict.clone_ref(py),
            raises: self.raises.clone_ref(py),
        }
    }
}

#[pyclass(frozen, unsendable)]
struct XfailState {
    value: RefCell<Option<Xfail>>,
}

#[pymethods]
impl XfailState {
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its GC visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(value) = self.value.try_borrow()
            && let Some(value) = value.as_ref()
        {
            visit.call(&value.reason)?;
            visit.call(&value.run)?;
            visit.call(&value.strict)?;
            visit.call(&value.raises)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let previous = self.value.borrow_mut().take();
        drop(previous);
    }
}

fn state<'py>(item: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, XfailState>>> {
    match item.getattr("_vfy_xfail_state") {
        Ok(state) if state.is_none() => Ok(None),
        Ok(state) => Ok(Some(state.cast_into::<XfailState>()?)),
        Err(error) if error.is_instance_of::<PyAttributeError>(item.py()) => {
            item.setattr("_vfy_xfail_state", item.py().None())?;
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn current(item: &Bound<'_, PyAny>) -> PyResult<Option<Xfail>> {
    let Some(state) = state(item)? else {
        return Ok(None);
    };
    let state = state.borrow();
    let value = state.value.borrow();
    Ok(value.as_ref().map(|value| value.clone_ref(item.py())))
}

fn replace(item: &Bound<'_, PyAny>, value: Option<Xfail>) -> PyResult<()> {
    let state = match state(item)? {
        Some(state) => state,
        None if value.is_none() => return Ok(()),
        None => {
            let state = Bound::new(
                item.py(),
                XfailState {
                    value: RefCell::new(None),
                },
            )?;
            item.setattr("_vfy_xfail_state", &state)?;
            state
        }
    };
    let previous = state.borrow().value.replace(value);
    drop(previous);
    Ok(())
}

fn conditions(mark: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    let kwargs = mark.getattr("kwargs")?.cast_into::<PyDict>()?;
    kwargs.get_item("condition")?.map_or_else(
        || mark.getattr("args")?.extract(),
        |condition| Ok(vec![condition.unbind()]),
    )
}

fn keyword<'py>(
    kwargs: &Bound<'py, PyDict>,
    name: &str,
    default: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    Ok(kwargs.get_item(name)?.unwrap_or(default))
}

fn evaluate_xfail(item: &Bound<'_, PyAny>) -> PyResult<Option<Xfail>> {
    let py = item.py();
    for mark in item.call_method1("iter_markers", ("xfail",))?.try_iter()? {
        let mark = mark?;
        let kwargs = mark.getattr("kwargs")?.cast_into::<PyDict>()?;
        let run = keyword(
            &kwargs,
            "run",
            true.into_pyobject(py)?.to_owned().into_any(),
        )?;
        let mut strict = keyword(&kwargs, "strict", py.None().into_bound(py))?;
        let config = item.getattr("config")?;
        if strict.is_none() {
            strict = config.call_method1("getini", ("strict_xfail",))?;
        }
        if strict.is_none() {
            strict = config.call_method1("getini", ("strict",))?;
        }
        let raises = keyword(&kwargs, "raises", py.None().into_bound(py))?;
        let conditions = conditions(&mark)?;
        let reason = if conditions.is_empty() {
            Some(keyword(
                &kwargs,
                "reason",
                PyString::new(py, "").into_any(),
            )?)
        } else {
            let mut reason = None;
            for condition in conditions {
                let (active, message) = conditions::evaluate(item, &mark, condition.bind(py))?;
                if active {
                    reason = Some(message);
                    break;
                }
            }
            reason
        };
        if let Some(reason) = reason {
            return Ok(Some(Xfail {
                reason: reason.unbind(),
                run: run.unbind(),
                strict: strict.unbind(),
                raises: raises.unbind(),
            }));
        }
    }
    Ok(None)
}

fn evaluate_skip(item: &Bound<'_, PyAny>) -> PyResult<Option<Py<PyAny>>> {
    let py = item.py();
    for mark in item.call_method1("iter_markers", ("skipif",))?.try_iter()? {
        let mark = mark?;
        let conditions = conditions(&mark)?;
        if conditions.is_empty() {
            let kwargs = mark.getattr("kwargs")?.cast_into::<PyDict>()?;
            return Ok(Some(
                keyword(&kwargs, "reason", PyString::new(py, "").into_any())?.unbind(),
            ));
        }
        for condition in conditions {
            let (active, reason) = conditions::evaluate(item, &mark, condition.bind(py))?;
            if active {
                return Ok(Some(reason.unbind()));
            }
        }
    }
    let mark = item
        .call_method1("iter_markers", ("skip",))?
        .try_iter()?
        .next();
    let Some(mark) = mark else {
        return Ok(None);
    };
    let mark = mark?;
    let args = mark.getattr("args")?.cast_into::<PyTuple>()?;
    let kwargs = mark.getattr("kwargs")?.cast_into::<PyDict>()?;
    for (key, _) in kwargs.iter() {
        if key.extract::<String>()? != "reason" {
            return Err(PyTypeError::new_err(format!(
                "Skip.__init__() got an unexpected keyword argument {} - maybe you meant pytest.mark.skipif?",
                key.repr()?
            )));
        }
    }
    if args.len() > 1 || (args.len() == 1 && kwargs.contains("reason")?) {
        return Err(PyTypeError::new_err(
            "skip marker received multiple reasons - maybe you meant pytest.mark.skipif?",
        ));
    }
    Ok(Some(if args.is_empty() {
        keyword(
            &kwargs,
            "reason",
            PyString::new(py, "unconditional skip").into_any(),
        )?
        .unbind()
    } else {
        args.get_item(0)?.unbind()
    }))
}

fn runxfail(item: &Bound<'_, PyAny>) -> PyResult<bool> {
    item.getattr("config")?
        .getattr("option")?
        .getattr("runxfail")?
        .is_truthy()
}

fn no_run(item: &Bound<'_, PyAny>, value: Option<&Xfail>) -> PyResult<()> {
    if let Some(value) = value
        && !runxfail(item)?
        && !value.run.bind(item.py()).is_truthy()?
    {
        let py = item.py();
        let message = Module::Operator
            .get(py)?
            .call_method1("add", ("[NOTRUN] ", &value.reason))?;
        Module::Facade
            .get(py)?
            .getattr("xfail")?
            .call1((message,))?;
        return Err(PyAssertionError::new_err("xfail returned without raising"));
    }
    Ok(())
}

/// Methods are callbacks for the thin builtin hook wrappers; item state remains native.
#[pyclass(frozen)]
pub(crate) struct NativeSkippingHooks;

#[pymethods]
impl NativeSkippingHooks {
    #[staticmethod]
    fn configure(config: &Bound<'_, PyAny>) -> PyResult<()> {
        if !config.getattr("option")?.getattr("runxfail")?.is_truthy()? {
            return Ok(());
        }
        let py = config.py();
        let module = Module::Sys
            .get(py)?
            .getattr("modules")?
            .get_item("pytest")?;
        let original = module.getattr("xfail")?;
        let replacement = Module::Protocol
            .get(py)?
            .getattr("_runxfail_callable")?
            .call1((Module::Facade.get(py)?.getattr("XFailed")?,))?;
        let cleanup = Module::Functools.get(py)?.getattr("partial")?.call1((
            Module::Builtins.get(py)?.getattr("setattr")?,
            &module,
            "xfail",
            original,
        ))?;
        config.call_method1("add_cleanup", (cleanup,))?;
        module.setattr("xfail", replacement)
    }

    #[staticmethod]
    fn setup(item: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = item.py();
        if let Some(reason) = evaluate_skip(item)? {
            let error = Module::Facade
                .get(py)?
                .getattr("Skipped")?
                .call1((reason,))?;
            error.setattr("_use_item_location", true)?;
            return Err(PyErr::from_value(error));
        }
        let value = evaluate_xfail(item)?;
        replace(item, value)?;
        no_run(item, current(item)?.as_ref())
    }

    #[staticmethod]
    pub(crate) fn call_start(item: &Bound<'_, PyAny>) -> PyResult<()> {
        if current(item)?.is_none() {
            replace(item, evaluate_xfail(item)?)?;
        }
        no_run(item, current(item)?.as_ref())
    }

    #[staticmethod]
    pub(crate) fn call_finish(item: &Bound<'_, PyAny>) -> PyResult<()> {
        if current(item)?.is_none() {
            replace(item, evaluate_xfail(item)?)?;
        }
        Ok(())
    }

    #[staticmethod]
    pub(crate) fn report(
        item: &Bound<'_, PyAny>,
        call: &Bound<'_, NativeIndependentCallInfo>,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        report_shared(
            item,
            crate::hooks::invocation::OriginalPhaseCall::Python(call),
            SkipReport::Python(report),
        )
    }

    #[expect(
        clippy::unused_self,
        reason = "the registered status hook owns its native operation"
    )]
    fn status(
        &self,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
        status_shared(report.py(), SkipReport::Python(report))
    }
}

/// The same xfail transform serves a native row and an exposed report.
#[derive(Clone, Copy)]
pub(crate) enum SkipReport<'a, 'py> {
    Native(&'a super::reporting::ReportHandle),
    Python(&'a Bound<'py, PyAny>),
}

impl SkipReport<'_, '_> {
    fn get(&self, py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
        match self {
            Self::Native(report) => report.getattr(py, name),
            Self::Python(report) => Ok(report.getattr(name)?.unbind()),
        }
    }

    fn set(&self, py: Python<'_>, name: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        match self {
            Self::Native(report) => report.setattr(py, name, value),
            Self::Python(report) => report.setattr(name, value),
        }
    }
}

pub(crate) fn status_shared(
    py: Python<'_>,
    report: SkipReport<'_, '_>,
) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
    let wasxfail = match &report {
        SkipReport::Native(report) => report.hasattr(py, "wasxfail")?,
        SkipReport::Python(report) => {
            crate::module_cache::optional_attr(report, pyo3::intern!(py, "wasxfail"))?.is_some()
        }
    };
    if wasxfail {
        if report.get(py, "skipped")?.bind(py).is_truthy()? {
            return Ok(Some(("xfailed", "x", "XFAIL")));
        }
        if report.get(py, "passed")?.bind(py).is_truthy()? {
            return Ok(Some(("xpassed", "X", "XPASS")));
        }
    }
    Ok(None)
}

pub(crate) fn report_shared(
    item: &Bound<'_, PyAny>,
    call: crate::hooks::invocation::OriginalPhaseCall<'_, '_>,
    report: SkipReport<'_, '_>,
) -> PyResult<()> {
    use crate::hooks::invocation::OriginalPhaseCall;
    if runxfail(item)? {
        return Ok(());
    }
    let py = item.py();
    let error = match &call {
        OriginalPhaseCall::Native(call) => call.error_state(py)?,
        OriginalPhaseCall::Python(call) => NativeIndependentCallInfo::error(call)?,
    };
    let direct = Module::Facade.get(py)?.getattr("XFailed")?;
    if let Some(error) = &error
        && error.is_instance(py, &direct)
    {
        report.set(py, "wasxfail", &error.value(py).getattr("msg")?)?;
        report.set(py, "outcome", &"skipped".into_pyobject(py)?.into_any())?;
    } else if !report.get(py, "skipped")?.bind(py).is_truthy()?
        && let Some(value) = current(item)?
    {
        if let Some(error) = error {
            let raises = value.raises.bind(py);
            let matches = if raises.is_none() {
                true
            } else if raises.is_instance_of::<PyType>() || raises.is_instance_of::<PyTuple>() {
                error.value(py).is_instance(raises)?
            } else {
                raises.is_instance(
                    &py.import("vfy.independent.raises")?
                        .getattr("AbstractRaises")?,
                )? && raises
                    .call_method1("matches", (error.value(py),))?
                    .is_truthy()?
            };
            report.set(
                py,
                "outcome",
                &if matches { "skipped" } else { "failed" }
                    .into_pyobject(py)?
                    .into_any(),
            )?;
            if matches {
                report.set(py, "wasxfail", value.reason.bind(py))?;
            }
        } else if match &call {
            OriginalPhaseCall::Native(call) => call.phase()?,
            OriginalPhaseCall::Python(call) => call.borrow().phase()?,
        } == vfy_core::test_execution::ExecutionPhase::Call
        {
            if value.strict.bind(py).is_truthy()? {
                report.set(py, "outcome", &"failed".into_pyobject(py)?.into_any())?;
                report.set(
                    py,
                    "longrepr",
                    &Module::Operator
                        .get(py)?
                        .call_method1("add", ("[XPASS(strict)] ", value.reason))?,
                )?;
            } else {
                report.set(py, "outcome", &"passed".into_pyobject(py)?.into_any())?;
                report.set(py, "wasxfail", value.reason.bind(py))?;
            }
        }
    }
    Ok(())
}

impl NativeSkippingHooks {
    pub(crate) fn report_untyped(
        item: &Bound<'_, PyAny>,
        call: &Bound<'_, PyAny>,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        Self::report(item, call.cast::<NativeIndependentCallInfo>()?, report)
    }
}

/// Native operation declarations own the public generator program.
#[pyclass(frozen)]
pub(crate) struct NativeSkippingCall {
    factory: Py<PyAny>,
}

#[pymethods]
impl NativeSkippingCall {
    fn pytest_runtest_call(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        Ok(self.factory.bind(py).call1((item,))?.unbind())
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its GC visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.factory)
    }
}

#[pyclass(frozen)]
pub(crate) struct NativeSkippingReport {
    factory: Py<PyAny>,
}

#[pymethods]
impl NativeSkippingReport {
    fn pytest_runtest_makereport(
        &self,
        py: Python<'_>,
        item: &Bound<'_, PyAny>,
        call: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(self.factory.bind(py).call1((item, call))?.unbind())
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its GC visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.factory)
    }
}

pub(super) fn install(py: Python<'_>, context: &super::lifecycle::Context) -> PyResult<()> {
    let hooks = Bound::new(py, NativeSkippingHooks)?;
    let factories = crate::hooks::nativeop::skipping_factories(
        py,
        &hooks.getattr("call_start")?,
        &hooks.getattr("call_finish")?,
        &hooks.getattr("report")?,
    )?;
    let call_wrapper = Bound::new(
        py,
        NativeSkippingCall {
            factory: factories.get_item(0)?.unbind(),
        },
    )?;
    let report_wrapper = Bound::new(
        py,
        NativeSkippingReport {
            factory: factories.get_item(1)?.unbind(),
        },
    )?;
    let plugin = Module::Protocol
        .get(py)?
        .getattr("BuiltinSkippingHooks")?
        .call1((
            hooks.getattr("setup")?,
            hooks.getattr("status")?,
            hooks.getattr("configure")?,
            call_wrapper.getattr("pytest_runtest_call")?,
            report_wrapper.getattr("pytest_runtest_makereport")?,
        ))?;
    context.register(py, plugin.unbind(), "skipping")
}
