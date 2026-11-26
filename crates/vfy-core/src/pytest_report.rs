//! Deterministic pytest result HTML rendering and atomic artifact writes.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Path, PathBuf};

use base64::Engine as _;
use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use fancy_regex::Regex;
use serde::{Deserialize, Serialize};

use crate::selection::TestOutcome;
use crate::test_execution::{
    CaseResult, CollectionOutcome, ExecutionOutcome, ExecutionPhase, RunRecord,
};
use sha2::{Digest, Sha256};

use crate::html::escape;
use crate::output::{
    NEW_FILE_MODE, OutputTarget, PreparedFile, PublishedFile, output_target, prepare,
    replacement_file, restore,
};
use crate::{CoreError, absolute_path, atomic_write};

// Python strict decoding permits nonzero unused bits in the final base64 digit.
const IMAGE_BASE64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
);

const DEFAULT_CSS: &str = "body{font:14px system-ui,sans-serif;margin:2rem;color:#202124}h1{margin-bottom:.25rem}.meta,.results{border-collapse:collapse;width:100%;margin:1.5rem 0}.meta th,.meta td,.results th,.results td{border:1px solid #d8dce3;padding:.55rem;text-align:left;vertical-align:top}.Passed{color:#137333}.Failed,.Error{color:#b3261e}.Skipped,.XFailed{color:#6b7280}.XPassed{color:#9a6700}details{margin:.35rem 0}pre{white-space:pre-wrap}.summary{font-weight:600}";

#[derive(Deserialize)]
struct Payload {
    title: String,
    metadata: Vec<(String, String)>,
    redact: Vec<String>,
    results: Vec<TestResult<'static>>,
    css: Vec<String>,
    self_contained: bool,
    prefix: Vec<String>,
    summary: Vec<String>,
    postfix: Vec<String>,
    headers: Vec<String>,
}

#[derive(Serialize, Deserialize)]
struct TestResult<'a> {
    nodeid: Cow<'a, str>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    worker_id: String,
    phases: Vec<TestPhase<'a>>,
    extras: Cow<'a, [PytestHtmlExtra]>,
    cells: Cow<'a, [String]>,
}

#[derive(Serialize, Deserialize)]
struct TestPhase<'a> {
    when: Phase,
    outcome: PhaseOutcome,
    duration: f64,
    wasxfail: bool,
    details: Vec<Cow<'a, str>>,
}

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Phase {
    Setup,
    Call,
    Teardown,
    Collect,
}

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum PhaseOutcome {
    Passed,
    Failed,
    Skipped,
}

impl TestResult<'_> {
    fn outcome(&self) -> &'static str {
        if self
            .phases
            .iter()
            .any(|phase| phase.when != Phase::Call && phase.outcome == PhaseOutcome::Failed)
        {
            return "Error";
        }
        if let Some(phase) = self
            .phases
            .iter()
            .find(|phase| phase.when == Phase::Call && phase.outcome == PhaseOutcome::Failed)
        {
            return if phase.wasxfail { "XPassed" } else { "Failed" };
        }
        if let Some(phase) = self
            .phases
            .iter()
            .find(|phase| phase.outcome == PhaseOutcome::Skipped)
        {
            return if phase.wasxfail { "XFailed" } else { "Skipped" };
        }
        match self.phases.iter().find(|phase| phase.when == Phase::Call) {
            Some(phase) if phase.wasxfail => "XPassed",
            Some(_) => "Passed",
            None => "Error",
        }
    }

    fn details(&self) -> String {
        let mut details = Vec::new();
        for detail in self.phases.iter().flat_map(|phase| &phase.details) {
            if !detail.is_empty() && !details.contains(&detail.as_ref()) {
                details.push(detail.as_ref());
            }
        }
        details.join("\n\n")
    }
}

/// A validated HTML attachment supplied by a pytest fixture or report hook.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PytestHtmlExtra {
    pub kind: String,
    pub name: String,
    pub content: String,
}

fn native_html_result<'a>(case: &'a CaseResult, extras: &'a [PytestHtmlExtra]) -> TestResult<'a> {
    TestResult {
        nodeid: Cow::Borrowed(&case.nodeid),
        worker_id: String::new(),
        phases: case
            .phases
            .iter()
            .map(|phase| {
                let mut detail = Cow::Borrowed(phase.detail.as_str());
                let mut outcome = phase.outcome;
                if phase.phase == ExecutionPhase::Call {
                    for subtest in case.subtests() {
                        if matches!(
                            outcome,
                            ExecutionOutcome::Passed | ExecutionOutcome::XPassed
                        ) && matches!(
                            subtest.outcome,
                            ExecutionOutcome::Failed | ExecutionOutcome::Error
                        ) {
                            outcome = ExecutionOutcome::Failed;
                        }
                        if subtest.outcome != ExecutionOutcome::Passed {
                            let _ = write!(
                                detail.to_mut(),
                                "\nSubtest {} ({}):\n",
                                subtest.context,
                                subtest.outcome.as_str()
                            );
                            detail.to_mut().push_str(&subtest.detail);
                        }
                    }
                }
                TestPhase {
                    when: match phase.phase {
                        ExecutionPhase::Setup => Phase::Setup,
                        ExecutionPhase::Call => Phase::Call,
                        ExecutionPhase::Teardown => Phase::Teardown,
                    },
                    outcome: match outcome.report_outcome() {
                        TestOutcome::Passed => PhaseOutcome::Passed,
                        TestOutcome::Skipped => PhaseOutcome::Skipped,
                        TestOutcome::Failed => PhaseOutcome::Failed,
                    },
                    duration: phase.duration_ms / 1_000.0,
                    wasxfail: matches!(
                        phase.outcome,
                        ExecutionOutcome::XFailed | ExecutionOutcome::XPassed
                    ),
                    details: vec![
                        detail,
                        Cow::Borrowed(&phase.stdout),
                        Cow::Borrowed(&phase.stderr),
                        Cow::Borrowed(&phase.log),
                    ],
                }
            })
            .collect(),
        extras: Cow::Borrowed(extras),
        cells: Cow::Borrowed(&[]),
    }
}

fn native_html_results<'a>(
    record: &'a RunRecord,
    extras: &'a BTreeMap<String, Vec<PytestHtmlExtra>>,
) -> impl Iterator<Item = TestResult<'a>> {
    let cases = record
        .cases
        .iter()
        .map(|case| native_html_result(case, extras.get(&case.nodeid).map_or(&[], Vec::as_slice)));
    let collection = record.collection_reports.iter().filter_map(|report| {
        let outcome = match report.outcome {
            CollectionOutcome::Passed => return None,
            CollectionOutcome::Failed => PhaseOutcome::Failed,
            CollectionOutcome::Skipped => PhaseOutcome::Skipped,
        };
        Some(TestResult {
            nodeid: Cow::Borrowed(&report.nodeid),
            worker_id: String::new(),
            phases: vec![TestPhase {
                when: Phase::Collect,
                outcome,
                duration: report.duration_ms / 1_000.0,
                wasxfail: false,
                details: vec![
                    Cow::Borrowed(&report.payload.detail),
                    Cow::Borrowed(&report.payload.stdout),
                    Cow::Borrowed(&report.payload.stderr),
                    Cow::Borrowed(&report.payload.log),
                ],
            }],
            extras: Cow::Borrowed(&[]),
            cells: Cow::Borrowed(&[]),
        })
    });
    cases.chain(collection)
}

/// Serialize native rows for diagnostic consumers requiring a JSON representation.
///
/// # Errors
/// Returns an error if a row cannot be serialized.
pub fn native_pytest_html_results_json(
    record: &RunRecord,
    extras: &BTreeMap<String, Vec<PytestHtmlExtra>>,
) -> Result<String, CoreError> {
    let rows = native_html_results(record, extras).collect::<Vec<_>>();
    serde_json::to_string(&rows).map_err(CoreError::Serialization)
}

fn artifact_error(path: &Path, operation: &'static str, source: std::io::Error) -> CoreError {
    CoreError::ArtifactIo {
        path: path.to_path_buf(),
        operation,
        source,
    }
}

fn stylesheet(payload: &Payload) -> Result<String, CoreError> {
    let mut css = String::from(DEFAULT_CSS);
    for path in &payload.css {
        let path = Path::new(path);
        let custom = std::fs::read_to_string(path)
            .map_err(|error| artifact_error(path, "read pytest stylesheet", error))?;
        css.push('\n');
        css.push_str(&custom);
    }
    Ok(css)
}

fn render_extra(
    extra: &PytestHtmlExtra,
    self_contained: bool,
    assets: &mut Vec<(PathBuf, Vec<u8>)>,
    name: &str,
) -> String {
    let label = escape(&extra.name);
    match extra.kind.as_str() {
        "url" => format!("<a href=\"{}\">{label}</a>", escape(&extra.content)),
        "html" => extra.content.clone(),
        "text" | "json" if !self_contained => {
            let extension = if extra.kind == "json" { "json" } else { "txt" };
            let filename = format!("{name}.{extension}");
            assets.push((PathBuf::from(&filename), extra.content.as_bytes().to_vec()));
            format!("<a href=\"assets/{filename}\">{label}</a>")
        }
        kind if kind.starts_with("image/") => {
            let source = if let Ok(content) = IMAGE_BASE64.decode(&extra.content) {
                if self_contained {
                    format!("data:{kind};base64,{}", extra.content)
                } else {
                    let extension = match kind {
                        "image/png" => "png",
                        "image/jpeg" => "jpg",
                        "image/svg+xml" => "svg",
                        _ => "img",
                    };
                    let filename = format!("{name}.{extension}");
                    assets.push((PathBuf::from(&filename), content));
                    format!("assets/{filename}")
                }
            } else {
                extra.content.clone()
            };
            format!("<img alt=\"{label}\" src=\"{}\">", escape(&source))
        }
        _ => format!(
            "<details><summary>{label}</summary><pre>{}</pre></details>",
            escape(&extra.content)
        ),
    }
}

fn render_metadata(
    payload: &Payload,
    patterns: &[Regex],
    html: &mut String,
) -> Result<(), CoreError> {
    if !payload.metadata.is_empty() {
        html.push_str("<table class=\"meta\"><thead><tr><th>Environment</th><th>Value</th></tr></thead><tbody>");
        for (key, value) in &payload.metadata {
            let mut redacted = false;
            for pattern in patterns {
                if pattern
                    .find(key)
                    .map_err(|error| {
                        CoreError::InvalidInput(format!(
                            "failed to evaluate metadata redaction pattern {:?}: {error}",
                            pattern.as_str()
                        ))
                    })?
                    .is_some_and(|matched| matched.start() == 0)
                {
                    redacted = true;
                    break;
                }
            }
            let shown = if redacted {
                "••••••"
            } else {
                value
            };
            let _ = write!(
                html,
                "<tr><th>{}</th><td>{}</td></tr>",
                escape(key),
                escape(shown)
            );
        }
        html.push_str("</tbody></table>");
    }
    Ok(())
}

#[derive(Eq, Ord, PartialEq, PartialOrd)]
struct ResultIdentity {
    nodeid: String,
    worker_id: String,
}

struct RenderedResult {
    html: String,
    outcome: &'static str,
}

fn render_result(
    result: &TestResult<'_>,
    self_contained: bool,
    assets: &mut Vec<(PathBuf, Vec<u8>)>,
) -> RenderedResult {
    let outcome = result.outcome();
    let duration = result
        .phases
        .iter()
        .map(|phase| phase.duration.max(0.0))
        .sum::<f64>();
    let mut html = format!(
        "<tr><td class=\"{outcome}\">{outcome}</td><td>{}</td><td>{duration:.3}s</td><td>",
        escape(&result.nodeid)
    );
    let details = result.details();
    if !details.is_empty() {
        let _ = write!(
            html,
            "<details><summary>Output</summary><pre>{}</pre></details>",
            escape(&details)
        );
    }
    let mut identity = String::with_capacity(64);
    let mut digest = Sha256::new();
    digest.update(result.nodeid.as_bytes());
    if !result.worker_id.is_empty() {
        digest.update([0]);
        digest.update(result.worker_id.as_bytes());
    }
    for byte in digest.finalize() {
        let _ = write!(identity, "{byte:02x}");
    }
    for (index, extra) in result.extras.iter().enumerate() {
        html.push_str(&render_extra(
            extra,
            self_contained,
            assets,
            &format!("{identity}_{index}"),
        ));
    }
    html.push_str("</td>");
    for cell in result.cells.iter() {
        let _ = write!(html, "<td>{cell}</td>");
    }
    html.push_str("</tr>");
    RenderedResult { html, outcome }
}

fn render_results(
    payload: &Payload,
    rows: &BTreeMap<&ResultIdentity, &RenderedResult>,
    html: &mut String,
) {
    html.push_str("<p class=\"summary\">");
    for outcome in ["Passed", "Failed", "Skipped", "XFailed", "XPassed", "Error"] {
        let count = rows.values().filter(|row| row.outcome == outcome).count();
        let _ = write!(html, "{count} {outcome}, ");
    }
    html.push_str("</p>");
    for fragment in &payload.summary {
        html.push_str(fragment);
    }
    html.push_str("<table class=\"results\"><thead><tr><th>Result</th><th>Test</th><th>Duration</th><th>Details</th>");
    for header in &payload.headers {
        let _ = write!(html, "<th>{header}</th>");
    }
    html.push_str("</tr></thead><tbody>");
    for row in rows.values() {
        html.push_str(&row.html);
    }
    html.push_str("</tbody></table>");
}

fn render(
    payload: &Payload,
    css: &str,
    patterns: &[Regex],
    rows: &BTreeMap<&ResultIdentity, &RenderedResult>,
) -> Result<String, CoreError> {
    let style = if payload.self_contained {
        format!("<style>{css}</style>")
    } else {
        "<link rel=\"stylesheet\" href=\"assets/style.css\">".to_owned()
    };
    let mut html = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>{}</title>{style}</head><body><h1>{}</h1>",
        escape(&payload.title),
        escape(&payload.title)
    );
    for fragment in &payload.prefix {
        html.push_str(fragment);
    }
    render_metadata(payload, patterns, &mut html)?;
    render_results(payload, rows, &mut html);
    for fragment in &payload.postfix {
        html.push_str(fragment);
    }
    html.push_str("</body></html>\n");
    Ok(html)
}

struct Output<'a> {
    destination: PathBuf,
    content: &'a [u8],
}

fn resolve_outputs(outputs: &[(PathBuf, Vec<u8>)]) -> Result<Vec<Output<'_>>, CoreError> {
    let mut parents = BTreeMap::new();
    let mut paths = BTreeSet::new();
    let mut resolved = Vec::with_capacity(outputs.len());
    for (path, content) in outputs {
        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let parent = parents
            .entry(parent)
            .or_insert_with(|| absolute_path(parent));
        let metadata = match std::fs::symlink_metadata(path) {
            Ok(metadata) => Some(metadata),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(artifact_error(path, "inspect pytest HTML output", error)),
        };
        let destination = if metadata.as_ref().is_some_and(std::fs::Metadata::is_symlink) {
            absolute_path(path)
        } else {
            parent.join(path.file_name().ok_or_else(|| {
                CoreError::InvalidInput("pytest HTML output requires a filename".to_owned())
            })?)
        };
        if !paths.insert(destination.clone()) || metadata.is_some_and(|metadata| metadata.is_dir())
        {
            return Err(CoreError::InvalidInput(format!(
                "pytest HTML output is duplicated or not a file: {}",
                path.display()
            )));
        }
        resolved.push(Output {
            destination,
            content,
        });
    }
    for path in &paths {
        if path
            .ancestors()
            .skip(1)
            .any(|parent| paths.contains(parent))
        {
            return Err(CoreError::InvalidInput(format!(
                "overlapping pytest HTML output: {}",
                path.display()
            )));
        }
    }
    Ok(resolved)
}

fn commit_outputs(outputs: &[(PathBuf, Vec<u8>)]) -> Result<(), CoreError> {
    // A single output needs no rollback copy: atomic_write preserves it on failure.
    if let [(path, content)] = outputs {
        return atomic_write(&absolute_path(path), content)
            .map_err(|error| artifact_error(path, "write pytest HTML output", error));
    }
    let mut prepared = Vec::with_capacity(outputs.len());
    let mut created_parents = BTreeSet::new();
    for Output {
        destination,
        content,
    } in resolve_outputs(outputs)?
    {
        let parent = destination.parent().ok_or_else(|| {
            CoreError::InvalidInput("pytest HTML output requires a parent directory".to_owned())
        })?;
        let mut prepare_output = || -> std::io::Result<PreparedFile> {
            if created_parents.insert(parent.to_path_buf()) {
                std::fs::create_dir_all(parent)?;
            }
            let previous = match prepare(&destination, parent) {
                Ok(previous) => Some(previous),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(error),
            };
            // Files published together are replaced by rename; a stream cannot be rolled back.
            let OutputTarget::File { path, mode } = output_target(&destination)? else {
                return Err(std::io::Error::other(
                    "a report with several files can only replace regular files",
                ));
            };
            let mut replacement = replacement_file(&path, mode, NEW_FILE_MODE)?;
            replacement.write_all(content)?;
            Ok(PreparedFile {
                destination: path,
                replacement,
                previous,
            })
        };
        prepared.push(
            prepare_output().map_err(|error| {
                artifact_error(&destination, "prepare pytest HTML output", error)
            })?,
        );
    }
    let mut published = Vec::with_capacity(prepared.len());
    for file in prepared {
        if let Err(error) = file.replacement.persist(&file.destination) {
            let failure = match restore(published) {
                Ok(()) => error.error,
                Err(rollback) => std::io::Error::other(format!("{}; {rollback}", error.error)),
            };
            return Err(artifact_error(
                &file.destination,
                "write pytest HTML output",
                failure,
            ));
        }
        published.push(PublishedFile {
            destination: file.destination,
            previous: file.previous,
        });
    }
    Ok(())
}

/// A pytest session's rendered rows, committed only after successful publication.
pub struct PytestHtmlReport {
    destination: PathBuf,
    rows: BTreeMap<ResultIdentity, RenderedResult>,
    style: Option<ReportStyle>,
    css: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReportStyle {
    SelfContained,
    Assets,
}

impl PytestHtmlReport {
    #[must_use]
    pub fn new(destination: PathBuf) -> Self {
        Self {
            destination,
            rows: BTreeMap::new(),
            style: None,
            css: String::new(),
        }
    }

    /// Publish new or updated rows together with the current document metadata.
    ///
    /// # Errors
    /// Rejects invalid payloads or a changed output style. Publication errors
    /// preserve the prior in-memory rows and attempt to restore replaced files.
    pub fn write(&mut self, payload: &str) -> Result<(), CoreError> {
        let payload: Payload = serde_json::from_str(payload).map_err(CoreError::Serialization)?;
        self.write_payload(&payload, std::iter::empty())
    }

    /// Publish native execution rows without a Python result projection.
    ///
    /// # Errors
    /// Rejects malformed metadata, duplicate rows, or attachments without a case.
    /// Publication preserves previously committed rows and artifacts on failure.
    pub fn write_native(
        &mut self,
        payload: &str,
        record: &RunRecord,
        extras: &BTreeMap<String, Vec<PytestHtmlExtra>>,
    ) -> Result<(), CoreError> {
        let payload: Payload = serde_json::from_str(payload).map_err(CoreError::Serialization)?;
        if !extras.is_empty() {
            let nodeids = record
                .cases
                .iter()
                .map(|case| case.nodeid.as_str())
                .collect::<BTreeSet<_>>();
            if extras
                .keys()
                .any(|nodeid| !nodeids.contains(nodeid.as_str()))
            {
                return Err(CoreError::InvalidInput(
                    "native HTML attachments refer to an unknown test".to_owned(),
                ));
            }
        }
        let results = native_html_results(record, extras);
        self.write_payload(&payload, results)
    }

    fn write_payload<'a>(
        &mut self,
        payload: &Payload,
        native_results: impl IntoIterator<Item = TestResult<'a>>,
    ) -> Result<(), CoreError> {
        let style = if payload.self_contained {
            ReportStyle::SelfContained
        } else {
            ReportStyle::Assets
        };
        if self.style.is_some_and(|previous| previous != style) {
            return Err(CoreError::InvalidInput(
                "pytest HTML output style cannot change during a session".to_owned(),
            ));
        }
        let patterns = payload
            .redact
            .iter()
            .map(|pattern| {
                Regex::new(pattern).map_err(|error| {
                    CoreError::InvalidInput(format!(
                        "invalid metadata redaction pattern {pattern:?}: {error}"
                    ))
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let destination = &self.destination;
        if destination.file_name().is_none() || destination.is_dir() {
            return Err(CoreError::InvalidInput(format!(
                "pytest HTML destination is not a file: {}",
                destination.display()
            )));
        }
        let css = stylesheet(payload)?;
        let mut assets = Vec::new();
        let mut updates = BTreeMap::new();
        let mut stage_result = |result: &TestResult<'_>| {
            let identity = ResultIdentity {
                nodeid: result.nodeid.to_string(),
                worker_id: result.worker_id.clone(),
            };
            if updates.contains_key(&identity) {
                return Err(CoreError::InvalidInput(format!(
                    "duplicate pytest result: {}",
                    result.nodeid
                )));
            }
            updates.insert(
                identity,
                render_result(result, payload.self_contained, &mut assets),
            );
            Ok(())
        };
        for result in &payload.results {
            stage_result(result)?;
        }
        for result in native_results {
            stage_result(&result)?;
        }
        let mut rows = self.rows.iter().collect::<BTreeMap<_, _>>();
        rows.extend(updates.iter());
        let html = render(payload, &css, &patterns, &rows)?;
        let parent = destination
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        if !payload.self_contained && self.css != css {
            assets.push((PathBuf::from("style.css"), css.as_bytes().to_vec()));
        }
        let mut outputs = assets
            .into_iter()
            .map(|(relative, content)| (parent.join("assets").join(relative), content))
            .collect::<Vec<_>>();
        outputs.push((destination.clone(), html.into_bytes()));
        commit_outputs(&outputs)?;
        self.rows.extend(updates);
        self.style = Some(style);
        self.css = css;
        Ok(())
    }
}

/// Render and atomically replace a complete pytest HTML report.
///
/// # Errors
/// Returns an error for malformed input or failed artifact publication.
pub fn write_pytest_html_report(payload: &str, destination: &Path) -> Result<(), CoreError> {
    PytestHtmlReport::new(destination.to_path_buf()).write(payload)
}

#[cfg(test)]
mod tests;
