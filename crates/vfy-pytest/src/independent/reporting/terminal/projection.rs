//! Project completed worker phases into the same terminal report protocol.

use super::super::call::PhaseTiming;
use super::super::{
    NativeIndependentCollectReport, NativeIndependentReport, ReportKind, ReportSections,
    ReportUserProperties,
};
use crate::results::{
    NativeRunResult, ReportLocation, append_phase_capture_sections, pytest_phase_report,
};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use vfy_core::test_execution::{CaseResult, CollectionOutcome, ExecutionPhase, PhaseResult};

fn phase_report(
    py: Python<'_>,
    nodeid: &str,
    location: ReportLocation,
    phase: PhaseResult,
    sections: Vec<(String, String)>,
    context: Option<Py<crate::independent::subtests::NativeSubtestContext>>,
) -> PyResult<Py<NativeIndependentReport>> {
    let (_, _, _, _, longrepr, wasxfail, sections, _) =
        pytest_phase_report(py, nodeid, &phase, sections, None)?;
    let summary = phase.report_summary.clone();
    let duration = phase.duration_ms / 1000.0;
    let mut fields = NativeIndependentReport {
        origin: None,
        phase,
        issued_dicts: Vec::new(),
        pending_copy: None,
        row_id: None,
        pending_attributes: None,
        nodeid: std::sync::Arc::from(nodeid),
        location: std::sync::Arc::new(location),
        sections: ReportSections::Live(PyList::new(py, sections)?.unbind()),
        longrepr: Some(longrepr.unwrap_or_else(|| py.None())),
        representation_changed: false,
        refresh_representation: false,
        xfail_reason: None,
        user_properties: ReportUserProperties::Empty,
        timing: PhaseTiming {
            start: 0.0,
            stop: duration,
            duration,
        },
        kind: ReportKind::Phase,
    };
    let report = if let Some(context) = context {
        fields.kind = ReportKind::Subtest(Some(context));
        Py::new(
            py,
            (super::super::subtest_report::NativeSubtestReport, fields),
        )?
        .bind(py)
        .as_super()
        .clone()
        .unbind()
    } else {
        Py::new(py, fields)?
    };
    if let Some(reason) = wasxfail {
        report.bind(py).setattr("wasxfail", reason)?;
    }
    if let Some(summary) = summary {
        let keywords = PyDict::new(py);
        for keyword in summary.keywords {
            keywords.set_item(keyword, 1)?;
        }
        report.bind(py).setattr("keywords", keywords)?;
        let message = summary
            .message
            .map(|message| crate::reporting::python_text_to_python(py, &message).map(Bound::unbind))
            .transpose()?
            .unwrap_or_else(|| py.None());
        report.bind(py).setattr("_vfy_summary_message", message)?;
        for (name, value) in [
            ("_hypothesis_stats", summary.hypothesis_statistics),
            (
                "_hypothesis_failing_examples",
                summary.hypothesis_failing_examples,
            ),
        ] {
            if let Some(value) = value {
                report
                    .bind(py)
                    .setattr(name, crate::reporting::python_text_to_python(py, &value)?)?;
            }
        }
    }
    Ok(report)
}

fn add_report(
    reporter: &Bound<'_, PyAny>,
    report: &Bound<'_, PyAny>,
    case: &CaseResult,
) -> PyResult<()> {
    let report_handle = report.cast::<NativeIndependentReport>()?;
    let test = case.test_id().ok_or_else(|| {
        pyo3::exceptions::PyValueError::new_err(
            "worker terminal case has no authenticated test identity",
        )
    })?;
    let history = reporter
        .getattr("_status_index")?
        .cast_into::<super::super::NativeSessionReports>()?;
    let generation = history.borrow().generation();
    report_handle.borrow_mut().origin =
        Some(super::super::origin::ReportOrigin::new(test, generation));
    // Projection creates this report immediately before registration, so it
    // cannot already occur in the controller's chronological history.
    super::super::NativeSessionReports::retain_report(&history, report_handle);
    // The terminal's logreport consumer records the stats and writes progress,
    // exactly as it does for a serial report.
    reporter.call_method1("pytest_runtest_logreport", (report,))?;
    Ok(())
}

fn add_subtests(
    reporter: &Bound<'_, PyAny>,
    case: &CaseResult,
    location: &ReportLocation,
    sections: &[(String, String)],
) -> PyResult<()> {
    let py = reporter.py();
    for subtest in case.subtests() {
        let Some(metadata) = &subtest.report else {
            continue;
        };
        let subphase = PhaseResult::new(
            subtest.outcome,
            ExecutionPhase::Call,
            metadata.duration_ms,
            vfy_core::test_execution::PhasePayload {
                exception: subtest.exception.clone(),
                detail: subtest.detail.clone(),
                skip_location: subtest.skip_location.clone(),
                terminal_lead: subtest.terminal_lead.clone(),
                report_sections: metadata.report_sections.clone(),
                report_summary: metadata.report_summary.clone(),
                stdout: metadata.stdout.clone(),
                stderr: metadata.stderr.clone(),
                log: metadata.log.clone(),
            },
        );
        let arguments = PyDict::new(py);
        arguments.set_item("msg", &metadata.context.msg)?;
        arguments.set_item("kwargs", PyDict::new(py))?;
        let context = py
            .get_type::<crate::independent::subtests::NativeSubtestContext>()
            .call((), Some(&arguments))?
            .extract::<Py<crate::independent::subtests::NativeSubtestContext>>()?;
        let parameters = context.bind(py).getattr("kwargs")?;
        for (key, value) in &metadata.context.kwargs {
            parameters.set_item(key, value)?;
        }
        let report = phase_report(
            py,
            &case.nodeid,
            location.clone(),
            subphase,
            sections.to_vec(),
            Some(context),
        )?;
        add_report(reporter, report.bind(py).as_any(), case)?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn result(
    result: &Bound<'_, NativeRunResult>,
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if reporter
        .getattr("config")?
        .getattr("option")?
        .getattr("workers")?
        .le(1)?
    {
        return Ok(());
    }
    let py = reporter.py();
    // Release the result borrow before user status/markup hooks can run.
    let (cases, collection_reports, locations) = {
        let result = result.borrow();
        let locations = (0..result.record().cases.len())
            .map(|index| result.report_location(index))
            .collect::<PyResult<Vec<_>>>()?;
        (
            result
                .record()
                .cases
                .iter()
                .map(CaseResult::clone)
                .collect::<Vec<_>>(),
            result.record().collection_reports.clone(),
            locations,
        )
    };
    for collected in &collection_reports {
        let category = match collected.outcome {
            CollectionOutcome::Skipped => "skipped",
            CollectionOutcome::Failed => "error",
            CollectionOutcome::Passed => continue,
        };
        let longrepr = if let (Some(location), Some(message)) = (
            &collected.payload.skip_location,
            &collected.payload.exception,
        ) {
            (
                location.path.as_str(),
                location.line,
                crate::reporting::python_text_to_python(py, message)?,
            )
                .into_pyobject(py)?
                .into_any()
        } else {
            collected
                .payload
                .detail
                .clone()
                .into_pyobject(py)?
                .into_any()
        };
        let report = py.get_type::<NativeIndependentCollectReport>().call1((
            collected.nodeid.as_str(),
            if collected.outcome == CollectionOutcome::Skipped {
                "skipped"
            } else {
                "failed"
            },
            longrepr,
            PyList::empty(py),
        ))?;
        if let Some(summary) = &collected.payload.report_summary {
            let message = summary
                .message
                .as_ref()
                .map(|message| {
                    crate::reporting::python_text_to_python(py, message).map(Bound::unbind)
                })
                .transpose()?
                .unwrap_or_else(|| py.None());
            report.setattr("_vfy_summary_message", message)?;
        }
        reporter.call_method1("_add_stats", (category, PyList::new(py, [report])?))?;
    }
    for (case, location) in cases.iter().zip(locations) {
        let mut sections = Vec::new();
        for phase in &case.phases {
            if phase.phase == ExecutionPhase::Call {
                add_subtests(reporter, case, &location, &sections)?;
            }
            append_phase_capture_sections(&mut sections, phase);
            let report = phase_report(
                py,
                &case.nodeid,
                location.clone(),
                phase.clone(),
                sections.clone(),
                None,
            )?;
            add_report(reporter, report.bind(py).as_any(), case)?;
        }
    }
    // The controller's run loop ended before the worker results were projected,
    // so it closes the progress line here.
    super::live::finish_loop(reporter)
}
