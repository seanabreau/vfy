//! Coverage report models, row construction, and path and context selection.

mod error;
pub use error::ReportError;
type ReportResult<T> = Result<T, ReportError>;

use std::collections::{BTreeMap, BTreeSet};
use std::ops::{Add, AddAssign};
use std::path::Path;
use std::sync::OnceLock;

use fancy_regex::Regex;
use rayon::prelude::*;

use crate::analysis::{
    CodeRegion, ExitDescription, PythonSemantics, SourceAnalysis, SourceFailure, WithJump,
    analyze_cached, analyze_uncached_with_patterns, compile_patterns,
};
use crate::data::{Arc, CoverageData, ReportInput};
use crate::path::{absolute_path, discover_python_source_files, glob_path};

/// Return measurements matching the requested context patterns and file paths.
///
/// # Errors
/// Returns an invalid-input error for a pattern that cannot be compiled or matched.
pub fn report_inputs_for_contexts(
    data: &CoverageData,
    contexts: &[String],
    selected_paths: &BTreeSet<String>,
) -> ReportResult<Vec<ReportInput>> {
    let patterns = compile_contexts(contexts)?;
    report_inputs_for_context_patterns(data, &patterns, Some(selected_paths))
}

fn report_inputs_for_context_patterns(
    data: &CoverageData,
    patterns: &[Regex],
    selected_paths: Option<&BTreeSet<String>>,
) -> ReportResult<Vec<ReportInput>> {
    report_inputs_with_detail(data, patterns, selected_paths, ReportContextDetail::Include)
}

fn report_inputs_with_detail(
    data: &CoverageData,
    patterns: &[Regex],
    selected_paths: Option<&BTreeSet<String>>,
    detail: ReportContextDetail,
) -> ReportResult<Vec<ReportInput>> {
    let retain_path = |path: &str| selected_paths.is_none_or(|selected| selected.contains(path));
    if patterns.is_empty() {
        if detail == ReportContextDetail::Omit {
            return Ok(data.report_union_inputs(retain_path));
        }
        return data.report_inputs_filtered(retain_path, |_, _| Ok::<bool, ReportError>(true));
    }
    let context_paths: BTreeSet<_> = selected_paths.map_or_else(
        || data.context_path_identities().collect(),
        |selected| {
            data.context_path_identities_for_paths(selected)
                .into_iter()
                .collect()
        },
    );
    let mut selected_context_paths = BTreeSet::new();
    for (context, path) in context_paths {
        if matches_context(patterns, context).map_err(|error| with_report_path(path, error))? {
            selected_context_paths.insert((context, path));
        }
    }
    data.report_inputs_filtered(retain_path, |path, context| {
        Ok::<bool, ReportError>(selected_context_paths.contains(&(context, path)))
    })
}

/// Select measured and discovered report paths before evidence materialization.
///
/// # Errors
/// Returns an invalid-input error for an invalid include or omit pattern.
pub fn select_report_paths(
    paths: &[String],
    include: &[String],
    omit: &[String],
    source_roots: &[String],
) -> ReportResult<BTreeSet<String>> {
    select_report_paths_with_unicode_version(
        paths,
        include,
        omit,
        source_roots,
        crate::path::UnicodeCaseVersion::default(),
    )
}

fn select_report_paths_with_unicode_version(
    paths: &[String],
    include: &[String],
    omit: &[String],
    source_roots: &[String],
    unicode_case_version: crate::path::UnicodeCaseVersion,
) -> ReportResult<BTreeSet<String>> {
    let includes = crate::path::glob_set_with_unicode_version(include, unicode_case_version)
        .map_err(ReportError::InvalidPattern)?;
    let omits = crate::path::glob_set_with_unicode_version(omit, unicode_case_version)
        .map_err(ReportError::InvalidPattern)?;
    let relative_paths = paths.iter().any(|path| Path::new(path).is_relative());
    let working_root = std::env::current_dir()
        .ok()
        .map(|path| absolute_path(&path));
    let mut candidates: BTreeSet<String> = paths.iter().cloned().collect();
    for root in source_roots {
        let root = absolute_path(Path::new(root));
        for path in discover_python_source_files(&root) {
            let path = path.to_string_lossy().into_owned();
            let path = if relative_paths {
                working_root
                    .as_ref()
                    .and_then(|root| Path::new(&path).strip_prefix(root).ok())
                    .map_or(path.clone(), glob_path)
            } else {
                path
            };
            candidates.insert(path);
        }
    }
    Ok(candidates
        .into_iter()
        .filter(|path| {
            let rendered = glob_path(Path::new(path));
            let relative = working_root
                .as_ref()
                .and_then(|root| Path::new(path).strip_prefix(root).ok())
                .map(glob_path);
            let matches = |patterns: &crate::path::CoverageGlobSet| {
                patterns.is_match(&rendered)
                    || relative
                        .as_ref()
                        .is_some_and(|relative| patterns.is_match(relative))
            };
            includes.as_ref().is_none_or(&matches) && !omits.as_ref().is_some_and(matches)
        })
        .collect())
}

/// Select custom reporter identities and resolve their measurement keys.
///
/// # Errors
/// Returns an invalid-input error for an invalid include or omit pattern.
pub fn select_plugin_report_paths(
    paths: &[String],
    include: &[String],
    omit: &[String],
    path_semantics: ReportPathSemantics,
) -> ReportResult<Vec<(String, String)>> {
    Ok(select_report_paths(paths, include, omit, &[])?
        .into_iter()
        .map(|path| {
            let data_filename = report_filenames(&path, path_semantics).0;
            (path, data_filename)
        })
        .collect())
}

/// One file's measured and analyzed evidence for report rendering.
#[derive(Clone)]
pub struct ReportRow {
    pub(crate) path: String,
    report_file_paths: Option<ReportFilePaths>,
    pub(crate) executable: BTreeSet<i32>,
    pub(crate) covered: BTreeSet<i32>,
    pub(crate) excluded: BTreeSet<i32>,
    pub(crate) possible_arcs: BTreeSet<Arc>,
    pub(crate) arc_exit_descriptions: BTreeMap<Arc, ExitDescription>,
    pub(crate) covered_arcs: BTreeSet<Arc>,
    pub(crate) executed_arcs: BTreeSet<Arc>,
    pub(crate) contexts_by_line: BTreeMap<i32, Vec<String>>,
    partial_override: Option<usize>,
    source_override: Option<String>,
    pub(crate) regions: Vec<CodeRegion>,
    source_line_count: i32,
    pub(crate) has_arcs: bool,
    source_failure: Option<SourceFailure>,
}

#[derive(Clone)]
struct ReportFilePaths {
    data: String,
    relative: String,
    display: Option<String>,
}

/// Filename policy for ordinary Python file reporters.
#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub enum ReportPathSemantics {
    /// Keep the stored filenames used by the low-level report API.
    #[default]
    Stored,
    /// Canonicalize reporter filenames and query absolute data keys.
    Canonical,
    /// Preserve reporter filenames and query relative data keys.
    Relative,
}

impl From<Option<bool>> for ReportPathSemantics {
    fn from(relative_files: Option<bool>) -> Self {
        match relative_files {
            None => Self::Stored,
            Some(false) => Self::Canonical,
            Some(true) => Self::Relative,
        }
    }
}

/// Statement and branch counts for one file or a whole report.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReportCounts {
    statements: usize,
    covered: usize,
    missing: usize,
    excluded: usize,
    branches: usize,
    covered_branches: usize,
    missing_branches: usize,
    partial_branches: usize,
}

impl ReportCounts {
    fn line_percent_value(self) -> f64 {
        percentage(self.covered, self.statements)
    }

    fn percent_value(self) -> f64 {
        percentage(
            self.covered + self.covered_branches,
            self.statements + self.branches,
        )
    }

    fn branch_percent_value(self) -> f64 {
        percentage(self.covered_branches, self.branches)
    }
}

impl From<&ReportRow> for ReportCounts {
    fn from(row: &ReportRow) -> Self {
        let statements = row.executable.len();
        let covered = row.covered.len();
        let branches = row.possible_arcs.len();
        let covered_branches = row.covered_arcs.len();
        Self {
            statements,
            covered,
            missing: statements - covered,
            excluded: row.excluded.len(),
            branches,
            covered_branches,
            missing_branches: branches - covered_branches,
            partial_branches: row.partial_count(),
        }
    }
}

impl AddAssign for ReportCounts {
    fn add_assign(&mut self, rhs: Self) {
        self.statements += rhs.statements;
        self.covered += rhs.covered;
        self.missing += rhs.missing;
        self.excluded += rhs.excluded;
        self.branches += rhs.branches;
        self.covered_branches += rhs.covered_branches;
        self.missing_branches += rhs.missing_branches;
        self.partial_branches += rhs.partial_branches;
    }
}

impl Add for ReportCounts {
    type Output = Self;

    fn add(mut self, rhs: Self) -> Self {
        self += rhs;
        self
    }
}

impl std::iter::Sum for ReportCounts {
    fn sum<I: Iterator<Item = Self>>(iter: I) -> Self {
        iter.fold(Self::default(), Add::add)
    }
}

impl ReportCounts {
    #[must_use]
    pub const fn statements(&self) -> usize {
        self.statements
    }
    #[must_use]
    pub const fn covered(&self) -> usize {
        self.covered
    }
    #[must_use]
    pub const fn missing(&self) -> usize {
        self.missing
    }
    #[must_use]
    pub const fn excluded(&self) -> usize {
        self.excluded
    }
    #[must_use]
    pub const fn branches(&self) -> usize {
        self.branches
    }
    #[must_use]
    pub const fn covered_branches(&self) -> usize {
        self.covered_branches
    }
    #[must_use]
    pub const fn missing_branches(&self) -> usize {
        self.missing_branches
    }
    #[must_use]
    pub const fn partial_branches(&self) -> usize {
        self.partial_branches
    }
    #[must_use]
    pub fn line_percent(&self) -> f64 {
        self.line_percent_value()
    }
    #[must_use]
    pub fn percent(&self) -> f64 {
        self.percent_value()
    }
    #[must_use]
    pub fn branch_percent(&self) -> f64 {
        self.branch_percent_value()
    }
}

impl ReportRow {
    fn statement_count(&self) -> usize {
        self.executable.len()
    }

    fn covered_count(&self) -> usize {
        self.covered.len()
    }

    fn missing_count(&self) -> usize {
        self.statement_count() - self.covered_count()
    }

    fn branch_count(&self) -> usize {
        self.possible_arcs.len()
    }

    fn covered_branch_count(&self) -> usize {
        self.covered_arcs.len()
    }

    fn missing_branch_count(&self) -> usize {
        self.branch_count() - self.covered_branch_count()
    }

    fn missing_set(&self) -> BTreeSet<i32> {
        self.executable.difference(&self.covered).copied().collect()
    }

    fn missing_arcs_set(&self) -> BTreeSet<Arc> {
        self.possible_arcs
            .difference(&self.covered_arcs)
            .copied()
            .collect()
    }

    fn line_percent_value(&self) -> f64 {
        percentage(self.covered_count(), self.statement_count())
    }

    fn percent_value(&self) -> f64 {
        percentage(
            self.covered_count() + self.covered_branch_count(),
            self.statement_count() + self.branch_count(),
        )
    }

    fn branch_percent_value(&self) -> f64 {
        percentage(self.covered_branch_count(), self.branch_count())
    }

    fn partial_count(&self) -> usize {
        if let Some(value) = self.partial_override {
            return value;
        }
        partial_branch_count(
            &self.possible_arcs,
            &self.covered_arcs,
            &self.executable,
            &self.covered,
        )
    }
}

fn partial_branch_count(
    possible_arcs: &BTreeSet<Arc>,
    covered_arcs: &BTreeSet<Arc>,
    executable: &BTreeSet<i32>,
    covered_lines: &BTreeSet<i32>,
) -> usize {
    possible_arcs
        .difference(covered_arcs)
        .filter(|(origin, _)| !executable.contains(origin) || covered_lines.contains(origin))
        .count()
}

impl ReportRow {
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }

    #[must_use]
    pub fn data_filename(&self) -> &str {
        self.report_file_paths
            .as_ref()
            .map_or(&self.path, |paths| &paths.data)
    }

    /// Return the filename shown by reports that support custom file reporters.
    #[must_use]
    pub fn report_filename(&self) -> &str {
        self.report_file_paths
            .as_ref()
            .and_then(|paths| paths.display.as_deref())
            .unwrap_or(&self.path)
    }

    /// Attach a plug-in's measurement key and displayed filename to its source row.
    #[must_use]
    pub fn with_plugin_report_filenames(
        mut self,
        data_filename: String,
        relative_filename: String,
    ) -> Self {
        self.report_file_paths = Some(ReportFilePaths {
            data: data_filename,
            display: Some(relative_filename.clone()),
            relative: relative_filename,
        });
        self
    }

    #[must_use]
    pub fn source_missing(&self) -> bool {
        self.source_failure.is_some()
    }

    pub fn source_error_kind(&self) -> Option<&str> {
        self.source_failure.as_ref().map(SourceFailure::kind)
    }

    pub fn source_error_detail(&self) -> Option<&str> {
        self.source_failure.as_ref().and_then(SourceFailure::detail)
    }

    #[must_use]
    pub fn statements(&self) -> usize {
        self.statement_count()
    }

    #[must_use]
    pub fn covered(&self) -> usize {
        self.covered_count()
    }

    #[must_use]
    pub fn missing(&self) -> BTreeSet<i32> {
        self.missing_set()
    }

    #[must_use]
    pub fn executable_lines(&self) -> BTreeSet<i32> {
        self.executable.clone()
    }

    #[must_use]
    pub fn covered_lines(&self) -> BTreeSet<i32> {
        self.covered.clone()
    }

    #[must_use]
    pub fn excluded_lines(&self) -> BTreeSet<i32> {
        self.excluded.clone()
    }

    #[must_use]
    pub fn branches(&self) -> usize {
        self.possible_arcs.len()
    }

    #[must_use]
    pub fn covered_branches(&self) -> usize {
        self.covered_arcs.len()
    }

    #[must_use]
    pub fn possible_arcs(&self) -> BTreeSet<Arc> {
        self.possible_arcs.clone()
    }

    #[must_use]
    pub fn covered_arcs(&self) -> BTreeSet<Arc> {
        self.covered_arcs.clone()
    }

    #[must_use]
    pub fn missing_arcs(&self) -> BTreeSet<Arc> {
        self.missing_arcs_set()
    }

    #[must_use]
    pub fn contexts_by_line(&self) -> BTreeMap<i32, Vec<String>> {
        self.contexts_by_line.clone()
    }

    #[must_use]
    pub fn line_percent(&self) -> f64 {
        self.line_percent_value()
    }

    #[must_use]
    pub fn percent(&self) -> f64 {
        self.percent_value()
    }

    #[must_use]
    pub fn branch_percent(&self) -> f64 {
        self.branch_percent_value()
    }

    #[must_use]
    pub fn partial_branches(&self) -> usize {
        self.partial_count()
    }
}

#[expect(
    clippy::cast_precision_loss,
    reason = "percentages are floating-point ratios of counts that stay integers"
)]
fn percentage(covered: usize, total: usize) -> f64 {
    if total == 0 {
        100.0
    } else {
        covered as f64 * 100.0 / total as f64
    }
}

fn unavailable_analysis(error: &std::io::Error) -> SourceAnalysis {
    SourceAnalysis {
        executable: Vec::new(),
        excluded: Vec::new(),
        possible_arcs: Vec::new(),
        arc_exit_descriptions: BTreeMap::new(),
        no_branch: Vec::new(),
        source_failure: Some(SourceFailure::from_io_error(error)),
        regions: Vec::new(),
        source_line_count: 0,
        line_map: BTreeMap::new(),
        report_excluded: Vec::new(),
        with_jumps: Vec::new(),
    }
}

fn fix_with_jumps(mut arcs: BTreeSet<Arc>, jumps: &[WithJump]) -> BTreeSet<Arc> {
    loop {
        let mut replacements = Vec::new();
        for jump in jumps {
            replacements.extend(
                arcs.iter()
                    .filter(|(origin, destination)| {
                        *destination == jump.header
                            && (jump.body_start..=jump.body_end).contains(origin)
                    })
                    .map(|arc| (*arc, (arc.0, jump.after), (jump.header, jump.after))),
            );
        }
        if replacements.is_empty() {
            return arcs;
        }
        let mut changed = false;
        for (measured, corrected, redundant) in replacements {
            changed |= arcs.remove(&measured);
            arcs.remove(&redundant);
            arcs.insert(corrected);
        }
        if !changed {
            return arcs;
        }
    }
}

fn compile_contexts(contexts: &[String]) -> ReportResult<Vec<Regex>> {
    contexts
        .iter()
        .map(|pattern| {
            Regex::new(pattern).map_err(|error| {
                ReportError::InvalidInput(format!("invalid context pattern {pattern:?}: {error}"))
            })
        })
        .collect()
}

fn matches_context(patterns: &[Regex], context: &str) -> ReportResult<bool> {
    for pattern in patterns {
        match pattern.is_match(context) {
            Ok(true) => return Ok(true),
            Ok(false) => {}
            Err(error) => {
                return Err(ReportError::InvalidInput(format!(
                    "context pattern {:?} failed while matching context {context:?}: {error}",
                    pattern.as_str()
                )));
            }
        }
    }
    Ok(false)
}

fn with_report_path(path: &str, error: ReportError) -> ReportError {
    match error {
        ReportError::InvalidInput(message) => {
            ReportError::InvalidInput(format!("failed to build report row for {path}: {message}"))
        }
        other => other,
    }
}

fn covered_branch_arcs(
    possible: &BTreeSet<Arc>,
    observed: &BTreeSet<Arc>,
    no_branch: &BTreeSet<i32>,
) -> BTreeSet<Arc> {
    possible
        .intersection(observed)
        .copied()
        .chain(
            possible
                .iter()
                .filter(|(origin, _)| no_branch.contains(origin))
                .copied(),
        )
        .collect()
}

fn observed_lines(
    lines: Vec<i32>,
    arcs: &[Arc],
    line_contexts: &BTreeMap<String, BTreeSet<i32>>,
    selected_contexts: &BTreeSet<String>,
    filter_contexts: bool,
    line_map: &BTreeMap<i32, i32>,
) -> BTreeSet<i32> {
    let observed = if filter_contexts {
        selected_contexts
            .iter()
            .flat_map(|context| line_contexts.get(context).into_iter().flatten().copied())
            .collect()
    } else {
        let mut observed: BTreeSet<i32> = lines.into_iter().collect();
        if observed.is_empty() {
            observed.extend(
                arcs.iter()
                    .flat_map(|(start, end)| [*start, *end])
                    .filter(|line| *line > 0),
            );
        }
        observed
    };
    observed
        .into_iter()
        .map(|line| line_map.get(&line).copied().unwrap_or(line))
        .collect()
}

fn select_contexts(input: &ReportInput, patterns: &[Regex]) -> ReportResult<BTreeSet<String>> {
    let all_contexts: BTreeSet<&str> = input
        .3
        .iter()
        .map(|(context, _)| context.as_str())
        .chain(input.4.iter().map(|(context, _)| context.as_str()))
        .collect();
    let mut selected = BTreeSet::new();
    for context in all_contexts {
        if patterns.is_empty() || matches_context(patterns, context)? {
            selected.insert(context.to_owned());
        }
    }
    Ok(selected)
}

fn select_contexts_across_files(
    files: &[ReportInput],
    patterns: &[Regex],
) -> ReportResult<BTreeMap<String, BTreeSet<String>>> {
    let mut selected_by_path: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let context_paths: BTreeSet<_> = files
        .iter()
        .flat_map(|(path, _, _, line_contexts, arc_contexts)| {
            line_contexts
                .iter()
                .map(move |(context, _)| (context.as_str(), path.as_str()))
                .chain(
                    arc_contexts
                        .iter()
                        .map(move |(context, _)| (context.as_str(), path.as_str())),
                )
        })
        .collect();
    for (context, path) in context_paths {
        if matches_context(patterns, context).map_err(|error| with_report_path(path, error))? {
            selected_by_path
                .entry(path.to_owned())
                .or_default()
                .insert(context.to_owned());
        }
    }
    Ok(selected_by_path)
}

fn normalize_arc((start, end): Arc, line_map: &BTreeMap<i32, i32>) -> Arc {
    (
        line_map.get(&start).copied().unwrap_or(start),
        line_map.get(&end).copied().unwrap_or(end),
    )
}

fn build_row(
    input: ReportInput,
    analysis: SourceAnalysis,
    has_arcs: bool,
    context_patterns: &[Regex],
) -> ReportResult<ReportRow> {
    let selected_contexts = select_contexts(&input, context_patterns)?;
    Ok(build_row_with_contexts(
        input,
        analysis,
        has_arcs,
        &selected_contexts,
        !context_patterns.is_empty(),
        ReportContextDetail::Include,
    ))
}

fn build_row_with_contexts(
    input: ReportInput,
    analysis: SourceAnalysis,
    has_arcs: bool,
    selected_contexts: &BTreeSet<String>,
    filter_contexts: bool,
    detail: ReportContextDetail,
) -> ReportRow {
    let (path, lines, arcs, context_lines, context_arcs) = input;
    let line_contexts: BTreeMap<String, BTreeSet<i32>> = context_lines
        .into_iter()
        .map(|(context, lines)| (context, lines.into_iter().collect()))
        .collect();
    let arc_contexts: BTreeMap<String, BTreeSet<Arc>> = context_arcs
        .into_iter()
        .map(|(context, arcs)| (context, arcs.into_iter().collect()))
        .collect();
    let observed = observed_lines(
        lines,
        &arcs,
        &line_contexts,
        selected_contexts,
        filter_contexts,
        &analysis.line_map,
    );
    let raw_observed_arcs: BTreeSet<Arc> = if filter_contexts {
        selected_contexts
            .iter()
            .flat_map(|context| arc_contexts.get(context).into_iter().flatten().copied())
            .collect()
    } else {
        arcs.into_iter().collect()
    };
    let observed_arcs: BTreeSet<Arc> = fix_with_jumps(raw_observed_arcs, &analysis.with_jumps)
        .into_iter()
        .map(|arc| normalize_arc(arc, &analysis.line_map))
        .collect();
    // Missing or invalid source has no executable lines. Report policy raises
    // or omits the row.
    let executable: BTreeSet<i32> = analysis.executable.into_iter().collect();
    let covered = executable.intersection(&observed).copied().collect();
    let possible_arcs: BTreeSet<Arc> = if has_arcs {
        analysis.possible_arcs.into_iter().collect()
    } else {
        BTreeSet::new()
    };
    let no_branch: BTreeSet<i32> = analysis.no_branch.into_iter().collect();
    let executed_arcs = possible_arcs
        .intersection(&observed_arcs)
        .copied()
        .collect();
    let covered_arcs = covered_branch_arcs(&possible_arcs, &observed_arcs, &no_branch);
    let mut contexts_by_line: BTreeMap<i32, BTreeSet<String>> = BTreeMap::new();
    if detail == ReportContextDetail::Include {
        for context in selected_contexts {
            for line in line_contexts
                .get(context)
                .into_iter()
                .flatten()
                .copied()
                .chain(
                    arc_contexts
                        .get(context)
                        .into_iter()
                        .flatten()
                        .flat_map(|(start, end)| [*start, *end])
                        .filter(|line| *line > 0),
                )
            {
                let line = analysis.line_map.get(&line).copied().unwrap_or(line);
                contexts_by_line
                    .entry(line)
                    .and_modify(|contexts| {
                        if !contexts.contains(context) {
                            contexts.insert(context.to_owned());
                        }
                    })
                    .or_insert_with(|| BTreeSet::from([context.to_owned()]));
            }
        }
    }
    ReportRow {
        path,
        report_file_paths: None,
        executable,
        covered,
        excluded: analysis.report_excluded.into_iter().collect(),
        possible_arcs,
        arc_exit_descriptions: analysis.arc_exit_descriptions,
        covered_arcs,
        executed_arcs,
        contexts_by_line: contexts_by_line
            .into_iter()
            .map(|(line, contexts)| (line, contexts.into_iter().collect()))
            .collect(),
        partial_override: None,
        source_override: None,
        regions: analysis.regions,
        source_line_count: analysis.source_line_count,
        has_arcs,
        source_failure: analysis.source_failure,
    }
}

/// Settings for one source-report operation.
#[derive(Clone, Copy)]
pub struct ReportOptions<'a> {
    pub has_arcs: bool,
    pub include: &'a [String],
    pub omit: &'a [String],
    pub excludes: &'a [String],
    pub contexts: &'a [String],
    pub partials: &'a [String],
    pub source_roots: &'a [String],
    pub path_semantics: ReportPathSemantics,
    pub requested_paths: Option<&'a [String]>,
    pub python_semantics: PythonSemantics,
    pub unicode_case_version: crate::path::UnicodeCaseVersion,
    pub context_detail: ReportContextDetail,
}

/// Whether report rows retain the contexts that observed each line.
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub enum ReportContextDetail {
    /// Keep per-line contexts for reports that render them.
    #[default]
    Include,
    /// Aggregate measurements only; context filters still apply.
    Omit,
}

/// Analyze and aggregate measured and configured source files.
///
/// # Errors
/// Returns an invalid-input error for patterns that cannot be compiled or matched.
pub fn build_report_rows(
    mut files: Vec<ReportInput>,
    options: ReportOptions<'_>,
) -> ReportResult<Vec<ReportRow>> {
    let paths = options.requested_paths.filter(|paths| !paths.is_empty());
    let measured_paths: Vec<_> = files.iter().map(|(path, ..)| path.clone()).collect();
    let selected_paths = select_python_report_paths(paths.unwrap_or(&measured_paths), options)?;
    let (analysis_patterns, context_patterns, partial_patterns) = compile_report_patterns(options)?;
    files = resolve_report_inputs(files, selected_paths, options);
    build_selected_report_rows(
        files,
        options,
        &analysis_patterns,
        &context_patterns,
        &partial_patterns,
    )
}

/// Filter coverage by file and context, then analyze the selected source files.
///
/// # Errors
/// Returns an invalid-input error for patterns that cannot be compiled or matched.
pub fn build_report_rows_from_data(
    data: &CoverageData,
    options: ReportOptions<'_>,
) -> ReportResult<Vec<ReportRow>> {
    let measured_paths = data.measured_files();
    let selected_paths = select_python_report_paths(
        options
            .requested_paths
            .filter(|paths| !paths.is_empty())
            .unwrap_or(&measured_paths),
        options,
    )?;
    let (analysis_patterns, context_patterns, partial_patterns) = compile_report_patterns(options)?;
    let lookup_paths: BTreeSet<_> = selected_paths
        .iter()
        .map(|path| report_filenames(path, options.path_semantics).0)
        .collect();
    let restrict_paths = (!options.include.is_empty()
        || !options.omit.is_empty()
        || options.requested_paths.is_some())
    .then_some(&lookup_paths);
    let mut files = report_inputs_with_detail(
        data,
        &context_patterns,
        restrict_paths,
        options.context_detail,
    )?;
    files = resolve_report_inputs(files, selected_paths, options);
    build_selected_report_rows(files, options, &analysis_patterns, &[], &partial_patterns)
}

fn relative_report_filename(filename: &str) -> String {
    std::env::current_dir()
        .ok()
        .and_then(|root| {
            Path::new(filename)
                .strip_prefix(root)
                .ok()
                .map(|relative| relative.to_string_lossy().into_owned())
        })
        .unwrap_or_else(|| filename.to_owned())
}

fn select_python_report_paths(
    paths: &[String],
    options: ReportOptions<'_>,
) -> ReportResult<BTreeSet<String>> {
    if options.path_semantics != ReportPathSemantics::Canonical {
        return select_report_paths_with_unicode_version(
            paths,
            options.include,
            options.omit,
            options.source_roots,
            options.unicode_case_version,
        );
    }
    let candidates = select_report_paths(paths, &[], &[], options.source_roots)?;
    let reporters: BTreeMap<_, _> = candidates
        .into_iter()
        .map(|path| {
            let reporter = report_filenames(&path, options.path_semantics).1;
            (path, reporter)
        })
        .collect();
    let selected = select_report_paths_with_unicode_version(
        &reporters.values().cloned().collect::<Vec<_>>(),
        options.include,
        options.omit,
        &[],
        options.unicode_case_version,
    )?;
    Ok(reporters
        .into_iter()
        .filter_map(|(path, reporter)| selected.contains(&reporter).then_some(path))
        .collect())
}

fn report_filenames(path: &str, semantics: ReportPathSemantics) -> (String, String) {
    let reporter = match semantics {
        ReportPathSemantics::Canonical => absolute_path(Path::new(path))
            .to_string_lossy()
            .into_owned(),
        ReportPathSemantics::Stored | ReportPathSemantics::Relative => path.to_owned(),
    };
    let data = if semantics == ReportPathSemantics::Relative {
        relative_report_filename(&reporter)
    } else {
        reporter.clone()
    };
    (data, reporter)
}

fn resolve_report_inputs(
    mut files: Vec<ReportInput>,
    selected: BTreeSet<String>,
    options: ReportOptions<'_>,
) -> Vec<ReportInput> {
    if options.path_semantics == ReportPathSemantics::Stored && options.requested_paths.is_none() {
        files.retain(|(path, ..)| selected.contains(path));
        add_unmeasured_paths(&mut files, selected);
        return files;
    }
    let mut inputs: BTreeMap<_, _> = files
        .into_iter()
        .map(|file| (file.0.clone(), file))
        .collect();
    let paths: Vec<_> = options
        .requested_paths
        .filter(|paths| !paths.is_empty())
        .map_or_else(|| selected.iter().cloned().collect(), <[String]>::to_vec);
    let mut reporters: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for path in paths.into_iter().filter(|path| selected.contains(path)) {
        let (data, reporter) = report_filenames(&path, options.path_semantics);
        reporters.entry(data).or_default().push(reporter);
    }
    let mut result = Vec::new();
    for (data, mut filenames) in reporters {
        let Some(last) = filenames.pop() else {
            continue;
        };
        let mut input = inputs
            .remove(&data)
            .unwrap_or_else(|| (data, Vec::new(), Vec::new(), Vec::new(), Vec::new()));
        // Only repeated reporter aliases need copies of the selected evidence.
        for filename in filenames {
            let mut alias = input.clone();
            alias.0 = filename;
            result.push(alias);
        }
        input.0 = last;
        result.push(input);
    }
    result
}

fn add_unmeasured_paths(files: &mut Vec<ReportInput>, selected_paths: BTreeSet<String>) {
    let mut known: BTreeSet<_> = files.iter().map(|(path, ..)| path.clone()).collect();
    for path in selected_paths {
        if known.insert(path.clone()) {
            files.push((path, Vec::new(), Vec::new(), Vec::new(), Vec::new()));
        }
    }
}

fn build_selected_report_rows(
    mut files: Vec<ReportInput>,
    options: ReportOptions<'_>,
    analysis_patterns: &[Regex],
    context_patterns: &[Regex],
    partial_patterns: &[Regex],
) -> ReportResult<Vec<ReportRow>> {
    let ReportOptions {
        has_arcs,
        excludes,
        partials,
        python_semantics,
        ..
    } = options;
    files.sort_unstable_by(|left, right| left.0.cmp(&right.0));
    let mut selected_by_path = (!context_patterns.is_empty())
        .then(|| select_contexts_across_files(&files, context_patterns))
        .transpose()?;
    let mut pending = files.into_iter().peekable();
    let mut files = Vec::new();
    while let Some(input) = pending.next() {
        let repeated = pending.peek().is_some_and(|next| next.0 == input.0);
        let selected_contexts = selected_by_path.as_mut().map_or_else(
            || select_contexts(&input, context_patterns),
            |selected| {
                Ok(if repeated {
                    selected.get(&input.0).cloned().unwrap_or_default()
                } else {
                    selected.remove(&input.0).unwrap_or_default()
                })
            },
        )?;
        files.push((input, selected_contexts));
    }
    let worker_count = rayon::current_num_threads();
    // Fancy regex clones share VM scratch pools, so each worker compiles its own
    // patterns the first time it analyzes a file.
    let worker_patterns: Vec<_> = if worker_count > 1 && files.len() > worker_count {
        (0..worker_count).map(|_| OnceLock::new()).collect()
    } else {
        Vec::new()
    };
    let rows: Vec<ReportResult<ReportRow>> = files
        .into_par_iter()
        .map(|(input, selected_contexts)| {
            let path = input.0.clone();
            let analysis = match crate::read_python_source(&input.0) {
                Ok(source) => analyze_cached(
                    &source,
                    excludes,
                    partials,
                    has_arcs,
                    python_semantics,
                    || {
                        let local = rayon::current_thread_index()
                            .and_then(|index| worker_patterns.get(index));
                        let (analysis_patterns, partial_patterns): (&[Regex], &[Regex]) =
                            if let Some(slot) = local {
                                let (analysis, partial) = slot
                                    .get_or_init(|| -> Result<_, crate::CoreError> {
                                        Ok((
                                            compile_patterns(excludes)?,
                                            compile_patterns(partials)?,
                                        ))
                                    })
                                    .as_ref()
                                    .map_err(|error| {
                                        crate::CoreError::InvalidInput(error.to_string())
                                    })?;
                                (analysis, partial)
                            } else {
                                (analysis_patterns, partial_patterns)
                            };
                        analyze_uncached_with_patterns(
                            &source,
                            analysis_patterns,
                            partial_patterns,
                            has_arcs,
                            python_semantics,
                        )
                    },
                )
                .map_err(|error| {
                    ReportError::InvalidInput(format!("failed to analyze {path}: {error}"))
                })?,
                Err(error) => unavailable_analysis(&error),
            };
            let mut row = build_row_with_contexts(
                input,
                analysis,
                has_arcs,
                &selected_contexts,
                !context_patterns.is_empty(),
                options.context_detail,
            );
            if options.path_semantics != ReportPathSemantics::Stored {
                row.report_file_paths = Some(ReportFilePaths {
                    data: report_filenames(&row.path, options.path_semantics).0,
                    relative: relative_report_filename(&row.path),
                    display: None,
                });
            }
            Ok(row)
        })
        .collect();
    rows.into_iter()
        .filter_map(skip_non_python_parse_failure)
        .collect()
}

fn skip_non_python_parse_failure(
    result: ReportResult<ReportRow>,
) -> Option<ReportResult<ReportRow>> {
    match result {
        Ok(row)
            if matches!(
                row.source_failure.as_ref(),
                Some(SourceFailure::NotPython { .. })
            ) && !should_be_python(&row.path) =>
        {
            None
        }
        other => Some(other),
    }
}

fn should_be_python(path: &str) -> bool {
    Path::new(path)
        .extension()
        .is_none_or(|extension| extension.to_string_lossy().starts_with("py"))
}

fn compile_report_patterns(
    options: ReportOptions<'_>,
) -> ReportResult<(Vec<Regex>, Vec<Regex>, Vec<Regex>)> {
    let analysis_patterns = compile_patterns(options.excludes)
        .map_err(|error| ReportError::InvalidInput(error.to_string()))?;
    let context_patterns = compile_contexts(options.contexts)?;
    let partial_patterns = compile_patterns(options.partials)
        .map_err(|error| ReportError::InvalidInput(error.to_string()))?;
    Ok((analysis_patterns, context_patterns, partial_patterns))
}

/// Source and measurements supplied by one plug-in reporter.
pub struct PluginReportInput<'a> {
    pub file: ReportInput,
    pub has_arcs: bool,
    pub executable: Vec<i32>,
    pub excluded: Vec<i32>,
    pub possible_arcs: Vec<Arc>,
    pub no_branch: Vec<i32>,
    pub contexts: &'a [String],
    pub source: String,
}

impl PluginReportInput<'_> {
    /// Retain branch origins identified by the custom reporter's exit counts.
    #[must_use]
    pub fn with_exit_counts(mut self, exit_counts: &BTreeMap<i32, i32>) -> Self {
        self.possible_arcs
            .retain(|(origin, _)| exit_counts.get(origin).is_some_and(|count| *count > 1));
        self
    }
}

/// Aggregate a plug-in's source and measurements.
///
/// # Errors
/// Returns an invalid-input error for a context pattern that cannot be compiled or matched.
pub fn build_plugin_report_row(input: PluginReportInput<'_>) -> ReportResult<ReportRow> {
    let PluginReportInput {
        file,
        has_arcs,
        executable,
        excluded,
        possible_arcs,
        no_branch,
        contexts,
        source,
    } = input;
    let path = file.0.clone();
    let context_patterns = compile_contexts(contexts)?;
    let mut row = build_row(
        file,
        SourceAnalysis {
            executable,
            report_excluded: excluded.clone(),
            excluded,
            possible_arcs,
            arc_exit_descriptions: BTreeMap::new(),
            no_branch,
            source_failure: None,
            regions: Vec::new(),
            source_line_count: i32::try_from(source.lines().count()).unwrap_or(i32::MAX),
            line_map: BTreeMap::new(),
            with_jumps: Vec::new(),
        },
        has_arcs,
        &context_patterns,
    )
    .map_err(|error| with_report_path(&path, error))?;
    row.source_override = Some(source);
    Ok(row)
}

fn total_counts(rows: &[&ReportRow]) -> ReportCounts {
    rows.iter().map(|row| ReportCounts::from(&**row)).sum()
}

/// Sum the counts of `rows`.
#[must_use]
pub fn report_totals(rows: &[&ReportRow]) -> ReportCounts {
    total_counts(rows)
}

/// Display rows and aggregate counts from the complete report input.
pub struct ReportDisplay<'a> {
    rows: Vec<&'a ReportRow>,
    totals: ReportCounts,
    has_arcs: bool,
    /// Files hidden by `skip_covered`, then by `skip_empty`, as Coverage.py counts them.
    skipped_covered: usize,
    skipped_empty: usize,
}

impl<'a> ReportDisplay<'a> {
    #[must_use]
    pub fn all(rows: &[&'a ReportRow]) -> Self {
        Self {
            rows: rows.to_vec(),
            totals: total_counts(rows),
            has_arcs: rows.iter().any(|row| row.has_arcs),
            skipped_covered: 0,
            skipped_empty: 0,
        }
    }

    /// Select display rows without changing aggregate counts or branch mode.
    ///
    /// # Errors
    /// Returns an invalid-input error for an unsupported sort key.
    pub fn selected(
        rows: &[&'a ReportRow],
        skip_covered: bool,
        skip_empty: bool,
        sort: Option<&str>,
    ) -> ReportResult<Self> {
        let indices = select_report_indices(rows, skip_covered, skip_empty, sort)?;
        let complete = |row: &ReportRow| row.missing_count() + row.missing_branch_count() == 0;
        let skipped_covered = rows
            .iter()
            .filter(|row| skip_covered && complete(row))
            .count();
        let skipped_empty = rows
            .iter()
            .filter(|row| skip_empty && row.statement_count() == 0)
            .filter(|row| !(skip_covered && complete(row)))
            .count();
        Ok(Self {
            rows: indices.into_iter().map(|index| rows[index]).collect(),
            totals: total_counts(rows),
            has_arcs: rows.iter().any(|row| row.has_arcs),
            skipped_covered,
            skipped_empty,
        })
    }
}

/// Filter report rows and return their indices in the requested sort order.
///
/// Names use natural numeric order; numeric columns break ties by displayed
/// filename. A leading `-` reverses the sorted result, including ties.
///
/// # Errors
/// Returns an invalid-input error for an unsupported sort key.
pub fn select_report_indices(
    rows: &[&ReportRow],
    skip_covered: bool,
    skip_empty: bool,
    sort: Option<&str>,
) -> ReportResult<Vec<usize>> {
    let mut selected: Vec<usize> = (0..rows.len())
        .filter(|&index| !skip_empty || rows[index].statement_count() > 0)
        .filter(|&index| {
            !skip_covered || rows[index].missing_count() + rows[index].missing_branch_count() > 0
        })
        .collect();
    let requested = sort.unwrap_or("name");
    let descending = requested.starts_with('-');
    let key = requested
        .strip_prefix('-')
        .unwrap_or(requested)
        .to_ascii_lowercase();
    let display_name_cmp = |left: usize, right: usize| {
        let left_name = render::terminal_filename(rows[left]);
        let right_name = render::terminal_filename(rows[right]);
        left_name.cmp(&right_name)
    };
    match key.as_str() {
        "name" => selected.sort_by(|&left, &right| {
            let left_name = render::terminal_filename(rows[left]);
            let right_name = render::terminal_filename(rows[right]);
            render::human_cmp(&left_name, &right_name)
        }),
        "stmts" => selected.sort_by(|&left, &right| {
            rows[left]
                .statement_count()
                .cmp(&rows[right].statement_count())
                .then_with(|| display_name_cmp(left, right))
        }),
        "miss" => selected.sort_by(|&left, &right| {
            rows[left]
                .missing_count()
                .cmp(&rows[right].missing_count())
                .then_with(|| display_name_cmp(left, right))
        }),
        "branch" => selected.sort_by(|&left, &right| {
            rows[left]
                .branch_count()
                .cmp(&rows[right].branch_count())
                .then_with(|| display_name_cmp(left, right))
        }),
        "brpart" => selected.sort_by(|&left, &right| {
            rows[left]
                .partial_count()
                .cmp(&rows[right].partial_count())
                .then_with(|| display_name_cmp(left, right))
        }),
        "cover" => selected.sort_by(|&left, &right| {
            rows[left]
                .percent_value()
                .total_cmp(&rows[right].percent_value())
                .then_with(|| display_name_cmp(left, right))
        }),
        _ => {
            return Err(ReportError::InvalidInput(format!(
                "invalid report sort {requested:?}: use name, stmts, miss, branch, brpart, or cover, optionally prefixed with `-`"
            )));
        }
    }
    if descending {
        selected.reverse();
    }
    Ok(selected)
}

pub mod render;

#[cfg(test)]
mod tests;
