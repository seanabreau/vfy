//! Semantic report discovery, aggregation, and rendering.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use fancy_regex::Regex;
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::path::{UnicodeCaseVersion, glob_set_with_unicode_version};
use crate::selection::rank_test_contexts;
use crate::{CoreError, analyze_semantic_source_data, read_python_source, valid_observation};

use super::{DecisionDefinition, SemanticData};

#[cfg(test)]
mod tests;

/// Selection and discovery inputs for one semantic report.
#[derive(Clone, Debug, Default)]
pub struct SemanticReportOptions {
    pub include: Vec<String>,
    pub omit: Vec<String>,
    pub contexts: Vec<String>,
    pub source_roots: Vec<String>,
    pub exclude_decisions: Vec<String>,
    pub optimize: bool,
    pub unicode_case_version: UnicodeCaseVersion,
}

/// One fully aggregated semantic report row.
#[derive(Clone, Debug)]
pub struct SemanticReportRow {
    path: String,
    line: i32,
    expression: String,
    kind: String,
    decisions_covered: usize,
    decisions_total: usize,
    conditions_covered: usize,
    conditions_total: usize,
    mcdc_covered: usize,
    mcdc_total: usize,
    missing: Vec<String>,
    key: String,
    source_hash: String,
    logic: String,
    condition_lines: Vec<i32>,
    condition_ranges: Vec<(i32, i32, i32, i32)>,
    vectors: Vec<(String, String, bool)>,
    outcomes: Vec<(String, bool, Vec<String>)>,
}

impl SemanticReportRow {
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }
    #[must_use]
    pub const fn line(&self) -> i32 {
        self.line
    }
    #[must_use]
    pub fn expression(&self) -> &str {
        &self.expression
    }
    #[must_use]
    pub fn kind(&self) -> &str {
        &self.kind
    }
    #[must_use]
    pub const fn decisions_covered(&self) -> usize {
        self.decisions_covered
    }
    #[must_use]
    pub const fn decisions_total(&self) -> usize {
        self.decisions_total
    }
    #[must_use]
    pub const fn conditions_covered(&self) -> usize {
        self.conditions_covered
    }
    #[must_use]
    pub const fn conditions_total(&self) -> usize {
        self.conditions_total
    }
    #[must_use]
    pub const fn mcdc_covered(&self) -> usize {
        self.mcdc_covered
    }
    #[must_use]
    pub const fn mcdc_total(&self) -> usize {
        self.mcdc_total
    }
    #[must_use]
    pub fn missing(&self) -> &[String] {
        &self.missing
    }
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }
    #[must_use]
    pub fn source_hash(&self) -> &str {
        &self.source_hash
    }
    #[must_use]
    pub fn logic(&self) -> &str {
        &self.logic
    }
    #[must_use]
    pub fn condition_lines(&self) -> &[i32] {
        &self.condition_lines
    }
    #[must_use]
    pub fn condition_ranges(&self) -> &[(i32, i32, i32, i32)] {
        &self.condition_ranges
    }
    #[must_use]
    pub fn vectors(&self) -> &[(String, String, bool)] {
        &self.vectors
    }
    #[must_use]
    pub fn outcomes(&self) -> &[(String, bool, Vec<String>)] {
        &self.outcomes
    }
}

/// Aggregate semantic coverage report.
#[derive(Clone, Debug)]
pub struct SemanticReport {
    rows: Vec<SemanticReportRow>,
    decisions_covered: usize,
    decisions_total: usize,
    conditions_covered: usize,
    conditions_total: usize,
    mcdc_covered: usize,
    mcdc_total: usize,
}

impl SemanticReport {
    #[must_use]
    pub fn rows(&self) -> &[SemanticReportRow] {
        &self.rows
    }
    #[must_use]
    pub const fn decisions_covered(&self) -> usize {
        self.decisions_covered
    }
    #[must_use]
    pub const fn decisions_total(&self) -> usize {
        self.decisions_total
    }
    #[must_use]
    pub const fn conditions_covered(&self) -> usize {
        self.conditions_covered
    }
    #[must_use]
    pub const fn conditions_total(&self) -> usize {
        self.conditions_total
    }
    #[must_use]
    pub const fn mcdc_covered(&self) -> usize {
        self.mcdc_covered
    }
    #[must_use]
    pub const fn mcdc_total(&self) -> usize {
        self.mcdc_total
    }
}

fn discover_python_sources(root: &Path, output: &mut BTreeSet<PathBuf>) -> Result<(), CoreError> {
    let entries = std::fs::read_dir(root).map_err(|source| CoreError::ArtifactIo {
        path: root.to_path_buf(),
        operation: "read semantic source directory",
        source,
    })?;
    let mut entries: Vec<_> =
        entries
            .collect::<Result<_, _>>()
            .map_err(|source| CoreError::ArtifactIo {
                path: root.to_path_buf(),
                operation: "iterate semantic source directory",
                source,
            })?;
    entries.sort_by_key(std::fs::DirEntry::file_name);
    for entry in entries {
        let path = entry.path();
        let file_type = entry.file_type().map_err(|source| CoreError::ArtifactIo {
            path: path.clone(),
            operation: "inspect semantic source",
            source,
        })?;
        if file_type.is_dir() {
            discover_python_sources(&path, output)?;
        } else if file_type.is_file() && path.extension().is_some_and(|suffix| suffix == "py") {
            output.insert(path);
        }
    }
    Ok(())
}

fn regex_matches(patterns: &[Regex], value: &str) -> Result<bool, CoreError> {
    for pattern in patterns {
        if pattern
            .is_match(value)
            .map_err(|error| CoreError::InvalidInput(format!("invalid semantic filter: {error}")))?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn selected_path(
    path: &str,
    includes: Option<&crate::path::CoverageGlobSet>,
    omits: Option<&crate::path::CoverageGlobSet>,
) -> bool {
    let rendered = path.replace('\\', "/");
    includes.is_none_or(|patterns| patterns.is_match(&rendered))
        && !omits.is_some_and(|patterns| patterns.is_match(&rendered))
}

fn empty_semantic_row(definition: &DecisionDefinition) -> SemanticReportRow {
    // Persisted definitions pass the validated constructor before entering the
    // model, so this bounded logic value is the sole sizing authority.
    let count = definition.logic.condition_count();
    let mut missing = vec!["decision=true".to_owned(), "decision=false".to_owned()];
    for index in 0..count {
        missing.push(format!("condition {}=true", index + 1));
        missing.push(format!("condition {}=false", index + 1));
    }
    for index in 0..count {
        missing.push(format!("condition {} lacks independent effect", index + 1));
    }
    SemanticReportRow {
        path: definition.path.clone(),
        line: definition.line,
        expression: definition.expression.clone(),
        kind: definition.kind.clone(),
        decisions_covered: 0,
        decisions_total: 2,
        conditions_covered: 0,
        conditions_total: count * 2,
        mcdc_covered: 0,
        mcdc_total: count,
        missing,
        key: definition.key.clone(),
        source_hash: definition.source_hash.clone(),
        logic: definition.logic.render(),
        condition_lines: if definition.condition_lines.is_empty() {
            vec![definition.line; count]
        } else {
            definition.condition_lines.clone()
        },
        condition_ranges: definition.condition_ranges.clone(),
        vectors: Vec::new(),
        outcomes: Vec::new(),
    }
}

fn validate_semantic_sources<'a>(
    definitions: impl IntoIterator<Item = (&'a str, &'a str)>,
) -> Result<(), CoreError> {
    let mut validated = BTreeSet::new();
    for (path, expected) in definitions {
        if !validated.insert((path, expected)) {
            continue;
        }
        let source_path = Path::new(&path);
        match std::fs::metadata(source_path) {
            Ok(metadata) if metadata.is_file() => {}
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(source) => {
                return Err(CoreError::ArtifactIo {
                    path: source_path.to_path_buf(),
                    operation: "inspect semantic source",
                    source,
                });
            }
        }
        let source = read_python_source(source_path).map_err(|source| CoreError::ArtifactIo {
            path: source_path.to_path_buf(),
            operation: "read semantic source",
            source,
        })?;
        let mut digest = String::with_capacity(64);
        for byte in Sha256::digest(source) {
            write!(digest, "{byte:02x}").expect("writing a digest to String cannot fail");
        }
        if digest != expected {
            return Err(CoreError::InvalidInput(format!(
                "semantic data for {path} is stale; rerun collection with --decisions"
            )));
        }
    }
    Ok(())
}

/// Build a filtered report from validated semantic observations.
///
/// # Errors
///
/// Returns an error for invalid filters, unreadable source, stale definitions, or no data.
#[expect(
    clippy::too_many_lines,
    reason = "Boolean and path rows share source validation, filters, and totals"
)]
pub fn build_semantic_report(
    mut data: SemanticData,
    options: &SemanticReportOptions,
) -> Result<SemanticReport, CoreError> {
    let mut source_files = BTreeSet::new();
    for root in &options.source_roots {
        let path = PathBuf::from(root);
        let metadata = match std::fs::metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(source) => {
                return Err(CoreError::ArtifactIo {
                    path,
                    operation: "inspect semantic source root",
                    source,
                });
            }
        };
        if metadata.is_file() && path.extension().is_some_and(|suffix| suffix == "py") {
            source_files.insert(path);
        } else if metadata.is_dir() {
            discover_python_sources(&path, &mut source_files)?;
        }
    }
    for path in source_files {
        let path = path
            .canonicalize()
            .unwrap_or(path)
            .to_string_lossy()
            .into_owned();
        let source = read_python_source(&path).map_err(|source| CoreError::ArtifactIo {
            path: PathBuf::from(&path),
            operation: "read semantic source",
            source,
        })?;
        let (definitions, path_definitions) = analyze_semantic_source_data(&path, &source)?;
        for definition in definitions {
            data.definitions
                .entry(definition.key.clone())
                .or_insert(definition);
        }
        for definition in path_definitions {
            data.path_definitions
                .entry(definition.key.clone())
                .or_insert(definition);
        }
    }

    let sources = data
        .definitions
        .values()
        .map(|definition| (definition.path.as_str(), definition.source_hash.as_str()))
        .chain(
            data.path_definitions
                .values()
                .map(|definition| (definition.path.as_str(), definition.source_hash.as_str())),
        );
    validate_semantic_sources(sources)?;

    if options.optimize {
        data.definitions
            .retain(|_, definition| definition.kind != "assert");
    }

    let includes = glob_set_with_unicode_version(&options.include, options.unicode_case_version)
        .map_err(|error| {
            CoreError::InvalidInput(format!("invalid semantic include pattern: {error}"))
        })?;
    let omits = glob_set_with_unicode_version(&options.omit, options.unicode_case_version)
        .map_err(|error| {
            CoreError::InvalidInput(format!("invalid semantic omit pattern: {error}"))
        })?;
    let exclude_patterns = options
        .exclude_decisions
        .iter()
        .map(|pattern| Regex::new(pattern))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| CoreError::InvalidInput(format!("invalid semantic filter: {error}")))?;
    let context_patterns = options
        .contexts
        .iter()
        .map(|pattern| Regex::new(pattern))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| CoreError::InvalidInput(format!("invalid context filter: {error}")))?;
    let mut selected = Vec::new();
    for definition in data.definitions.values() {
        if selected_path(&definition.path, includes.as_ref(), omits.as_ref())
            && !regex_matches(&exclude_patterns, &definition.expression)?
            && !regex_matches(&exclude_patterns, &definition.kind)?
        {
            selected.push(definition.clone());
        }
    }
    let mut selected_paths = Vec::new();
    for definition in data.path_definitions.values() {
        if selected_path(&definition.path, includes.as_ref(), omits.as_ref())
            && !regex_matches(&exclude_patterns, &definition.expression)?
            && !regex_matches(&exclude_patterns, definition.kind.as_str())?
        {
            selected_paths.push(definition.clone());
        }
    }
    if selected.is_empty() && selected_paths.is_empty() {
        return Err(CoreError::InvalidInput(
            "no semantic decision data found; collect with --decisions first".to_owned(),
        ));
    }

    let filter_contexts = !context_patterns.is_empty();
    let matching_contexts = if filter_contexts {
        let all_contexts: BTreeSet<_> = data
            .observations
            .iter()
            .map(|observation| observation.context.as_str())
            .chain(
                data.path_observations
                    .iter()
                    .map(|observation| observation.context.as_str()),
            )
            .collect();
        let mut matching = Vec::new();
        for context in all_contexts {
            if regex_matches(&context_patterns, context)? {
                matching.push(context.to_owned());
            }
        }
        matching
    } else {
        Vec::new()
    };
    let selected_keys: Vec<String> = selected
        .iter()
        .map(|definition| definition.key.clone())
        .collect();
    let selected_path_keys: Vec<String> = selected_paths
        .iter()
        .map(|definition| definition.key.clone())
        .collect();
    let rust_rows: BTreeMap<_, _> =
        if selected_keys.is_empty() || (filter_contexts && matching_contexts.is_empty()) {
            BTreeMap::new()
        } else {
            data.report_records(&selected_keys, &matching_contexts)
                .into_iter()
                .map(|record| (record.0.clone(), record))
                .collect()
        };
    let rust_path_rows: BTreeMap<_, _> =
        if selected_path_keys.is_empty() || (filter_contexts && matching_contexts.is_empty()) {
            BTreeMap::new()
        } else {
            data.path_report_records(&selected_path_keys, &matching_contexts)
                .into_iter()
                .map(|record| (record.0.clone(), record))
                .collect()
        };

    let selected_keys: BTreeSet<_> = selected_keys.iter().map(String::as_str).collect();
    let selected_path_keys: BTreeSet<_> = selected_path_keys.iter().map(String::as_str).collect();
    let contexts = matching_contexts.iter().map(String::as_str).collect();
    let observations =
        if selected_keys.is_empty() || (filter_contexts && matching_contexts.is_empty()) {
            BTreeMap::new()
        } else {
            data.selected_observations(&selected_keys, &contexts)
        };
    let path_observations =
        if selected_path_keys.is_empty() || (filter_contexts && matching_contexts.is_empty()) {
            BTreeMap::new()
        } else {
            data.selected_path_observations(&selected_path_keys, &contexts)
        };
    let mut rows = Vec::new();
    for definition in selected {
        let Some(record) = rust_rows.get(&definition.key) else {
            rows.push(empty_semantic_row(&definition));
            continue;
        };
        let vectors = observations
            .get(definition.key.as_str())
            .into_iter()
            .flatten()
            .filter(|observation| {
                valid_observation(&definition.logic, &observation.vector, observation.outcome)
            })
            .map(|observation| {
                (
                    observation.context.clone(),
                    observation.vector.clone(),
                    observation.outcome,
                )
            })
            .collect();
        rows.push(SemanticReportRow {
            path: record.1.clone(),
            line: record.2,
            expression: record.3.clone(),
            kind: record.4.clone(),
            decisions_covered: record.5,
            decisions_total: record.6,
            conditions_covered: record.7,
            conditions_total: record.8,
            mcdc_covered: record.9,
            mcdc_total: record.10,
            missing: record.11.clone(),
            key: definition.key,
            source_hash: definition.source_hash,
            logic: definition.logic.render(),
            condition_lines: if definition.condition_lines.is_empty() {
                vec![definition.line; definition.logic.condition_count()]
            } else {
                definition.condition_lines
            },
            condition_ranges: definition.condition_ranges,
            vectors,
            outcomes: Vec::new(),
        });
    }
    for definition in selected_paths {
        let (covered, total, missing) = rust_path_rows.get(&definition.key).map_or(
            (
                0,
                definition.outcomes.len(),
                definition
                    .outcomes
                    .iter()
                    .map(|outcome| outcome.label.clone())
                    .collect(),
            ),
            |record| (record.5, record.6, record.7.clone()),
        );
        let mut observed: BTreeMap<usize, BTreeSet<String>> = BTreeMap::new();
        for observation in path_observations
            .get(definition.key.as_str())
            .into_iter()
            .flatten()
        {
            observed
                .entry(observation.outcome)
                .or_default()
                .insert(observation.context.clone());
        }
        rows.push(SemanticReportRow {
            path: definition.path.clone(),
            line: definition.line,
            expression: definition.expression.clone(),
            kind: definition.kind.as_str().to_owned(),
            decisions_covered: covered,
            decisions_total: total,
            conditions_covered: 0,
            conditions_total: 0,
            mcdc_covered: 0,
            mcdc_total: 0,
            missing,
            key: definition.key,
            source_hash: definition.source_hash,
            logic: String::new(),
            condition_lines: Vec::new(),
            condition_ranges: Vec::new(),
            vectors: Vec::new(),
            outcomes: definition
                .outcomes
                .iter()
                .enumerate()
                .map(|(index, outcome)| {
                    let contexts = observed.get(&index).cloned().unwrap_or_default();
                    (
                        outcome.label.clone(),
                        observed.contains_key(&index),
                        contexts.into_iter().collect(),
                    )
                })
                .collect(),
        });
    }
    rows.sort_by(|left, right| {
        (left.path.as_str(), left.line, left.key.as_str()).cmp(&(
            right.path.as_str(),
            right.line,
            right.key.as_str(),
        ))
    });
    Ok(SemanticReport {
        decisions_covered: rows.iter().map(|row| row.decisions_covered).sum(),
        decisions_total: rows.iter().map(|row| row.decisions_total).sum(),
        conditions_covered: rows.iter().map(|row| row.conditions_covered).sum(),
        conditions_total: rows.iter().map(|row| row.conditions_total).sum(),
        mcdc_covered: rows.iter().map(|row| row.mcdc_covered).sum(),
        mcdc_total: rows.iter().map(|row| row.mcdc_total).sum(),
        rows,
    })
}

fn semantic_source_text(path: &str, range: (i32, i32, i32, i32)) -> Option<String> {
    let source = read_python_source(path).ok()?;
    let bytes = source.as_bytes();
    let lines: Vec<&[u8]> = bytes.split(|byte| *byte == b'\n').collect();
    let (start_line, start_column, end_line, end_column) = range;
    let start = usize::try_from(start_line.saturating_sub(1)).ok()?;
    let end = usize::try_from(end_line).ok()?;
    let selected = lines.get(start..end)?;
    if selected.is_empty() {
        return None;
    }
    let mut selected: Vec<&[u8]> = selected.to_vec();
    let start_column = usize::try_from(start_column).ok()?;
    let end_column = usize::try_from(end_column).ok()?;
    selected[0] = selected[0].get(start_column..)?;
    if start_line == end_line {
        selected[0] = selected[0].get(..end_column.saturating_sub(start_column))?;
    } else {
        let last = selected.len().saturating_sub(1);
        selected[last] = selected[last].get(..end_column)?;
    }
    let mut text = String::new();
    for (index, line) in selected.into_iter().enumerate() {
        if index > 0 {
            text.push(' ');
        }
        text.push_str(std::str::from_utf8(line).ok()?);
    }
    Some(text.split_whitespace().collect::<Vec<_>>().join(" "))
}

fn semantic_missing_evidence(row: &SemanticReportRow, missing: &str) -> String {
    let Some(rest) = missing.strip_prefix("condition ") else {
        return missing.to_owned();
    };
    let (index, suffix) = if let Some((index, value)) = rest.split_once('=') {
        (index, format!("= {value}"))
    } else if let Some(index) = rest.strip_suffix(" lacks independent effect") {
        (index, "lacks independent effect".to_owned())
    } else {
        return missing.to_owned();
    };
    let Ok(index) = index.parse::<usize>() else {
        return missing.to_owned();
    };
    let index = index.saturating_sub(1);
    let Some(&range) = row.condition_ranges.get(index) else {
        return missing.to_owned();
    };
    let expression = semantic_source_text(&row.path, range)
        .unwrap_or_else(|| format!("condition {}", index + 1));
    let obligation = if let Some(value) = suffix.strip_prefix("= ") {
        format!("must be {value}")
    } else if suffix == "lacks independent effect" {
        "needs an independent effect pair".to_owned()
    } else {
        return missing.to_owned();
    };
    format!("'{expression}' {obligation} ({}:{})", range.0, range.1 + 1)
}

/// Quote `word` for a POSIX shell when it holds characters the shell would
/// interpret, such as the brackets of a parametrized node ID.
fn shell_word(word: &str) -> String {
    let plain = !word.is_empty()
        && word
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_-./:=@+,%".contains(character));
    if plain {
        word.to_owned()
    } else {
        format!("'{}'", word.replace('\'', r"'\''"))
    }
}

fn test_target(nodeid: &str) -> String {
    if nodeid.starts_with('-') {
        shell_word(&format!("./{nodeid}"))
    } else {
        shell_word(nodeid)
    }
}

#[must_use]
pub fn render_semantic_text(
    report: &SemanticReport,
    precision: usize,
    history_path: Option<&Path>,
) -> String {
    let mut output = String::from(
        "Name                                      Decisions  Conditions  MC/DC\n----------------------------------------  ---------  ----------  -----\n",
    );
    let context_groups = report
        .rows
        .iter()
        .map(|row| {
            row.vectors
                .iter()
                .map(|(context, _, _)| context.clone())
                .collect()
        })
        .collect();
    let history_path = history_path.unwrap_or_else(|| {
        let destination = Path::new(".vfy/test-history.sqlite3");
        let source = Path::new(".vfy/test-history.json");
        if !destination.exists() && source.is_file() {
            source
        } else {
            destination
        }
    });
    let ranked = rank_test_contexts(
        context_groups,
        &crate::read_test_history(history_path).durations,
    );
    for (row, candidates) in report.rows.iter().zip(ranked) {
        let name: String = format!("{}:{} {}", row.path, row.line, row.expression)
            .chars()
            .take(40)
            .collect();
        writeln!(
            output,
            "{name:<40}  {}/{}       {}/{}         {}/{}",
            row.decisions_covered,
            row.decisions_total,
            row.conditions_covered,
            row.conditions_total,
            row.mcdc_covered,
            row.mcdc_total,
        )
        .expect("writing a semantic report line cannot fail");
        for missing in &row.missing {
            writeln!(
                output,
                "  missing: {}",
                semantic_missing_evidence(row, missing)
            )
            .expect("writing a semantic report line cannot fail");
        }
        if !row.missing.is_empty()
            && let Some((test, duration)) = candidates.first()
        {
            let timing = duration.map_or_else(String::new, |value| format!(" ({value:.1} ms)"));
            writeln!(output, "  try: {test}{timing}")
                .expect("writing a semantic report line cannot fail");
            // A node ID runs with every engine; `PATH:LINE` targets need history
            // that only the native and pytest engines read.
            writeln!(output, "       vfy test {}", test_target(test))
                .expect("writing a semantic report line cannot fail");
        }
    }
    output.push('\n');
    writeln!(
        output,
        "Decision coverage: {:.precision$}%  Condition coverage: {:.precision$}%  MC/DC: {:.precision$}%",
        percentage(report.decisions_covered, report.decisions_total),
        percentage(report.conditions_covered, report.conditions_total),
        percentage(report.mcdc_covered, report.mcdc_total),
        precision = precision,
    )
    .expect("writing a semantic report total cannot fail");
    output
}

#[expect(
    clippy::cast_precision_loss,
    reason = "Percentages use floating-point ratios; report counts remain integers"
)]
fn percentage(covered: usize, total: usize) -> f64 {
    if total == 0 {
        100.0
    } else {
        covered as f64 * 100.0 / total as f64
    }
}

/// Encode a semantic report as the stable JSON interchange format.
///
/// # Errors
///
/// Returns an error when the stable report payload cannot be serialized.
pub fn render_semantic_json(report: &SemanticReport, pretty: bool) -> Result<String, CoreError> {
    let files: Vec<_> = report
        .rows
        .iter()
        .map(|row| {
            json!({
                "path": row.path,
                "decision": row.key,
                "source_digest": row.source_hash,
                "line": row.line,
                "kind": row.kind,
                "expression": row.expression,
                "logic": row.logic,
                "conditions_definition": row.condition_ranges.iter().enumerate().map(|(index, &(line, column, end_line, end_column))| json!({"index": index, "line": line, "column": column, "end_line": end_line, "end_column": end_column})).collect::<Vec<_>>(),
                "vectors": row.vectors.iter().map(|(context, vector, outcome)| json!({"context": context, "vector": vector, "outcome": outcome})).collect::<Vec<_>>(),
                "outcomes": row.outcomes.iter().map(|(label, covered, contexts)| json!({"label": label, "covered": covered, "contexts": contexts})).collect::<Vec<_>>(),
                "decisions": {"covered": row.decisions_covered, "total": row.decisions_total},
                "conditions": {"covered": row.conditions_covered, "total": row.conditions_total},
                "mcdc": {"covered": row.mcdc_covered, "total": row.mcdc_total},
                "missing": row.missing,
            })
        })
        .collect();
    let payload = json!({
        "schema": "vfy.semantic",
        "totals": {
            "decisions": {"covered": report.decisions_covered, "total": report.decisions_total},
            "conditions": {"covered": report.conditions_covered, "total": report.conditions_total},
            "mcdc": {"covered": report.mcdc_covered, "total": report.mcdc_total},
            "percent": {
                "decisions": percentage(report.decisions_covered, report.decisions_total),
                "conditions": percentage(report.conditions_covered, report.conditions_total),
                "mcdc": percentage(report.mcdc_covered, report.mcdc_total),
            },
        },
        "files": files,
    });
    let mut encoded = if pretty {
        serde_json::to_string_pretty(&payload)
    } else {
        serde_json::to_string(&payload)
    }
    .map_err(CoreError::Serialization)?;
    encoded.push('\n');
    Ok(encoded)
}
