//! Adapt stdlib `TestCase` discovery and result callbacks to reports.

use super::reporting::call::PhaseCompletion;
use super::reporting::{self, NativeIndependentReport};
use super::{NativeIndependentRequest, controls};
use pyo3::exceptions::{PyAttributeError, PyBaseException, PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyType};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;
use std::collections::VecDeque;
use vfy_core::test_execution::PhasePayload;

pub(super) fn is_testcase(class: &Bound<'_, PyAny>) -> PyResult<bool> {
    let modules = class.py().import("sys")?.getattr("modules")?;
    let Some(unittest) = modules.cast::<PyDict>()?.get_item("unittest")? else {
        return Ok(false);
    };
    // Like pytest, an object that inspect reports as a class but that is not
    // a type, such as a wrapt proxy, is not a TestCase.
    let Ok(class) = class.cast::<PyType>() else {
        return Ok(false);
    };
    class.is_subclass(&unittest.getattr("TestCase")?)
}

pub(super) fn test_names(class: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let loader = class
        .py()
        .import("unittest")?
        .getattr("TestLoader")?
        .call0()?;
    let mut names = Vec::new();
    for name in loader
        .call_method1("getTestCaseNames", (class,))?
        .try_iter()?
    {
        let name = name?.extract::<String>()?;
        let method = class.getattr(name.as_str())?;
        if !method.hasattr("__test__")? || method.getattr("__test__")?.is_truthy()? {
            names.push(name);
        }
    }
    if names.is_empty() && class.hasattr("runTest")? {
        names.push("runTest".to_owned());
    }
    Ok(names)
}

fn raw_error(raw: &Bound<'_, PyAny>) -> PyResult<Option<PyErr>> {
    if raw.is_none() {
        return Ok(None);
    }
    let value = raw.get_item(1)?;
    if !value.is_instance_of::<PyBaseException>() {
        return Err(PyTypeError::new_err(
            "unittest result requires an exception triple",
        ));
    }
    let value = raw
        .py()
        .get_type::<PyBaseException>()
        .getattr("with_traceback")?
        .call1((value, raw.get_item(2)?))?;
    Ok(Some(PyErr::from_value(value)))
}

#[pyclass(frozen, unsendable, module = "vfy._native._pytest")]
struct NativeUnitTestResult {
    item: RefCell<Option<Py<PyAny>>>,
    // The item owns unconsumed exceptions for its natural lifetime, as stdlib result callbacks do.
    // The weak item reference prevents this adapter from extending that lifetime.
    pending: RefCell<VecDeque<Py<PyAny>>>,
}

#[pymethods]
impl NativeUnitTestResult {
    #[pyo3(signature = (raw, msg=None, params=None))]
    fn __call__(
        &self,
        py: Python<'_>,
        raw: &Bound<'_, PyAny>,
        msg: Option<Py<PyAny>>,
        params: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<()> {
        let error = raw_error(raw)?;
        if params.is_none() {
            if let Some(error) = error {
                if controls::is_control_error(py, &error)? {
                    return Err(error);
                }
                let value = error.into_value(py).into_any();
                self.pending.borrow_mut().push_back(value);
            }
            return Ok(());
        }
        let item = self
            .item
            .borrow()
            .as_ref()
            .map(|item| item.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("unittest result has been cleared"))?;
        let item = item.bind(py).call0()?;
        if item.is_none() {
            return Err(PyRuntimeError::new_err("unittest item has been released"));
        }
        let item = &item;
        let request = item
            .getattr("_request")?
            .cast_into::<NativeIndependentRequest>()?;
        let origin = request.borrow().report_source(py)?;
        let ihook = item.getattr("ihook")?;
        let completion = PhaseCompletion::unittest_callback(error.map_or(Ok(()), Err));
        let (base, call) = reporting::make_subtest_report(py, &ihook, item, completion, origin)?;
        let values =
            params.ok_or_else(|| PyRuntimeError::new_err("unittest subtest has no parameters"))?;
        let values = py.get_type::<PyDict>().call1((values,))?;
        let context = super::subtests::NativeSubtestContext::new(
            py,
            msg.unwrap_or_else(|| py.None()),
            &values,
        )?;
        let report = reporting::subtest_report::clone_report(
            py,
            base.bind(py),
            Py::new(py, context)?,
            &PhasePayload::default(),
        )?;
        publish_subtest(py, &ihook, item, &call, &report)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(item) = self.item.try_borrow()
            && let Some(item) = &*item
        {
            visit.call(item)?;
        }
        if let Ok(pending) = self.pending.try_borrow() {
            for error in pending.iter() {
                visit.call(error)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let item = self.item.borrow_mut().take();
        let pending = self.pending.take();
        drop(item);
        drop(pending);
    }
}

fn publish_subtest(
    py: Python<'_>,
    ihook: &Bound<'_, PyAny>,
    item: &Bound<'_, PyAny>,
    call: &Py<super::NativeIndependentCallInfo>,
    report: &Py<NativeIndependentReport>,
) -> PyResult<()> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("report", report)?;
    ihook
        .getattr("pytest_runtest_logreport")?
        .call((), Some(&kwargs))?;
    if reporting::interaction::eligible(py, call.bind(py).as_any(), report.bind(py).as_any())? {
        reporting::interaction::dispatch_subtest(
            py,
            ihook,
            item,
            call.bind(py).as_any(),
            report.bind(py).as_any(),
        )?;
    }
    Ok(())
}

pub(super) fn run(py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<()> {
    let callback = Py::new(
        py,
        NativeUnitTestResult {
            item: RefCell::new(Some(
                py.import("weakref")?.call_method1("ref", (item,))?.unbind(),
            )),
            pending: RefCell::default(),
        },
    )?;
    item.setattr("_vfy_unittest_result", callback)?;
    item.call_method0("runtest").map(|_| ())
}

pub(super) fn completion(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    mut completion: PhaseCompletion,
) -> PyResult<PhaseCompletion> {
    let callback = match item.getattr("_vfy_unittest_result") {
        Ok(callback) => Some(callback),
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => None,
        Err(error) => return Err(error),
    };
    if let Some(callback) = callback
        && let Ok(callback) = callback.cast::<NativeUnitTestResult>()
    {
        let error = callback.borrow().pending.borrow_mut().pop_front();
        if let Some(error) = error {
            completion.replace_exception(PyErr::from_value(error.into_bound(py)));
        }
    }
    completion.map_error(|error| normalize_skip(py, error))
}

fn normalize_skip(py: Python<'_>, error: PyErr) -> PyResult<PyErr> {
    let modules = py.import("sys")?.getattr("modules")?;
    if let Some(unittest) = modules.cast::<PyDict>()?.get_item("unittest")?
        && error.is_instance(py, &unittest.getattr("SkipTest")?)
    {
        return match py
            .import("vfy.independent.facade")?
            .call_method1("skip", (error.value(py).str()?,))
        {
            Err(skipped) => {
                // The skip helper's own frame is hidden; report the location
                // that raised SkipTest.
                skipped
                    .value(py)
                    .call_method1("with_traceback", (error.traceback(py),))?;
                Ok(skipped)
            }
            Ok(_) => Err(PyRuntimeError::new_err("skip did not raise its outcome")),
        };
    }
    Ok(error)
}
