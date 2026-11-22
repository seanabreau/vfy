//! In-memory store for line, arc, and semantic evidence collected during a run.

use std::sync::Arc as Shared;
use std::thread::ThreadId;

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::evidence::{LineSetTable, SparseLines};
use crate::{
    PathDecisionDefinition, PathDecisionKind, PathObservation, SemanticData, SemanticObservation,
    analyze_semantic_source_data, render_vector,
};

type Arc = (i32, i32);
type SourceRange = (i32, i32, i32, i32);
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct ContextId(usize);

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct PathId(usize);

#[derive(Default)]
struct EvidenceInterner {
    contexts: Vec<Shared<str>>,
    context_ids: HashMap<Shared<str>, ContextId>,
    last_context: Option<(Shared<str>, ContextId)>,
    paths: Vec<Shared<str>>,
    path_ids: HashMap<Shared<str>, PathId>,
    last_path: Option<(Shared<str>, PathId)>,
}

impl EvidenceInterner {
    fn context_id(&mut self, context: &str) -> ContextId {
        if let Some((value, id)) = &self.last_context
            && value.as_ref() == context
        {
            return *id;
        }
        if let Some(&id) = self.context_ids.get(context) {
            self.last_context = Some((Shared::clone(&self.contexts[id.0]), id));
            return id;
        }
        let id = ContextId(self.contexts.len());
        let context = Shared::<str>::from(context);
        self.contexts.push(Shared::clone(&context));
        self.context_ids.insert(Shared::clone(&context), id);
        self.last_context = Some((context, id));
        id
    }

    fn path_id(&mut self, path: &str) -> PathId {
        if let Some((value, id)) = &self.last_path
            && value.as_ref() == path
        {
            return *id;
        }
        if let Some(&id) = self.path_ids.get(path) {
            self.last_path = Some((Shared::clone(&self.paths[id.0]), id));
            return id;
        }
        let id = PathId(self.paths.len());
        let path = Shared::<str>::from(path);
        self.paths.push(Shared::clone(&path));
        self.path_ids.insert(Shared::clone(&path), id);
        self.last_path = Some((path, id));
        id
    }

    fn context(&self, id: ContextId) -> &str {
        &self.contexts[id.0]
    }

    fn path(&self, id: PathId) -> &str {
        &self.paths[id.0]
    }
}

struct PathRecords<T> {
    first: Option<(PathId, T)>,
    remaining: Option<Box<HashMap<PathId, T>>>,
}

impl<T> Default for PathRecords<T> {
    fn default() -> Self {
        Self {
            first: None,
            remaining: None,
        }
    }
}

impl<T: Default> PathRecords<T> {
    fn get_or_insert(&mut self, path: PathId) -> &mut T {
        if self.first.is_none() {
            self.first = Some((path, T::default()));
        }
        if let Some((first_path, value)) = self.first.as_mut()
            && *first_path == path
        {
            return value;
        }
        self.remaining
            .get_or_insert_with(Box::default)
            .entry(path)
            .or_default()
    }

    fn into_entries(self) -> impl Iterator<Item = (PathId, T)> {
        self.first
            .into_iter()
            .chain(self.remaining.into_iter().flat_map(|paths| *paths))
    }

    fn values_mut(&mut self) -> impl Iterator<Item = &mut T> {
        self.first.iter_mut().map(|(_, value)| value).chain(
            self.remaining
                .iter_mut()
                .flat_map(|paths| paths.values_mut()),
        )
    }

    fn iter(&self) -> impl Iterator<Item = (PathId, &T)> {
        self.first.iter().map(|(path, value)| (*path, value)).chain(
            self.remaining
                .iter()
                .flat_map(|paths| paths.iter().map(|(&path, value)| (path, value))),
        )
    }
}

type LineRecords = Vec<PathRecords<SparseLines>>;

/// Line evidence for each context and file, sorted by context and path.
///
/// It carries the collector's bit chunks into coverage data without
/// expanding them into line numbers.
pub struct TraceLines(pub(crate) Vec<(Shared<str>, Shared<str>, SparseLines)>);

impl TraceLines {
    fn sorted(mut records: Vec<(Shared<str>, Shared<str>, SparseLines)>) -> Self {
        records.sort_unstable_by(|left, right| {
            (left.0.as_ref(), left.1.as_ref()).cmp(&(right.0.as_ref(), right.1.as_ref()))
        });
        Self(records)
    }
}
type ArcRecords = Vec<PathRecords<HashSet<Arc>>>;

fn context_partition<T>(
    records: &mut Vec<PathRecords<T>>,
    context: ContextId,
) -> &mut PathRecords<T> {
    if records.len() <= context.0 {
        records.resize_with(context.0 + 1, PathRecords::default);
    }
    &mut records[context.0]
}

/// Collection modes selected before monitoring begins.
#[derive(Clone, Copy, Debug, Default)]
pub struct TraceOptions {
    pub branch: bool,
    pub sysmon_arcs: bool,
    pub decisions: bool,
}

struct ActiveFrame {
    code_id: usize,
    previous_line: Option<i32>,
    previous_path: Option<PathId>,
    suppress_exit: bool,
}

impl ActiveFrame {
    const fn new(code_id: usize, previous_line: Option<i32>) -> Self {
        Self {
            code_id,
            previous_line,
            previous_path: None,
            suppress_exit: false,
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct PathRuntimeState {
    raised: bool,
    completed: bool,
    last_line: Option<i32>,
}

type CodePaths = HashMap<usize, PathRecords<HashSet<Arc>>>;
type CodeRecords = HashMap<ContextId, CodePaths>;

thread_local! {
    // ThreadId is stable for the thread's lifetime. Copy it without acquiring
    // and dropping a reference-counted Thread handle on each monitoring event.
    static CURRENT_THREAD_ID: ThreadId = std::thread::current().id();
}

fn current_thread_id() -> ThreadId {
    CURRENT_THREAD_ID.with(|id| *id)
}

fn code_record<'a>(
    records: &'a mut CodeRecords,
    default_records: &'a mut CodePaths,
    context: ContextId,
    default_context: bool,
    code_id: usize,
    path: PathId,
) -> &'a mut HashSet<Arc> {
    let paths = if default_context {
        default_records
    } else {
        records.entry(context).or_default()
    };
    // Dynamic tracers can map separate executions of one code object to
    // different files. Keep the ordinary single-path record inline.
    paths.entry(code_id).or_default().get_or_insert(path)
}

/// Execution data partitioned by context.
///
/// The interpreter adapter resolves code objects and passes primitive events.
/// Line and arc snapshots deduplicate records and sort by context and file path.
pub struct TraceStore {
    branch: bool,
    sysmon_arcs: bool,
    decisions: bool,
    interner: EvidenceInterner,
    lines: LineRecords,
    line_sets: LineSetTable,
    // Contexts with line evidence written since their sets were last shared.
    unshared_line_contexts: Vec<ContextId>,
    last_line_context: Option<ContextId>,
    arcs: ArcRecords,
    // The line-derived branch path is keyed by code-object identity. This
    // avoids hashing a full source path twice on every executed line; records
    // are folded back to file paths only when a snapshot is requested.
    code_records: CodeRecords,
    default_code_records: CodePaths,
    active_thread: ThreadId,
    active_stack: Vec<ActiveFrame>,
    stacks: HashMap<ThreadId, Vec<ActiveFrame>>,
    semantic: SemanticData,
    semantic_definition_keys: HashMap<String, Vec<String>>,
    path_definition_keys: HashMap<String, Vec<String>>,
    value_parent: HashMap<String, (String, usize)>,
    semantic_active: HashMap<(ThreadId, usize, String), Vec<Option<bool>>>,
    semantic_pending: HashMap<(ThreadId, usize), Vec<SemanticObservation>>,
    path_active: HashMap<(ThreadId, usize, String), PathRuntimeState>,
    value_active: HashSet<(ThreadId, usize, String)>,
}

impl TraceStore {
    #[must_use]
    pub const fn has_arcs(&self) -> bool {
        self.branch
    }

    #[must_use]
    pub fn new(branch: bool) -> Self {
        Self::with_options(TraceOptions {
            branch,
            ..TraceOptions::default()
        })
    }

    pub fn add_line(&mut self, context: &str, path: &str, line: i32) {
        let context = self.interner.context_id(context);
        if self.last_line_context != Some(context) {
            self.last_line_context = Some(context);
            self.unshared_line_contexts.push(context);
        }
        let path = self.interner.path_id(path);
        context_partition(&mut self.lines, context)
            .get_or_insert(path)
            .insert(line);
    }

    pub fn add_arc(&mut self, context: &str, path: &str, start: i32, end: i32) {
        if self.branch && (self.sysmon_arcs || start != end) {
            let context = self.interner.context_id(context);
            let path = self.interner.path_id(path);
            context_partition(&mut self.arcs, context)
                .get_or_insert(path)
                .insert((start, end));
        }
    }

    pub fn add_repeated_arc(&mut self, context: &str, path: &str, line: i32) {
        if self.branch {
            let context = self.interner.context_id(context);
            let path = self.interner.path_id(path);
            context_partition(&mut self.arcs, context)
                .get_or_insert(path)
                .insert((line, line));
        }
    }

    pub fn observe_semantic(
        &mut self,
        context: &str,
        path: &str,
        line: i32,
        column: i32,
        ordinal: usize,
        outcome: bool,
    ) {
        if !self.decisions {
            return;
        }
        let Some(definition_keys) = self.semantic_definition_keys.get(path) else {
            return;
        };
        let definitions = &self.semantic.definitions;
        let Some((key, condition_index, condition_count, logic)) = definition_keys
            .iter()
            .filter_map(|key| definitions.get(key))
            .find_map(|definition| {
                let condition = if column >= 0 {
                    let mut matching = definition.condition_ranges.iter().enumerate().filter_map(
                        |(index, &(candidate_line, start, end_line, end))| {
                            (candidate_line == line
                                && line <= end_line
                                && column >= start
                                && (line < end_line || column < end || start == end))
                                .then_some(index)
                        },
                    );
                    let first = matching.next()?;
                    if let Some(second) = matching.next() {
                        std::iter::once(first)
                            .chain(std::iter::once(second))
                            .chain(matching)
                            .nth(ordinal)
                    } else {
                        Some(first)
                    }
                } else {
                    definition
                        .condition_lines
                        .iter()
                        .enumerate()
                        .filter_map(|(index, &candidate)| (candidate == line).then_some(index))
                        .nth(ordinal)
                };
                condition.map(|index| {
                    (
                        definition.key.clone(),
                        index,
                        definition.logic.condition_count(),
                        definition.logic.clone(),
                    )
                })
            })
        else {
            return;
        };
        let depth = self.current_stack().len();
        let thread = current_thread_id();
        let state_key = (thread, depth, key.clone());
        let values = self
            .semantic_active
            .entry(state_key.clone())
            .or_insert_with(|| vec![None; condition_count]);
        if let Some(slot) = values.get_mut(condition_index) {
            *slot = Some(outcome);
        }
        if let Some(decision_outcome) = logic.evaluate_observation(values) {
            let observation = SemanticObservation {
                context: context.to_owned(),
                decision: key,
                vector: render_vector(values),
                outcome: decision_outcome,
            };
            self.semantic_active.remove(&state_key);
            self.semantic_pending
                .entry((thread, depth))
                .or_default()
                .push(observation);
        }
    }

    /// Register definitions for a source path when semantic collection is enabled.
    ///
    /// # Errors
    ///
    /// Returns a source read or parse error. Missing source is permitted.
    pub fn register_semantic_path(&mut self, path: &str) -> Result<(), crate::CoreError> {
        if !self.decisions || self.semantic_definition_keys.contains_key(path) {
            return Ok(());
        }
        let source = match crate::read_python_source(path) {
            Ok(source) => source,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                self.semantic_definition_keys
                    .insert(path.to_owned(), Vec::new());
                self.path_definition_keys
                    .insert(path.to_owned(), Vec::new());
                return Ok(());
            }
            Err(source) => {
                return Err(crate::CoreError::ArtifactIo {
                    path: path.into(),
                    operation: "read semantic source",
                    source,
                });
            }
        };
        let (definitions, path_definitions) = analyze_semantic_source_data(path, &source)?;
        let mut semantic_keys = Vec::new();
        let mut path_keys = Vec::new();
        for definition in definitions {
            semantic_keys.push(definition.key.clone());
            self.semantic
                .definitions
                .insert(definition.key.clone(), definition);
        }
        let mut parents_by_range = HashMap::default();
        for parent in path_definitions
            .iter()
            .filter(|definition| definition.kind.is_value())
        {
            for (index, outcome) in parent.outcomes.iter().enumerate() {
                parents_by_range.insert(outcome.range, (parent.key.clone(), index));
            }
        }
        for child in path_definitions
            .iter()
            .filter(|definition| definition.kind.is_value())
        {
            if let Some(parent) = parents_by_range.get(&child.range)
                && parent.0 != child.key
            {
                self.value_parent.insert(child.key.clone(), parent.clone());
            }
        }
        for definition in path_definitions {
            path_keys.push(definition.key.clone());
            self.semantic
                .path_definitions
                .insert(definition.key.clone(), definition);
        }
        self.semantic_definition_keys
            .insert(path.to_owned(), semantic_keys);
        self.path_definition_keys.insert(path.to_owned(), path_keys);
        Ok(())
    }

    pub fn observe_path_branch(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        expression_range: SourceRange,
        expression_ordinal: usize,
        outcome: bool,
    ) {
        if !self.decisions {
            return;
        }
        let Some(definition_keys) = self.path_definition_keys.get(path) else {
            return;
        };
        let Some(decision) = definition_keys
            .iter()
            .filter_map(|key| self.semantic.path_definitions.get(key))
            .filter(|definition| definition.kind.is_value() && definition.range == expression_range)
            .map(|definition| definition.key.as_str())
            .next()
        else {
            return;
        };
        Self::record_value_outcome(
            &self.semantic.path_definitions,
            &mut self.semantic.path_observations,
            &mut self.value_active,
            &self.value_parent,
            context,
            code_id,
            decision,
            expression_ordinal,
            outcome,
        );
    }

    pub fn flush_path_current(&mut self, context: &str, code_id: usize) {
        let thread = current_thread_id();
        let path_definitions = &self.semantic.path_definitions;
        let path_observations = &mut self.semantic.path_observations;
        self.value_active
            .retain(|(candidate, candidate_code, key)| {
                if *candidate != thread || *candidate_code != code_id {
                    return true;
                }
                if let Some(last) = path_definitions
                    .get(key)
                    .and_then(|definition| definition.outcomes.len().checked_sub(1))
                {
                    path_observations.insert(PathObservation {
                        context: context.to_owned(),
                        decision: key.clone(),
                        outcome: last,
                    });
                }
                false
            });
    }

    #[expect(
        clippy::too_many_lines,
        reason = "match and exception paths share one line-event state machine"
    )]
    pub fn observe_path_line(&mut self, context: &str, path: &str, code_id: usize, line: i32) {
        if !self.decisions {
            return;
        }
        let Some(definition_keys) = self.path_definition_keys.get(path) else {
            return;
        };
        let thread = current_thread_id();
        let SemanticData {
            path_definitions,
            path_observations,
            ..
        } = &mut self.semantic;
        for definition in definition_keys
            .iter()
            .filter_map(|key| path_definitions.get(key))
            .filter(|definition| !definition.kind.is_value())
        {
            let state_key = (thread, code_id, definition.key.clone());
            if definition.kind.is_match() {
                let Some(first_case_line) =
                    definition.outcomes.first().map(|outcome| outcome.range.0)
                else {
                    continue;
                };
                let in_match = line >= definition.body_start && line <= definition.end_line;
                let in_header = in_match && line <= first_case_line;
                let mut state = self
                    .path_active
                    .get(&state_key)
                    .copied()
                    .unwrap_or_default();
                if in_header {
                    // A match can be re-entered without a line event after its
                    // previous invocation (for example, when it is the last
                    // statement in a loop). Close that invocation before
                    // starting the next one.
                    if state.last_line.is_some_and(|last| line < last) {
                        if !state.raised
                            && !state.completed
                            && let Some(outcome) = definition
                                .outcomes
                                .iter()
                                .position(|outcome| outcome.role == crate::PathOutcomeRole::NoMatch)
                        {
                            path_observations.insert(PathObservation {
                                context: context.to_owned(),
                                decision: definition.key.clone(),
                                outcome,
                            });
                        }
                        state = PathRuntimeState::default();
                    }
                    state.last_line = Some(line);
                    self.path_active.insert(state_key.clone(), state);
                } else if !in_match && !self.path_active.contains_key(&state_key) {
                    continue;
                }
                let Some(mut state) = self.path_active.get(&state_key).copied() else {
                    continue;
                };
                if !state.raised
                    && !state.completed
                    && let Some(outcome) = definition.outcomes.iter().position(|outcome| {
                        matches!(outcome.role, crate::PathOutcomeRole::MatchCase { body }
                            if line >= body.0 && line <= body.1)
                    })
                {
                    path_observations.insert(PathObservation {
                        context: context.to_owned(),
                        decision: definition.key.clone(),
                        outcome,
                    });
                    state.completed = true;
                }
                if in_match {
                    state.last_line = Some(line);
                }
                if line > definition.end_line {
                    if !state.raised
                        && !state.completed
                        && let Some(outcome) = definition
                            .outcomes
                            .iter()
                            .position(|outcome| outcome.role == crate::PathOutcomeRole::NoMatch)
                    {
                        path_observations.insert(PathObservation {
                            context: context.to_owned(),
                            decision: definition.key.clone(),
                            outcome,
                        });
                    }
                    self.path_active.remove(&state_key);
                } else {
                    self.path_active.insert(state_key, state);
                }
                continue;
            }
            if line >= definition.body_start && line <= definition.body_end {
                self.path_active.entry(state_key).or_default();
                continue;
            }
            let Some(mut state) = self.path_active.get(&state_key).copied() else {
                continue;
            };
            if definition.kind.is_try() && state.raised {
                if let Some(outcome) = definition.outcomes.iter().position(|outcome| {
                    outcome
                        .role
                        .handler_body()
                        .is_some_and(|(start, end)| line >= start && line <= end)
                }) {
                    path_observations.insert(PathObservation {
                        context: context.to_owned(),
                        decision: definition.key.clone(),
                        outcome,
                    });
                    state.completed = true;
                }
            } else if definition.kind.is_try() && !state.raised && line <= definition.end_line {
                path_observations.insert(PathObservation {
                    context: context.to_owned(),
                    decision: definition.key.clone(),
                    outcome: 0,
                });
                state.completed = true;
            }
            if line > definition.end_line {
                if !state.completed {
                    let outcome = if state.raised {
                        definition
                            .outcomes
                            .iter()
                            .position(|outcome| outcome.role == crate::PathOutcomeRole::Unhandled)
                    } else {
                        Some(0)
                    };
                    if let Some(outcome) = outcome {
                        path_observations.insert(PathObservation {
                            context: context.to_owned(),
                            decision: definition.key.clone(),
                            outcome,
                        });
                    }
                }
                self.path_active.remove(&state_key);
            } else {
                self.path_active.insert(state_key, state);
            }
        }
    }

    pub fn observe_path_raise(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        line: i32,
        cancelled: bool,
    ) {
        if !self.decisions {
            return;
        }
        let Some(definition_keys) = self.path_definition_keys.get(path) else {
            return;
        };
        let thread = current_thread_id();
        self.value_active.retain(|(candidate, candidate_code, _)| {
            *candidate != thread || *candidate_code != code_id
        });
        let SemanticData {
            path_definitions,
            path_observations,
            ..
        } = &mut self.semantic;
        for definition in definition_keys
            .iter()
            .filter_map(|key| path_definitions.get(key))
            .filter(|definition| line >= definition.body_start && line <= definition.body_end)
        {
            let key = (thread, code_id, definition.key.clone());
            let is_cancelled = definition.kind == PathDecisionKind::AsyncCancellation && cancelled;
            {
                let state = self.path_active.entry(key).or_default();
                state.raised = true;
                state.completed |= is_cancelled;
            }
            if is_cancelled {
                path_observations.insert(PathObservation {
                    context: context.to_owned(),
                    decision: definition.key.clone(),
                    outcome: 1,
                });
            }
        }
    }

    pub fn finish_paths(&mut self, context: &str, code_id: usize, unwinding: bool) {
        self.flush_path_current(context, code_id);
        let thread = current_thread_id();
        let path_definitions = &self.semantic.path_definitions;
        let path_observations = &mut self.semantic.path_observations;
        for (key, state) in self
            .path_active
            .extract_if(|(candidate, candidate_code, _), _| {
                *candidate == thread && *candidate_code == code_id
            })
        {
            if state.completed {
                continue;
            }
            let Some(definition) = path_definitions.get(&key.2) else {
                continue;
            };
            if definition.kind.is_match() {
                if !unwinding
                    && !state.raised
                    && !state.completed
                    && let Some(outcome) = definition
                        .outcomes
                        .iter()
                        .position(|outcome| outcome.role == crate::PathOutcomeRole::NoMatch)
                {
                    path_observations.insert(PathObservation {
                        context: context.to_owned(),
                        decision: key.2,
                        outcome,
                    });
                }
                continue;
            }
            let outcome = if unwinding || state.raised {
                definition
                    .outcomes
                    .iter()
                    .position(|outcome| outcome.role == crate::PathOutcomeRole::Unhandled)
            } else {
                Some(0)
            };
            if let Some(outcome) = outcome {
                path_observations.insert(PathObservation {
                    context: context.to_owned(),
                    decision: key.2,
                    outcome,
                });
            }
        }
    }

    pub fn flush_semantic_current(&mut self) {
        let depth = self.current_stack().len();
        self.flush_semantic_depth(depth);
    }

    pub fn start_frame(&mut self, code_id: usize) {
        self.current_stack().push(ActiveFrame::new(code_id, None));
    }

    pub fn resume_frame(&mut self, code_id: usize, suspended_line: Option<i32>) {
        self.current_stack()
            .push(ActiveFrame::new(code_id, suspended_line));
    }

    pub fn suppress_frame_exit(&mut self, code_id: usize) {
        if let Some(frame) = self
            .current_stack()
            .iter_mut()
            .rev()
            .find(|frame| frame.code_id == code_id)
        {
            frame.suppress_exit = true;
        }
    }

    pub fn suspend_frame(&mut self, code_id: usize) {
        let depth = self.current_stack().len();
        self.pop_frame(code_id);
        self.clear_semantic_depth(depth);
    }

    pub fn line_event(
        &mut self,
        context: &str,
        path: &str,
        code_id: usize,
        first_line: i32,
        line: i32,
    ) {
        if self.branch && !self.sysmon_arcs {
            let path = self.interner.path_id(path);
            let previous = {
                let stack = self.current_stack();
                if stack.last().is_none_or(|frame| frame.code_id != code_id) {
                    stack.push(ActiveFrame::new(code_id, None));
                }
                let Some(frame) = stack.last_mut() else {
                    return;
                };
                // A resumed frame has a suspended line before its first path is known.
                let previous = frame
                    .previous_line
                    .filter(|_| frame.previous_path.is_none_or(|previous| previous == path));
                frame.previous_line = Some(line);
                frame.previous_path = Some(path);
                frame.suppress_exit = false;
                previous
            };
            let record = code_record(
                &mut self.code_records,
                &mut self.default_code_records,
                self.interner.context_id(context),
                context.is_empty(),
                code_id,
                path,
            );
            let start = previous.unwrap_or(-first_line);
            record.insert((start, line));
            return;
        }
        self.add_line(context, path, line);
        if !self.branch {
            return;
        }
        let previous = {
            let stack = self.current_stack();
            if stack.last().is_none_or(|frame| frame.code_id != code_id) {
                stack.push(ActiveFrame::new(code_id, None));
            }
            let Some(frame) = stack.last_mut() else {
                return;
            };
            let previous = frame.previous_line;
            frame.previous_line = Some(line);
            frame.suppress_exit = false;
            previous
        };
        if self.sysmon_arcs {
            self.add_arc(context, path, line, line);
        } else {
            self.add_arc(context, path, previous.unwrap_or(-first_line), line);
        }
    }

    pub fn end_frame(
        &mut self,
        context: &str,
        path: Option<&str>,
        code_id: usize,
        first_line: i32,
        return_line: Option<i32>,
    ) {
        let depth = self.current_stack().len();
        let frame = self.pop_frame_state(code_id);
        let previous = frame.as_ref().and_then(|frame| frame.previous_line);
        // A condition that raises, unwinds, or suspends before its decision
        // outcome is known is not evidence. Drop its partial vector with the
        // frame instead of allowing it to contaminate the next invocation at
        // the same recursion depth.
        self.clear_semantic_depth(depth);
        let end_line = if self.sysmon_arcs {
            return_line
        } else {
            previous
        };
        let previous_path = frame.as_ref().and_then(|frame| frame.previous_path);
        if frame.is_none_or(|frame| !frame.suppress_exit)
            && let Some(end_line) = end_line
        {
            if self.branch && !self.sysmon_arcs {
                // A return event can map elsewhere; its exit belongs to the last line's file.
                let Some(path) =
                    previous_path.or_else(|| path.map(|path| self.interner.path_id(path)))
                else {
                    return;
                };
                let record = code_record(
                    &mut self.code_records,
                    &mut self.default_code_records,
                    self.interner.context_id(context),
                    context.is_empty(),
                    code_id,
                    path,
                );
                if end_line != -first_line {
                    record.insert((end_line, -first_line));
                }
            } else if let Some(path) = path {
                self.add_arc(context, path, end_line, -first_line);
            }
        }
    }

    pub fn pop_frame(&mut self, code_id: usize) -> Option<i32> {
        self.pop_frame_state(code_id)
            .and_then(|frame| frame.previous_line)
    }

    fn pop_frame_state(&mut self, code_id: usize) -> Option<ActiveFrame> {
        let stack = self.current_stack();
        let index = stack.iter().rposition(|frame| frame.code_id == code_id)?;
        Some(stack.remove(index))
    }

    pub fn clear_semantic_depth(&mut self, depth: usize) {
        if !self.decisions {
            return;
        }
        let thread = current_thread_id();
        self.semantic_active
            .retain(|(candidate, candidate_depth, _), _| {
                *candidate != thread || *candidate_depth != depth
            });
        self.semantic_pending.remove(&(thread, depth));
    }

    pub fn flush_semantic_depth(&mut self, depth: usize) {
        let thread = current_thread_id();
        if let Some(observations) = self.semantic_pending.remove(&(thread, depth)) {
            self.semantic.observations.extend(observations);
        }
    }

    pub fn clear_semantic_frame(&mut self, code_id: usize) {
        let depth = self
            .current_stack()
            .iter()
            .rposition(|frame| frame.code_id == code_id)
            .map_or(0, |index| index + 1);
        if depth > 0 {
            self.clear_semantic_depth(depth);
        }
    }

    #[must_use]
    pub const fn branch(&self) -> bool {
        self.branch
    }

    #[must_use]
    pub const fn sysmon_arcs(&self) -> bool {
        self.sysmon_arcs
    }

    #[must_use]
    pub fn line_records(&self) -> Vec<(String, String, Vec<i32>)> {
        let mut records: Vec<_> = self
            .lines
            .iter()
            .enumerate()
            .flat_map(|(context, paths)| {
                let context = ContextId(context);
                paths.iter().map(move |(path, lines)| {
                    (
                        self.interner.context(context).to_owned(),
                        self.interner.path(path).to_owned(),
                        lines.sorted(),
                    )
                })
            })
            .collect();
        records.sort_unstable_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
        records
    }

    /// Borrow the file path of each line record without copying its evidence.
    pub fn line_paths(&self) -> impl Iterator<Item = &str> {
        self.lines
            .iter()
            .flat_map(|paths| paths.iter().map(|(path, _)| self.interner.path(path)))
    }

    /// Move line evidence out of the store and leave its line records empty.
    ///
    /// Each context's chunks are released as they move, so the store and the
    /// resulting coverage data never hold the evidence twice.
    pub fn take_lines(&mut self) -> TraceLines {
        self.share_finished_lines();
        let lines = std::mem::take(&mut self.lines);
        let mut records = Vec::new();
        for (context, paths) in lines.into_iter().enumerate() {
            let context = &self.interner.contexts[context];
            records.extend(paths.into_entries().map(|(path, lines)| {
                (
                    Shared::clone(context),
                    Shared::clone(&self.interner.paths[path.0]),
                    lines,
                )
            }));
        }
        TraceLines::sorted(records)
    }

    /// Share the line sets of contexts written since the last call.
    ///
    /// Call this when the active context changes. Equal sets from different
    /// contexts then use one allocation; a later write to a shared set copies
    /// it first.
    pub fn share_finished_lines(&mut self) {
        self.last_line_context = None;
        for context in self.unshared_line_contexts.drain(..) {
            if let Some(paths) = self.lines.get_mut(context.0) {
                for lines in paths.values_mut() {
                    self.line_sets.share(lines);
                }
            }
        }
    }

    /// Copy line evidence while the store keeps collecting.
    #[must_use]
    pub fn copy_lines(&self) -> TraceLines {
        let mut records = Vec::new();
        for (context, paths) in self.lines.iter().enumerate() {
            let context = &self.interner.contexts[context];
            records.extend(paths.iter().map(|(path, lines)| {
                (
                    Shared::clone(context),
                    Shared::clone(&self.interner.paths[path.0]),
                    lines.clone(),
                )
            }));
        }
        TraceLines::sorted(records)
    }

    #[must_use]
    pub fn arc_records(&self) -> Vec<(String, String, Vec<Arc>)> {
        let mut merged: HashMap<(ContextId, PathId), Vec<Arc>> = HashMap::default();
        for (context, paths) in self.arcs.iter().enumerate() {
            let context = ContextId(context);
            for (path, arcs) in paths.iter() {
                merged
                    .entry((context, path))
                    .or_default()
                    .extend(arcs.iter().copied());
            }
        }
        let default_context = self.interner.context_ids.get("").copied();
        for code in self.default_code_records.values() {
            if let Some(context) = default_context {
                for (path, arcs) in code.iter() {
                    merged
                        .entry((context, path))
                        .or_default()
                        .extend(arcs.iter().copied());
                }
            }
        }
        for (&context, codes) in &self.code_records {
            for code in codes.values() {
                for (path, arcs) in code.iter() {
                    merged
                        .entry((context, path))
                        .or_default()
                        .extend(arcs.iter().copied());
                }
            }
        }
        let mut records: Vec<_> = merged
            .into_iter()
            .map(|((context, path), mut arcs)| {
                arcs.sort_unstable();
                arcs.dedup();
                (
                    self.interner.context(context).to_owned(),
                    self.interner.path(path).to_owned(),
                    arcs,
                )
            })
            .collect();
        records.sort_unstable_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
        records
    }
}

impl TraceStore {
    pub fn clear_records(&mut self) {
        self.lines.clear();
        self.line_sets.clear();
        self.unshared_line_contexts.clear();
        self.last_line_context = None;
        self.arcs.clear();
        self.code_records.clear();
        self.default_code_records.clear();
        let mut interner = EvidenceInterner::default();
        for frame in self
            .active_stack
            .iter_mut()
            .chain(self.stacks.values_mut().flatten())
        {
            if let Some(path) = frame.previous_path {
                frame.previous_path = Some(interner.path_id(self.interner.path(path)));
            }
        }
        self.interner = interner;
        self.semantic.observations.clear();
        self.semantic.path_observations.clear();
    }

    pub fn reset_records(&mut self) {
        self.clear_records();
        self.semantic_active.clear();
        self.semantic_pending.clear();
        self.path_active.clear();
        self.value_active.clear();
    }

    #[must_use]
    pub fn with_options(options: TraceOptions) -> Self {
        let TraceOptions {
            branch,
            sysmon_arcs,
            decisions,
        } = options;
        Self {
            branch,
            sysmon_arcs,
            decisions,
            interner: EvidenceInterner::default(),
            lines: Vec::new(),
            line_sets: LineSetTable::default(),
            unshared_line_contexts: Vec::new(),
            last_line_context: None,
            arcs: Vec::new(),
            code_records: HashMap::default(),
            default_code_records: HashMap::default(),
            active_thread: current_thread_id(),
            active_stack: Vec::new(),
            stacks: HashMap::default(),
            semantic: SemanticData::default(),
            semantic_definition_keys: HashMap::default(),
            path_definition_keys: HashMap::default(),
            value_parent: HashMap::default(),
            semantic_active: HashMap::default(),
            semantic_pending: HashMap::default(),
            path_active: HashMap::default(),
            value_active: HashSet::default(),
        }
    }

    #[expect(
        clippy::too_many_arguments,
        reason = "disjoint runtime fields avoid cloning recursive decision keys"
    )]
    fn record_value_outcome(
        path_definitions: &std::collections::BTreeMap<String, PathDecisionDefinition>,
        path_observations: &mut std::collections::BTreeSet<PathObservation>,
        value_active: &mut HashSet<(ThreadId, usize, String)>,
        value_parent: &HashMap<String, (String, usize)>,
        context: &str,
        code_id: usize,
        decision: &str,
        outcome_index: usize,
        outcome: bool,
    ) {
        let Some(definition) = path_definitions.get(decision) else {
            return;
        };
        if outcome_index >= definition.outcomes.len().saturating_sub(1) {
            return;
        }
        let selected = (definition.kind == PathDecisionKind::ValueOr && outcome)
            || (definition.kind == PathDecisionKind::ValueAnd && !outcome);
        let thread = current_thread_id();
        let active_key = (thread, code_id, decision.to_owned());
        if selected {
            path_observations.insert(PathObservation {
                context: context.to_owned(),
                decision: decision.to_owned(),
                outcome: outcome_index,
            });
            value_active.remove(&active_key);
        } else {
            value_active.insert(active_key);
            return;
        }

        // CPython can jump directly from a nested short-circuit expression to
        // its parent's next operand. Propagate the completed Boolean value so
        // the parent observes the edge even when no parent-span event exists.
        if let Some((parent, parent_index)) = value_parent.get(decision) {
            Self::record_value_outcome(
                path_definitions,
                path_observations,
                value_active,
                value_parent,
                context,
                code_id,
                parent,
                *parent_index,
                outcome,
            );
        }
    }

    #[must_use]
    pub const fn semantic_data(&self) -> &SemanticData {
        &self.semantic
    }

    fn current_stack(&mut self) -> &mut Vec<ActiveFrame> {
        let current = current_thread_id();
        if current != self.active_thread {
            let previous = std::mem::take(&mut self.active_stack);
            if !previous.is_empty() {
                self.stacks.insert(self.active_thread, previous);
            }
            self.active_stack = self.stacks.remove(&current).unwrap_or_default();
            self.active_thread = current;
        }
        &mut self.active_stack
    }
}

#[cfg(test)]
mod tests;
