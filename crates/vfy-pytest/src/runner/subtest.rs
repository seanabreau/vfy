//! Owns the subtest collector.

use super::model::XfailPlan;
use super::test_call::classify_call_result;
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyErr, PyResult, PyTupleMethods, Python, pyclass, pymethods,
};
use pyo3::types::{PyAny, PyTuple};
use std::sync::Mutex;
use vfy_core::test_execution::SubtestResult;

#[pyclass]
pub(super) struct NativeSubtestCollector {
    pub(super) item: Py<PyAny>,
    pub(super) original: Py<PyAny>,
    pub(super) xfail: Option<XfailPlan>,
    pub(super) reset_xfail_match: Option<Py<PyAny>>,
    pub(super) reports: Mutex<Vec<SubtestResult>>,
}

#[pymethods]
impl NativeSubtestCollector {
    #[pyo3(name = "addSubTest")]
    fn add_subtest(
        &self,
        py: Python<'_>,
        test_case: &Bound<'_, PyAny>,
        test: &Bound<'_, PyAny>,
        exc_info: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if let Some(reset) = &self.reset_xfail_match {
            reset.bind(py).call0()?;
        }
        self.original.bind(py).call1((test_case, test, exc_info))?;
        if !exc_info.is_none() {
            let exceptions = self.item.bind(py).getattr("_excinfo")?;
            if !exceptions.is_none() && exceptions.len()? > 0 {
                let last = exceptions.get_item(exceptions.len()? - 1)?;
                if last.is(exc_info) {
                    exceptions.call_method0("pop")?;
                }
            }
        }
        let report = subtest_result(py, self.item.bind(py), test, exc_info, self.xfail.as_ref())?;
        self.reports
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(report);
        Ok(())
    }
}

impl NativeSubtestCollector {
    pub(super) fn take_reports(&self) -> Vec<SubtestResult> {
        std::mem::take(
            &mut *self
                .reports
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
        )
    }
}

fn subtest_result(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    test: &Bound<'_, PyAny>,
    exc_info: &Bound<'_, PyAny>,
    xfail: Option<&XfailPlan>,
) -> PyResult<SubtestResult> {
    let context = subtest_context(py, test)?;
    let result = if exc_info.is_none() {
        Ok(())
    } else {
        let exception = if let Ok(exception) = exc_info.getattr("value") {
            exception
        } else {
            exc_info.cast::<PyTuple>()?.get_item(1)?
        };
        Err(PyErr::from_value(exception))
    };
    let report = classify_call_result(py, item, result, xfail)?;
    Ok(SubtestResult {
        report: None,
        outcome: report.outcome,
        context,
        exception: report.exception.map(Into::into),
        detail: report.detail,
        skip_location: report.skip_location,
        terminal_lead: report.terminal_lead,
    })
}

fn subtest_context(py: Python<'_>, test: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut parts = Vec::new();
    let message = test.getattr("_message")?;
    let sentinel = py
        .import("unittest.case")?
        .getattr("_subtest_msg_sentinel")?;
    if !message.is_none() && !message.is(&sentinel) {
        parts.push(format!("[{}]", message.str()?));
    }
    let params = test.getattr("params")?;
    let entries = params.call_method0("items")?;
    let values = entries
        .try_iter()?
        .map(|entry| {
            let pair = entry?.cast_into::<PyTuple>()?;
            let name = pair.get_item(0)?.extract::<String>()?;
            let value = pair.get_item(1)?;
            let rendered = py
                .import("_pytest._io.saferepr")?
                .getattr("saferepr")?
                .call1((value,))?
                .extract::<String>()?;
            Ok(format!("{name}={rendered}"))
        })
        .collect::<PyResult<Vec<_>>>()?;
    if !values.is_empty() {
        parts.push(format!("({})", values.join(", ")));
    }
    if parts.is_empty() {
        Ok("(<subtest>)".to_owned())
    } else {
        Ok(parts.join(" "))
    }
}
