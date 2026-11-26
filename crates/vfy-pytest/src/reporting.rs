//! Terminal summaries and `JUnit` output for native pytest runs.

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyString, PyTuple};
use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use std::collections::HashMap;
use std::fmt::Write as _;
use std::fs;
use std::io;
use std::path::Path;
use vfy_core::test_execution::{
    CaseResult, CollectionOutcome, ExecutionOutcome, ExecutionPhase, PhaseResult, PythonText,
    RunRecord, SubtestResult,
};

pub(crate) struct TerminalSummary {
    pub(crate) short_lines: Vec<Py<PyAny>>,
    pub(crate) duration_heading: Option<String>,
    pub(crate) duration_lines: Vec<String>,
    pub(crate) summary_parts: Vec<(String, String, bool)>,
    pub(crate) main_color: String,
}

pub(crate) fn terminal_summary(
    record: &RunRecord,
    options: &Bound<'_, PyAny>,
) -> PyResult<TerminalSummary> {
    let options = options.cast::<PyDict>()?;
    let requested_durations = option_optional_usize(options, "durations")?;
    let configured_minimum = option_optional_f64(options, "durations_min")?;
    let verbosity = option_i32(options, "verbosity", 0)?;
    let collection_skipped = option_usize(options, "collection_skipped", 0)?;
    let collection_errors = option_usize(options, "collection_errors", 0)?;
    let warning_count = option_usize(options, "warning_count", 0)?;
    let deselected_count =
        option_usize(options, "deselected_count", record.deselected_nodeids.len())?;
    let reportchars = options
        .get_item("reportchars")?
        .map(|value| value.extract::<String>())
        .transpose()?
        .unwrap_or_default();
    let (duration_heading, duration_lines) = duration_summary(
        &record.cases,
        requested_durations,
        configured_minimum.unwrap_or(if verbosity < 2 { 0.005 } else { 0.0 }),
        configured_minimum.is_some(),
    );
    let failed = record.counts.failed;
    let errors = record.counts.errors + collection_errors;
    let mut skipped = record.counts.skipped;
    let mut xfailed = record.counts.xfailed;
    let mut xpassed = record.counts.xpassed;
    let mut subtests_passed = record.counts.subtests_passed;
    if option_i32(options, "subtest_verbosity", 1)? == 0 {
        // Pytest's quiet subtest status hook returns an empty category for
        // nonfailures. Keep execution evidence intact and hide them only here.
        for subtest in record.cases.iter().flat_map(CaseResult::subtests) {
            match subtest.outcome {
                ExecutionOutcome::Passed => subtests_passed -= 1,
                ExecutionOutcome::Skipped => skipped -= 1,
                ExecutionOutcome::XFailed => xfailed -= 1,
                ExecutionOutcome::XPassed => xpassed -= 1,
                ExecutionOutcome::Failed | ExecutionOutcome::Error => {}
            }
        }
    }
    let main_color = if failed + errors > 0 {
        "red"
    } else if xpassed > 0 || warning_count > 0 || record.counts.passed == 0 {
        "yellow"
    } else {
        "green"
    };
    let entries = [
        ("failed", failed),
        ("passed", record.counts.passed),
        ("skipped", skipped + collection_skipped),
        ("deselected", deselected_count),
        ("xfailed", xfailed),
        ("xpassed", xpassed),
        ("warning", warning_count),
        ("error", errors),
        ("subtests passed", subtests_passed),
    ];
    let mut summary_parts = Vec::new();
    for (name, count) in entries {
        if count == 0 {
            continue;
        }
        let label = match (name, count) {
            ("error" | "warning", 1) => name.to_owned(),
            ("error", _) => "errors".to_owned(),
            ("warning", _) => "warnings".to_owned(),
            _ => name.to_owned(),
        };
        let color = match name {
            "failed" | "error" => "red",
            "passed" | "subtests passed" => "green",
            _ => "yellow",
        };
        summary_parts.push((
            format!("{count} {label}"),
            color.to_owned(),
            color == main_color,
        ));
    }
    if summary_parts.is_empty() {
        summary_parts.push(("no tests ran".to_owned(), "yellow".to_owned(), false));
    }
    Ok(TerminalSummary {
        short_lines: short_summary(record, &reportchars, options)?,
        duration_heading,
        duration_lines,
        summary_parts,
        main_color: main_color.to_owned(),
    })
}

fn short_summary(
    record: &RunRecord,
    reportchars: &str,
    options: &Bound<'_, PyDict>,
) -> PyResult<Vec<Py<PyAny>>> {
    let mut lines = Vec::new();
    if reportchars.is_empty() {
        return Ok(lines);
    }
    let fullwidth = option_usize(options, "fullwidth", 80)?;
    let long_summary = option_bool(options, "long_summary")?;
    let subtests_forwarded = option_bool(options, "subtests_forwarded")?;
    let subtest_verbosity = option_i32(options, "subtest_verbosity", 1)?;
    let xpasses_forwarded = option_bool(options, "xpasses_forwarded")?;
    let terminal = options.py().import("_pytest.terminal")?;
    let measure = options
        .py()
        .import("_pytest._io.wcwidth")?
        .getattr("wcswidth")?;
    let format_line =
        |label: &str, nodeid: &str, reason: &PythonText, trim: bool| -> PyResult<Py<PyAny>> {
            let mut line = PythonText::from(format!("{label} {nodeid}"));
            if !reason.is_empty() {
                if !long_summary && trim {
                    let width: isize = measure
                        .call1((python_text_to_python(options.py(), &line)?,))?
                        .extract()?;
                    let suffix = terminal.getattr("_format_trimmed")?.call1((
                        " - {}",
                        python_text_to_python(options.py(), reason)?,
                        fullwidth.saturating_add_signed(width.saturating_neg()),
                    ))?;
                    if !suffix.is_none() {
                        let suffix = python_text_from_python(&suffix)?;
                        line = PythonText::joined(&[&line, &suffix]);
                    }
                } else {
                    line = PythonText::joined(&[&line, &PythonText::from(" - "), reason]);
                }
            }
            Ok(python_text_to_python(options.py(), &line)?.unbind())
        };
    for requested in reportchars.chars() {
        let (outcome, label) = match requested {
            'f' => (ExecutionOutcome::Failed, "FAILED"),
            'E' => (ExecutionOutcome::Error, "ERROR"),
            's' => (ExecutionOutcome::Skipped, "SKIPPED"),
            'x' => (ExecutionOutcome::XFailed, "XFAIL"),
            'X' => (ExecutionOutcome::XPassed, "XPASS"),
            'p' => (ExecutionOutcome::Passed, "PASSED"),
            _ => continue,
        };
        for case in &record.cases {
            if !subtests_forwarded {
                for subtest in case.subtests() {
                    if subtest.outcome != outcome
                        || (subtest_verbosity == 0 && outcome != ExecutionOutcome::Failed)
                        || (outcome == ExecutionOutcome::XPassed && xpasses_forwarded)
                    {
                        continue;
                    }
                    let reason = subtest
                        .exception
                        .clone()
                        .unwrap_or_else(|| subtest.detail.clone().into());
                    let subtest_label = if outcome == ExecutionOutcome::XPassed {
                        label.to_owned()
                    } else {
                        format!("SUB{label}{}", subtest.context)
                    };
                    lines.push(format_line(
                        &subtest_label,
                        &case.nodeid,
                        &reason,
                        matches!(requested, 'f' | 'E' | 'p'),
                    )?);
                }
            }
            for phase in &case.phases {
                if phase.outcome != outcome
                    || (outcome == ExecutionOutcome::Passed && phase.phase != ExecutionPhase::Call)
                    || (outcome == ExecutionOutcome::XPassed && xpasses_forwarded)
                {
                    continue;
                }
                let reason = phase
                    .exception
                    .clone()
                    .unwrap_or_else(|| phase.detail.clone().into());
                lines.push(format_line(
                    label,
                    &case.nodeid,
                    &reason,
                    matches!(requested, 'f' | 'E' | 'p'),
                )?);
            }
        }
    }
    Ok(lines)
}

fn option_usize(dict: &Bound<'_, PyDict>, name: &str, default: usize) -> PyResult<usize> {
    dict.get_item(name)?
        .map(|value| value.extract::<usize>())
        .transpose()
        .map(|value| value.unwrap_or(default))
}

fn option_bool(dict: &Bound<'_, PyDict>, name: &str) -> PyResult<bool> {
    dict.get_item(name)?
        .map(|value| value.extract::<bool>())
        .transpose()
        .map(|value| value.unwrap_or(false))
}

fn option_i32(dict: &Bound<'_, PyDict>, name: &str, default: i32) -> PyResult<i32> {
    dict.get_item(name)?
        .map(|value| value.extract::<i32>())
        .transpose()
        .map(|value| value.unwrap_or(default))
}

fn option_optional_usize(dict: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<usize>> {
    dict.get_item(name)?
        .filter(|value| !value.is_none())
        .map(|value| value.extract::<usize>())
        .transpose()
}

fn option_optional_f64(dict: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<f64>> {
    dict.get_item(name)?
        .filter(|value| !value.is_none())
        .map(|value| value.extract::<f64>())
        .transpose()
}

fn duration_summary(
    cases: &[CaseResult],
    requested: Option<usize>,
    minimum: f64,
    explicit_minimum: bool,
) -> (Option<String>, Vec<String>) {
    let Some(requested) = requested else {
        return (None, Vec::new());
    };
    let mut records = cases
        .iter()
        .flat_map(|case| {
            case.phases.iter().map(|phase| {
                (
                    phase.duration_ms / 1_000.0,
                    phase.phase.as_str(),
                    case.nodeid.as_str(),
                )
            })
        })
        .collect::<Vec<_>>();
    records.sort_by(|left, right| right.0.total_cmp(&left.0));
    if records.is_empty() {
        return (None, Vec::new());
    }
    if requested > 0 {
        records.truncate(requested);
    }
    let heading = if requested > 0 {
        format!("slowest {requested} durations")
    } else {
        "slowest durations".to_owned()
    };
    let mut lines = Vec::with_capacity(records.len() + 2);
    for (index, (duration, phase, nodeid)) in records.iter().enumerate() {
        if *duration < minimum {
            let hidden = records.len() - index;
            lines.push(String::new());
            let mut message = format!("({hidden} durations < {minimum}s hidden.");
            if !explicit_minimum {
                message.push_str("  Use -vv to show these durations.");
            }
            message.push(')');
            lines.push(message);
            break;
        }
        lines.push(format!("{duration:02.2}s {phase:<8} {nodeid}"));
    }
    (Some(heading), lines)
}

/// Convert Python text, including lone surrogates, to the shared code-point model.
pub(crate) fn python_text_from_python(value: &Bound<'_, PyAny>) -> PyResult<PythonText> {
    let value = value.cast::<PyString>()?;
    let encoded = value
        .py()
        .get_type::<PyString>()
        .call_method1("encode", (value, "utf-32-le", "surrogatepass"))?
        .cast_into::<PyBytes>()?;
    let points = encoded
        .as_bytes()
        .chunks_exact(4)
        .map(|bytes| u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
        .collect();
    PythonText::from_codepoints(points).map_err(PyValueError::new_err)
}

/// Rebuild the Python string, including lone surrogates, from shared text.
pub(crate) fn python_text_to_python<'py>(
    py: Python<'py>,
    text: &PythonText,
) -> PyResult<Bound<'py, PyAny>> {
    let bytes: Vec<_> = text
        .codepoints()
        .iter()
        .flat_map(|point| point.to_le_bytes())
        .collect();
    PyBytes::new(py, &bytes).call_method1("decode", ("utf-32-le", "surrogatepass"))
}

struct JUnitLocation {
    file: String,
    line: Option<i64>,
}

#[derive(Clone, Copy)]
enum JUnitCaseCount {
    OriginalCase,
    PhaseGroup,
}

#[derive(Clone, Copy)]
enum JUnitMessageStyle {
    Full,
    Bounded,
}

pub(crate) struct JUnitOptions {
    suite_name: String,
    prefix: Option<String>,
    family: String,
    logging: String,
    report_duration: String,
    case_count: JUnitCaseCount,
    message_style: JUnitMessageStyle,
    log_passing_tests: bool,
    timestamp: String,
    hostname: String,
    locations: HashMap<String, JUnitLocation>,
    global_properties: Vec<(String, String)>,
    /// Collection reports the default engine records itself; the independent
    /// engine's run record carries its own.
    collection_reports: Option<Vec<JUnitCollectionReport>>,
    /// `str(excrepr)` of each `pytest_internalerror`, written as `pytest.internal`.
    internal_errors: Vec<String>,
    /// Reporters that only accumulated a duration, such as an interrupted item's
    /// passed setup; pytest writes them as testcases without a name.
    duration_only: Vec<(Vec<(String, String)>, f64)>,
    /// `ElementTree`'s registered namespace prefixes, keyed by URI.
    namespace_map: HashMap<String, String>,
}

struct JUnitCollectionReport {
    nodeid: String,
    outcome: String,
    detail: String,
    file: String,
    line: Option<i64>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub(crate) struct WorkerJUnitProperties {
    pub(crate) global_properties: Vec<(String, String)>,
}

impl JUnitOptions {
    pub(crate) fn from_python(options: &Bound<'_, PyAny>) -> PyResult<Self> {
        let options = options.cast::<PyDict>()?;
        let suite_name = option_string(options, "suite_name", "pytest")?;
        let prefix = option_optional_string(options, "prefix")?;
        let family = match option_string(options, "family", "xunit2")?.as_str() {
            "legacy" | "xunit1" => "xunit1".to_owned(),
            "xunit2" => "xunit2".to_owned(),
            value => {
                return Err(PyValueError::new_err(format!(
                    "invalid junit_family value: {value:?}"
                )));
            }
        };
        let logging = option_string(options, "logging", "no")?;
        if !matches!(
            logging.as_str(),
            "no" | "log" | "system-out" | "system-err" | "out-err" | "all"
        ) {
            return Err(PyValueError::new_err(format!(
                "invalid junit_logging value: {logging:?}"
            )));
        }
        let report_duration = option_string(options, "report_duration", "total")?;
        if !matches!(report_duration.as_str(), "total" | "call") {
            return Err(PyValueError::new_err(format!(
                "invalid junit_duration_report value: {report_duration:?}"
            )));
        }
        let case_count = match option_string(options, "junit_case_count", "phase_group")?.as_str() {
            "original_case" => JUnitCaseCount::OriginalCase,
            "phase_group" => JUnitCaseCount::PhaseGroup,
            value => {
                return Err(PyValueError::new_err(format!(
                    "invalid junit_case_count: {value:?}"
                )));
            }
        };
        let message_style = match option_string(options, "junit_message_style", "full")?.as_str() {
            "full" => JUnitMessageStyle::Full,
            "bounded" => JUnitMessageStyle::Bounded,
            value => {
                return Err(PyValueError::new_err(format!(
                    "invalid junit_message_style: {value:?}"
                )));
            }
        };
        let log_passing_tests = options
            .get_item("log_passing_tests")?
            .map(|value| value.extract::<bool>())
            .transpose()?
            .unwrap_or(true);
        let timestamp = option_string(options, "timestamp", "")?;
        let hostname = option_string(options, "hostname", "")?;
        let mut locations = HashMap::new();
        if let Some(values) = options.get_item("locations")? {
            for (nodeid, value) in values.cast::<PyDict>()?.iter() {
                let nodeid = nodeid.extract::<String>()?;
                let value = value.cast::<PyTuple>()?;
                let file = value.get_item(0)?.extract::<String>()?;
                let line = value.get_item(1)?.extract::<Option<i64>>()?;
                locations.insert(nodeid, JUnitLocation { file, line });
            }
        }
        let global_properties = options
            .get_item("global_properties")?
            .map(|value| parse_properties(&value))
            .transpose()?
            .unwrap_or_default();
        let collection_reports = options
            .get_item("collection_reports")?
            .map(|reports| parse_collection_reports(&reports))
            .transpose()?;
        let (internal_errors, duration_only, namespace_map) = parse_session_records(options)?;
        Ok(Self {
            suite_name,
            prefix,
            family,
            logging,
            report_duration,
            case_count,
            message_style,
            log_passing_tests,
            timestamp,
            hostname,
            locations,
            global_properties,
            collection_reports,
            internal_errors,
            duration_only,
            namespace_map,
        })
    }
}

/// Read the internal errors, duration-only reporters, and namespace prefixes.
#[expect(
    clippy::type_complexity,
    reason = "each field matches one JUnitOptions member"
)]
fn parse_session_records(
    options: &Bound<'_, PyDict>,
) -> PyResult<(
    Vec<String>,
    Vec<(Vec<(String, String)>, f64)>,
    HashMap<String, String>,
)> {
    let internal_errors = options
        .get_item("internal_errors")?
        .map(|errors| {
            errors
                .try_iter()?
                .map(|error| junit_text(&error?))
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let duration_only = options
        .get_item("duration_only")?
        .map(|testcases| {
            testcases
                .try_iter()?
                .map(|testcase| {
                    let (attributes, duration) = testcase?.extract::<(Bound<'_, PyAny>, f64)>()?;
                    Ok((parse_properties(&attributes)?, duration))
                })
                .collect::<PyResult<Vec<_>>>()
        })
        .transpose()?
        .unwrap_or_default();
    let namespace_map = options
        .get_item("namespace_map")?
        .map(|map| map.extract::<HashMap<String, String>>())
        .transpose()?
        .unwrap_or_default();
    Ok((internal_errors, duration_only, namespace_map))
}

fn parse_collection_reports(value: &Bound<'_, PyAny>) -> PyResult<Vec<JUnitCollectionReport>> {
    value
        .try_iter()?
        .map(|report| {
            let report = report?.cast_into::<PyTuple>()?;
            Ok(JUnitCollectionReport {
                nodeid: report.get_item(0)?.extract::<String>()?,
                outcome: report.get_item(1)?.extract::<String>()?,
                detail: report.get_item(2)?.extract::<String>()?,
                file: report.get_item(3)?.extract::<String>()?,
                line: report.get_item(4)?.extract::<Option<i64>>()?,
            })
        })
        .collect()
}

/// Project the record's failed and skipped collection reports, as pytest's
/// `pytest_collectreport` hook records them for serial and worker runs alike.
fn junit_collection_reports(
    py: Python<'_>,
    record: &RunRecord,
) -> PyResult<Vec<JUnitCollectionReport>> {
    let mut reports = Vec::new();
    for report in &record.collection_reports {
        let payload = &report.payload;
        let (outcome, detail) = match report.outcome {
            CollectionOutcome::Passed => continue,
            CollectionOutcome::Failed => ("failed", payload.detail.clone()),
            CollectionOutcome::Skipped => {
                // A skipped collection report's longrepr is `(path, line, message)`.
                let detail = match (&payload.skip_location, &payload.exception) {
                    (Some(location), Some(message)) => (
                        location.path.as_str(),
                        location.line,
                        python_text_to_python(py, message)?,
                    )
                        .into_pyobject(py)?
                        .str()?
                        .extract::<String>()?,
                    _ => payload.detail.clone(),
                };
                ("skipped", detail)
            }
        };
        let file = report.nodeid.split("::").next().unwrap_or_default();
        reports.push(JUnitCollectionReport {
            nodeid: report.nodeid.clone(),
            outcome: outcome.to_owned(),
            detail,
            file: file.to_owned(),
            line: None,
        });
    }
    Ok(reports)
}

/// Read `(name, value)` pairs; values keep pytest's `#xHHHH` form for lone surrogates.
pub(crate) fn parse_properties(value: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    value
        .try_iter()?
        .map(|property| {
            let property = property?;
            let property = property.cast::<PyTuple>()?;
            Ok((
                property.get_item(0)?.extract::<String>()?,
                junit_text(&property.get_item(1)?)?,
            ))
        })
        .collect()
}

/// Convert `str(value)` to the document text pytest's `bin_xml_escape` produces.
pub(crate) fn junit_text(value: &Bound<'_, PyAny>) -> PyResult<String> {
    let text = python_text_from_python(value.str()?.as_any())?;
    Ok(exception_text(Some(&text), "").into_owned())
}

fn option_string(dict: &Bound<'_, PyDict>, name: &str, default: &str) -> PyResult<String> {
    dict.get_item(name)?
        .map(|value| value.extract::<String>())
        .transpose()
        .map(|value| value.unwrap_or_else(|| default.to_owned()))
}

fn option_optional_string(dict: &Bound<'_, PyDict>, name: &str) -> PyResult<Option<String>> {
    dict.get_item(name)?
        .filter(|value| !value.is_none())
        .map(|value| value.extract::<String>())
        .transpose()
}

/// Create the report's directory; pytest does this before it serializes anything.
pub(crate) fn create_junit_parent(py: Python<'_>, destination: &Path) -> PyResult<()> {
    if let Some(parent) = destination
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
    {
        fs::create_dir_all(parent).map_err(|error| os_error(py, &error, parent))?;
    }
    Ok(())
}

pub(crate) fn write_junit_atomic(
    py: Python<'_>,
    destination: &Path,
    record: &RunRecord,
    mut options: JUnitOptions,
    worker_properties: &WorkerJUnitProperties,
) -> PyResult<()> {
    options
        .global_properties
        .extend(worker_properties.global_properties.iter().cloned());
    let collection_reports = match options.collection_reports.take() {
        Some(reports) => reports,
        None => junit_collection_reports(py, record)?,
    };
    let mut counts = JUnitCounts::for_record(record, &collection_reports, options.case_count);
    counts.tests += options.internal_errors.len();
    counts.errors += options.internal_errors.len();
    let mut qnames = QNames::new(&options.namespace_map);
    let mut body = String::new();
    let _ = write!(
        body,
        "<testsuite name=\"{}\" errors=\"{}\" failures=\"{}\" skipped=\"{}\" tests=\"{}\" time=\"{:.3}\" timestamp=\"{}\" hostname=\"{}\">",
        xml_escape(&options.suite_name, true),
        counts.errors,
        counts.failures,
        counts.skipped,
        counts.tests,
        record.duration_ms / 1_000.0,
        xml_escape(&options.timestamp, true),
        xml_escape(&options.hostname, true)
    );
    append_properties(&mut body, &options.global_properties);
    let mut collection_groups: Vec<Vec<&JUnitCollectionReport>> = Vec::new();
    let mut collection_indices = HashMap::new();
    for report in &collection_reports {
        let index = *collection_indices.entry(&report.nodeid).or_insert_with(|| {
            collection_groups.push(Vec::new());
            collection_groups.len() - 1
        });
        collection_groups[index].push(report);
    }
    for reports in &collection_groups {
        append_collection_testcase(&mut body, reports, &options);
    }
    for case in &record.cases {
        let split = phase_groups(case).count() == 2;
        for (index, phases) in written_groups(case).enumerate() {
            let properties = match case.call_failure_properties() {
                Some(properties) if split && index == 0 => properties,
                _ => case.user_properties(),
            };
            append_testcase(&mut body, case, phases, properties, &options, &mut qnames)?;
        }
    }
    for (attributes, duration) in &options.duration_only {
        body.push_str("<testcase");
        for (name, value) in attributes.iter().filter(|(name, _)| name != "time") {
            append_attribute(&mut body, &qnames.name(name)?, value);
        }
        let _ = write!(body, " time=\"{duration:.3}\"></testcase>");
    }
    for error in &options.internal_errors {
        body.push_str("<testcase classname=\"pytest\" name=\"internal\" time=\"0.000\">");
        append_message_element(&mut body, "error", "internal error", error);
        body.push_str("</testcase>");
    }
    body.push_str("</testsuite></testsuites>");
    let mut document = String::from("<?xml version=\"1.0\" encoding=\"utf-8\"?><testsuites");
    for (prefix, uri) in qnames.declarations() {
        if prefix.is_empty() {
            let _ = write!(document, " xmlns=\"{}\"", xml_escape(uri, true));
        } else {
            let _ = write!(document, " xmlns:{prefix}=\"{}\"", xml_escape(uri, true));
        }
    }
    document.push_str(" name=\"pytest tests\">");
    document.push_str(&body);
    vfy_core::atomic_write(destination, document.as_bytes())
        .map_err(|error| os_error(py, &error, destination))
}

#[pyfunction]
pub(crate) fn publish_junit_xml(py: Python<'_>, destination: &str, xml: &str) -> PyResult<()> {
    py.detach(|| vfy_core::atomic_write(Path::new(destination), xml.as_bytes()))
        // This replaces pytest's own `open(logfile, "w")`, so failures raise the
        // same `OSError` that pytest would.
        .map_err(|error| os_error(py, &error, Path::new(destination)))
}

struct JUnitCounts {
    tests: usize,
    failures: usize,
    errors: usize,
    skipped: usize,
}

impl JUnitCounts {
    fn for_record(
        record: &RunRecord,
        collection_reports: &[JUnitCollectionReport],
        case_count: JUnitCaseCount,
    ) -> Self {
        let mut counts = Self {
            tests: 0,
            failures: 0,
            errors: 0,
            skipped: 0,
        };
        for case in &record.cases {
            let groups = written_groups(case);
            counts.tests += if matches!(case_count, JUnitCaseCount::PhaseGroup) {
                groups.clone().count()
            } else {
                1
            };
            for phases in groups {
                let has_call = phases
                    .iter()
                    .any(|phase| phase.phase == ExecutionPhase::Call);
                counts.tests += usize::from(has_call) * case.subtests().len();
                for phase in phases {
                    match phase.outcome {
                        ExecutionOutcome::Failed => counts.failures += 1,
                        ExecutionOutcome::Error => counts.errors += 1,
                        ExecutionOutcome::Skipped | ExecutionOutcome::XFailed => {
                            counts.skipped += 1;
                        }
                        ExecutionOutcome::Passed | ExecutionOutcome::XPassed => {}
                    }
                }
                if has_call {
                    for subtest in case.subtests() {
                        match subtest.outcome {
                            ExecutionOutcome::Failed => counts.failures += 1,
                            ExecutionOutcome::Skipped | ExecutionOutcome::XFailed => {
                                counts.skipped += 1;
                            }
                            ExecutionOutcome::Passed
                            | ExecutionOutcome::Error
                            | ExecutionOutcome::XPassed => {}
                        }
                    }
                }
            }
        }
        counts.tests += collection_reports.len();
        for report in collection_reports {
            match report.outcome.as_str() {
                "failed" => counts.errors += 1,
                "skipped" => counts.skipped += 1,
                _ => {}
            }
        }
        counts
    }
}

/// Raise the `OSError` subclass Python's own file functions raise for `path`.
fn os_error(py: Python<'_>, error: &io::Error, path: &Path) -> PyErr {
    let Some(code) = error.raw_os_error() else {
        return PyOSError::new_err(error.to_string());
    };
    let message = py
        .import("os")
        .and_then(|os| os.call_method1("strerror", (code,)))
        .and_then(|message| message.extract::<String>())
        .unwrap_or_else(|_| error.to_string());
    PyOSError::new_err((code, message, path.to_string_lossy().into_owned()))
}

fn testcase_attributes(
    case: &CaseResult,
    phases: &[PhaseResult],
    options: &JUnitOptions,
) -> Vec<(String, String)> {
    let (classname, name) = junit_identity(&case.nodeid, options.prefix.as_deref());
    let mut attributes = vec![
        ("classname".to_owned(), classname),
        ("name".to_owned(), name),
        (
            "time".to_owned(),
            format!("{:.3}", testcase_duration(phases, &options.report_duration)),
        ),
    ];
    if options.family == "xunit1"
        && let Some(location) = options.locations.get(&case.nodeid)
    {
        attributes.push(("file".to_owned(), location.file.clone()));
        if let Some(line) = location.line {
            attributes.push(("line".to_owned(), line.to_string()));
        }
    }
    // Pytest finalizes a failed call before opening its separate teardown-error
    // testcase. Recorded attributes belong only to the original testcase.
    if phases
        .first()
        .is_some_and(|phase| phase.phase == ExecutionPhase::Setup)
    {
        for (name, value) in case.xml_attributes() {
            // Pytest always writes its measured duration, and xunit2 permits
            // only the name and classname overrides from record_xml_attribute.
            if name == "time"
                || (options.family == "xunit2" && !matches!(name.as_str(), "classname" | "name"))
            {
                continue;
            }
            if let Some((_, existing)) = attributes.iter_mut().find(|(key, _)| key == name) {
                existing.clone_from(value);
            } else {
                attributes.push((name.clone(), value.clone()));
            }
        }
    }
    attributes
}

fn append_testcase(
    xml: &mut String,
    case: &CaseResult,
    phases: &[PhaseResult],
    properties: &[(String, String)],
    options: &JUnitOptions,
    qnames: &mut QNames<'_>,
) -> PyResult<()> {
    xml.push_str("<testcase");
    for (name, value) in testcase_attributes(case, phases, options) {
        append_attribute(xml, &qnames.name(&name)?, &value);
    }
    xml.push('>');
    append_properties(xml, properties);
    for phase in phases {
        append_phase(xml, case, phase, options);
        if phase.phase == ExecutionPhase::Call {
            let setup_end = case
                .phases
                .iter()
                .position(|phase| phase.phase == ExecutionPhase::Call)
                .unwrap_or(case.phases.len());
            for subtest in case.subtests() {
                append_subtest(xml, subtest, options.message_style);
                if subtest.outcome == ExecutionOutcome::Skipped
                    || (subtest.outcome == ExecutionOutcome::Failed && !options.log_passing_tests)
                {
                    // Subtest reports carry setup sections; the containing
                    // call's capture is published only by its own report.
                    append_captured_output(xml, &case.phases[..setup_end], options);
                }
            }
        }
        // Pytest writes the cumulative capture when a skip is reported, when
        // a call fails with passing logs disabled, and again at teardown. A
        // split teardown-error testcase still receives the whole case prefix.
        let prefix_end = case
            .phases
            .iter()
            .position(|candidate| candidate.phase == phase.phase)
            .map_or(case.phases.len(), |index| index + 1);
        let prefix = &case.phases[..prefix_end];
        if phase.outcome == ExecutionOutcome::Skipped
            || (phase.phase == ExecutionPhase::Call
                && phase.outcome == ExecutionOutcome::Failed
                && !options.log_passing_tests)
        {
            append_captured_output(xml, prefix, options);
        }
        if phase.phase == ExecutionPhase::Teardown
            && (options.log_passing_tests
                || !matches!(
                    phase.outcome,
                    ExecutionOutcome::Passed | ExecutionOutcome::XPassed
                ))
        {
            append_captured_output(xml, prefix, options);
        }
    }
    xml.push_str("</testcase>");
    Ok(())
}

fn append_collection_testcase(
    xml: &mut String,
    reports: &[&JUnitCollectionReport],
    options: &JUnitOptions,
) {
    let Some(report) = reports.first() else {
        return;
    };
    let (classname, name) = junit_identity(&report.nodeid, options.prefix.as_deref());
    let mut attributes = format!(
        " classname=\"{}\" name=\"{}\" time=\"0.000\"",
        xml_escape(&classname, true),
        xml_escape(&name, true),
    );
    if options.family == "xunit1" {
        write!(attributes, " file=\"{}\"", xml_escape(&report.file, true))
            .expect("String formatting is infallible");
        if let Some(line) = report.line {
            write!(attributes, " line=\"{line}\"").expect("String formatting is infallible");
        }
    }
    write!(xml, "<testcase{attributes}>").expect("String formatting is infallible");
    for report in reports {
        match report.outcome.as_str() {
            "failed" => append_message_element(xml, "error", "collection failure", &report.detail),
            "skipped" => {
                append_message_element(xml, "skipped", "collection skipped", &report.detail);
            }
            _ => {}
        }
    }
    xml.push_str("</testcase>");
}

fn append_phase(xml: &mut String, case: &CaseResult, phase: &PhaseResult, options: &JUnitOptions) {
    let exception = exception_text(phase.exception.as_ref(), &phase.detail);
    match phase.outcome {
        ExecutionOutcome::Failed => {
            let message = junit_message(
                phase.exception.as_ref(),
                &phase.detail,
                "",
                "",
                options.message_style,
            );
            append_message_element(xml, "failure", &message, &phase.detail);
        }
        ExecutionOutcome::Error => {
            let when = phase.phase.as_str();
            let message = junit_message(
                phase.exception.as_ref(),
                &phase.detail,
                &format!("failed on {when} with \""),
                "\"",
                options.message_style,
            );
            append_message_element(xml, "error", &message, &phase.detail);
        }
        ExecutionOutcome::Skipped => {
            let reason = exception.strip_prefix("Skipped: ").unwrap_or(&exception);
            let detail = phase.skip_location.as_ref().map_or_else(
                || {
                    options.locations.get(&case.nodeid).map_or_else(
                        || phase.detail.clone(),
                        |location| {
                            format!(
                                "{}:{}: {reason}",
                                location.file,
                                location.line.unwrap_or_default()
                            )
                        },
                    )
                },
                |location| format!("{}:{}: {reason}", location.path, location.line),
            );
            append_skipped(xml, "pytest.skip", reason, &detail);
        }
        ExecutionOutcome::XFailed => {
            let reason = exception.strip_prefix("reason: ").unwrap_or(&exception);
            append_skipped(xml, "pytest.xfail", reason, "");
        }
        // Pytest reports a non-strict xpass as a normal pass in JUnit XML.
        ExecutionOutcome::Passed | ExecutionOutcome::XPassed => {}
    }
}

fn append_subtest(xml: &mut String, subtest: &SubtestResult, style: JUnitMessageStyle) {
    let exception = exception_text(subtest.exception.as_ref(), &subtest.detail);
    let exception = exception.as_ref();
    match subtest.outcome {
        ExecutionOutcome::Failed => {
            let message = junit_message(subtest.exception.as_ref(), &subtest.detail, "", "", style);
            append_message_element(xml, "failure", &message, &subtest.detail);
        }
        ExecutionOutcome::Skipped => {
            let reason = exception.strip_prefix("Skipped: ").unwrap_or(exception);
            let detail = subtest.skip_location.as_ref().map_or_else(
                || subtest.detail.clone(),
                |location| format!("{}:{}: {reason}", location.path, location.line),
            );
            append_skipped(xml, "pytest.skip", reason, &detail);
        }
        ExecutionOutcome::XFailed => {
            append_skipped(
                xml,
                "pytest.xfail",
                exception.strip_prefix("reason: ").unwrap_or(exception),
                "",
            );
        }
        ExecutionOutcome::Passed | ExecutionOutcome::Error | ExecutionOutcome::XPassed => {}
    }
}

/// Render a report message; pytest writes each lone surrogate as `#xHHHH`.
fn exception_text<'a>(exception: Option<&'a PythonText>, detail: &'a str) -> Cow<'a, str> {
    let Some(exception) = exception else {
        return Cow::Borrowed(detail);
    };
    if let Some(text) = exception.as_scalar() {
        return Cow::Borrowed(text);
    }
    Cow::Owned(exception_codepoints(&exception.codepoints()))
}

fn exception_codepoints(points: &[u32]) -> String {
    let mut text = String::new();
    for &point in points {
        match char::from_u32(point) {
            Some(character) => text.push(character),
            None => {
                let _ = write!(text, "#x{point:04X}");
            }
        }
    }
    text
}

fn junit_message(
    exception: Option<&PythonText>,
    detail: &str,
    prefix: &str,
    suffix: &str,
    style: JUnitMessageStyle,
) -> String {
    if matches!(style, JUnitMessageStyle::Full) {
        return format!("{prefix}{}{suffix}", exception_text(exception, detail));
    }
    // Pytest 9.2 budgets Python code points before XML escaping. Retain whole
    // lines with Python's splitlines boundaries, leaving the report text intact.
    let mut points: Vec<u32> = prefix.chars().map(u32::from).collect();
    points.extend(exception.map_or_else(
        || detail.chars().map(u32::from).collect(),
        PythonText::codepoints,
    ));
    points.extend(suffix.chars().map(u32::from));
    let mut lines = Vec::new();
    let (mut start, mut index) = (0, 0);
    while index < points.len() {
        if matches!(points[index], 10..=13 | 28..=30 | 0x85 | 0x2028 | 0x2029) {
            lines.push(&points[start..index]);
            if points[index] == 13 && points.get(index + 1) == Some(&10) {
                index += 1;
            }
            start = index + 1;
        }
        index += 1;
    }
    if start < points.len() || lines.is_empty() {
        lines.push(&points[start..]);
    }
    let mut shortened = Vec::new();
    let mut kept = 0;
    let mut size = 0;
    for line in &lines {
        if kept > 0 && size + line.len() + 1 > 500 {
            break;
        }
        if kept > 0 {
            shortened.push(10);
        }
        shortened.extend_from_slice(line);
        size += line.len() + 1;
        kept += 1;
    }
    let first_line_cut = shortened.len() > 500;
    if first_line_cut {
        shortened.truncate(500);
        shortened.extend("...".chars().map(u32::from));
    }
    let remaining = lines.len() - kept;
    let note = if remaining > 0 {
        format!(
            "+{remaining} more line{}",
            if remaining > 1 { "s" } else { "" }
        )
    } else if first_line_cut {
        "truncated".to_owned()
    } else {
        return exception_codepoints(&points);
    };
    shortened.extend(
        format!("\n[{note}; full report in element text]")
            .chars()
            .map(u32::from),
    );
    exception_codepoints(&shortened)
}

fn append_properties(xml: &mut String, properties: &[(String, String)]) {
    if properties.is_empty() {
        return;
    }
    xml.push_str("<properties>");
    for (name, value) in properties {
        let _ = write!(
            xml,
            "<property name=\"{}\" value=\"{}\"/>",
            markup_escape(name),
            xml_escape(value, true)
        );
    }
    xml.push_str("</properties>");
}

/// The phase groups `JUnit` writes as testcases, after any omitted teardown testcase.
fn written_groups(case: &CaseResult) -> impl Iterator<Item = &[PhaseResult]> + Clone {
    let written = if case.teardown_testcase_unwritten() {
        1
    } else {
        2
    };
    phase_groups(case).take(written)
}

fn phase_groups(case: &CaseResult) -> impl Iterator<Item = &[PhaseResult]> + Clone {
    let call_failure = case.phases.iter().position(|phase| {
        phase.phase == ExecutionPhase::Call
            && case.report_phase_outcome(phase) == ExecutionOutcome::Failed
    });
    let teardown_error = case.phases.iter().position(|phase| {
        phase.phase == ExecutionPhase::Teardown && phase.outcome == ExecutionOutcome::Error
    });
    let groups = match (call_failure, teardown_error) {
        (Some(call_failure), Some(teardown_error)) => [
            Some(&case.phases[..=call_failure]),
            Some(&case.phases[teardown_error..]),
        ],
        _ => [Some(case.phases.as_slice()), None],
    };
    groups.into_iter().flatten()
}

fn junit_identity(nodeid: &str, prefix: Option<&str>) -> (String, String) {
    let parameter_offset = nodeid.find('[').unwrap_or(nodeid.len());
    let (address, parameters) = nodeid.split_at(parameter_offset);
    let mut names = address.split("::").map(str::to_owned).collect::<Vec<_>>();
    if let Some(module) = names.first_mut() {
        *module = module.replace(['/', '\\'], ".");
        if let Some(module_name) = module.strip_suffix(".py") {
            *module = module_name.to_owned();
        }
    }
    if let Some(test_name) = names.last_mut() {
        test_name.push_str(parameters);
    }
    let name = names.pop().unwrap_or_default();
    if let Some(prefix) = prefix {
        names.insert(0, prefix.to_owned());
    }
    (names.join("."), name)
}

fn testcase_duration(phases: &[PhaseResult], report_duration: &str) -> f64 {
    if report_duration == "call" {
        return phases
            .iter()
            .find(|phase| phase.phase == ExecutionPhase::Call)
            .map_or(0.0, |phase| phase.duration_ms / 1_000.0);
    }
    phases.iter().map(|phase| phase.duration_ms).sum::<f64>() / 1_000.0
}

fn append_message_element(xml: &mut String, tag: &str, message: &str, detail: &str) {
    let _ = write!(
        xml,
        "<{tag} message=\"{}\">{}</{tag}>",
        xml_escape(message, true),
        xml_escape(detail, false)
    );
}

fn append_skipped(xml: &mut String, kind: &str, message: &str, detail: &str) {
    let _ = write!(
        xml,
        "<skipped type=\"{}\" message=\"{}\">{}</skipped>",
        xml_escape(kind, true),
        xml_escape(message, true),
        xml_escape(detail, false)
    );
}

/// One stream's capture as of the last phase's report.
///
/// pytest reads the report's own, cumulative `sections`; a report whose
/// sections a hook replaced retains them, so they take precedence.
fn captured<'a>(
    phases: &'a [PhaseResult],
    title: &'a str,
    stream: fn(&PhaseResult) -> &String,
) -> Vec<&'a str> {
    match phases
        .last()
        .and_then(|phase| phase.report_sections.as_ref())
    {
        Some(sections) => sections
            .iter()
            .filter(|section| section.name.starts_with(title))
            .map(|section| section.content.as_str())
            .collect(),
        None => phases.iter().map(|phase| stream(phase).as_str()).collect(),
    }
}

fn append_captured_output(xml: &mut String, phases: &[PhaseResult], options: &JUnitOptions) {
    if options.logging == "no" {
        return;
    }
    let mut system_out = String::new();
    let mut system_err = String::new();
    if matches!(options.logging.as_str(), "log" | "all") {
        append_captured_section(
            &mut system_out,
            "Captured Log",
            captured(phases, "Captured log", |phase| &phase.log),
            "\n",
        );
    }
    if matches!(options.logging.as_str(), "system-out" | "out-err" | "all") {
        append_captured_section(
            &mut system_out,
            "Captured Out",
            captured(phases, "Captured stdout", |phase| &phase.stdout),
            "",
        );
        let _ = write!(
            xml,
            "<system-out>{}</system-out>",
            xml_escape(&system_out, false)
        );
    }
    if matches!(options.logging.as_str(), "system-err" | "out-err" | "all") {
        append_captured_section(
            &mut system_err,
            "Captured Err",
            captured(phases, "Captured stderr", |phase| &phase.stderr),
            "",
        );
        let _ = write!(
            xml,
            "<system-err>{}</system-err>",
            xml_escape(&system_err, false)
        );
    }
    if options.logging == "log" {
        let _ = write!(
            xml,
            "<system-out>{}</system-out>",
            xml_escape(&system_out, false)
        );
    }
}

fn append_captured_section<'a>(
    output: &mut String,
    label: &str,
    content: impl IntoIterator<Item = &'a str>,
    separator: &str,
) {
    let header = format!(" {label} ");
    let padding = 80usize.saturating_sub(header.len());
    output.push_str(&"-".repeat(padding / 2));
    output.push_str(&header);
    output.push_str(&"-".repeat(padding - padding / 2));
    output.push('\n');
    for (index, part) in content
        .into_iter()
        .filter(|part| !part.is_empty())
        .enumerate()
    {
        if index != 0 {
            output.push_str(separator);
        }
        output.push_str(part);
    }
    output.push('\n');
}

fn append_attribute(xml: &mut String, name: &str, value: &str) {
    let _ = write!(
        xml,
        " {}=\"{}\"",
        markup_escape(name),
        xml_escape(value, true)
    );
}

/// Serialized attribute names, following `ElementTree`'s `{uri}local` rules.
///
/// A URI uses its registered prefix or the next `ns<N>`, numbered in document
/// order; the root element declares every prefix, sorted by prefix.
struct QNames<'a> {
    registered: &'a HashMap<String, String>,
    declared: Vec<(String, String)>,
}

impl<'a> QNames<'a> {
    fn new(registered: &'a HashMap<String, String>) -> Self {
        Self {
            registered,
            declared: Vec::new(),
        }
    }

    fn name<'n>(&mut self, name: &'n str) -> PyResult<Cow<'n, str>> {
        let Some(qualified) = name.strip_prefix('{') else {
            return Ok(Cow::Borrowed(name));
        };
        let (uri, local) = qualified.rsplit_once('}').ok_or_else(|| {
            PyValueError::new_err("not enough values to unpack (expected 2, got 1)")
        })?;
        let prefix = if let Some((_, prefix)) = self.declared.iter().find(|(known, _)| known == uri)
        {
            prefix.clone()
        } else {
            let prefix = self
                .registered
                .get(uri)
                .cloned()
                .unwrap_or_else(|| format!("ns{}", self.declared.len()));
            if prefix != "xml" {
                self.declared.push((uri.to_owned(), prefix.clone()));
            }
            prefix
        };
        if prefix.is_empty() {
            Ok(Cow::Borrowed(local))
        } else {
            Ok(Cow::Owned(format!("{prefix}:{local}")))
        }
    }

    fn declarations(&self) -> impl Iterator<Item = (&str, &str)> {
        let mut declared = self
            .declared
            .iter()
            .map(|(uri, prefix)| (prefix.as_str(), uri.as_str()))
            .collect::<Vec<_>>();
        declared.sort_by_key(|(prefix, _)| *prefix);
        declared.into_iter()
    }
}

/// Escape an attribute as `ElementTree` does, leaving other characters as written.
///
/// pytest passes property names to `ElementTree` without `bin_xml_escape`.
fn markup_escape(value: &str) -> Cow<'_, str> {
    if !value.contains(['&', '<', '>', '"', '\r', '\n', '\t']) {
        return Cow::Borrowed(value);
    }
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\r' => escaped.push_str("&#13;"),
            '\n' => escaped.push_str("&#10;"),
            '\t' => escaped.push_str("&#09;"),
            _ => escaped.push(character),
        }
    }
    Cow::Owned(escaped)
}

fn xml_escape(value: &str, attribute: bool) -> Cow<'_, str> {
    if value.bytes().all(|byte| match byte {
        b'&' | b'<' | b'>' | b'\r' => false,
        b'"' | b'\'' if attribute => false,
        b'\n' | b'\t' => !attribute,
        0x20..=0x7e => true,
        _ => false,
    }) {
        return Cow::Borrowed(value);
    }
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        let code = character as u32;
        if !(code == 0x09
            || code == 0x0a
            || code == 0x0d
            || (0x20..=0x7e).contains(&code)
            || (0x80..=0xd7ff).contains(&code)
            || (0xe000..=0xfffd).contains(&code)
            || (0x1_0000..=0x10_ffff).contains(&code))
            || code == 0x7f
        {
            let width = if code <= 0xff { 2 } else { 4 };
            let _ = write!(escaped, "#x{code:0width$X}");
            continue;
        }
        match character {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' if attribute => escaped.push_str("&quot;"),
            '\'' if attribute => escaped.push_str("&apos;"),
            '\r' => escaped.push_str("&#13;"),
            '\n' if attribute => escaped.push_str("&#10;"),
            '\t' if attribute => escaped.push_str("&#9;"),
            _ => escaped.push(character),
        }
    }
    Cow::Owned(escaped)
}

#[cfg(test)]
mod tests;
