//! Python subreports backed by report state.

use super::{NativeIndependentReport, ReportKind, ReportSections};
use crate::independent::subtests::NativeSubtestContext;
use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyType};
use vfy_core::test_execution::{
    PhasePayload, SubtestContext, SubtestReportMetadata, SubtestResult,
};

#[pyclass(extends = NativeIndependentReport, module = "_pytest.subtests", name = "SubtestReport")]
pub(crate) struct NativeSubtestReport;

pub(in crate::independent) fn clone_report(
    py: Python<'_>,
    source: &Bound<'_, NativeIndependentReport>,
    context: Py<NativeSubtestContext>,
    captured: &PhasePayload,
) -> PyResult<Py<NativeIndependentReport>> {
    // The actual public protocol is observable and may reenter or modify the
    // wire dictionary. Invoke it once, without any report/context owner borrow.
    let data = source.call_method0("_to_json")?.cast_into::<PyDict>()?;
    let result = from_wire(py, &data, context, captured)?;
    let origin = source.borrow().origin.clone();
    result.borrow_mut(py).origin = origin;
    Ok(result)
}

fn from_wire(
    py: Python<'_>,
    data: &Bound<'_, PyDict>,
    context: Py<NativeSubtestContext>,
    captured: &PhasePayload,
) -> PyResult<Py<NativeIndependentReport>> {
    let kwargs = super::serialization::constructor_kwargs(data)?;
    let base = super::serialization::from_json_fields(&kwargs)?;
    from_fields(py, &kwargs, base, Some(context), captured)
}

fn from_fields(
    py: Python<'_>,
    data: &Bound<'_, PyDict>,
    mut base: NativeIndependentReport,
    context: Option<Py<NativeSubtestContext>>,
    captured: &PhasePayload,
) -> PyResult<Py<NativeIndependentReport>> {
    base.kind = ReportKind::Subtest(context);
    let ReportSections::Live(sections) = &base.sections else {
        return Err(PyValueError::new_err("wire sections are not initialized"));
    };
    for (name, text) in [
        ("Captured stdout call", &captured.stdout),
        ("Captured stderr call", &captured.stderr),
        ("Captured log call", &captured.log),
    ] {
        if !text.is_empty() {
            sections.bind(py).append((name, text))?;
        }
    }
    let result = Py::new(py, (NativeSubtestReport, base))?;
    super::serialization::populate_extras(data, result.bind(py).as_any())?;
    Ok(result.bind(py).as_super().clone().unbind())
}

pub(in crate::independent) fn snapshot(
    report: &Bound<'_, NativeIndependentReport>,
) -> PyResult<SubtestResult> {
    let py = report.py();
    let context = report
        .borrow()
        .subtest_context(py)
        .ok_or_else(|| PyValueError::new_err("subreport has no context"))?;
    let phase = NativeIndependentReport::snapshot(report)?;
    let description = context.borrow(py).describe(py)?;
    let (msg, kwargs) = context.borrow(py).handles(py)?;
    let msg = if msg.bind(py).is_none() {
        None
    } else {
        Some(msg.bind(py).str()?.to_str()?.to_owned())
    };
    let kwargs = kwargs
        .bind(py)
        .as_any()
        .call_method0("items")?
        .try_iter()?
        .map(|entry| {
            let (key, value) = entry?.extract::<(String, Py<PyAny>)>()?;
            Ok((key, value.bind(py).str()?.to_str()?.to_owned()))
        })
        .collect::<PyResult<Vec<_>>>()?;
    let timing = report.borrow().timing;
    Ok(SubtestResult {
        outcome: phase.outcome,
        context: description,
        exception: phase.exception.clone(),
        detail: phase.detail.clone(),
        skip_location: phase.skip_location.clone(),
        terminal_lead: phase.terminal_lead.clone(),
        report: Some(Box::new(SubtestReportMetadata {
            context: SubtestContext { msg, kwargs },
            duration_ms: phase.duration_ms,
            start: timing.start,
            stop: timing.stop,
            stdout: NativeIndependentReport::strings(report, "Captured stdout")?.concat(),
            stderr: NativeIndependentReport::strings(report, "Captured stderr")?.concat(),
            log: NativeIndependentReport::strings(report, "Captured log")?.join("\n"),
            report_sections: phase.report_sections.clone(),
            report_summary: phase.report_summary.clone(),
        })),
    })
}

#[pymethods]
impl NativeSubtestReport {
    #[getter]
    fn context(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<Py<NativeSubtestContext>> {
        slf.into_super()
            .subtest_context(py)
            .ok_or_else(|| PyAttributeError::new_err("subreport has no context"))
    }
    #[setter(context)]
    fn set_context(slf: PyRefMut<'_, Self>, value: Py<NativeSubtestContext>) {
        let mut base = slf.into_super();
        let previous = std::mem::replace(&mut base.kind, ReportKind::Subtest(Some(value)));
        drop(base);
        drop(previous);
    }
    fn _sub_test_description(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<String> {
        let context = slf
            .into_super()
            .subtest_context(py)
            .ok_or_else(|| PyAttributeError::new_err("subreport has no context"))?;
        context.borrow(py).describe(py)
    }
    #[getter]
    fn head_line(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<String> {
        let base = slf.into_super();
        let domain = base.location.2.clone();
        let context = base
            .subtest_context(py)
            .ok_or_else(|| PyAttributeError::new_err("subreport has no context"))?;
        drop(base);
        let description = context.borrow(py).describe(py)?;
        Ok(format!("{domain} {description}"))
    }
    #[classmethod]
    #[pyo3(signature = (test_report, context, captured_output=None, captured_logs=None))]
    fn _new(
        _class: &Bound<'_, PyType>,
        test_report: &Bound<'_, NativeIndependentReport>,
        context: Py<NativeSubtestContext>,
        captured_output: Option<&Bound<'_, PyAny>>,
        captured_logs: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<NativeIndependentReport>> {
        let py = test_report.py();
        let data = test_report
            .call_method0("_to_json")?
            .cast_into::<PyDict>()?;
        let data = super::serialization::constructor_kwargs(&data)?;
        // Reconstruct all core fields and extras before any capture descriptor.
        let reconstructed = super::serialization::from_json_fields(&data)?;
        let report = from_fields(
            py,
            &data,
            reconstructed,
            Some(context),
            &PhasePayload::default(),
        )?;
        let origin = test_report.borrow().origin.clone();
        report.borrow_mut(py).origin = origin;
        if let Some(output) = captured_output
            && output.is_truthy()?
        {
            for (attribute, heading) in [
                ("out", "Captured stdout call"),
                ("err", "Captured stderr call"),
            ] {
                if output.getattr(attribute)?.is_truthy()? {
                    let append = report.bind(py).getattr("sections")?.getattr("append")?;
                    append.call1(((heading, output.getattr(attribute)?),))?;
                }
            }
        }
        if let Some(logs) = captured_logs
            && logs.is_truthy()?
        {
            let text = logs
                .getattr("handler")?
                .getattr("stream")?
                .call_method0("getvalue")?;
            if text.is_truthy()? {
                report
                    .bind(py)
                    .getattr("sections")?
                    .call_method1("append", (("Captured log call", text),))?;
            }
        }
        Ok(report)
    }
    fn _to_json(slf: &Bound<'_, Self>) -> PyResult<Py<PyDict>> {
        let base = slf.as_super();
        let result = super::serialization::to_json(base)?;
        if result.bind(slf.py()).contains("context")? {
            result.bind(slf.py()).del_item("context")?;
        }
        result
            .bind(slf.py())
            .set_item("_report_type", "SubTestReport")?;
        let context = base
            .borrow()
            .subtest_context(slf.py())
            .ok_or_else(|| PyAttributeError::new_err("subreport has no context"))?;
        result.bind(slf.py()).set_item(
            "_subtest.context",
            context.bind(slf.py()).call_method0("_to_json")?,
        )?;
        Ok(result)
    }
    #[classmethod]
    fn _from_json(
        _class: &Bound<'_, PyType>,
        data: &Bound<'_, PyDict>,
    ) -> PyResult<Py<NativeIndependentReport>> {
        let py = data.py();
        let copied = super::serialization::constructor_kwargs(data)?;
        let mut base = super::serialization::from_json_fields(&copied)?;
        base.pending_copy = Some(data.clone().unbind());
        let report = from_fields(py, &copied, base, None, &PhasePayload::default())?;
        let context = py
            .get_type::<NativeSubtestContext>()
            .call_method1("_from_json", (data.as_any().get_item("_subtest.context")?,))?
            .extract::<Py<NativeSubtestContext>>()?;
        let previous = {
            let mut base = report.borrow_mut(py);
            std::mem::replace(&mut base.kind, ReportKind::Subtest(Some(context)))
        };
        drop(previous);
        Ok(report)
    }
}
