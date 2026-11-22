use std::borrow::Cow;
use std::collections::BTreeMap;
use std::io::{BufWriter, Write};
use std::path::Path;

use serde::de::{MapAccess, SeqAccess, Visitor, value::MapAccessDeserializer};
use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Deserialize, Serialize};
use serde_json::{Number, Value};

use crate::CoreError;

mod history;
#[cfg(test)]
mod tests;

use history::{CompactEntry, CompactObservation, History};

const MAX_OBSERVATIONS: usize = 20;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum FailedPhase {
    Setup,
    Call,
    Teardown,
}

impl FailedPhase {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Setup => "setup",
            Self::Call => "call",
            Self::Teardown => "teardown",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum TestOutcome {
    Passed,
    Failed,
    Skipped,
}

impl TestOutcome {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TestAttempt {
    Primary,
    Confirmation,
}

impl TestAttempt {
    /// Parse the public run-attempt name.
    ///
    /// # Errors
    /// Returns an error for an unsupported attempt.
    pub fn parse(value: &str) -> Result<Self, CoreError> {
        match value {
            "primary" => Ok(Self::Primary),
            "confirmation" => Ok(Self::Confirmation),
            _ => Err(CoreError::InvalidInput(
                "invalid test run attempt".to_owned(),
            )),
        }
    }

    const fn as_str(self) -> &'static str {
        match self {
            Self::Primary => "primary",
            Self::Confirmation => "confirmation",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TestReport {
    pub nodeid: String,
    pub outcome: TestOutcome,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failed_phase: Option<FailedPhase>,
    pub duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub call_duration_ms: Option<f64>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TestRun {
    pub infrastructure_failed: bool,
    pub tests: Vec<TestReport>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecordedTestRun {
    pub failed: Vec<String>,
    pub outcomes: BTreeMap<String, TestOutcome>,
    pub infrastructure_failed: bool,
}

impl RecordedTestRun {
    fn project(run: &TestRun, projection: OutcomeProjection) -> Self {
        Self {
            failed: run
                .tests
                .iter()
                .filter(|test| test.outcome == TestOutcome::Failed)
                .map(|test| test.nodeid.clone())
                .collect(),
            outcomes: if matches!(projection, OutcomeProjection::All) {
                run.tests
                    .iter()
                    .map(|test| (test.nodeid.clone(), test.outcome))
                    .collect()
            } else {
                BTreeMap::new()
            },
            infrastructure_failed: run.infrastructure_failed,
        }
    }
}

#[derive(Clone, Copy, Default)]
struct State {
    first_failed_phase: Option<FailedPhase>,
    passed_call: bool,
    skipped: bool,
    total_duration_ms: f64,
    call_duration_ms: Option<f64>,
}

impl State {
    fn record(&mut self, phase: FailedPhase, outcome: TestOutcome, duration_ms: f64) {
        self.total_duration_ms += duration_ms;
        if phase == FailedPhase::Call {
            self.call_duration_ms = Some(duration_ms);
            self.passed_call |= outcome == TestOutcome::Passed;
        }
        if outcome == TestOutcome::Failed && self.first_failed_phase.is_none() {
            self.first_failed_phase = Some(phase);
        }
        self.skipped |= outcome == TestOutcome::Skipped;
    }

    fn into_report(self, nodeid: String) -> Option<TestReport> {
        let outcome = if self.first_failed_phase.is_some() {
            TestOutcome::Failed
        } else if self.passed_call {
            TestOutcome::Passed
        } else if self.skipped {
            TestOutcome::Skipped
        } else {
            return None;
        };
        Some(TestReport {
            nodeid,
            outcome,
            failed_phase: self.first_failed_phase,
            duration_ms: self.total_duration_ms,
            call_duration_ms: self.call_duration_ms,
        })
    }
}

/// Incrementally normalize phase reports while preserving deferred publication errors.
#[derive(Default)]
pub struct TestReportAccumulator {
    states: BTreeMap<String, State>,
    invalid: Option<String>,
}

impl TestReportAccumulator {
    /// Retain one test state per node ID; invalid input prevents any later publication.
    pub fn append(
        &mut self,
        nodeid: &str,
        phase_value: &str,
        outcome_value: &str,
        duration_ms: f64,
    ) {
        if self.invalid.is_some() {
            return;
        }
        let validated = (|| {
            validate_nodeid(nodeid)?;
            let phase = phase(phase_value)?;
            let outcome = outcome(outcome_value)?;
            validate_duration(duration_ms)?;
            Ok::<_, CoreError>((phase, outcome))
        })();
        match validated {
            Ok((phase, outcome)) => {
                if let Some(state) = self.states.get_mut(nodeid) {
                    state.record(phase, outcome, duration_ms);
                } else {
                    self.states.entry(nodeid.to_owned()).or_default().record(
                        phase,
                        outcome,
                        duration_ms,
                    );
                }
            }
            Err(error) => self.invalid = Some(error.to_string()),
        }
    }

    /// Atomically publish the same ordered result as the batch phase writer.
    ///
    /// # Errors
    /// Returns the first deferred input error or an atomic publication error.
    pub fn write(&self, path: &Path, infrastructure_failed: bool) -> Result<(), CoreError> {
        if let Some(error) = &self.invalid {
            return Err(CoreError::InvalidInput(error.clone()));
        }
        let run = TestRun {
            infrastructure_failed,
            tests: self
                .states
                .iter()
                .filter_map(|(nodeid, state)| state.into_report(nodeid.clone()))
                .collect(),
        };
        write_test_run_result(path, &run)
    }
}

fn validate_duration(duration_ms: f64) -> Result<(), CoreError> {
    if !duration_ms.is_finite() || duration_ms < 0.0 {
        return Err(CoreError::InvalidInput(
            "test result duration must be a finite non-negative number".to_owned(),
        ));
    }
    Ok(())
}

fn phase(value: &str) -> Result<FailedPhase, CoreError> {
    match value {
        "setup" => Ok(FailedPhase::Setup),
        "call" => Ok(FailedPhase::Call),
        "teardown" => Ok(FailedPhase::Teardown),
        _ => Err(CoreError::InvalidInput(format!(
            "test result has invalid phase {value:?}"
        ))),
    }
}

fn outcome(value: &str) -> Result<TestOutcome, CoreError> {
    match value {
        "passed" => Ok(TestOutcome::Passed),
        "failed" => Ok(TestOutcome::Failed),
        "skipped" => Ok(TestOutcome::Skipped),
        _ => Err(CoreError::InvalidInput(format!(
            "test result has invalid outcome {value:?}"
        ))),
    }
}

/// Combine pytest phase reports into one record per test, ordered by node ID.
///
/// # Errors
/// Returns an error when any record is invalid; no artifact is written.
pub fn normalize_test_results<I>(
    reports: I,
    infrastructure_failed: bool,
) -> Result<TestRun, CoreError>
where
    I: IntoIterator<Item = (String, String, String, f64)>,
{
    let reports = reports
        .into_iter()
        .map(|(nodeid, phase_value, outcome_value, duration_ms)| {
            validate_nodeid(&nodeid)?;
            Ok((
                Cow::Owned(nodeid),
                phase(&phase_value)?,
                outcome(&outcome_value)?,
                duration_ms,
            ))
        });
    normalize_reports(reports, infrastructure_failed)
}

fn validate_nodeid(nodeid: &str) -> Result<(), CoreError> {
    if nodeid.is_empty() {
        return Err(CoreError::InvalidInput(
            "test result nodeid cannot be empty".to_owned(),
        ));
    }
    Ok(())
}

fn normalize_reports<'a, I>(reports: I, infrastructure_failed: bool) -> Result<TestRun, CoreError>
where
    I: IntoIterator<Item = Result<(Cow<'a, str>, FailedPhase, TestOutcome, f64), CoreError>>,
{
    let mut states = BTreeMap::<Cow<'a, str>, State>::new();
    for report in reports {
        let (nodeid, phase, outcome, duration_ms) = report?;
        validate_nodeid(&nodeid)?;
        validate_duration(duration_ms)?;
        let state = states.entry(nodeid).or_default();
        state.record(phase, outcome, duration_ms);
    }
    let tests = states
        .into_iter()
        .filter_map(|(nodeid, state)| state.into_report(nodeid.into_owned()))
        .collect();
    Ok(TestRun {
        infrastructure_failed,
        tests,
    })
}

fn write_json<T: Serialize>(
    path: &Path,
    value: &T,
    operation: &'static str,
) -> Result<(), CoreError> {
    let io_error = |source| CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation,
        source,
    };
    crate::output::atomic_write_with(
        path,
        |file| {
            let mut writer = BufWriter::new(file);
            serde_json::to_writer_pretty(&mut writer, value).map_err(|error| {
                if error.is_io() {
                    io_error(error.into())
                } else {
                    CoreError::Serialization(error)
                }
            })?;
            writer.flush().map_err(io_error)
        },
        io_error,
    )
}

#[derive(Serialize)]
struct OrderedTestReport<'a> {
    #[serde(skip_serializing_if = "Option::is_none")]
    call_duration_ms: Option<f64>,
    duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    failed_phase: Option<FailedPhase>,
    nodeid: &'a str,
    outcome: TestOutcome,
}

struct OrderedTestReports<'a>(&'a [TestReport]);

impl Serialize for OrderedTestReports<'_> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        let mut sequence = serializer.serialize_seq(Some(self.0.len()))?;
        for report in self.0 {
            sequence.serialize_element(&OrderedTestReport {
                call_duration_ms: report.call_duration_ms,
                duration_ms: report.duration_ms,
                failed_phase: report.failed_phase,
                nodeid: &report.nodeid,
                outcome: report.outcome,
            })?;
        }
        sequence.end()
    }
}

#[derive(Serialize)]
struct OrderedTestRun<'a> {
    infrastructure_failed: bool,
    tests: OrderedTestReports<'a>,
}

/// Atomically write a normalized test-run artifact.
///
/// # Errors
/// Returns an error when serialization or replacement fails.
pub fn write_test_run_result(path: &Path, run: &TestRun) -> Result<(), CoreError> {
    write_json(
        path,
        &OrderedTestRun {
            infrastructure_failed: run.infrastructure_failed,
            tests: OrderedTestReports(&run.tests),
        },
        "write test result",
    )
}

/// Normalize borrowed, typed pytest phase reports.
///
/// # Errors
/// Returns an error when any report is invalid.
pub fn normalize_test_phases<'a, I>(
    reports: I,
    infrastructure_failed: bool,
) -> Result<TestRun, CoreError>
where
    I: IntoIterator<Item = (&'a str, FailedPhase, TestOutcome, f64)>,
{
    let reports = reports
        .into_iter()
        .map(|(nodeid, phase, outcome, duration)| {
            Ok((Cow::Borrowed(nodeid), phase, outcome, duration))
        });
    normalize_reports(reports, infrastructure_failed)
}

/// Validate and atomically write borrowed, typed pytest phase reports.
///
/// # Errors
/// Returns an error without replacing the destination when a report is invalid.
pub fn write_test_phases<'a, I>(
    path: &Path,
    reports: I,
    infrastructure_failed: bool,
) -> Result<(), CoreError>
where
    I: IntoIterator<Item = (&'a str, FailedPhase, TestOutcome, f64)>,
{
    let run = normalize_test_phases(reports, infrastructure_failed)?;
    write_test_run_result(path, &run)
}

/// Validate and atomically write a batch of pytest phase reports.
///
/// # Errors
/// Returns an error without replacing the destination when any report is invalid.
pub fn write_test_reports<I>(
    path: &Path,
    reports: I,
    infrastructure_failed: bool,
) -> Result<(), CoreError>
where
    I: IntoIterator<Item = (String, String, String, f64)>,
{
    let run = normalize_test_results(reports, infrastructure_failed)?;
    write_test_run_result(path, &run)
}

/// Read and validate a normalized test-run artifact.
///
/// # Errors
/// Returns an error for I/O failures or malformed fields.
pub fn read_test_run(path: &Path) -> Result<TestRun, CoreError> {
    let text = std::fs::read_to_string(path).map_err(|source| CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation: "read test result",
        source,
    })?;
    // Keep the complete UTF-8 read ahead of parsing so I/O and decoding failures
    // retain precedence over malformed JSON and invalid report fields.
    if let Ok(run) = serde_json::from_str::<ParsedTestRun>(&text) {
        return Ok(run.0);
    }
    parse_test_run(&text)
}

fn parse_test_run(text: &str) -> Result<TestRun, CoreError> {
    let value: Value = serde_json::from_str(text).map_err(|error| {
        CoreError::InvalidInput(format!("invalid test result artifact: {error}"))
    })?;
    let root = value.as_object().ok_or_else(|| {
        CoreError::InvalidInput("test result artifact must be an object".to_owned())
    })?;
    let infrastructure_failed = root
        .get("infrastructure_failed")
        .and_then(Value::as_bool)
        .ok_or_else(|| {
            CoreError::InvalidInput(
                "test result artifact has invalid infrastructure status".to_owned(),
            )
        })?;
    let entries = root.get("tests").and_then(Value::as_array).ok_or_else(|| {
        CoreError::InvalidInput("test result artifact has invalid tests".to_owned())
    })?;
    let mut tests = Vec::with_capacity(entries.len());
    for value in entries {
        tests.push(parse_test_report(value)?);
    }
    Ok(TestRun {
        infrastructure_failed,
        tests,
    })
}

fn parse_test_report(value: &Value) -> Result<TestReport, CoreError> {
    let item = value
        .as_object()
        .ok_or_else(|| CoreError::InvalidInput("test result entry must be an object".to_owned()))?;
    let nodeid = item
        .get("nodeid")
        .and_then(Value::as_str)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| CoreError::InvalidInput("test result entry has invalid nodeid".to_owned()))?
        .to_owned();
    let outcome = item
        .get("outcome")
        .and_then(Value::as_str)
        .ok_or_else(|| CoreError::InvalidInput("test result entry has invalid outcome".to_owned()))
        .and_then(outcome)?;
    let failed_phase = item
        .get("failed_phase")
        .map(|v| {
            v.as_str()
                .ok_or_else(|| {
                    CoreError::InvalidInput("test result entry has invalid phase".to_owned())
                })
                .and_then(phase)
        })
        .transpose()?;
    if outcome == TestOutcome::Failed && failed_phase.is_none() {
        return Err(CoreError::InvalidInput(
            "failed test result entry requires a phase".to_owned(),
        ));
    }
    let duration_ms = item
        .get("duration_ms")
        .and_then(Value::as_f64)
        .filter(|v| v.is_finite() && *v >= 0.0)
        .ok_or_else(|| {
            CoreError::InvalidInput("test result entry has invalid duration".to_owned())
        })?;
    let call_duration_ms = item
        .get("call_duration_ms")
        .map(|v| {
            v.as_f64()
                .filter(|n| n.is_finite() && *n >= 0.0)
                .ok_or_else(|| {
                    CoreError::InvalidInput(
                        "test result entry has invalid call duration".to_owned(),
                    )
                })
        })
        .transpose()?;
    Ok(TestReport {
        nodeid,
        outcome,
        failed_phase,
        duration_ms,
        call_duration_ms,
    })
}

// Unsupported shapes, duplicate root keys, and any parse or validation failure
// use the canonical parser so its last-key-wins behavior and diagnostics remain
// authoritative. The ordinary writer's shape is decoded without intermediate JSON maps.
struct ParsedTestRun(TestRun);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TestRunFields {
    infrastructure_failed: bool,
    #[serde(deserialize_with = "deserialize_test_reports")]
    tests: Vec<TestReport>,
}

impl<'de> Deserialize<'de> for ParsedTestRun {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct RunVisitor;

        impl<'de> Visitor<'de> for RunVisitor {
            type Value = ParsedTestRun;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a test result object")
            }

            fn visit_map<A>(self, map: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let fields = TestRunFields::deserialize(MapAccessDeserializer::new(map))?;
                Ok(ParsedTestRun(TestRun {
                    infrastructure_failed: fields.infrastructure_failed,
                    tests: fields.tests,
                }))
            }
        }

        // Derived struct deserialization also accepts arrays. Require an object
        // here, matching the canonical artifact parser.
        deserializer.deserialize_map(RunVisitor)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TestReportFields {
    nodeid: String,
    outcome: TestOutcome,
    #[serde(default, deserialize_with = "present_option")]
    failed_phase: Option<FailedPhase>,
    duration_ms: Number,
    #[serde(default, deserialize_with = "present_option")]
    call_duration_ms: Option<Number>,
}

// Missing fields are allowed; an explicit null follows the canonical error path.
fn present_option<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de>,
{
    T::deserialize(deserializer).map(Some)
}

impl TestReportFields {
    fn into_report(self) -> Option<TestReport> {
        let duration_ms = self.duration_ms.as_f64()?;
        let call_duration_ms = match self.call_duration_ms {
            Some(value) => Some(value.as_f64()?),
            None => None,
        };
        if self.nodeid.is_empty()
            || (self.outcome == TestOutcome::Failed && self.failed_phase.is_none())
            || !duration_ms.is_finite()
            || duration_ms < 0.0
            || call_duration_ms.is_some_and(|value| !value.is_finite() || value < 0.0)
        {
            return None;
        }
        Some(TestReport {
            nodeid: self.nodeid,
            outcome: self.outcome,
            failed_phase: self.failed_phase,
            duration_ms,
            call_duration_ms,
        })
    }
}

struct ParsedTestReport(TestReport);

impl<'de> Deserialize<'de> for ParsedTestReport {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct ReportVisitor;

        impl<'de> Visitor<'de> for ReportVisitor {
            type Value = ParsedTestReport;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a test result object")
            }

            fn visit_map<A>(self, map: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                TestReportFields::deserialize(MapAccessDeserializer::new(map))?
                    .into_report()
                    .map(ParsedTestReport)
                    .ok_or_else(|| serde::de::Error::custom("invalid test result entry"))
            }
        }

        deserializer.deserialize_map(ReportVisitor)
    }
}

fn deserialize_test_reports<'de, D>(deserializer: D) -> Result<Vec<TestReport>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    struct ReportsVisitor;

    impl<'de> Visitor<'de> for ReportsVisitor {
        type Value = Vec<TestReport>;

        fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            formatter.write_str("an array of test results")
        }

        fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
        where
            A: SeqAccess<'de>,
        {
            let mut tests = Vec::new();
            while let Some(value) = sequence.next_element::<ParsedTestReport>()? {
                tests.push(value.0);
            }
            Ok(tests)
        }
    }

    deserializer.deserialize_seq(ReportsVisitor)
}

struct HistoryEntry<'a> {
    previous: Option<HistoryPreviousEntry<'a>>,
    reports: Vec<&'a TestReport>,
    duration_ms: Option<f64>,
}

impl<'a> HistoryEntry<'a> {
    fn new(previous: Option<HistoryPreviousEntry<'a>>) -> Self {
        Self {
            previous,
            reports: Vec::new(),
            duration_ms: None,
        }
    }
}

#[derive(Clone, Copy)]
enum HistoryPreviousEntry<'a> {
    Compact(&'a CompactEntry),
    Canonical(&'a Value),
}

impl Serialize for HistoryPreviousEntry<'_> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        match self {
            Self::Compact(entry) => entry.serialize(serializer),
            Self::Canonical(entry) => entry.serialize(serializer),
        }
    }
}

impl<'a> HistoryPreviousEntry<'a> {
    fn observations(self) -> HistoryPreviousObservations<'a> {
        match self {
            Self::Compact(entry) => HistoryPreviousObservations::Compact(
                entry.observations.as_deref().unwrap_or_default(),
            ),
            Self::Canonical(entry) => HistoryPreviousObservations::Canonical(
                entry
                    .get("observations")
                    .and_then(Value::as_array)
                    .map_or(&[][..], Vec::as_slice),
            ),
        }
    }
}

struct HistoryTests<'a> {
    entries: BTreeMap<&'a str, HistoryEntry<'a>>,
    run_id: &'a str,
    attempt: TestAttempt,
}

struct HistoryEntryView<'a, 'b> {
    entry: &'b HistoryEntry<'a>,
    run_id: &'a str,
    attempt: TestAttempt,
}

#[derive(Serialize)]
struct HistoryObservation<'a> {
    attempt: &'static str,
    duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    failed_phase: Option<FailedPhase>,
    outcome: TestOutcome,
    run_id: &'a str,
}

struct HistoryObservations<'a, 'b> {
    previous: HistoryPreviousObservations<'b>,
    reports: &'b [&'a TestReport],
    run_id: &'a str,
    attempt: TestAttempt,
}

#[derive(Clone, Copy)]
enum HistoryPreviousObservations<'a> {
    Compact(&'a [CompactObservation]),
    Canonical(&'a [Value]),
}

impl HistoryPreviousObservations<'_> {
    fn len(self) -> usize {
        match self {
            Self::Compact(observations) => observations.len(),
            Self::Canonical(observations) => observations.len(),
        }
    }
}

impl Serialize for HistoryObservations<'_, '_> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        let length = self.previous.len().saturating_add(self.reports.len());
        let skip = length.saturating_sub(MAX_OBSERVATIONS);
        let mut sequence = serializer.serialize_seq(Some(length.min(MAX_OBSERVATIONS)))?;
        match self.previous {
            HistoryPreviousObservations::Compact(observations) => {
                for observation in observations.iter().skip(skip) {
                    sequence.serialize_element(observation)?;
                }
            }
            HistoryPreviousObservations::Canonical(observations) => {
                for observation in observations.iter().skip(skip) {
                    sequence.serialize_element(observation)?;
                }
            }
        }
        for report in self
            .reports
            .iter()
            .skip(skip.saturating_sub(self.previous.len()))
        {
            sequence.serialize_element(&HistoryObservation {
                attempt: self.attempt.as_str(),
                duration_ms: report.duration_ms,
                failed_phase: report.failed_phase,
                outcome: report.outcome,
                run_id: self.run_id,
            })?;
        }
        sequence.end()
    }
}

#[derive(Serialize)]
#[serde(untagged)]
enum HistoryEntryField<'a, 'b> {
    Duration(f64),
    Number(&'b Number),
    Text(&'b str),
    PreviousObservations(&'b [CompactObservation]),
    Observations(HistoryObservations<'a, 'b>),
}

fn serialize_history_fields<'a, 'b, S, I, const N: usize>(
    serializer: S,
    previous: I,
    updates: [Option<(&str, HistoryEntryField<'a, 'b>)>; N],
) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
    I: Iterator<Item = (&'b str, &'b Value)> + Clone,
{
    let length = previous.clone().count()
        + updates
            .iter()
            .flatten()
            .filter(|(key, _)| {
                !previous
                    .clone()
                    .any(|(previous_key, _)| previous_key == *key)
            })
            .count();
    let mut map = serializer.serialize_map(Some(length))?;
    let mut fields = previous.peekable();
    // Merge sorted fields directly without allocating a map for each entry.
    for (key, value) in updates.into_iter().flatten() {
        while let Some((previous_key, previous_value)) =
            fields.next_if(|(previous_key, _)| *previous_key < key)
        {
            map.serialize_entry(previous_key, previous_value)?;
        }
        fields.next_if(|(previous_key, _)| *previous_key == key);
        map.serialize_entry(key, &value)?;
    }
    for (key, value) in fields {
        map.serialize_entry(key, value)?;
    }
    map.end()
}

impl Serialize for HistoryEntryView<'_, '_> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        if self.entry.reports.is_empty() {
            return self.entry.previous.serialize(serializer);
        }
        let observations = self.entry.previous.map_or(
            HistoryPreviousObservations::Canonical(&[]),
            HistoryPreviousEntry::observations,
        );
        let duration = self
            .entry
            .duration_ms
            .map(HistoryEntryField::Duration)
            .or_else(|| match self.entry.previous {
                Some(HistoryPreviousEntry::Compact(entry)) => {
                    entry.duration_ms.as_ref().map(HistoryEntryField::Number)
                }
                Some(HistoryPreviousEntry::Canonical(_)) | None => None,
            });
        let updates = [
            duration.map(|value| ("duration_ms", value)),
            Some((
                "observations",
                HistoryEntryField::Observations(HistoryObservations {
                    previous: observations,
                    reports: &self.entry.reports,
                    run_id: self.run_id,
                    attempt: self.attempt,
                }),
            )),
        ];
        match self.entry.previous {
            Some(HistoryPreviousEntry::Compact(entry)) => serialize_history_fields(
                serializer,
                entry
                    .custom
                    .iter()
                    .map(|(key, value)| (key.as_str(), value)),
                updates,
            ),
            Some(HistoryPreviousEntry::Canonical(entry)) => serialize_history_fields(
                serializer,
                entry
                    .as_object()
                    .into_iter()
                    .flatten()
                    .map(|(key, value)| (key.as_str(), value)),
                updates,
            ),
            None => serialize_history_fields(serializer, std::iter::empty(), updates),
        }
    }
}

impl Serialize for HistoryTests<'_> {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        let mut map = serializer.serialize_map(Some(self.entries.len()))?;
        for (nodeid, entry) in &self.entries {
            map.serialize_entry(
                nodeid,
                &HistoryEntryView {
                    entry,
                    run_id: self.run_id,
                    attempt: self.attempt,
                },
            )?;
        }
        map.end()
    }
}

#[derive(Serialize)]
#[serde(untagged)]
enum HistoryRootField<'a, 'b> {
    Previous(&'b Value),
    Tests(&'b HistoryTests<'a>),
}

fn history_root_fields<'a, 'b, I>(fields: I) -> BTreeMap<&'a str, HistoryRootField<'a, 'b>>
where
    'a: 'b,
    I: Iterator<Item = (&'a String, &'a Value)>,
{
    fields
        .filter(|(key, _)| key.as_str() != "version" && key.as_str() != "tests")
        .map(|(key, value)| (key.as_str(), HistoryRootField::Previous(value)))
        .collect()
}

/// Merge one validated run into bounded history and atomically replace it.
///
/// # Errors
/// Returns an error for invalid history shape or a failed replacement.
pub fn record_test_run(
    history_path: &Path,
    run: &TestRun,
    run_id: &str,
    attempt: TestAttempt,
) -> Result<RecordedTestRun, CoreError> {
    record_test_run_projected(history_path, run, run_id, attempt, OutcomeProjection::All)
}

#[derive(Clone, Copy)]
enum OutcomeProjection {
    All,
    FailuresOnly,
}

fn sort_history_objects(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            // Retained histories are already sorted. Sorting their maps again
            // rebuilds an IndexMap hash table for every past observation.
            if !fields.keys().is_sorted() {
                fields.sort_keys();
            }
            fields.values_mut().for_each(sort_history_objects);
        }
        Value::Array(values) => values.iter_mut().for_each(sort_history_objects),
        _ => {}
    }
}

fn record_test_run_projected(
    history_path: &Path,
    run: &TestRun,
    run_id: &str,
    attempt: TestAttempt,
    projection: OutcomeProjection,
) -> Result<RecordedTestRun, CoreError> {
    if run_id.is_empty() {
        return Err(CoreError::InvalidInput(
            "test run id cannot be empty".to_owned(),
        ));
    }
    if super::sqlite_history::uses_sqlite(history_path) {
        super::sqlite_history::record_run(history_path, run, run_id, attempt)?;
        return Ok(RecordedTestRun::project(run, projection));
    }
    let history = history::read_history(history_path);
    let (mut fields, entries) = match &history {
        History::Compact(history) => (
            history_root_fields(history.custom.iter()),
            history
                .tests
                .iter()
                .map(|(nodeid, entry)| {
                    (
                        nodeid.as_str(),
                        HistoryEntry::new(Some(HistoryPreviousEntry::Compact(entry))),
                    )
                })
                .collect(),
        ),
        History::Canonical(root) => {
            let previous_tests = root.get("tests");
            if previous_tests.is_some_and(|tests| !tests.is_object()) {
                return Err(CoreError::InvalidInput(
                    "test history has an invalid tests object".to_owned(),
                ));
            }
            let entries = previous_tests
                .and_then(Value::as_object)
                .into_iter()
                .flatten()
                .map(|(nodeid, entry)| {
                    (
                        nodeid.as_str(),
                        HistoryEntry::new(Some(HistoryPreviousEntry::Canonical(entry))),
                    )
                })
                .collect();
            (history_root_fields(root.iter()), entries)
        }
    };
    let mut tests = HistoryTests {
        entries,
        run_id,
        attempt,
    };
    let mut failed = Vec::new();
    let mut outcomes = BTreeMap::new();
    for test in &run.tests {
        if matches!(projection, OutcomeProjection::All) {
            outcomes.insert(test.nodeid.clone(), test.outcome);
        }
        if test.outcome == TestOutcome::Failed {
            failed.push(test.nodeid.clone());
        }
        let entry = tests
            .entries
            .entry(&test.nodeid)
            .or_insert_with(|| HistoryEntry::new(None));
        if let Some(HistoryPreviousEntry::Canonical(previous)) = entry.previous {
            if !previous.is_object() {
                return Err(CoreError::InvalidInput(format!(
                    "test history entry for {:?} must be an object",
                    test.nodeid
                )));
            }
            if previous
                .get("observations")
                .is_some_and(|observations| !observations.is_array())
            {
                return Err(CoreError::InvalidInput(format!(
                    "test history observations for {:?} must be an array",
                    test.nodeid
                )));
            }
        }
        if attempt == TestAttempt::Primary
            && let Some(duration) = test.call_duration_ms
        {
            entry.duration_ms = Some(duration);
        }
        if entry.reports.len() == MAX_OBSERVATIONS {
            entry.reports.remove(0);
        }
        entry.reports.push(test);
    }
    // New observations borrow the run and its identity. Existing custom fields
    // remain in the parsed history; only one entry's field projection is live
    // while the serializer writes the atomic replacement.
    fields.insert("tests", HistoryRootField::Tests(&tests));
    write_json(history_path, &fields, "write test history")?;
    Ok(RecordedTestRun {
        failed,
        outcomes,
        infrastructure_failed: run.infrastructure_failed,
    })
}

/// Validate a result artifact and atomically merge it into test history.
///
/// # Errors
/// Returns an error without replacing history when the result or history is invalid.
pub fn record_test_run_artifact(
    history_path: &Path,
    result_path: &Path,
    run_id: &str,
    attempt: TestAttempt,
) -> Result<RecordedTestRun, CoreError> {
    let run = read_test_run(result_path)?;
    record_test_run(history_path, &run, run_id, attempt)
}

/// Record controller evidence, projecting outcomes only for confirmation attempts.
/// Primary attempts retain failure identities and infrastructure status; all test
/// observations are still persisted in history.
///
/// # Errors
/// Returns an error without replacing history when the result or history is invalid.
pub fn record_test_execution_artifact(
    history_path: &Path,
    result_path: &Path,
    run_id: &str,
    attempt: TestAttempt,
) -> Result<RecordedTestRun, CoreError> {
    let run = read_test_run(result_path)?;
    let projection = match attempt {
        TestAttempt::Primary => OutcomeProjection::FailuresOnly,
        TestAttempt::Confirmation => OutcomeProjection::All,
    };
    record_test_run_projected(history_path, &run, run_id, attempt, projection)
}
