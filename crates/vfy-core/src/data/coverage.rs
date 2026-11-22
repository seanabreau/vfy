//! The in-memory schema-7 coverage model and its validation.

use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc as Shared;
use std::{error::Error, fmt};

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

mod merge;
mod read;
mod replay;
mod reports;
mod semantic;
mod write;

use crate::evidence::{SparseArcs, SparseLines};
use crate::semantic::SemanticData;
use crate::trace::TraceLines;
pub use merge::validate_file_tracers;
pub use replay::LineShift;
#[cfg(test)]
use write::ARC_INSERT_BATCH;

const SCHEMA_VERSION: i64 = 7;
const CONTEXT_ORDER_SAMPLE_LIMIT: usize = 1024;
const CONTEXT_ORDER_SAMPLE_MINIMUM: usize = 256;
/// A context name shared by every file partition it measured.
type Context = Shared<str>;
/// Evidence keyed by file path, then context.
type Partitions<T> = BTreeMap<String, BTreeMap<Context, T>>;
/// A branch arc as `(from_line, to_line)`; negative lines mark code-object entry and exit.
pub type Arc = (i32, i32);
/// Measured lines grouped by context name.
pub type ContextLines = Vec<(String, Vec<i32>)>;
/// Measured arcs grouped by context name.
pub type ContextArcs = Vec<(String, Vec<Arc>)>;
/// One file's path, lines, arcs, and per-context lines and arcs.
pub type ReportInput = (String, Vec<i32>, Vec<Arc>, ContextLines, ContextArcs);
type IdMap = BTreeMap<i64, String>;
type ContextIds = BTreeMap<i64, Context>;
type DataIndex = (CoverageData, IdMap, ContextIds);

/// Return the shared name equal to `name`, adding it to `names` when absent.
fn shared_context(names: &mut BTreeSet<Context>, name: &str) -> Context {
    if let Some(shared) = names.get(name) {
        return Shared::clone(shared);
    }
    let shared = Context::from(name);
    names.insert(Shared::clone(&shared));
    shared
}

fn context_ordered_partitions<'a, T>(
    partitions: &'a Partitions<T>,
) -> Box<dyn Iterator<Item = (&'a str, &'a str, &'a T)> + 'a> {
    let mut sample = HashSet::default();
    let sampled = partitions
        .values()
        .flat_map(BTreeMap::keys)
        .take(CONTEXT_ORDER_SAMPLE_LIMIT)
        .inspect(|context| {
            sample.insert(&**context);
        })
        .count();
    // High-cardinality inputs avoid allocating one small vector per context.
    if sampled >= CONTEXT_ORDER_SAMPLE_MINIMUM && sample.len() * 4 > sampled * 3 {
        let mut ordered: Vec<_> = partitions
            .iter()
            .flat_map(|(path, contexts)| {
                contexts
                    .iter()
                    .map(move |(context, evidence)| (context.as_ref(), path.as_str(), evidence))
            })
            .collect();
        ordered.sort_by(|left, right| left.0.cmp(right.0).then_with(|| left.1.cmp(right.1)));
        return Box::new(ordered.into_iter());
    }

    let mut by_context: HashMap<&str, Vec<(&str, &T)>> = HashMap::default();
    for (path, contexts) in partitions {
        for (context, evidence) in contexts {
            by_context
                .entry(context)
                .or_default()
                .push((path, evidence));
        }
    }
    let mut by_context: Vec<_> = by_context.into_iter().collect();
    by_context.sort_unstable_by_key(|(context, _)| *context);
    // Outer traversal is path ordered, so each context group is already path ordered.
    Box::new(by_context.into_iter().flat_map(|(context, paths)| {
        paths
            .into_iter()
            .map(move |(path, evidence)| (context, path, evidence))
    }))
}

#[cfg(test)]
mod tests;

/// Coverage data whose records do not reference each other consistently.
#[derive(Debug, PartialEq, Eq)]
pub struct DataValidationError {
    path: String,
    relationship: &'static str,
}

impl DataValidationError {
    const fn new(path: String, relationship: &'static str) -> Self {
        Self { path, relationship }
    }
}

impl fmt::Display for DataValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "invalid coverage data relationship for {:?}: {}",
            self.path, self.relationship
        )
    }
}

impl Error for DataValidationError {}

/// Schema-7 coverage measurements with their contexts, file tracers, and semantic data.
#[derive(Clone)]
pub struct CoverageData {
    pub(super) has_arcs: bool,
    pub(super) metadata: BTreeMap<String, String>,
    pub(super) files: BTreeSet<String>,
    pub(super) file_order: Vec<String>,
    pub(super) contexts: BTreeSet<String>,
    pub(super) tracers: BTreeMap<String, String>,
    lines: Partitions<SparseLines>,
    arcs: Partitions<SparseArcs>,
    pub(super) semantic: SemanticData,
}

impl CoverageData {
    /// Construct validated coverage data from compatibility-layer records.
    ///
    /// # Errors
    ///
    /// Returns an error when collection mode or record relationships are inconsistent.
    #[expect(
        clippy::too_many_lines,
        reason = "every record relationship is validated before the replacement is constructed"
    )]
    pub fn from_materialized_records(
        has_arcs: bool,
        files: Vec<ReportInput>,
        tracers: BTreeMap<String, String>,
        metadata: BTreeMap<String, String>,
        semantic: Option<&Self>,
    ) -> Result<Self, DataValidationError> {
        if let Some(mode) = metadata.get("has_arcs")
            && mode != if has_arcs { "1" } else { "0" }
        {
            return Err(DataValidationError::new(
                "has_arcs".to_owned(),
                "metadata conflicts with the supplied collection mode",
            ));
        }

        let measured: BTreeSet<String> = files.iter().map(|record| record.0.clone()).collect();
        for (path, _lines, arcs, _context_lines, context_arcs) in &files {
            if !has_arcs && (!arcs.is_empty() || !context_arcs.is_empty()) {
                return Err(DataValidationError::new(
                    path.clone(),
                    "arc record conflicts with line collection mode",
                ));
            }
        }
        for path in tracers.keys() {
            if !measured.contains(path) {
                return Err(DataValidationError::new(
                    path.clone(),
                    "file tracer references an unmeasured file",
                ));
            }
        }

        let retained_contexts = semantic.map_or_else(BTreeSet::new, CoverageData::all_contexts);
        let semantic = semantic.map_or_else(SemanticData::default, |data| data.semantic.clone());
        for definition in semantic.definitions.values() {
            if !measured.contains(&definition.path) {
                return Err(DataValidationError::new(
                    definition.path.clone(),
                    "semantic definition references an unmeasured file",
                ));
            }
        }
        for definition in semantic.path_definitions.values() {
            if !measured.contains(&definition.path) {
                return Err(DataValidationError::new(
                    definition.path.clone(),
                    "semantic path definition references an unmeasured file",
                ));
            }
        }
        for observation in &semantic.observations {
            if !semantic.definitions.contains_key(&observation.decision) {
                return Err(DataValidationError::new(
                    observation.decision.clone(),
                    "semantic observation references an undefined decision",
                ));
            }
        }
        for observation in &semantic.path_observations {
            let Some(definition) = semantic.path_definitions.get(&observation.decision) else {
                return Err(DataValidationError::new(
                    observation.decision.clone(),
                    "semantic path observation references an undefined decision",
                ));
            };
            if observation.outcome >= definition.outcomes.len() {
                return Err(DataValidationError::new(
                    definition.path.clone(),
                    "semantic path observation references an undefined outcome",
                ));
            }
        }

        let mut data = Self::empty();
        data.has_arcs = has_arcs;
        data.metadata = metadata;
        data.tracers = tracers;
        data.contexts = retained_contexts;
        data.semantic = semantic
            .normalize_generated_keys()
            .map_err(|(key, invariant)| DataValidationError::new(key, invariant))?;
        let mut names = BTreeSet::new();
        for (path, lines, arcs, mut context_lines, mut context_arcs) in files {
            data.touch_file(&path);
            if has_arcs {
                if context_arcs.is_empty() && !arcs.is_empty() {
                    context_arcs.push((String::new(), arcs));
                }
                for (context, arcs) in context_arcs {
                    data.arcs
                        .entry(path.clone())
                        .or_default()
                        .entry(shared_context(&mut names, &context))
                        .or_default()
                        .extend(arcs);
                }
            } else {
                if context_lines.is_empty() && !lines.is_empty() {
                    context_lines.push((String::new(), lines));
                }
                for (context, lines) in context_lines {
                    data.lines
                        .entry(path.clone())
                        .or_default()
                        .entry(shared_context(&mut names, &context))
                        .or_default()
                        .extend(lines);
                }
            }
        }
        Ok(data)
    }

    fn references_measured_files(&self) -> bool {
        self.tracers.keys().all(|path| self.files.contains(path))
            && self.lines.keys().all(|path| self.files.contains(path))
            && self.arcs.keys().all(|path| self.files.contains(path))
            && self
                .semantic
                .definitions
                .values()
                .all(|definition| self.files.contains(&definition.path))
            && self
                .semantic
                .path_definitions
                .values()
                .all(|definition| self.files.contains(&definition.path))
    }

    #[must_use]
    pub const fn branch_mode(&self) -> bool {
        self.has_arcs
    }

    #[must_use]
    pub const fn semantic_data(&self) -> &SemanticData {
        &self.semantic
    }

    #[must_use]
    pub const fn metadata(&self) -> &BTreeMap<String, String> {
        &self.metadata
    }

    pub fn line_measurements(&self) -> impl Iterator<Item = (&str, &str, BTreeSet<i32>)> {
        context_ordered_partitions(&self.lines)
            .map(|(context, path, lines)| (context, path, lines.sorted().into_iter().collect()))
    }

    pub fn arc_measurements(&self) -> impl Iterator<Item = (&str, &str, Vec<Arc>)> {
        context_ordered_partitions(&self.arcs)
            .map(|(context, path, arcs)| (context, path, arcs.sorted()))
    }

    pub(crate) fn line_evidence(&self) -> impl Iterator<Item = (&str, &str, &SparseLines)> {
        self.lines.iter().flat_map(|(path, contexts)| {
            contexts
                .iter()
                .map(move |(context, lines)| (path.as_str(), context.as_ref(), lines))
        })
    }

    pub(crate) fn arc_evidence(&self) -> impl Iterator<Item = (&str, &str, &SparseArcs)> {
        self.arcs.iter().flat_map(|(path, contexts)| {
            contexts
                .iter()
                .map(move |(context, arcs)| (path.as_str(), context.as_ref(), arcs))
        })
    }

    pub(crate) fn line_evidence_for_paths<'a>(
        &'a self,
        selected_paths: &'a BTreeSet<String>,
    ) -> impl Iterator<Item = (&'a str, &'a str, &'a SparseLines)> {
        selected_paths
            .iter()
            .filter_map(|path| self.lines.get_key_value(path))
            .flat_map(|(path, contexts)| {
                contexts
                    .iter()
                    .map(move |(context, lines)| (path.as_str(), context.as_ref(), lines))
            })
    }

    pub(crate) fn arc_evidence_for_paths<'a>(
        &'a self,
        selected_paths: &'a BTreeSet<String>,
    ) -> impl Iterator<Item = (&'a str, &'a str, &'a SparseArcs)> {
        selected_paths
            .iter()
            .filter_map(|path| self.arcs.get_key_value(path))
            .flat_map(|(path, contexts)| {
                contexts
                    .iter()
                    .map(move |(context, arcs)| (path.as_str(), context.as_ref(), arcs))
            })
    }

    pub(super) fn has_arc_evidence(&self) -> bool {
        self.arcs
            .values()
            .any(|contexts| contexts.values().any(|arcs| arcs.iter().next().is_some()))
    }

    pub(crate) fn context_path_identities(&self) -> impl Iterator<Item = (&str, &str)> {
        self.lines
            .iter()
            .flat_map(|(path, contexts)| {
                contexts
                    .keys()
                    .map(move |context| (context.as_ref(), path.as_str()))
            })
            .chain(self.arcs.iter().flat_map(|(path, contexts)| {
                contexts
                    .keys()
                    .map(move |context| (context.as_ref(), path.as_str()))
            }))
    }

    pub(crate) fn context_path_identities_for_paths(
        &self,
        selected_paths: &BTreeSet<String>,
    ) -> Vec<(&str, &str)> {
        let mut identities = Vec::new();
        for path in selected_paths {
            if let Some((stored_path, contexts)) = self.lines.get_key_value(path) {
                identities.extend(
                    contexts
                        .keys()
                        .map(|context| (context.as_ref(), stored_path.as_str())),
                );
            }
            if let Some((stored_path, contexts)) = self.arcs.get_key_value(path) {
                identities.extend(
                    contexts
                        .keys()
                        .map(|context| (context.as_ref(), stored_path.as_str())),
                );
            }
        }
        identities
    }

    /// Create data with no files, contexts, or metadata.
    #[must_use]
    pub fn empty() -> Self {
        Self {
            has_arcs: false,
            metadata: BTreeMap::new(),
            files: BTreeSet::new(),
            file_order: Vec::new(),
            contexts: BTreeSet::new(),
            tracers: BTreeMap::new(),
            lines: BTreeMap::new(),
            arcs: BTreeMap::new(),
            semantic: SemanticData::default(),
        }
    }

    /// Construct an empty store with a known collection mode.
    #[must_use]
    pub fn new(has_arcs: bool) -> Self {
        let mut data = Self::empty();
        data.has_arcs = has_arcs;
        data
    }

    /// Record a measured file even when it has no executable measurements.
    pub fn touch_file(&mut self, path: &str) {
        if self.files.insert(path.to_owned()) {
            self.file_order.push(path.to_owned());
        }
    }

    #[must_use]
    pub fn mode_is_known(&self) -> bool {
        self.has_arcs || !self.files.is_empty() || self.metadata.contains_key("has_arcs")
    }

    pub fn all_contexts(&self) -> BTreeSet<String> {
        self.context_names()
            .into_iter()
            .map(str::to_owned)
            .collect()
    }

    fn context_names(&self) -> BTreeSet<&str> {
        self.contexts
            .iter()
            .map(String::as_str)
            .chain(
                self.lines
                    .values()
                    .flat_map(BTreeMap::keys)
                    .chain(self.arcs.values().flat_map(BTreeMap::keys))
                    .map(AsRef::as_ref),
            )
            .chain(
                self.semantic
                    .observations
                    .iter()
                    .map(|observation| observation.context.as_str()),
            )
            .chain(
                self.semantic
                    .path_observations
                    .iter()
                    .map(|observation| observation.context.as_str()),
            )
            .collect()
    }

    /// Build data from the records of a `TraceStore` snapshot.
    #[must_use]
    pub fn from_trace_records(
        has_arcs: bool,
        lines: Vec<(String, String, Vec<i32>)>,
        arcs: Vec<(String, String, Vec<Arc>)>,
        tracers: Vec<(String, String)>,
        semantic: SemanticData,
    ) -> Self {
        let mut data = Self::empty();
        data.has_arcs = has_arcs;
        let mut names = BTreeSet::new();
        for (context, path, values) in lines {
            data.touch_file(&path);
            data.lines
                .entry(path)
                .or_default()
                .entry(shared_context(&mut names, &context))
                .or_default()
                .extend(values);
        }
        for (context, path, values) in arcs {
            data.touch_file(&path);
            data.arcs
                .entry(path)
                .or_default()
                .entry(shared_context(&mut names, &context))
                .or_default()
                .extend(values);
        }
        for (path, tracer) in tracers {
            data.touch_file(&path);
            data.tracers.insert(path, tracer);
        }
        data.semantic = semantic;
        data
    }

    /// Construct coverage data from collector line chunks and arc records.
    ///
    /// Line chunks move into the store; the result matches
    /// [`Self::from_trace_records`] for the same evidence.
    #[must_use]
    pub fn from_trace(
        has_arcs: bool,
        lines: TraceLines,
        arcs: Vec<(String, String, Vec<Arc>)>,
        tracers: Vec<(String, String)>,
        semantic: SemanticData,
    ) -> Self {
        let mut data =
            Self::from_trace_records(has_arcs, Vec::new(), Vec::new(), Vec::new(), semantic);
        // Line and arc partitions share one name per context.
        let mut names = BTreeSet::new();
        for (context, path, values) in lines.0 {
            let context = if let Some(shared) = names.get(context.as_ref()) {
                Shared::clone(shared)
            } else {
                names.insert(Shared::clone(&context));
                context
            };
            data.touch_file(&path);
            let contexts = match data.lines.get_mut(path.as_ref()) {
                Some(contexts) => contexts,
                None => data.lines.entry(path.as_ref().to_owned()).or_default(),
            };
            match contexts.entry(context) {
                Entry::Vacant(entry) => {
                    entry.insert(values);
                }
                Entry::Occupied(mut entry) => entry.get_mut().extend_set(&values),
            }
        }
        for (context, path, values) in arcs {
            data.touch_file(&path);
            data.arcs
                .entry(path)
                .or_default()
                .entry(shared_context(&mut names, &context))
                .or_default()
                .extend(values);
        }
        for (path, tracer) in tracers {
            data.touch_file(&path);
            data.tracers.insert(path, tracer);
        }
        data
    }

    /// Return a copy containing only the requested measured files and their evidence.
    #[must_use]
    pub fn select_files(&self, paths: Vec<String>) -> Self {
        let selected: BTreeSet<String> = paths.into_iter().collect();
        let mut data = Self::empty();
        data.has_arcs = self.has_arcs;
        data.metadata.clone_from(&self.metadata);
        for path in self
            .file_order
            .iter()
            .filter(|path| selected.contains(*path))
        {
            data.touch_file(path);
        }
        data.tracers = self
            .tracers
            .iter()
            .filter(|(path, _)| selected.contains(*path))
            .map(|(path, tracer)| (path.clone(), tracer.clone()))
            .collect();
        data.lines = selected
            .iter()
            .filter_map(|path| {
                self.lines
                    .get(path)
                    .map(|lines| (path.clone(), lines.clone()))
            })
            .collect();
        data.arcs = selected
            .iter()
            .filter_map(|path| self.arcs.get(path).map(|arcs| (path.clone(), arcs.clone())))
            .collect();
        data.semantic.definitions = self
            .semantic
            .definitions
            .iter()
            .filter(|(_, definition)| selected.contains(&definition.path))
            .map(|(key, definition)| (key.clone(), definition.clone()))
            .collect();
        data.semantic.observations = self
            .semantic
            .observations
            .iter()
            .filter(|observation| {
                data.semantic
                    .definitions
                    .contains_key(&observation.decision)
            })
            .cloned()
            .collect();
        data.semantic.path_definitions = self
            .semantic
            .path_definitions
            .iter()
            .filter(|(_, definition)| selected.contains(&definition.path))
            .map(|(key, definition)| (key.clone(), definition.clone()))
            .collect();
        data.semantic.path_observations = self
            .semantic
            .path_observations
            .iter()
            .filter(|observation| {
                data.semantic
                    .path_definitions
                    .contains_key(&observation.decision)
            })
            .cloned()
            .collect();
        data
    }

    /// Keep only the requested measured files, moving their evidence.
    ///
    /// The result matches [`Self::select_files`] without copying the retained
    /// partitions.
    #[must_use]
    pub fn into_selected_files(self, paths: Vec<String>) -> Self {
        let selected: BTreeSet<String> = paths.into_iter().collect();
        let mut data = Self::empty();
        data.has_arcs = self.has_arcs;
        data.metadata = self.metadata;
        for path in self
            .file_order
            .iter()
            .filter(|path| selected.contains(*path))
        {
            data.touch_file(path);
        }
        data.tracers = self.tracers;
        data.tracers.retain(|path, _| selected.contains(path));
        data.lines = self.lines;
        data.lines.retain(|path, _| selected.contains(path));
        data.arcs = self.arcs;
        data.arcs.retain(|path, _| selected.contains(path));
        let mut semantic = self.semantic;
        semantic
            .definitions
            .retain(|_, definition| selected.contains(&definition.path));
        semantic
            .observations
            .retain(|observation| semantic.definitions.contains_key(&observation.decision));
        semantic
            .path_definitions
            .retain(|_, definition| selected.contains(&definition.path));
        semantic.path_observations.retain(|observation| {
            semantic
                .path_definitions
                .contains_key(&observation.decision)
        });
        data.semantic = semantic;
        data
    }

    /// Replace executable measurements while retaining matching semantic evidence.
    #[must_use]
    pub fn with_measurements(&self, measurements: &Self) -> Self {
        let mut data = self.select_files(measurements.measured_files());
        data.has_arcs = measurements.has_arcs;
        data.metadata.clone_from(&measurements.metadata);
        data.tracers.clone_from(&measurements.tracers);
        data.lines.clone_from(&measurements.lines);
        data.arcs.clone_from(&measurements.arcs);
        for path in &measurements.file_order {
            data.touch_file(path);
        }
        data.contexts.extend(self.contexts.iter().cloned());
        data.contexts.extend(measurements.all_contexts());
        data
    }

    /// Remove evidence for selected files while retaining their measured-file entries.
    pub fn purge_files(&mut self, paths: Vec<String>) {
        let purged: BTreeSet<String> = paths.into_iter().collect();
        let retained = self.files.difference(&purged).cloned().collect();
        let mut data = self.select_files(retained);
        data.files.clone_from(&self.files);
        data.file_order.clone_from(&self.file_order);
        data.tracers.clone_from(&self.tracers);
        data.contexts = self.all_contexts();
        *self = data;
    }

    /// Whether no files have been measured.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.files.is_empty()
    }

    /// Whether any measured file uses a dynamic source tracer.
    #[must_use]
    pub fn has_tracers(&self) -> bool {
        !self.tracers.is_empty()
    }

    /// Return the union of measured lines for a file.
    #[must_use]
    pub fn lines_for(&self, path: &str) -> Option<Vec<i32>> {
        if !self.files.contains(path) {
            return None;
        }
        let mut lines: BTreeSet<i32> = self
            .lines
            .get(path)
            .into_iter()
            .flat_map(BTreeMap::values)
            .flat_map(SparseLines::sorted)
            .collect();
        if lines.is_empty() {
            lines.extend(
                self.arcs
                    .get(path)
                    .into_iter()
                    .flat_map(BTreeMap::values)
                    .flat_map(SparseArcs::iter)
                    .flat_map(|(start, end)| [start, end])
                    .filter(|line| *line > 0),
            );
        }
        Some(lines.into_iter().collect())
    }

    /// Return the union of measured arcs for a file.
    #[must_use]
    pub fn arcs_for(&self, path: &str) -> Option<Vec<Arc>> {
        self.files.contains(path).then(|| {
            self.arcs
                .get(path)
                .into_iter()
                .flat_map(BTreeMap::values)
                .flat_map(SparseArcs::iter)
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect()
        })
    }

    /// Set one metadata value.
    pub fn set_metadata(&mut self, key: &str, value: &str) {
        self.metadata.insert(key.to_owned(), value.to_owned());
    }

    /// Set a file tracer and ensure the file is measured.
    pub fn set_tracer(&mut self, path: &str, tracer: &str) {
        self.touch_file(path);
        self.tracers.insert(path.to_owned(), tracer.to_owned());
    }

    /// Atomically add file tracers, rejecting conflicts within or against the batch.
    ///
    /// # Errors
    ///
    /// Returns an error when a file has two different tracer names.
    pub fn add_file_tracers(&mut self, tracers: Vec<(String, String)>) -> Result<(), &'static str> {
        validate_file_tracers(
            &self.tracers,
            tracers
                .iter()
                .map(|(path, tracer)| (path.as_str(), tracer.as_str())),
        )?;
        for (path, tracer) in tracers {
            self.touch_file(&path);
            if !tracer.is_empty() {
                self.tracers.insert(path, tracer);
            }
        }
        Ok(())
    }

    /// Touch files in an initialized collection without changing existing tracers.
    ///
    /// # Errors
    ///
    /// Returns an error before mutation for an unknown collection mode or conflicting tracers.
    pub fn touch_files(&mut self, paths: Vec<String>, tracer: &str) -> Result<(), &'static str> {
        if !self.mode_is_known() {
            return Err("Can't touch files in an empty CoverageData");
        }
        if tracer.is_empty() {
            for path in paths {
                self.touch_file(&path);
            }
            Ok(())
        } else {
            self.add_file_tracers(
                paths
                    .into_iter()
                    .map(|path| (path, tracer.to_owned()))
                    .collect(),
            )
        }
    }

    /// Add line measurements for one context and file.
    pub fn add_lines(&mut self, context: &str, path: &str, lines: Vec<i32>) {
        self.touch_file(path);
        self.lines
            .entry(path.to_owned())
            .or_default()
            .entry(Context::from(context))
            .or_default()
            .extend(lines);
    }

    /// Add arc measurements for one context and file.
    ///
    /// An empty arc set registers no file, as in Coverage.py.
    pub fn add_arcs(&mut self, context: &str, path: &str, arcs: Vec<Arc>) {
        if arcs.is_empty() {
            return;
        }
        self.touch_file(path);
        self.arcs
            .entry(path.to_owned())
            .or_default()
            .entry(Context::from(context))
            .or_default()
            .extend(arcs);
    }

    /// Return measured file paths in lexicographic order.
    #[must_use]
    pub fn measured_files(&self) -> Vec<String> {
        self.files.iter().cloned().collect()
    }

    /// Return all measurement and semantic contexts in lexicographic order.
    #[must_use]
    pub fn measured_contexts(&self) -> Vec<String> {
        self.all_contexts().into_iter().collect()
    }

    /// Return metadata records ordered by key.
    #[must_use]
    pub fn metadata_records(&self) -> Vec<(String, String)> {
        self.metadata
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect()
    }

    /// Return tracer records ordered by file path.
    #[must_use]
    pub fn tracer_records(&self) -> Vec<(String, String)> {
        self.tracers
            .iter()
            .map(|(path, tracer)| (path.clone(), tracer.clone()))
            .collect()
    }

    /// Return line records ordered by context, then file path, with sorted lines.
    #[must_use]
    pub fn line_records(&self) -> Vec<(String, String, Vec<i32>)> {
        // Public batch records remain context/path ordered despite path-major storage.
        context_ordered_partitions(&self.lines)
            .map(|(context, path, lines)| (context.to_owned(), path.to_owned(), lines.sorted()))
            .collect()
    }

    /// Return arc records ordered by context, then file path, with sorted arcs.
    #[must_use]
    pub fn arc_records(&self) -> Vec<(String, String, Vec<Arc>)> {
        context_ordered_partitions(&self.arcs)
            .map(|(context, path, arcs)| (context.to_owned(), path.to_owned(), arcs.sorted()))
            .collect()
    }

    /// Whether semantic definitions are present.
    #[must_use]
    pub fn has_semantic_data(&self) -> bool {
        !self.semantic.definitions.is_empty() || !self.semantic.path_definitions.is_empty()
    }
}
