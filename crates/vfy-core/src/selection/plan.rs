use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::analysis::{PythonSemantics, analyze_with_patterns, compile_patterns};
use crate::data::{CoverageData, LineShift};
use crate::evidence::{SparseArcs, SparseLines};
use crate::{CoreError, absolute_path, glob_path, read_python_source};

use super::scope::{DiffHunk, changed_function_spans};
use super::static_dependents;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SelectionMode {
    Safe,
    Balanced,
    Aggressive,
}

impl SelectionMode {
    /// Parse the public selection policy name.
    ///
    /// # Errors
    /// Returns an error for an unsupported policy.
    pub fn parse(value: &str) -> Result<Self, CoreError> {
        match value {
            "safe" => Ok(Self::Safe),
            "balanced" => Ok(Self::Balanced),
            "aggressive" => Ok(Self::Aggressive),
            _ => Err(CoreError::InvalidInput(
                "mode must be safe, balanced, or aggressive".to_owned(),
            )),
        }
    }
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Safe => "safe",
            Self::Balanced => "balanced",
            Self::Aggressive => "aggressive",
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ChangedFile {
    pub path: String,
    pub added: BTreeSet<i32>,
    pub hunks: Vec<DiffHunk>,
    /// True when a hunk header or line could not be parsed.
    pub malformed_hunks: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TestObservation {
    pub files: BTreeMap<String, BTreeSet<i32>>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct FileSelectionEvidence {
    pub executable_added: BTreeSet<i32>,
    pub affected_functions: usize,
    pub affected_branches: usize,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct LineSelectionRecord {
    pub context: String,
    pub path: String,
    pub lines: BTreeSet<i32>,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ArcSelectionRecord {
    pub context: String,
    pub path: String,
    pub arcs: Vec<(i32, i32)>,
}

pub struct SelectionEvidence {
    pub mode: SelectionMode,
    pub history_path: PathBuf,
    pub source_roots: Vec<PathBuf>,
    pub patch: String,
    pub location: Option<String>,
    pub source_identity: String,
    pub branch_mode: bool,
    /// The baseline holds semantic decision evidence, which replay cannot move
    /// between line positions; selection then keeps file-level evidence.
    pub semantic_evidence: bool,
    pub python_semantics: PythonSemantics,
    pub durations: BTreeMap<String, f64>,
    pub lines: Vec<LineSelectionRecord>,
    pub arcs: Vec<ArcSelectionRecord>,
}

/// Planner inputs independent of the measurement storage representation.
pub struct SelectionInput {
    pub mode: SelectionMode,
    pub history_path: PathBuf,
    pub source_roots: Vec<PathBuf>,
    pub patch: String,
    pub location: Option<String>,
    pub source_identity: String,
    pub branch_mode: bool,
    pub semantic_evidence: bool,
    pub python_semantics: PythonSemantics,
    pub durations: BTreeMap<String, f64>,
}

pub struct SelectionRequest {
    pub mode: SelectionMode,
    pub all_tests: BTreeSet<String>,
    pub durations: BTreeMap<String, f64>,
    pub changed: Vec<ChangedFile>,
    pub observations: BTreeMap<String, TestObservation>,
    pub file_evidence: BTreeMap<String, FileSelectionEvidence>,
    pub static_files: BTreeSet<String>,
    pub static_reasons: Vec<String>,
    /// Changed files whose edits stay inside function bodies, mapped to the
    /// baseline line spans of those bodies.
    pub function_spans: BTreeMap<String, Vec<(i32, i32)>>,
    pub location: Option<(String, i32)>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SelectionPlan {
    pub mode: SelectionMode,
    pub selected: Vec<String>,
    pub all_tests: Vec<String>,
    pub durations: BTreeMap<String, f64>,
    pub affected_files: Vec<String>,
    pub affected_functions: usize,
    pub affected_branches: usize,
    pub selected_seconds: Option<f64>,
    pub full_seconds: Option<f64>,
    pub fallback_full: bool,
    pub reasons: Vec<String>,
    pub location: Option<String>,
    /// Changed files whose unselected tests keep baseline evidence, with the
    /// hunks that move those tests' baseline lines.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub scoped_files: BTreeMap<String, Vec<LineShift>>,
    /// Test files a selected run must collect, as node-ID file parts. Every
    /// other file keeps its baseline test identities without being imported.
    /// `None` collects the complete suite.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub collection_files: Option<BTreeSet<String>>,
}

impl SelectionPlan {
    pub(super) fn select_full(&mut self) {
        self.fallback_full = true;
        self.selected.clone_from(&self.all_tests);
        self.scoped_files.clear();
        self.collection_files = None;
        self.full_seconds = seconds(&self.all_tests, &self.durations);
        self.selected_seconds = self.full_seconds;
    }

    /// Run the complete suite, recording why a subset cannot be trusted.
    pub fn fall_back_to_full(&mut self, reason: String) {
        self.reasons.push(reason);
        self.select_full();
    }
}

fn normal_path(path: &str) -> String {
    glob_path(&absolute_path(Path::new(path)))
}

fn same_path(left: &str, right: &str) -> bool {
    normal_path(left) == normal_path(right)
        || normal_path(left).ends_with(&format!("/{right}"))
        || normal_path(right).ends_with(&format!("/{left}"))
}

#[must_use]
pub fn test_context(context: &str) -> Option<&str> {
    let (test, phase) = context.rsplit_once('|')?;
    matches!(phase, "setup" | "run" | "teardown").then_some(test)
}

pub fn sort_tests(tests: &mut [String], durations: &BTreeMap<String, f64>) {
    tests.sort_by(|left, right| {
        durations
            .get(left)
            .unwrap_or(&f64::INFINITY)
            .total_cmp(durations.get(right).unwrap_or(&f64::INFINITY))
            .then_with(|| left.cmp(right))
    });
}

#[must_use]
pub fn rank_test_contexts(
    context_groups: Vec<Vec<String>>,
    durations: &BTreeMap<String, f64>,
) -> Vec<Vec<(String, Option<f64>)>> {
    context_groups
        .into_iter()
        .map(|contexts| {
            let mut tests: Vec<String> = contexts
                .iter()
                .filter_map(|context| test_context(context))
                .map(str::to_owned)
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect();
            sort_tests(&mut tests, durations);
            tests
                .into_iter()
                .map(|test| {
                    let duration = durations.get(&test).copied();
                    (test, duration)
                })
                .collect()
        })
        .collect()
}

fn parse_range(range: &str) -> Option<(i32, i32)> {
    let mut values = range.split(',');
    let start = values.next()?.parse().ok()?;
    let count = values.next().map_or(Some(1), |value| value.parse().ok())?;
    values.next().is_none().then_some((start, count))
}

fn parse_hunk_header(header: &str) -> Option<DiffHunk> {
    let mut fields = header.strip_prefix("@@ ")?.split(' ');
    let (old_start, old_count) = parse_range(fields.next()?.strip_prefix('-')?)?;
    let (new_start, new_count) = parse_range(fields.next()?.strip_prefix('+')?)?;
    Some(DiffHunk {
        old_start,
        old_count,
        new_start,
        new_count,
        ..DiffHunk::default()
    })
}

struct HunkCursor {
    old_line: i32,
    new_line: i32,
    old_remaining: i32,
    new_remaining: i32,
}

impl HunkCursor {
    fn complete(&self) -> bool {
        self.old_remaining == 0 && self.new_remaining == 0
    }

    fn record_line(&mut self, file: &mut ChangedFile, line: &str) {
        let Some(hunk) = file.hunks.last_mut() else {
            return;
        };
        if self.old_remaining <= 0 && self.new_remaining <= 0 {
            return;
        }
        let Some((kind, text)) = line.split_at_checked(line.len().min(1)) else {
            file.malformed_hunks = true;
            return;
        };
        let text = text.strip_suffix('\r').unwrap_or(text).to_owned();
        match kind {
            "-" => {
                hunk.removed.insert(self.old_line);
                hunk.old_text.push(text);
                self.old_line = self.old_line.saturating_add(1);
                self.old_remaining -= 1;
            }
            "+" => {
                file.added.insert(self.new_line);
                hunk.added.insert(self.new_line);
                hunk.new_text.push(text);
                self.new_line = self.new_line.saturating_add(1);
                self.new_remaining -= 1;
            }
            " " => {
                hunk.old_text.push(text.clone());
                hunk.new_text.push(text);
                self.old_line = self.old_line.saturating_add(1);
                self.new_line = self.new_line.saturating_add(1);
                self.old_remaining -= 1;
                self.new_remaining -= 1;
            }
            "\\" => {}
            _ => file.malformed_hunks = true,
        }
    }
}

fn finish_hunk(cursor: &mut Option<HunkCursor>, file: Option<&mut ChangedFile>) {
    if let Some(position) = cursor.take()
        && !position.complete()
        && let Some(file) = file
    {
        file.malformed_hunks = true;
    }
}

fn git_patch_path(header: &str) -> Option<String> {
    if !header.starts_with('"') {
        return Some(header.split('\t').next()?.to_owned());
    }
    let mut bytes = header.as_bytes()[1..].iter().copied();
    let mut decoded = Vec::new();
    while let Some(byte) = bytes.next() {
        match byte {
            b'"' => {
                let rest: Vec<u8> = bytes.collect();
                if !rest.is_empty() && rest[0] != b'\t' {
                    return None;
                }
                return String::from_utf8(decoded).ok();
            }
            b'\\' => {
                let escaped = bytes.next()?;
                decoded.push(match escaped {
                    b'a' => 7,
                    b'b' => 8,
                    b't' => b'\t',
                    b'n' => b'\n',
                    b'v' => 11,
                    b'f' => 12,
                    b'r' => b'\r',
                    b'\\' | b'"' => escaped,
                    b'0'..=b'3' => {
                        let second = bytes.next()?.checked_sub(b'0')?;
                        let third = bytes.next()?.checked_sub(b'0')?;
                        if second > 7 || third > 7 {
                            return None;
                        }
                        (escaped - b'0') * 64 + second * 8 + third
                    }
                    _ => return None,
                });
            }
            _ => decoded.push(byte),
        }
    }
    None
}

fn unsupported_section(result: &mut BTreeMap<String, ChangedFile>, root: &Path, header: &str) {
    let path = glob_path(&absolute_path(&root.join(header)));
    result.insert(
        path.clone(),
        ChangedFile {
            path,
            malformed_hunks: true,
            ..ChangedFile::default()
        },
    );
}

/// Parse a Git patch with filenames relative to the repository root.
#[must_use]
pub fn parse_diff(diff: &str) -> Vec<ChangedFile> {
    let root = super::git::repository_root(Path::new("."))
        .unwrap_or_else(|_| absolute_path(Path::new(".")));
    parse_diff_at(diff, &root)
}

pub(super) fn parse_diff_at(diff: &str, root: &Path) -> Vec<ChangedFile> {
    let mut result: BTreeMap<String, ChangedFile> = BTreeMap::new();
    let mut current = None;
    let mut cursor: Option<HunkCursor> = None;
    let mut pending_section = None;
    for line in diff.lines() {
        if line.starts_with("diff --git ") {
            finish_hunk(
                &mut cursor,
                current.as_ref().and_then(|path| result.get_mut(path)),
            );
            if let Some(header) = pending_section.take() {
                unsupported_section(&mut result, root, header);
            }
            current = None;
            cursor = None;
            pending_section = Some(line);
            continue;
        }
        if let Some(header) = line.strip_prefix("+++ ")
            && cursor.as_ref().is_none_or(HunkCursor::complete)
        {
            cursor = None;
            current = None;
            let path = git_patch_path(header);
            if let Some(file_path) = path.as_deref().and_then(|path| path.strip_prefix("b/")) {
                pending_section = None;
                let normalized = glob_path(&absolute_path(&root.join(file_path)));
                result.insert(
                    normalized.clone(),
                    ChangedFile {
                        path: normalized.clone(),
                        ..ChangedFile::default()
                    },
                );
                current = Some(normalized);
            } else {
                unsupported_section(&mut result, root, pending_section.take().unwrap_or(header));
            }
            continue;
        }
        let Some(file) = current.as_ref().and_then(|path| result.get_mut(path)) else {
            continue;
        };
        if line.starts_with("@@") {
            finish_hunk(&mut cursor, Some(file));
            if let Some(hunk) = parse_hunk_header(line) {
                cursor = Some(HunkCursor {
                    old_line: if hunk.old_count == 0 {
                        hunk.old_start.saturating_add(1)
                    } else {
                        hunk.old_start
                    },
                    new_line: hunk.new_start,
                    old_remaining: hunk.old_count,
                    new_remaining: hunk.new_count,
                });
                file.hunks.push(hunk);
            } else {
                file.malformed_hunks = true;
            }
            continue;
        }
        if let Some(position) = cursor.as_mut() {
            position.record_line(file, line);
        }
    }
    finish_hunk(
        &mut cursor,
        current.as_ref().and_then(|path| result.get_mut(path)),
    );
    if let Some(header) = pending_section {
        unsupported_section(&mut result, root, header);
    }
    result.into_values().collect()
}

fn selection_roots(source_roots: &[PathBuf]) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = source_roots
        .iter()
        .map(|root| absolute_path(root))
        .collect();
    let current = absolute_path(Path::new("."));
    if !roots.contains(&current) {
        roots.push(current);
    }
    roots
}

fn observed_tests(lines: &[LineSelectionRecord], arcs: &[ArcSelectionRecord]) -> BTreeSet<String> {
    owned_tests(
        lines
            .iter()
            .map(|record| record.context.as_str())
            .chain(arcs.iter().map(|record| record.context.as_str())),
    )
}

fn owned_tests<'a>(contexts: impl Iterator<Item = &'a str>) -> BTreeSet<String> {
    contexts
        .filter_map(test_context)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(str::to_owned)
        .collect()
}

fn record_path(path: &str) -> String {
    if Path::new(path).is_absolute() {
        glob_path(Path::new(path))
    } else {
        normal_path(path)
    }
}

fn recorded_lines<'a>(
    lines: &'a [LineSelectionRecord],
    arcs: &'a [ArcSelectionRecord],
) -> impl Iterator<Item = (&'a str, &'a str, Vec<i32>)> {
    lines
        .iter()
        .map(|record| {
            (
                record.context.as_str(),
                record.path.as_str(),
                record.lines.iter().copied().collect(),
            )
        })
        .chain(arcs.iter().map(|record| {
            (
                record.context.as_str(),
                record.path.as_str(),
                record
                    .arcs
                    .iter()
                    .flat_map(|(from, to)| [*from, *to])
                    .filter(|line| *line > 0)
                    .collect(),
            )
        }))
}

trait EntryEvidence {
    fn minimum(&self) -> Option<i32>;
    fn contains(&self, line: i32) -> bool;
}

impl EntryEvidence for BTreeSet<i32> {
    fn minimum(&self) -> Option<i32> {
        self.first().copied()
    }

    fn contains(&self, line: i32) -> bool {
        self.contains(&line)
    }
}

impl EntryEvidence for SparseLines {
    fn minimum(&self) -> Option<i32> {
        self.min_line()
    }

    fn contains(&self, line: i32) -> bool {
        self.contains(line)
    }
}

fn arc_minimum(arcs: impl Iterator<Item = (i32, i32)>) -> Option<i32> {
    arcs.flat_map(|(from, to)| [from, to])
        .filter(|line| *line > 0)
        .min()
}

fn arc_contains(arcs: impl Iterator<Item = (i32, i32)>, line: i32) -> bool {
    line > 0
        && arcs
            .into_iter()
            .any(|(from, to)| from == line || to == line)
}

impl EntryEvidence for Vec<(i32, i32)> {
    fn minimum(&self) -> Option<i32> {
        arc_minimum(self.iter().copied())
    }

    fn contains(&self, line: i32) -> bool {
        arc_contains(self.iter().copied(), line)
    }
}

impl EntryEvidence for SparseArcs {
    fn minimum(&self) -> Option<i32> {
        arc_minimum(self.iter())
    }

    fn contains(&self, line: i32) -> bool {
        arc_contains(self.iter(), line)
    }
}

struct EntryPartition<'a> {
    context: &'a str,
    path: &'a str,
    evidence: &'a dyn EntryEvidence,
}

fn importer_contexts<'a, I, F>(records: F) -> BTreeSet<String>
where
    I: Iterator<Item = EntryPartition<'a>>,
    F: Fn() -> I,
{
    let mut entries: BTreeMap<String, i32> = BTreeMap::new();
    let mut paths: BTreeMap<&str, String> = BTreeMap::new();
    for partition in records() {
        if let Some(first) = partition.evidence.minimum() {
            let path = paths
                .entry(partition.path)
                .or_insert_with(|| record_path(partition.path));
            let entry = entries.entry(path.clone()).or_insert(first);
            *entry = (*entry).min(first);
        }
    }
    records()
        .filter(|partition| {
            paths
                .get(partition.path)
                .and_then(|path| entries.get(path))
                .is_some_and(|entry| partition.evidence.contains(*entry))
        })
        .map(|partition| partition.context.to_owned())
        .collect()
}

fn record_partitions<'a>(
    lines: &'a [LineSelectionRecord],
    arcs: &'a [ArcSelectionRecord],
) -> impl Iterator<Item = EntryPartition<'a>> + 'a {
    lines
        .iter()
        .map(|record| EntryPartition {
            context: &record.context,
            path: &record.path,
            evidence: &record.lines,
        })
        .chain(arcs.iter().map(|record| EntryPartition {
            context: &record.context,
            path: &record.path,
            evidence: &record.arcs,
        }))
}

fn compact_partitions(data: &CoverageData) -> impl Iterator<Item = EntryPartition<'_>> {
    data.line_evidence()
        .map(|(path, context, lines)| EntryPartition {
            context,
            path,
            evidence: lines,
        })
        .chain(
            data.arc_evidence()
                .map(|(path, context, arcs)| EntryPartition {
                    context,
                    path,
                    evidence: arcs,
                }),
        )
}

fn compact_records_for_paths(
    data: &CoverageData,
    paths: &BTreeSet<String>,
) -> (Vec<LineSelectionRecord>, Vec<ArcSelectionRecord>) {
    let stored_paths: BTreeSet<String> = data
        .context_path_identities()
        .map(|(_, path)| path)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter(|path| paths.contains(&record_path(path)))
        .map(str::to_owned)
        .collect();
    let lines = data
        .line_evidence_for_paths(&stored_paths)
        .map(|(path, context, lines)| LineSelectionRecord {
            context: context.to_owned(),
            path: path.to_owned(),
            lines: lines.iter().collect(),
        })
        .collect();
    let arcs = data
        .arc_evidence_for_paths(&stored_paths)
        .map(|(path, context, arcs)| ArcSelectionRecord {
            context: context.to_owned(),
            path: path.to_owned(),
            arcs: arcs.iter().collect(),
        })
        .collect();
    (lines, arcs)
}

/// Find changed files whose edits can reach tests only through function calls.
///
/// A body is excluded when a context other than a test call executed it: an
/// import, collection, or fixture setup can share its result with tests that
/// never execute the body. A test context that executed the first recorded
/// line of any file imported that module, so its calls may also have happened
/// at import time.
fn body_only_changes(
    changed: &[ChangedFile],
    lines: &[LineSelectionRecord],
    arcs: &[ArcSelectionRecord],
    importer_contexts: impl FnOnce() -> BTreeSet<String>,
) -> BTreeMap<String, Vec<(i32, i32)>> {
    let mut candidates = BTreeMap::new();
    for file in changed {
        if file.malformed_hunks
            || Path::new(&file.path)
                .extension()
                .is_none_or(|extension| extension != "py")
        {
            continue;
        }
        let Ok(source) = read_python_source(Path::new(&file.path)) else {
            continue;
        };
        if let Some(spans) = changed_function_spans(&source, &file.hunks) {
            candidates.insert(file.path.clone(), spans);
        }
    }
    if candidates.is_empty() {
        return candidates;
    }
    let importers = importer_contexts();
    for (context, path, observed) in recorded_lines(lines, arcs) {
        let path = record_path(path);
        let Some(spans) = candidates.get(&path) else {
            continue;
        };
        let test_call = context
            .rsplit_once('|')
            .is_some_and(|(_, phase)| phase == "run")
            && !importers.contains(context);
        if !test_call
            && observed.iter().any(|line| {
                spans
                    .iter()
                    .any(|(start, end)| (*start..=*end).contains(line))
            })
        {
            candidates.remove(&path);
        }
    }
    candidates
}

fn observations(
    lines: &[LineSelectionRecord],
    arcs: &[ArcSelectionRecord],
    relevant: &BTreeSet<String>,
) -> BTreeMap<String, TestObservation> {
    let mut result: BTreeMap<String, TestObservation> = BTreeMap::new();
    for record in lines {
        let Some(test) = test_context(&record.context) else {
            continue;
        };
        let path = record_path(&record.path);
        if relevant.contains(&path) {
            result
                .entry(test.to_owned())
                .or_default()
                .files
                .entry(path)
                .or_default()
                .extend(&record.lines);
        }
    }
    for record in arcs {
        let Some(test) = test_context(&record.context) else {
            continue;
        };
        let path = record_path(&record.path);
        if relevant.contains(&path) {
            result
                .entry(test.to_owned())
                .or_default()
                .files
                .entry(path)
                .or_default()
                .extend(
                    record
                        .arcs
                        .iter()
                        .flat_map(|(from, to)| [*from, *to])
                        .filter(|line| *line > 0),
                );
        }
    }
    result
}

fn changes_test_discovery(path: &str) -> bool {
    let path = Path::new(path);
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    name == "conftest.py"
        || name.starts_with("test_")
        || name.ends_with("_test.py")
        || path.components().any(|part| part.as_os_str() == "tests")
}

pub(super) fn seconds(tests: &[String], durations: &BTreeMap<String, f64>) -> Option<f64> {
    tests
        .iter()
        .map(|test| durations.get(test).copied())
        .collect::<Option<Vec<_>>>()
        // Float `Sum` starts at -0.0; adding 0.0 keeps an empty selection
        // from printing "-0.00s".
        .map(|values| values.into_iter().sum::<f64>() / 1000.0 + 0.0)
}

#[must_use]
#[expect(
    clippy::too_many_lines,
    reason = "selection policy is one ordered fallback decision"
)]
pub fn plan_selection(request: SelectionRequest) -> SelectionPlan {
    let mut all_tests: Vec<String> = request
        .all_tests
        .iter()
        .cloned()
        .chain(request.durations.keys().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    sort_tests(&mut all_tests, &request.durations);
    if let Some((path, line)) = &request.location {
        let mut matching: Vec<String> = request
            .observations
            .iter()
            .filter(|(_, observation)| {
                observation
                    .files
                    .iter()
                    .any(|(candidate, lines)| same_path(candidate, path) && lines.contains(line))
            })
            .map(|(test, _)| test.clone())
            .collect();
        sort_tests(&mut matching, &request.durations);
        let selected: Vec<String> = matching.first().cloned().into_iter().collect();
        return SelectionPlan {
            mode: request.mode,
            selected_seconds: seconds(&selected, &request.durations),
            full_seconds: seconds(&matching, &request.durations),
            selected,
            all_tests: matching,
            durations: request.durations,
            affected_files: Vec::new(),
            affected_functions: 0,
            affected_branches: 0,
            fallback_full: false,
            reasons: Vec::new(),
            location: Some(format!("{path}:{line}")),
            scoped_files: BTreeMap::new(),
            collection_files: None,
        };
    }
    if request.changed.is_empty() {
        return SelectionPlan {
            mode: request.mode,
            selected: Vec::new(),
            full_seconds: seconds(&all_tests, &request.durations),
            all_tests,
            durations: request.durations,
            affected_files: Vec::new(),
            affected_functions: 0,
            affected_branches: 0,
            selected_seconds: Some(0.0),
            fallback_full: true,
            reasons: vec!["no changed files were found; running the full suite".to_owned()],
            location: None,
            scoped_files: BTreeMap::new(),
            collection_files: None,
        };
    }
    let mut direct = BTreeSet::new();
    let mut measured = BTreeSet::new();
    let mut affected_functions = 0;
    let mut affected_branches = 0;
    for file in &request.changed {
        if request.function_spans.contains_key(&file.path) {
            measured.insert(file.path.clone());
        }
        if let Some(evidence) = request.file_evidence.get(&file.path) {
            if !evidence.executable_added.is_empty() {
                measured.insert(file.path.clone());
            }
            affected_functions += evidence.affected_functions;
            affected_branches += evidence.affected_branches;
        }
        let spans = request.function_spans.get(&file.path);
        for (test, observation) in &request.observations {
            let Some(lines) = observation.files.get(&file.path) else {
                continue;
            };
            let reached = spans.map_or_else(
                || {
                    request.mode == SelectionMode::Safe
                        || lines.intersection(&file.added).next().is_some()
                },
                |spans| {
                    spans
                        .iter()
                        .any(|(start, end)| lines.range(start..=end).next().is_some())
                },
            );
            if reached {
                direct.insert(test.clone());
            }
        }
        if direct.is_empty()
            && let Some(evidence) = request.file_evidence.get(&file.path)
            && !evidence.executable_added.is_empty()
        {
            for (test, observation) in &request.observations {
                if observation.files.get(&file.path).is_some_and(|lines| {
                    lines
                        .intersection(&evidence.executable_added)
                        .next()
                        .is_some()
                }) {
                    direct.insert(test.clone());
                }
            }
        }
    }
    let mut static_tests = BTreeSet::new();
    for test in &all_tests {
        let node_path = normal_path(test.split("::").next().unwrap_or(test));
        if request.static_files.contains(&node_path)
            || request.observations.get(test).is_some_and(|observation| {
                observation
                    .files
                    .keys()
                    .any(|path| request.static_files.contains(path))
            })
        {
            static_tests.insert(test.clone());
        }
    }
    let selected_set = if request.mode == SelectionMode::Aggressive {
        direct
    } else {
        direct.union(&static_tests).cloned().collect()
    };
    let mut reasons = request.static_reasons;
    if request.changed.iter().any(|file| file.malformed_hunks) {
        reasons.push("a changed patch section could not be parsed".to_owned());
    }
    let mut fallback_full = !reasons.is_empty();
    if all_tests.is_empty() {
        fallback_full = true;
        reasons.push(
            "no test contexts are available; run a baseline with --cov-context=test".to_owned(),
        );
    }
    if request.mode == SelectionMode::Safe && measured.len() != request.changed.len() {
        fallback_full = true;
        reasons.push(
            "at least one changed file is not a Python source file that vfy can map to tests"
                .to_owned(),
        );
    }
    if request.mode == SelectionMode::Safe
        && request
            .changed
            .iter()
            .any(|file| changes_test_discovery(&file.path))
    {
        fallback_full = true;
        reasons.push("test discovery or fixtures changed".to_owned());
    }
    if selected_set.is_empty() {
        fallback_full = true;
        reasons.push("no historical test reaches the changed code".to_owned());
    }
    let mut selected = if fallback_full {
        all_tests.clone()
    } else {
        selected_set.into_iter().collect()
    };
    sort_tests(&mut selected, &request.durations);
    let scoped_files = if fallback_full {
        BTreeMap::new()
    } else {
        request
            .changed
            .iter()
            .filter(|file| request.function_spans.contains_key(&file.path))
            .map(|file| {
                let shifts = file
                    .hunks
                    .iter()
                    .map(|hunk| LineShift {
                        old_start: hunk.old_start,
                        old_count: hunk.old_count,
                        new_count: hunk.new_count,
                    })
                    .collect();
                (file.path.clone(), shifts)
            })
            .collect()
    };
    SelectionPlan {
        mode: request.mode,
        selected_seconds: seconds(&selected, &request.durations),
        full_seconds: seconds(&all_tests, &request.durations),
        selected,
        all_tests,
        durations: request.durations,
        affected_files: request
            .changed
            .iter()
            .map(|file| file.path.clone())
            .collect(),
        affected_functions,
        affected_branches,
        fallback_full,
        reasons,
        location: None,
        scoped_files,
        collection_files: None,
    }
}

fn changed_file_evidence(
    changed: &[ChangedFile],
    branch_mode: bool,
    python_semantics: PythonSemantics,
) -> Result<BTreeMap<String, FileSelectionEvidence>, CoreError> {
    let patterns = compile_patterns(&[])?;
    let mut file_evidence = BTreeMap::new();
    for file in changed {
        let Ok(source) = read_python_source(Path::new(&file.path)) else {
            continue;
        };
        let analysis = analyze_with_patterns(&source, &patterns, branch_mode, python_semantics)
            .map_err(|error| {
                CoreError::InvalidInput(format!("failed to analyze {}: {error}", file.path))
            })?;
        let executable: BTreeSet<i32> = analysis.executable.iter().copied().collect();
        file_evidence.insert(
            file.path.clone(),
            FileSelectionEvidence {
                executable_added: file.added.intersection(&executable).copied().collect(),
                affected_functions: analysis
                    .regions
                    .iter()
                    .filter(|region| region.lines.intersection(&file.added).next().is_some())
                    .count(),
                affected_branches: analysis
                    .possible_arcs
                    .iter()
                    .filter(|(origin, _)| file.added.contains(origin))
                    .count(),
            },
        );
    }
    Ok(file_evidence)
}

fn build_selection_plan_with_records(
    input: SelectionInput,
    all_tests: BTreeSet<String>,
    importer_contexts: impl FnOnce() -> BTreeSet<String>,
    records_for: impl Fn(&BTreeSet<String>) -> (Vec<LineSelectionRecord>, Vec<ArcSelectionRecord>),
) -> Result<SelectionPlan, CoreError> {
    if let Some(location) = input.location {
        let (path, line) = location.rsplit_once(':').ok_or_else(|| {
            CoreError::InvalidInput(
                "location must be PATH:LINE with a positive line number".to_owned(),
            )
        })?;
        let line = line
            .parse::<i32>()
            .ok()
            .filter(|line| *line > 0)
            .ok_or_else(|| {
                CoreError::InvalidInput("location line must be a positive integer".to_owned())
            })?;
        let relevant = BTreeSet::from([normal_path(path)]);
        let (lines, arcs) = records_for(&relevant);
        return Ok(plan_selection(SelectionRequest {
            mode: input.mode,
            all_tests,
            durations: input.durations,
            changed: Vec::new(),
            observations: observations(&lines, &arcs, &relevant),
            file_evidence: BTreeMap::new(),
            static_files: BTreeSet::new(),
            static_reasons: Vec::new(),
            function_spans: BTreeMap::new(),
            location: Some((path.to_owned(), line)),
        }));
    }
    let changed = parse_diff(&input.patch);
    let changed_paths: BTreeSet<String> = changed.iter().map(|file| file.path.clone()).collect();
    let changed_records = (input.mode == SelectionMode::Safe && !input.semantic_evidence)
        .then(|| records_for(&changed_paths));
    let function_spans = if let Some((lines, arcs)) = &changed_records {
        body_only_changes(&changed, lines, arcs, importer_contexts)
    } else {
        BTreeMap::new()
    };
    let module_paths: BTreeSet<String> = changed_paths
        .iter()
        .filter(|path| !function_spans.contains_key(*path))
        .cloned()
        .collect();
    let roots = selection_roots(&input.source_roots);
    let graph_path = input
        .history_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("import-graph.json");
    let static_result = if input.mode == SelectionMode::Aggressive || module_paths.is_empty() {
        super::ImportGraphResult::default()
    } else {
        static_dependents(&module_paths, &roots, &graph_path, &input.source_identity)
    };
    let relevant = changed_paths
        .union(&static_result.dependents)
        .cloned()
        .collect();
    let (lines, arcs) = if let Some((mut lines, mut arcs)) = changed_records {
        let dependent_paths = static_result
            .dependents
            .difference(&changed_paths)
            .cloned()
            .collect();
        let (more_lines, more_arcs) = records_for(&dependent_paths);
        lines.extend(more_lines);
        arcs.extend(more_arcs);
        (lines, arcs)
    } else {
        records_for(&relevant)
    };
    let observations = observations(&lines, &arcs, &relevant);
    let file_evidence = changed_file_evidence(&changed, input.branch_mode, input.python_semantics)?;
    Ok(plan_selection(SelectionRequest {
        mode: input.mode,
        all_tests,
        durations: input.durations,
        changed,
        observations,
        file_evidence,
        static_files: static_result.dependents,
        static_reasons: static_result.reasons,
        function_spans,
        location: None,
    }))
}

/// Build a test-selection plan from line, arc, timing, and source-change records.
///
/// # Errors
/// Returns an error for an invalid location or an invalid analysis pattern.
pub fn build_selection_plan(evidence: SelectionEvidence) -> Result<SelectionPlan, CoreError> {
    let all_tests = observed_tests(&evidence.lines, &evidence.arcs);
    let input = SelectionInput {
        mode: evidence.mode,
        history_path: evidence.history_path,
        source_roots: evidence.source_roots,
        patch: evidence.patch,
        location: evidence.location,
        source_identity: evidence.source_identity,
        branch_mode: evidence.branch_mode,
        semantic_evidence: evidence.semantic_evidence,
        python_semantics: evidence.python_semantics,
        durations: evidence.durations,
    };
    build_selection_plan_with_records(
        input,
        all_tests,
        || importer_contexts(|| record_partitions(&evidence.lines, &evidence.arcs)),
        |paths| {
            (
                evidence
                    .lines
                    .iter()
                    .filter(|record| paths.contains(&record_path(&record.path)))
                    .cloned()
                    .collect(),
                evidence
                    .arcs
                    .iter()
                    .filter(|record| paths.contains(&record_path(&record.path)))
                    .cloned()
                    .collect(),
            )
        },
    )
}

/// Whether every edit stays inside a body with only recorded test-call execution.
fn edits_confined_to_bodies(plan: &SelectionPlan) -> bool {
    !plan.fallback_full
        && plan.location.is_none()
        && !plan.affected_files.is_empty()
        && plan
            .affected_files
            .iter()
            .all(|path| plan.scoped_files.contains_key(path))
}

/// Return the selected test files after eligible function-body edits.
///
/// Tests without coverage contexts remain selected: an unmeasured subprocess
/// can depend on an edited body even when its recorded contexts do not.
fn collection_files(plan: &SelectionPlan) -> Option<BTreeSet<String>> {
    edits_confined_to_bodies(plan).then(|| {
        plan.selected
            .iter()
            .map(|test| node_file(test).to_owned())
            .collect()
    })
}

/// The file part of a pytest node ID.
#[must_use]
pub(crate) fn node_file(nodeid: &str) -> &str {
    nodeid.split_once("::").map_or(nodeid, |(file, _)| file)
}

/// Build a selection plan directly from compact coverage partitions.
///
/// # Errors
/// Returns an error for an invalid location or an invalid analysis pattern.
pub fn build_selection_plan_from_data(
    data: &CoverageData,
    mut input: SelectionInput,
) -> Result<SelectionPlan, CoreError> {
    let observed = owned_tests(data.context_path_identities().map(|(context, _)| context));
    let mut unmapped_contexts = false;
    let inventory_recorded = data.metadata().contains_key("vfy_green_test_collection");
    // Tests without a coverage context (setup skips, or tests that execute no
    // measured line in-process) cannot be proven unaffected by a body edit.
    // A child process or skip condition may depend on changed code.
    let mut contextless: Vec<String> = Vec::new();
    let all_tests = if let Some(collection) = data.metadata().get("vfy_green_test_collection") {
        let nodeids: Vec<String> = serde_json::from_str(collection).map_err(|error| {
            CoreError::InvalidInput(format!("invalid passing-baseline collection: {error}"))
        })?;
        let inventory = super::ci::collection_inventory(&nodeids)?;
        unmapped_contexts = !observed.is_subset(&inventory);
        // An unchanged tree replays its baseline without running anything.
        if input.location.is_none()
            && input.mode != SelectionMode::Aggressive
            && !input.patch.trim().is_empty()
        {
            contextless = inventory.difference(&observed).cloned().collect();
        }
        // Timing history can outlive deleted tests; it orders this baseline's inventory.
        input
            .durations
            .retain(|nodeid, _| inventory.contains(nodeid));
        inventory
    } else {
        observed
    };
    let mut plan = build_selection_plan_with_records(
        input,
        all_tests,
        || importer_contexts(|| compact_partitions(data)),
        |paths| compact_records_for_paths(data, paths),
    )?;
    if !plan.fallback_full && !contextless.is_empty() {
        plan.selected.extend(contextless);
        plan.selected.sort();
        plan.selected.dedup();
        sort_tests(&mut plan.selected, &plan.durations);
        plan.selected_seconds = seconds(&plan.selected, &plan.durations);
    }
    if unmapped_contexts {
        plan.reasons.push(
            "coverage contexts do not match collected test identities; running the full suite"
                .to_owned(),
        );
        plan.select_full();
    } else if inventory_recorded {
        plan.collection_files = collection_files(&plan);
    }
    Ok(plan)
}
