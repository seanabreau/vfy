//! Native report wire dictionaries preserve Python extras and core report fields.

use super::call::PhaseTiming;
use super::{NativeIndependentReport, ReportKind, ReportSections, ReportUserProperties};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use vfy_core::test_execution::{ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult};

pub(super) fn to_json(report: &Bound<'_, NativeIndependentReport>) -> PyResult<Py<PyDict>> {
    let py = report.py();
    let values = report.getattr("__dict__")?.cast_into::<PyDict>()?.copy()?;
    for name in [
        "nodeid",
        "location",
        "outcome",
        "when",
        "duration",
        "start",
        "stop",
        "sections",
        "user_properties",
    ] {
        values.set_item(name, report.getattr(name)?)?;
    }
    if !values.contains("keywords")? {
        values.set_item("keywords", PyDict::new(py))?;
    }
    let representation = super::graph_wire::to_json(report.as_any())?;
    values.set_item("longrepr", representation)?;
    super::copies::issue(report, &values);
    Ok(values.unbind())
}

/// Decode the wire before taking the constructor keyword snapshot.
pub(super) fn constructor_kwargs<'py>(data: &Bound<'py, PyDict>) -> PyResult<Bound<'py, PyDict>> {
    super::graph_wire::from_json(data)?;
    data.copy()
}

pub(super) fn from_json(data: &Bound<'_, PyDict>) -> PyResult<Py<NativeIndependentReport>> {
    let py = data.py();
    let copied = constructor_kwargs(data)?;
    let mut fields = from_json_fields(&copied)?;
    fields.pending_copy = Some(data.clone().unbind());
    let result = Py::new(py, fields)?;
    populate_extras(&copied, result.bind(py).as_any())?;
    Ok(result)
}

pub(super) fn from_json_fields(data: &Bound<'_, PyDict>) -> PyResult<NativeIndependentReport> {
    let py = data.py();
    let get = |name: &str| data.as_any().get_item(name);
    let when = match get("when")?.extract::<String>()?.as_str() {
        "setup" => ExecutionPhase::Setup,
        "call" => ExecutionPhase::Call,
        "teardown" => ExecutionPhase::Teardown,
        _ => {
            return Err(PyValueError::new_err(
                "report phase must be setup, call, or teardown",
            ));
        }
    };
    let outcome = match get("outcome")?.extract::<String>()?.as_str() {
        "passed" => ExecutionOutcome::Passed,
        "skipped" => ExecutionOutcome::Skipped,
        "failed" if when == ExecutionPhase::Call => ExecutionOutcome::Failed,
        "failed" => ExecutionOutcome::Error,
        _ => {
            return Err(PyValueError::new_err(
                "report outcome must be passed, failed, or skipped",
            ));
        }
    };
    let representation = get("longrepr")?;
    let duration = get("duration")?.extract::<f64>()?;
    let properties = get("user_properties")?;
    let properties = if properties.is_truthy()? {
        PyList::new(py, properties.try_iter()?.collect::<PyResult<Vec<_>>>()?)?
    } else {
        PyList::empty(py)
    };
    let sections = PyList::new(
        py,
        get("sections")?.try_iter()?.collect::<PyResult<Vec<_>>>()?,
    )?;
    Ok(NativeIndependentReport {
        phase: PhaseResult::new(outcome, when, duration * 1000.0, PhasePayload::default()),
        issued_dicts: Vec::new(),
        pending_copy: None,
        row_id: None,
        pending_attributes: None,
        nodeid: get("nodeid")?
            .cast_into::<PyString>()?
            .to_cow()
            .map(|nodeid| std::sync::Arc::<str>::from(nodeid.as_ref()))?,
        location: std::sync::Arc::new(get("location")?.extract()?),
        sections: ReportSections::Live(sections.unbind()),
        longrepr: Some(representation.unbind()),
        representation_changed: true,
        refresh_representation: true,
        xfail_reason: None,
        user_properties: ReportUserProperties::Live(properties.unbind().into_any()),
        origin: None,
        timing: PhaseTiming {
            start: get("start")?.extract()?,
            stop: get("stop")?.extract()?,
            duration,
        },
        kind: ReportKind::Phase,
    })
}

pub(super) fn populate_extras(data: &Bound<'_, PyDict>, report: &Bound<'_, PyAny>) -> PyResult<()> {
    let extras = report.getattr("__dict__")?;
    for (name, value) in data {
        let key = name.extract::<String>()?;
        if !matches!(
            key.as_str(),
            "nodeid"
                | "location"
                | "outcome"
                | "when"
                | "duration"
                | "start"
                | "stop"
                | "sections"
                | "user_properties"
                | "longrepr"
                | "_subtest.context"
                | "_report_type"
        ) {
            extras.set_item(name, value)?;
        }
    }
    Ok(())
}
