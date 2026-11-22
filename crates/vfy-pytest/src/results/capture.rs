//! Captured-stream sections and pytest phase reports projected from execution records.

use super::ReportLocation;
use crate::reporting;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use vfy_core::test_execution::{
    CaseResult, CollectionOutcome, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult,
    PythonText, RunRecord,
};

type CaptureSection = (String, String);
type TerminalCaptureReport = (
    String,
    ReportLocation,
    Option<String>,
    Option<String>,
    Vec<CaptureSection>,
    Vec<CaptureSection>,
);

/// Outcomes pytest's passing-capture summary renders (`-rP` and `-rX`).
#[derive(Clone, Copy, Default)]
pub(crate) struct CaptureSummary {
    passed: bool,
    xpassed: bool,
}

impl CaptureSummary {
    pub(crate) fn from_reportchars(reportchars: &str) -> Self {
        Self {
            passed: reportchars.contains('P'),
            xpassed: reportchars.contains('X'),
        }
    }

    pub(super) fn summary_call(self, case: &CaseResult) -> Option<&PhaseResult> {
        case.phases.iter().find(|phase| {
            phase.phase == ExecutionPhase::Call
                && match phase.outcome {
                    ExecutionOutcome::Passed => self.passed,
                    ExecutionOutcome::XPassed => self.xpassed,
                    _ => false,
                }
        })
    }

    pub(super) fn includes_subtests(self, case: &CaseResult) -> bool {
        self.xpassed
            && case
                .subtests()
                .iter()
                .any(|subtest| subtest.outcome == ExecutionOutcome::XPassed)
    }

    /// Whether the summary can render this case and therefore reads its location.
    pub(crate) fn includes(self, case: &CaseResult) -> bool {
        self.summary_call(case).is_some() || self.includes_subtests(case)
    }
}

// The upstream report API accepts text, a (path, line, message) skip location,
// or the terminal representation returned by `Item.repr_failure`.
pub(super) type PytestPhaseReport<'a> = (
    &'a str,
    &'static str,
    &'static str,
    f64,
    Option<Py<PyAny>>,
    Option<Py<PyAny>>,
    Vec<CaptureSection>,
    Option<Py<PyAny>>,
);

/// Append one phase's captured streams in pytest's section order.
pub(crate) fn append_phase_capture_sections(
    sections: &mut Vec<CaptureSection>,
    phase: &PhaseResult,
) {
    append_capture_sections(sections, phase.phase, phase);
}

/// Append real captured streams before constructing a phase report.
fn append_capture_sections(
    sections: &mut Vec<CaptureSection>,
    when: ExecutionPhase,
    payload: &PhasePayload,
) {
    for (stream, content) in [
        ("stdout", &payload.stdout),
        ("stderr", &payload.stderr),
        ("log", &payload.log),
    ] {
        if !content.is_empty() {
            sections.push((
                format!("Captured {stream} {}", when.as_str()),
                content.clone(),
            ));
        }
    }
}

/// Project one phase into pytest's report fields.
///
/// `representation` is the object `Item.repr_failure` returned for this phase.
/// Pytest reports carry that object, and its `str()` strips the leading blank
/// line and indentation that `toterminal` renders. Publishing the object keeps
/// live terminal and xdist output identical to pytest; the text in `phase`
/// remains the `str(longrepr)` form for records and reports.
pub(crate) fn pytest_phase_report<'a>(
    py: Python<'_>,
    nodeid: &'a str,
    phase: &PhaseResult,
    sections: Vec<CaptureSection>,
    representation: Option<&Py<PyAny>>,
) -> PyResult<PytestPhaseReport<'a>> {
    let longrepr = match representation {
        Some(representation) => Some(representation.clone_ref(py)),
        None => pytest_phase_longrepr(phase)
            .map(|value| value.into_python(py))
            .transpose()?,
    };
    Ok((
        nodeid,
        phase.phase.as_str(),
        phase.outcome.report_outcome().as_str(),
        phase.duration_ms / 1_000.0,
        longrepr,
        pytest_phase_xfail(phase)
            .map(|text| reporting::python_text_to_python(py, &text).map(Bound::unbind))
            .transpose()?,
        phase.report_sections.as_ref().map_or(sections, |sections| {
            sections
                .iter()
                .map(|section| (section.name.clone(), section.content.clone()))
                .collect()
        }),
        phase
            .exception
            .as_ref()
            .map(|text| reporting::python_text_to_python(py, text).map(Bound::unbind))
            .transpose()?,
    ))
}

#[derive(Debug, Eq, PartialEq)]
enum PytestLongRepr {
    Text(PythonText),
    Skipped(String, usize, PythonText),
}

impl PytestLongRepr {
    fn into_python(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::Text(text) => Ok(reporting::python_text_to_python(py, &text)?.unbind()),
            Self::Skipped(path, line, message) => {
                (path, line, reporting::python_text_to_python(py, &message)?).into_py_any(py)
            }
        }
    }
}

fn pytest_phase_longrepr(phase: &PhaseResult) -> Option<PytestLongRepr> {
    match phase.outcome {
        ExecutionOutcome::Passed | ExecutionOutcome::XPassed => None,
        ExecutionOutcome::Skipped => {
            let message = phase
                .exception
                .clone()
                .unwrap_or_else(|| phase.detail.clone().into());
            Some(phase.skip_location.as_ref().map_or_else(
                || PytestLongRepr::Text(message.clone()),
                |location| {
                    PytestLongRepr::Skipped(location.path.clone(), location.line, message.clone())
                },
            ))
        }
        ExecutionOutcome::Failed | ExecutionOutcome::Error | ExecutionOutcome::XFailed => {
            Some(PytestLongRepr::Text(if phase.detail.is_empty() {
                phase.exception.clone().unwrap_or_default()
            } else {
                phase.detail.clone().into()
            }))
        }
    }
}

pub(crate) fn pytest_phase_xfail(phase: &PhaseResult) -> Option<PythonText> {
    match phase.outcome {
        ExecutionOutcome::XFailed => {
            let reason = phase
                .exception
                .clone()
                .unwrap_or_else(|| phase.detail.clone().into());
            Some(reason.strip_prefix("reason: ").unwrap_or(reason))
        }
        ExecutionOutcome::XPassed => Some(
            phase
                .exception
                .clone()
                .unwrap_or_else(|| phase.detail.clone().into()),
        ),
        _ => None,
    }
}

type SubtestCaptureSections = (String, Vec<CaptureSection>, Vec<CaptureSection>);
pub(super) type TerminalCaptureProjection =
    (Vec<TerminalCaptureReport>, Vec<SubtestCaptureSections>);

pub(super) struct TerminalCaseSections {
    pub(super) setup: Vec<CaptureSection>,
    pub(super) call: Vec<CaptureSection>,
    pub(super) teardown: Vec<CaptureSection>,
}

impl TerminalCaseSections {
    pub(super) fn from_case(case: &CaseResult) -> Self {
        let mut sections = Self {
            setup: Vec::new(),
            call: Vec::new(),
            teardown: Vec::new(),
        };
        let streams = &[
            CaptureStream::Stdout,
            CaptureStream::Stderr,
            CaptureStream::Log,
        ];
        if !case.subtests().is_empty() {
            extend_capture_sections(&mut sections.setup, case, streams, CaptureScope::Subtest);
        }
        if let Some(report) = case
            .phases
            .iter()
            .rev()
            .find(|phase| phase.phase != ExecutionPhase::Teardown)
        {
            extend_capture_sections(
                &mut sections.call,
                case,
                streams,
                CaptureScope::Report(report),
            );
        }
        extend_capture_sections(
            &mut sections.teardown,
            case,
            streams,
            CaptureScope::PassedTeardown,
        );
        sections
    }
}

pub(super) enum CaptureStream {
    Stdout,
    Stderr,
    Log,
}

impl CaptureStream {
    fn name(&self) -> &'static str {
        match self {
            Self::Stdout => "stdout",
            Self::Stderr => "stderr",
            Self::Log => "log",
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum CaptureScope<'a> {
    /// A phase report carries the sections of its own and earlier phases.
    Report(&'a PhaseResult),
    /// Pytest prints a passed teardown's sections after each failure.
    PassedTeardown,
    /// A subtest report carries the setup sections.
    Subtest,
}

pub(super) enum FailureKind {
    Actual,
    Expected,
}

impl FailureKind {
    pub(super) fn includes(&self, outcome: ExecutionOutcome) -> bool {
        match self {
            Self::Actual => matches!(outcome, ExecutionOutcome::Failed | ExecutionOutcome::Error),
            Self::Expected => outcome == ExecutionOutcome::XFailed,
        }
    }
}

/// A terminal summary entry: headline, failure representation, and capture sections.
pub(super) type FailureEntry = (String, String, Vec<CaptureSection>);

fn phase_rank(phase: ExecutionPhase) -> u8 {
    match phase {
        ExecutionPhase::Setup => 0,
        ExecutionPhase::Call => 1,
        ExecutionPhase::Teardown => 2,
    }
}

/// Append the `Captured <stream> <phase>` sections that pytest's report carries.
pub(super) fn extend_capture_sections(
    sections: &mut Vec<CaptureSection>,
    case: &CaseResult,
    streams: &[CaptureStream],
    scope: CaptureScope<'_>,
) {
    let report = match scope {
        CaptureScope::Report(report) => Some(report),
        CaptureScope::PassedTeardown => case.phases.iter().find(|phase| {
            phase.phase == ExecutionPhase::Teardown && phase.outcome == ExecutionOutcome::Passed
        }),
        CaptureScope::Subtest => case
            .phases
            .iter()
            .find(|phase| phase.phase == ExecutionPhase::Setup),
    };
    if let Some(edited) = report.and_then(|report| report.report_sections.as_ref()) {
        for section in edited {
            // Pytest filters section titles by substring, including plugin sections.
            let shown = streams.len() == 3
                || streams
                    .iter()
                    .any(|stream| section.name.contains(stream.name()));
            if shown
                && (!matches!(scope, CaptureScope::PassedTeardown)
                    || section.name.contains("teardown"))
            {
                sections.push((section.name.clone(), section.content.clone()));
            }
        }
        return;
    }
    for captured in &case.phases {
        let include = match scope {
            CaptureScope::Report(report) => phase_rank(captured.phase) <= phase_rank(report.phase),
            CaptureScope::PassedTeardown => {
                captured.phase == ExecutionPhase::Teardown
                    && captured.outcome == ExecutionOutcome::Passed
            }
            CaptureScope::Subtest => captured.phase == ExecutionPhase::Setup,
        };
        if !include {
            continue;
        }
        for stream in streams {
            let (kind, content) = match stream {
                CaptureStream::Stdout => ("stdout", &captured.stdout),
                CaptureStream::Stderr => ("stderr", &captured.stderr),
                CaptureStream::Log => ("log", &captured.log),
            };
            if !content.is_empty() {
                sections.push((
                    format!("Captured {kind} {}", captured.phase.as_str()),
                    content.clone(),
                ));
            }
        }
    }
}

/// Return a failure representation as pytest's terminal writes it.
pub(super) fn terminal_detail(lead: &str, detail: &str) -> String {
    let mut text = format!("{lead}{detail}");
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text
}

/// Derive pytest's `head_line` for a case recorded without one.
pub(super) fn nodeid_head_line(nodeid: &str) -> String {
    nodeid
        .split_once("::")
        .map_or(nodeid, |(_, domain)| domain)
        .replace("::", ".")
}

pub(super) fn collection_failure_entries(
    record: &RunRecord,
    streams: &[CaptureStream],
) -> Vec<FailureEntry> {
    let mut errors = Vec::new();
    for report in &record.collection_reports {
        if report.outcome != CollectionOutcome::Failed {
            continue;
        }
        let mut sections = Vec::new();
        for stream in streams {
            let (name, content) = match stream {
                CaptureStream::Stdout => ("stdout", &report.payload.stdout),
                CaptureStream::Stderr => ("stderr", &report.payload.stderr),
                CaptureStream::Log => ("log", &report.payload.log),
            };
            if !content.is_empty() {
                sections.push((format!("Captured {name} collect"), content.clone()));
            }
        }
        errors.push((
            format!("ERROR collecting {}", report.nodeid),
            terminal_detail(&report.payload.terminal_lead, &report.payload.detail),
            sections,
        ));
    }
    errors
}

pub(super) fn internal_failure_entries(
    record: &RunRecord,
    streams: &[CaptureStream],
) -> Vec<FailureEntry> {
    record
        .internal_errors
        .iter()
        .map(|error| {
            let sections = streams
                .iter()
                .filter_map(|stream| {
                    let (name, content) = match stream {
                        CaptureStream::Stdout => ("stdout", &error.stdout),
                        CaptureStream::Stderr => ("stderr", &error.stderr),
                        CaptureStream::Log => ("log", &error.log),
                    };
                    (!content.is_empty())
                        .then(|| (format!("Captured {name} internal error"), content.clone()))
                })
                .collect();
            (
                "INTERNALERROR".to_owned(),
                terminal_detail(&error.terminal_lead, &error.detail),
                sections,
            )
        })
        .collect()
}
