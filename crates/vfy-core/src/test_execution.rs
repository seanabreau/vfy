//! Interpreter-independent outcomes produced by the native test engine.

mod case_identity;
pub use case_identity::case_for_test;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::fmt;
use std::ops::Deref;
use std::sync::Arc;

mod python_text;
use crate::selection::TestOutcome;
pub use python_text::{NonScalarText, PythonText};

mod observations;
pub use observations::{
    CaseMarkerObservation, ConfigurationObservation, ExecutionTopology, PluginObservation,
    RunObservations,
};

/// Lifecycle phase represented by an execution report.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ExecutionPhase {
    Setup,
    Call,
    Teardown,
}

impl ExecutionPhase {
    /// Return the pytest-compatible phase spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Setup => "setup",
            Self::Call => "call",
            Self::Teardown => "teardown",
        }
    }
}

/// Native outcome, retaining expected-failure and infrastructure distinctions.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ExecutionOutcome {
    Passed,
    Failed,
    Error,
    Skipped,
    XFailed,
    XPassed,
}

impl ExecutionOutcome {
    /// Return the public outcome spelling.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Error => "error",
            Self::Skipped => "skipped",
            Self::XFailed => "xfailed",
            Self::XPassed => "xpassed",
        }
    }

    /// Map this outcome to the pass/fail outcome used by selection and reports.
    #[must_use]
    pub const fn report_outcome(self) -> TestOutcome {
        match self {
            Self::Passed | Self::XPassed => TestOutcome::Passed,
            Self::Failed | Self::Error => TestOutcome::Failed,
            Self::Skipped | Self::XFailed => TestOutcome::Skipped,
        }
    }
}

/// Location used for a skipped phase's diagnostic.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SkipLocation {
    pub path: String,
    pub line: usize,
}

/// Captured output and diagnostics for an execution phase.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct PhasePayload {
    pub exception: Option<PythonText>,
    pub stdout: String,
    pub stderr: String,
    pub log: String,
    pub detail: String,
    pub skip_location: Option<SkipLocation>,
    /// Leading whitespace that pytest's terminal summary prints before
    /// `detail`, which holds the stripped `str(report.longrepr)`.
    pub terminal_lead: String,
    /// Report sections after report hooks have run. `None` means they were not
    /// recorded; an empty list records an explicit removal.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub report_sections: Option<Vec<ReportSection>>,
    /// Literal short-summary inputs retained across worker transport.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub report_summary: Option<ReportSummary>,
}

/// Hook-visible inputs that are distinct from the full failure diagnostic.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSummary {
    pub keywords: Vec<String>,
    pub message: Option<PythonText>,
    /// Literal statistics supplied by the admitted Hypothesis report hook.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hypothesis_statistics: Option<PythonText>,
    /// Opaque JSON text produced by Hypothesis for its own patch consumer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hypothesis_failing_examples: Option<PythonText>,
}

/// One ordered report section, including sections supplied by a plug-in.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReportSection {
    pub name: String,
    pub content: String,
}

impl PhasePayload {
    fn is_empty(&self) -> bool {
        self.exception.is_none()
            && self.stdout.is_empty()
            && self.stderr.is_empty()
            && self.log.is_empty()
            && self.detail.is_empty()
            && self.skip_location.is_none()
            && self.terminal_lead.is_empty()
            && self.report_sections.as_ref().is_none_or(Vec::is_empty)
            && self.report_summary.is_none()
    }
}

fn is_python_whitespace(character: char) -> bool {
    character.is_whitespace() || ('\x1c'..='\x1f').contains(&character)
}

/// Split text into its leading whitespace and the result of Python's `str.strip()`.
#[must_use]
pub fn split_python_strip(text: &str) -> (&str, &str) {
    let body = text.trim_start_matches(is_python_whitespace);
    (
        &text[..text.len() - body.len()],
        body.trim_end_matches(is_python_whitespace),
    )
}

const fn empty_phase_payload(report_sections: Option<Vec<ReportSection>>) -> PhasePayload {
    PhasePayload {
        exception: None,
        stdout: String::new(),
        stderr: String::new(),
        log: String::new(),
        detail: String::new(),
        skip_location: None,
        terminal_lead: String::new(),
        report_sections,
        report_summary: None,
    }
}

static EMPTY_PHASE_PAYLOAD: PhasePayload = empty_phase_payload(None);
static EMPTY_SECTIONS_PHASE_PAYLOAD: PhasePayload = empty_phase_payload(Some(Vec::new()));

#[derive(Clone, Copy, Debug)]
enum EmptyPhasePayload {
    Legacy,
    Sections,
}

impl EmptyPhasePayload {
    fn from_sections(present: bool) -> Self {
        if present {
            Self::Sections
        } else {
            Self::Legacy
        }
    }

    fn value(self) -> &'static PhasePayload {
        match self {
            Self::Legacy => &EMPTY_PHASE_PAYLOAD,
            Self::Sections => &EMPTY_SECTIONS_PHASE_PAYLOAD,
        }
    }
}

/// One setup, call, or teardown outcome and its retained output.
#[derive(Clone, Debug)]
pub struct PhaseResult {
    pub outcome: ExecutionOutcome,
    pub phase: ExecutionPhase,
    pub duration_ms: f64,
    // Empty-state semantics share the scalar fields' padding instead of widening the pointer.
    empty_payload: EmptyPhasePayload,
    payload: Option<Arc<PhasePayload>>,
}

impl PhaseResult {
    /// Retain output only when the phase has capture or diagnostics.
    #[must_use]
    pub fn new(
        outcome: ExecutionOutcome,
        phase: ExecutionPhase,
        duration_ms: f64,
        payload: PhasePayload,
    ) -> Self {
        Self {
            outcome,
            phase,
            duration_ms,
            empty_payload: EmptyPhasePayload::from_sections(payload.report_sections.is_some()),
            payload: (!payload.is_empty()).then(|| Arc::new(payload)),
        }
    }

    /// Set the phase's exception.
    pub fn set_exception(&mut self, exception: Option<String>) {
        if exception.is_some() || self.payload.is_some() {
            self.payload_mut().exception = exception.map(PythonText::from);
            self.compact_payload();
        }
    }

    /// Set the captured log.
    pub fn set_log(&mut self, log: String) {
        if !log.is_empty() || self.payload.is_some() {
            self.payload_mut().log = log;
            self.compact_payload();
        }
    }

    /// Replace the report sections recorded after report hooks have run.
    pub fn set_report_sections(&mut self, sections: Option<Vec<ReportSection>>) {
        if self.report_sections == sections {
            return;
        }
        if self.payload.is_none() && sections.as_ref().is_none_or(Vec::is_empty) {
            self.empty_payload = EmptyPhasePayload::from_sections(sections.is_some());
            return;
        }
        self.payload_mut().report_sections = sections;
        self.compact_payload();
    }

    /// Replace the current hook-visible short-summary inputs.
    pub fn set_report_summary(&mut self, summary: Option<ReportSummary>) {
        if self.report_summary == summary {
            return;
        }
        self.payload_mut().report_summary = summary;
        self.compact_payload();
    }

    /// Set the whitespace the terminal summary prints before the detail.
    pub fn set_terminal_lead(&mut self, lead: String) {
        if !lead.is_empty() || self.payload.is_some() {
            self.payload_mut().terminal_lead = lead;
            self.compact_payload();
        }
    }

    /// Set the skip diagnostic location.
    pub fn set_skip_location(&mut self, location: Option<SkipLocation>) {
        if location.is_some() || self.payload.is_some() {
            self.payload_mut().skip_location = location;
            self.compact_payload();
        }
    }

    /// Replace captured output.
    pub fn set_capture(&mut self, stdout: String, stderr: String, log: String) {
        if !stdout.is_empty() || !stderr.is_empty() || !log.is_empty() || self.payload.is_some() {
            let payload = self.payload_mut();
            payload.stdout = stdout;
            payload.stderr = stderr;
            payload.log = log;
            self.compact_payload();
        }
    }

    /// Append captured output.
    pub fn append_capture(&mut self, stdout: &str, stderr: &str, log: &str) {
        if !stdout.is_empty() || !stderr.is_empty() || !log.is_empty() {
            let payload = self.payload_mut();
            payload.stdout.push_str(stdout);
            payload.stderr.push_str(stderr);
            payload.log.push_str(log);
        }
    }

    /// Append a diagnostic.
    pub fn append_detail(&mut self, detail: &str) {
        if !detail.is_empty() {
            self.payload_mut().detail.push_str(detail);
        }
    }

    /// Drop capture without removing diagnostics.
    pub fn clear_capture(&mut self) {
        if let Some(payload) = &mut self.payload {
            let payload = Arc::make_mut(payload);
            payload.stdout = String::new();
            payload.stderr = String::new();
            payload.log = String::new();
            self.compact_payload();
        }
    }

    fn payload_mut(&mut self) -> &mut PhasePayload {
        Arc::make_mut(
            self.payload
                .get_or_insert_with(|| Arc::new(self.empty_payload.value().clone())),
        )
    }

    fn compact_payload(&mut self) {
        if let Some(payload) = &self.payload
            && payload.is_empty()
        {
            self.empty_payload =
                EmptyPhasePayload::from_sections(payload.report_sections.is_some());
            self.payload = None;
        }
    }
}

impl Deref for PhaseResult {
    type Target = PhasePayload;

    fn deref(&self) -> &Self::Target {
        self.payload
            .as_deref()
            .unwrap_or_else(|| self.empty_payload.value())
    }
}

#[derive(Serialize)]
#[serde(rename = "PhaseResult")]
struct PhaseResultView<'a> {
    outcome: ExecutionOutcome,
    phase: ExecutionPhase,
    duration_ms: f64,
    exception: &'a Option<PythonText>,
    stdout: &'a str,
    stderr: &'a str,
    log: &'a str,
    detail: &'a str,
    skip_location: &'a Option<SkipLocation>,
    #[serde(skip_serializing_if = "str_is_empty")]
    terminal_lead: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    report_sections: &'a Option<Vec<ReportSection>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    report_summary: &'a Option<ReportSummary>,
}

impl Serialize for PhaseResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        PhaseResultView {
            outcome: self.outcome,
            phase: self.phase,
            duration_ms: self.duration_ms,
            exception: &self.exception,
            stdout: &self.stdout,
            stderr: &self.stderr,
            log: &self.log,
            detail: &self.detail,
            skip_location: &self.skip_location,
            terminal_lead: &self.terminal_lead,
            report_sections: &self.report_sections,
            report_summary: &self.report_summary,
        }
        .serialize(serializer)
    }
}

#[derive(Deserialize)]
#[serde(rename = "PhaseResult")]
struct RawPhaseResult {
    outcome: ExecutionOutcome,
    phase: ExecutionPhase,
    duration_ms: f64,
    exception: Option<PythonText>,
    stdout: String,
    stderr: String,
    log: String,
    detail: String,
    skip_location: Option<SkipLocation>,
    #[serde(default)]
    terminal_lead: String,
    #[serde(default)]
    report_sections: Option<Vec<ReportSection>>,
    #[serde(default)]
    report_summary: Option<ReportSummary>,
}

impl<'de> Deserialize<'de> for PhaseResult {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawPhaseResult::deserialize(deserializer)?;
        Ok(Self::new(
            raw.outcome,
            raw.phase,
            raw.duration_ms,
            PhasePayload {
                exception: raw.exception,
                stdout: raw.stdout,
                stderr: raw.stderr,
                log: raw.log,
                detail: raw.detail,
                skip_location: raw.skip_location,
                terminal_lead: raw.terminal_lead,
                report_sections: raw.report_sections,
                report_summary: raw.report_summary,
            },
        ))
    }
}

/// Structured context retained alongside the rendered subtest description.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SubtestContext {
    pub msg: Option<String>,
    pub kwargs: Vec<(String, String)>,
}

/// Additional hook-visible fields for builtin subtest reports.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SubtestReportMetadata {
    pub context: SubtestContext,
    pub duration_ms: f64,
    pub start: f64,
    pub stop: f64,
    pub stdout: String,
    pub stderr: String,
    pub log: String,
    pub report_sections: Option<Vec<ReportSection>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub report_summary: Option<ReportSummary>,
}

/// A subtest outcome within a containing test's lifecycle.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SubtestResult {
    /// Absent on unittest reports and on records written before builtin subtests.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub report: Option<Box<SubtestReportMetadata>>,
    pub outcome: ExecutionOutcome,
    pub context: String,
    pub exception: Option<PythonText>,
    pub detail: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub skip_location: Option<SkipLocation>,
    /// Leading whitespace that pytest's terminal summary prints before `detail`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub terminal_lead: String,
}

/// Chronological references into one case's report arrays.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", content = "index", rename_all = "lowercase")]
pub enum CaseReportEvent {
    Phase(usize),
    Subtest(usize),
}

#[derive(Clone, Debug, Default)]
enum CaseEventStorage {
    #[default]
    Absent,
    ImplicitPhases(usize),
    Explicit(Box<[CaseReportEvent]>),
}

/// A borrowed chronological view over explicit or implicit case events.
#[derive(Clone, Debug)]
pub struct CaseReportEvents<'a> {
    kind: CaseReportEventsKind<'a>,
}

#[derive(Clone, Debug)]
enum CaseReportEventsKind<'a> {
    Implicit(std::ops::Range<usize>),
    Explicit(std::slice::Iter<'a, CaseReportEvent>),
}

impl CaseReportEvents<'_> {
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[must_use]
    pub fn get(&self, index: usize) -> Option<CaseReportEvent> {
        self.clone().nth(index)
    }
}

impl Iterator for CaseReportEvents<'_> {
    type Item = CaseReportEvent;

    fn next(&mut self) -> Option<Self::Item> {
        match &mut self.kind {
            CaseReportEventsKind::Implicit(indices) => indices.next().map(CaseReportEvent::Phase),
            CaseReportEventsKind::Explicit(events) => events.next().copied(),
        }
    }

    fn nth(&mut self, index: usize) -> Option<Self::Item> {
        match &mut self.kind {
            CaseReportEventsKind::Implicit(indices) => {
                indices.nth(index).map(CaseReportEvent::Phase)
            }
            CaseReportEventsKind::Explicit(events) => events.nth(index).copied(),
        }
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        match &self.kind {
            CaseReportEventsKind::Implicit(indices) => indices.size_hint(),
            CaseReportEventsKind::Explicit(events) => events.size_hint(),
        }
    }
}

impl ExactSizeIterator for CaseReportEvents<'_> {}

impl Serialize for CaseReportEvents<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_seq(self.clone())
    }
}

/// Inline storage for ordinary setup, call, and teardown phases.
pub type CasePhases = smallvec::SmallVec<[PhaseResult; 3]>;

/// Completed phases for a test, including any subtest outcomes.
#[derive(Clone, Debug)]
pub struct CaseResult {
    pub nodeid: String,
    pub duration_ms: f64,
    pub phases: CasePhases,
    test_id: Option<crate::test_plan::TestId>,
    head_line: Option<Box<str>>,
    report_events: CaseEventStorage,
    auxiliary: Option<Box<CaseAuxiliary>>,
}

#[derive(Clone, Debug, Default)]
struct CaseAuxiliary {
    subtests: Box<[SubtestResult]>,
    user_properties: Box<[(String, String)]>,
    call_failure_properties: Option<Box<[(String, String)]>>,
    teardown_testcase_unwritten: bool,
    xml_attributes: Box<[(String, String)]>,
}

impl CaseAuxiliary {
    fn is_empty(&self) -> bool {
        self.subtests.is_empty()
            && self.user_properties.is_empty()
            && self.call_failure_properties.is_none()
            && !self.teardown_testcase_unwritten
            && self.xml_attributes.is_empty()
    }
}

impl CaseResult {
    #[must_use]
    pub fn new(
        nodeid: String,
        duration_ms: f64,
        phases: impl IntoIterator<Item = PhaseResult>,
    ) -> Self {
        Self {
            nodeid,
            duration_ms,
            phases: phases.into_iter().collect(),
            test_id: None,
            head_line: None,
            report_events: CaseEventStorage::Absent,
            auxiliary: None,
        }
    }

    /// Return pytest's report headline, recorded for failed cases.
    #[must_use]
    pub fn head_line(&self) -> Option<&str> {
        self.head_line.as_deref()
    }

    pub fn set_head_line(&mut self, head_line: Option<String>) {
        self.head_line = head_line.map(String::into_boxed_str);
    }

    #[must_use]
    pub fn subtests(&self) -> &[SubtestResult] {
        self.auxiliary
            .as_ref()
            .map_or(&[], |aux| aux.subtests.as_ref())
    }

    #[must_use]
    pub fn user_properties(&self) -> &[(String, String)] {
        self.auxiliary
            .as_ref()
            .map_or(&[], |aux| aux.user_properties.as_ref())
    }

    /// Properties of the call-failure testcase pytest writes before a teardown error's.
    #[must_use]
    pub fn call_failure_properties(&self) -> Option<&[(String, String)]> {
        self.auxiliary
            .as_ref()
            .and_then(|aux| aux.call_failure_properties.as_deref())
    }

    pub fn set_call_failure_properties(&mut self, properties: Option<Box<[(String, String)]>>) {
        if properties.is_some() || self.auxiliary.is_some() {
            self.auxiliary_mut().call_failure_properties = properties;
            self.compact_auxiliary();
        }
    }

    /// Whether `JUnit` omits the teardown-error testcase, as pytest does when
    /// converting the call failure's properties raised before it was opened.
    #[must_use]
    pub fn teardown_testcase_unwritten(&self) -> bool {
        self.auxiliary
            .as_ref()
            .is_some_and(|aux| aux.teardown_testcase_unwritten)
    }

    pub fn set_teardown_testcase_unwritten(&mut self, unwritten: bool) {
        if unwritten || self.auxiliary.is_some() {
            self.auxiliary_mut().teardown_testcase_unwritten = unwritten;
            self.compact_auxiliary();
        }
    }

    #[must_use]
    pub fn xml_attributes(&self) -> &[(String, String)] {
        self.auxiliary
            .as_ref()
            .map_or(&[], |aux| aux.xml_attributes.as_ref())
    }

    #[must_use]
    pub fn report_events(&self) -> CaseReportEvents<'_> {
        let kind = match &self.report_events {
            CaseEventStorage::Absent => CaseReportEventsKind::Implicit(0..0),
            CaseEventStorage::ImplicitPhases(count) => CaseReportEventsKind::Implicit(0..*count),
            CaseEventStorage::Explicit(events) => CaseReportEventsKind::Explicit(events.iter()),
        };
        CaseReportEvents { kind }
    }

    /// Take the current report events without disturbing other case metadata.
    #[must_use]
    pub fn take_report_events(&mut self) -> Box<[CaseReportEvent]> {
        match std::mem::take(&mut self.report_events) {
            CaseEventStorage::Absent => Box::default(),
            CaseEventStorage::ImplicitPhases(count) => (0..count)
                .map(CaseReportEvent::Phase)
                .collect::<Vec<_>>()
                .into_boxed_slice(),
            CaseEventStorage::Explicit(events) => events,
        }
    }

    pub fn set_report_events(&mut self, events: Box<[CaseReportEvent]>) {
        self.report_events = if events.is_empty() {
            CaseEventStorage::Absent
        } else if events
            .iter()
            .enumerate()
            .all(|(index, event)| *event == CaseReportEvent::Phase(index))
        {
            CaseEventStorage::ImplicitPhases(events.len())
        } else {
            CaseEventStorage::Explicit(events)
        };
    }

    /// Record phase order without allocating an event array.
    pub fn set_implicit_phase_events(&mut self, count: usize) {
        self.report_events = if count == 0 {
            CaseEventStorage::Absent
        } else {
            CaseEventStorage::ImplicitPhases(count)
        };
    }

    pub fn set_subtests(&mut self, subtests: Box<[SubtestResult]>) {
        if !subtests.is_empty() || self.auxiliary.is_some() {
            self.auxiliary_mut().subtests = subtests;
            self.compact_auxiliary();
        }
    }

    pub fn set_user_properties(&mut self, properties: Box<[(String, String)]>) {
        if !properties.is_empty() || self.auxiliary.is_some() {
            self.auxiliary_mut().user_properties = properties;
            self.compact_auxiliary();
        }
    }

    pub fn set_xml_attributes(&mut self, attributes: Box<[(String, String)]>) {
        if !attributes.is_empty() || self.auxiliary.is_some() {
            self.auxiliary_mut().xml_attributes = attributes;
            self.compact_auxiliary();
        }
    }

    pub fn append_xml_attributes(&mut self, attributes: Vec<(String, String)>) {
        if !attributes.is_empty() {
            let auxiliary = self.auxiliary_mut();
            let mut combined = std::mem::take(&mut auxiliary.xml_attributes).into_vec();
            combined.extend(attributes);
            auxiliary.xml_attributes = combined.into_boxed_slice();
        }
    }

    fn auxiliary_mut(&mut self) -> &mut CaseAuxiliary {
        self.auxiliary.get_or_insert_with(Box::default)
    }

    fn compact_auxiliary(&mut self) {
        if self.auxiliary.as_ref().is_some_and(|aux| aux.is_empty()) {
            self.auxiliary = None;
        }
    }

    /// Count the reports that contribute to pytest's failure budget.
    #[must_use]
    pub fn failure_count(&self) -> usize {
        self.phases
            .iter()
            .filter(|phase| {
                matches!(
                    phase.outcome,
                    ExecutionOutcome::Failed | ExecutionOutcome::Error
                )
            })
            .count()
            + self
                .subtests()
                .iter()
                .filter(|subtest| subtest.outcome == ExecutionOutcome::Failed)
                .count()
    }

    /// Include failing subtests when projecting a call to one report outcome.
    #[must_use]
    pub fn report_phase_outcome(&self, phase: &PhaseResult) -> ExecutionOutcome {
        if phase.phase == ExecutionPhase::Call
            && matches!(
                phase.outcome,
                ExecutionOutcome::Passed | ExecutionOutcome::XPassed
            )
            && self
                .subtests()
                .iter()
                .filter(|subtest| subtest.report.is_none())
                .any(|subtest| {
                    matches!(
                        subtest.outcome,
                        ExecutionOutcome::Failed | ExecutionOutcome::Error
                    )
                })
        {
            ExecutionOutcome::Failed
        } else {
            phase.outcome
        }
    }
}

#[derive(Serialize)]
#[serde(rename = "CaseResult")]
struct CaseResultView<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    test_index: Option<usize>,
    nodeid: &'a str,
    duration_ms: f64,
    phases: &'a [PhaseResult],
    #[serde(skip_serializing_if = "Option::is_none")]
    head_line: Option<&'a str>,
    subtests: &'a [SubtestResult],
    #[serde(skip_serializing_if = "CaseReportEvents::is_empty")]
    report_events: CaseReportEvents<'a>,
    #[serde(skip_serializing_if = "slice_is_empty")]
    user_properties: &'a [(String, String)],
    #[serde(skip_serializing_if = "Option::is_none")]
    call_failure_properties: Option<&'a [(String, String)]>,
    #[serde(skip_serializing_if = "is_false")]
    teardown_testcase_unwritten: bool,
    #[serde(skip_serializing_if = "slice_is_empty")]
    xml_attributes: &'a [(String, String)],
}

impl Serialize for CaseResult {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        CaseResultView {
            test_index: self.test_id().map(crate::test_plan::TestId::index),
            nodeid: &self.nodeid,
            duration_ms: self.duration_ms,
            phases: &self.phases,
            head_line: self.head_line(),
            subtests: self.subtests(),
            report_events: self.report_events(),
            user_properties: self.user_properties(),
            call_failure_properties: self.call_failure_properties(),
            teardown_testcase_unwritten: self.teardown_testcase_unwritten(),
            xml_attributes: self.xml_attributes(),
        }
        .serialize(serializer)
    }
}

#[derive(Deserialize)]
#[serde(rename = "CaseResult")]
struct RawCaseResult {
    #[serde(default)]
    test_index: Option<usize>,
    nodeid: String,
    duration_ms: f64,
    #[serde(deserialize_with = "deserialize_case_phases")]
    phases: CasePhases,
    #[serde(default)]
    head_line: Option<String>,
    subtests: Box<[SubtestResult]>,
    #[serde(default)]
    report_events: Box<[CaseReportEvent]>,
    #[serde(default)]
    user_properties: Box<[(String, String)]>,
    #[serde(default)]
    call_failure_properties: Option<Box<[(String, String)]>>,
    #[serde(default)]
    teardown_testcase_unwritten: bool,
    #[serde(default)]
    xml_attributes: Box<[(String, String)]>,
}

fn deserialize_case_phases<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<CasePhases, D::Error> {
    struct PhasesVisitor;
    impl<'de> serde::de::Visitor<'de> for PhasesVisitor {
        type Value = CasePhases;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a sequence")
        }

        fn visit_seq<A: serde::de::SeqAccess<'de>>(
            self,
            mut sequence: A,
        ) -> Result<Self::Value, A::Error> {
            let mut phases = CasePhases::new();
            while let Some(phase) = sequence.next_element()? {
                phases.push(phase);
            }
            Ok(phases)
        }
    }
    deserializer.deserialize_seq(PhasesVisitor)
}

impl<'de> Deserialize<'de> for CaseResult {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawCaseResult::deserialize(deserializer)?;
        let mut result = Self::new(raw.nodeid, raw.duration_ms, raw.phases);
        if let Some(index) = raw.test_index {
            result.set_test_id(crate::test_plan::TestId::from_index(index));
        }
        result.set_head_line(raw.head_line);
        result.set_subtests(raw.subtests);
        result.set_report_events(raw.report_events);
        if !valid_case_events(&result) {
            return Err(serde::de::Error::custom(
                "case report events must reference every report exactly once",
            ));
        }
        result.set_user_properties(raw.user_properties);
        result.set_call_failure_properties(raw.call_failure_properties);
        result.set_teardown_testcase_unwritten(raw.teardown_testcase_unwritten);
        result.set_xml_attributes(raw.xml_attributes);
        Ok(result)
    }
}

fn valid_case_events(case: &CaseResult) -> bool {
    if let CaseEventStorage::ImplicitPhases(count) = &case.report_events {
        return *count == case.phases.len() && case.subtests().is_empty();
    }
    if case.report_events().is_empty() {
        return case
            .subtests()
            .iter()
            .all(|subtest| subtest.report.is_none());
    }
    let mut phases = vec![false; case.phases.len()];
    let mut subtests = vec![false; case.subtests().len()];
    for event in case.report_events() {
        let slot = match event {
            CaseReportEvent::Phase(index) => phases.get_mut(index),
            CaseReportEvent::Subtest(index) => subtests.get_mut(index),
        };
        let Some(slot) = slot else {
            return false;
        };
        if *slot {
            return false;
        }
        *slot = true;
    }
    !phases.contains(&false) && !subtests.contains(&false)
}

#[expect(
    clippy::trivially_copy_pass_by_ref,
    reason = "serde's skip_serializing_if passes the field by reference"
)]
fn is_false(value: &bool) -> bool {
    !value
}

fn str_is_empty(value: &&str) -> bool {
    value.is_empty()
}

fn slice_is_empty<T>(values: &[T]) -> bool {
    values.is_empty()
}

/// A warning observed during collection or execution.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct WarningRecord {
    pub message: String,
    pub nodeid: String,
    pub filename: String,
    pub lineno: usize,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub category: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw_message: Option<String>,
}

/// Aggregates matching pytest's phase and subtest counting semantics.
#[derive(Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct Counts {
    pub passed: usize,
    pub failed: usize,
    pub errors: usize,
    pub skipped: usize,
    pub xfailed: usize,
    pub xpassed: usize,
    pub subtests_passed: usize,
}

/// Trusted identity binding an execution record to its outer session.
#[derive(Debug, Deserialize, Serialize)]
pub struct ExecutionRecordIdentity {
    pub producer_pid: u32,
    pub session_token: String,
    pub observer_nonce: String,
    pub session_depth: u8,
    pub rootdir: String,
}

/// Result of importing or collecting one module or collector.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CollectionOutcome {
    Passed,
    Failed,
    Skipped,
}

/// Collection output, kept separate from executable test cases.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CollectionReport {
    pub nodeid: String,
    pub outcome: CollectionOutcome,
    pub duration_ms: f64,
    #[serde(flatten)]
    pub payload: PhasePayload,
}

/// Test run consumed by reports and persistence.
#[derive(Debug, Deserialize)]
pub struct RunRecord {
    /// Session failures outside collection and executable test phases.
    #[serde(default)]
    pub internal_errors: Vec<PhasePayload>,
    #[serde(default)]
    pub exitstatus: Option<i32>,
    #[serde(flatten)]
    pub observations: Option<RunObservations>,
    pub engine: String,
    #[serde(flatten)]
    pub execution_identity: Option<ExecutionRecordIdentity>,
    pub collected: usize,
    pub counts: Counts,
    pub duration_ms: f64,
    pub warnings: Vec<WarningRecord>,
    pub cases: Vec<CaseResult>,
    #[serde(default)]
    pub collection_reports: Vec<CollectionReport>,
    #[serde(default)]
    pub deselected_nodeids: Vec<String>,
}

impl Serialize for RunRecord {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        #[derive(Serialize)]
        struct WireRecord<'a> {
            #[serde(skip_serializing_if = "<[PhasePayload]>::is_empty")]
            internal_errors: &'a [PhasePayload],
            #[serde(skip_serializing_if = "Option::is_none")]
            exitstatus: Option<i32>,
            #[serde(flatten)]
            observations: &'a Option<RunObservations>,
            engine: &'a str,
            #[serde(flatten)]
            execution_identity: &'a Option<ExecutionRecordIdentity>,
            collected: usize,
            counts: &'a Counts,
            duration_ms: f64,
            warnings: &'a [WarningRecord],
            cases: &'a [CaseResult],
            #[serde(skip_serializing_if = "Option::is_none")]
            collection_reports: Option<&'a [CollectionReport]>,
            #[serde(skip_serializing_if = "Option::is_none")]
            deselected_nodeids: Option<&'a [String]>,
        }

        let independent = self.engine == "vfy-independent-v1";
        WireRecord {
            internal_errors: &self.internal_errors,
            exitstatus: self.exitstatus,
            observations: &self.observations,
            engine: &self.engine,
            execution_identity: &self.execution_identity,
            collected: self.collected,
            counts: &self.counts,
            duration_ms: self.duration_ms,
            warnings: &self.warnings,
            cases: &self.cases,
            collection_reports: (independent || !self.collection_reports.is_empty())
                .then_some(self.collection_reports.as_slice()),
            deselected_nodeids: (independent || !self.deselected_nodeids.is_empty())
                .then_some(self.deselected_nodeids.as_slice()),
        }
        .serialize(serializer)
    }
}

/// Why native run records could not be combined as file-group results.
#[derive(Debug, Eq, PartialEq)]
pub enum RunRecordMergeError {
    EmptyInput,
    DifferentEngines,
    Observations,
    ExecutionIdentity,
    CountMismatch,
    CollectedCountTooSmall,
    InvalidDuration,
    InvalidCasePhases(String),
    InvalidCaseReports(String),
    CollectedCountOverflow,
}

impl fmt::Display for RunRecordMergeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyInput => formatter.write_str("cannot merge an empty set of run records"),
            Self::Observations => formatter.write_str("run record observations are inconsistent"),
            Self::DifferentEngines => formatter.write_str("run records use different engines"),
            Self::ExecutionIdentity => {
                formatter.write_str("run records with outer execution identities cannot be merged")
            }
            Self::CountMismatch => formatter.write_str("run record counts do not match its cases"),
            Self::CollectedCountTooSmall => {
                formatter.write_str("run record collected count is smaller than its case count")
            }
            Self::InvalidDuration => {
                formatter.write_str("run record duration must be finite and nonnegative")
            }
            Self::InvalidCasePhases(nodeid) => {
                write!(
                    formatter,
                    "run record has invalid phase sequence for case {nodeid:?}"
                )
            }
            Self::InvalidCaseReports(nodeid) => write!(
                formatter,
                "run record has invalid report references for case {nodeid:?}"
            ),
            Self::CollectedCountOverflow => {
                formatter.write_str("merged collected count overflowed")
            }
        }
    }
}

impl std::error::Error for RunRecordMergeError {}

impl RunRecord {
    /// Return the first failure in case and phase order, ignoring expected failures.
    #[must_use]
    pub fn first_failure(&self) -> Option<(&str, ExecutionPhase)> {
        self.cases.iter().find_map(|case| {
            case.phases.iter().find_map(|phase| {
                let actual_failure = matches!(
                    case.report_phase_outcome(phase),
                    ExecutionOutcome::Failed | ExecutionOutcome::Error
                );
                if actual_failure {
                    return Some((case.nodeid.as_str(), phase.phase));
                }
                None
            })
        })
    }
}

/// Validate an independent native record before aggregation.
///
/// # Errors
/// Returns an error for outer execution identities or inconsistent counts,
/// collected counts, phase sequences, or durations.
pub fn validate_run_record(record: &RunRecord) -> Result<(), RunRecordMergeError> {
    if record.execution_identity.is_some() {
        return Err(RunRecordMergeError::ExecutionIdentity);
    }
    if record.counts != compute_run_counts(&record.cases, &record.collection_reports) {
        return Err(RunRecordMergeError::CountMismatch);
    }
    if record.collected < record.cases.len() {
        return Err(RunRecordMergeError::CollectedCountTooSmall);
    }
    if let Some(case) = record.cases.iter().find(|case| !valid_case_phases(case)) {
        return Err(RunRecordMergeError::InvalidCasePhases(case.nodeid.clone()));
    }
    if let Some(case) = record.cases.iter().find(|case| !valid_case_events(case)) {
        return Err(RunRecordMergeError::InvalidCaseReports(case.nodeid.clone()));
    }
    if !record.duration_ms.is_finite()
        || record.duration_ms < 0.0
        || record
            .collection_reports
            .iter()
            .any(|report| !report.duration_ms.is_finite() || report.duration_ms < 0.0)
        || record.cases.iter().any(|case| {
            !case.duration_ms.is_finite()
                || case.duration_ms < 0.0
                || case
                    .phases
                    .iter()
                    .any(|phase| !phase.duration_ms.is_finite() || phase.duration_ms < 0.0)
                || case
                    .subtests()
                    .iter()
                    .filter_map(|subtest| subtest.report.as_deref())
                    .any(|report| {
                        !report.duration_ms.is_finite()
                            || report.duration_ms < 0.0
                            || !report.start.is_finite()
                            || !report.stop.is_finite()
                    })
        })
    {
        return Err(RunRecordMergeError::InvalidDuration);
    }
    Ok(())
}

/// Validate and combine ordered, independent native file-group run records.
///
/// Preserve case, warning, collection-report, and deselection order, and sum
/// collected counts and durations. Derive outcome totals from cases and
/// collection reports so callers cannot supply conflicting totals.
///
/// # Errors
///
/// Returns an error when the input is empty, records have different engines or
/// outer execution identities, or a record has inconsistent counts, durations,
/// phase sequences, or collected counts.
pub fn merge_run_records(records: Vec<RunRecord>) -> Result<RunRecord, RunRecordMergeError> {
    let Some(first) = records.first() else {
        return Err(RunRecordMergeError::EmptyInput);
    };
    let engine = first.engine.clone();
    let observed = first.observations.is_some();
    let mut exitstatus = first.exitstatus;
    let mut observations: Option<RunObservations> = None;
    let mut collected = 0usize;
    let mut duration_ms = 0.0;
    let mut warnings = Vec::new();
    let mut cases = Vec::new();
    let mut internal_errors = Vec::new();
    let mut collection_reports = Vec::new();
    let mut deselected_nodeids = Vec::new();

    for record in records {
        if record.engine != engine {
            return Err(RunRecordMergeError::DifferentEngines);
        }
        validate_run_record(&record)?;
        if exitstatus.is_none_or(|code| code == 0) {
            exitstatus = record.exitstatus.or(exitstatus);
        }
        if record.observations.is_some() != observed {
            return Err(RunRecordMergeError::Observations);
        }
        if let Some(current) = record.observations {
            if let Some(merged) = &mut observations {
                merged.merge(current)?;
            } else {
                observations = Some(current);
            }
        }
        collected = collected
            .checked_add(record.collected)
            .ok_or(RunRecordMergeError::CollectedCountOverflow)?;
        duration_ms += record.duration_ms;
        if !duration_ms.is_finite() {
            return Err(RunRecordMergeError::InvalidDuration);
        }
        warnings.extend(record.warnings);
        cases.extend(record.cases);
        internal_errors.extend(record.internal_errors);
        collection_reports.extend(record.collection_reports);
        deselected_nodeids.extend(record.deselected_nodeids);
    }

    let counts = compute_run_counts(&cases, &collection_reports);
    Ok(RunRecord {
        internal_errors,
        exitstatus,
        observations,
        engine,
        execution_identity: None,
        collected,
        counts,
        duration_ms,
        warnings,
        cases,
        collection_reports,
        deselected_nodeids,
    })
}

/// Count test phases and collection outcomes without inventing executable cases.
#[must_use]
pub fn compute_run_counts(cases: &[CaseResult], collection_reports: &[CollectionReport]) -> Counts {
    let mut counts = compute_counts(cases);
    for report in collection_reports {
        match report.outcome {
            CollectionOutcome::Passed => {}
            CollectionOutcome::Failed => counts.errors += 1,
            CollectionOutcome::Skipped => counts.skipped += 1,
        }
    }
    counts
}

fn valid_case_phases(case: &CaseResult) -> bool {
    let builtin_subtests = !case.subtests().is_empty()
        && case
            .subtests()
            .iter()
            .all(|subtest| subtest.report.is_some());
    let mut phases = case.phases.iter();
    let Some(setup) = phases.next() else {
        return builtin_subtests;
    };
    if setup.phase != ExecutionPhase::Setup {
        return false;
    }
    let Some(next) = phases.next() else {
        return case.subtests().is_empty() || builtin_subtests;
    };
    match next.phase {
        ExecutionPhase::Call => {
            if let Some(teardown) = phases.next()
                && teardown.phase != ExecutionPhase::Teardown
            {
                return false;
            }
            phases.next().is_none()
        }
        ExecutionPhase::Teardown => {
            phases.next().is_none() && (case.subtests().is_empty() || builtin_subtests)
        }
        ExecutionPhase::Setup => false,
    }
}

/// Count native reports without materializing Python reports or JSON.
#[must_use]
pub fn compute_counts(cases: &[CaseResult]) -> Counts {
    let mut counts = Counts::default();
    for case in cases {
        for phase in &case.phases {
            match phase.outcome {
                ExecutionOutcome::Passed => {
                    counts.passed += usize::from(phase.phase == ExecutionPhase::Call);
                }
                ExecutionOutcome::Failed => {
                    counts.failed += usize::from(phase.phase == ExecutionPhase::Call);
                }
                ExecutionOutcome::Error => {
                    counts.errors += usize::from(phase.phase != ExecutionPhase::Call);
                }
                ExecutionOutcome::Skipped => counts.skipped += 1,
                ExecutionOutcome::XFailed => counts.xfailed += 1,
                ExecutionOutcome::XPassed => counts.xpassed += 1,
            }
        }
        for subtest in case.subtests() {
            match subtest.outcome {
                ExecutionOutcome::Passed => counts.subtests_passed += 1,
                ExecutionOutcome::Failed => counts.failed += 1,
                ExecutionOutcome::Skipped => counts.skipped += 1,
                ExecutionOutcome::XFailed => counts.xfailed += 1,
                ExecutionOutcome::XPassed => counts.xpassed += 1,
                ExecutionOutcome::Error => {}
            }
        }
    }
    counts
}

#[cfg(test)]
mod tests;

#[cfg(test)]
#[path = "test_execution/builtin_subtests_tests.rs"]
mod builtin_subtests_tests;

#[cfg(test)]
#[path = "test_execution/report_summary_tests.rs"]
mod report_summary_tests;
