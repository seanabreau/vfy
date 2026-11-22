//! Validated semantic coverage domain model.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

mod discovery;
mod logic;

pub mod report;

pub use discovery::analyze_semantic_source_data;
use logic::McdcProofInputs;
pub use logic::{
    Logic, LogicParseError, mcdc_indices, parse_logic, parse_vector, render_vector,
    valid_observation,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DecisionDefinition {
    pub(crate) key: String,
    pub(crate) path: String,
    pub(crate) line: i32,
    pub(crate) end_line: i32,
    pub(crate) kind: String,
    pub(crate) source_hash: String,
    pub(crate) expression: String,
    pub(crate) logic: Logic,
    pub(crate) condition_lines: Vec<i32>,
    pub(crate) condition_ranges: Vec<(i32, i32, i32, i32)>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SemanticValidationError(pub(crate) &'static str);

impl fmt::Display for SemanticValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl Error for SemanticValidationError {}

impl DecisionDefinition {
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }
    #[must_use]
    pub const fn line(&self) -> i32 {
        self.line
    }
    #[must_use]
    pub const fn end_line(&self) -> i32 {
        self.end_line
    }
    #[must_use]
    pub fn kind(&self) -> &str {
        &self.kind
    }
    #[must_use]
    pub fn source_hash(&self) -> &str {
        &self.source_hash
    }
    #[must_use]
    pub fn expression(&self) -> &str {
        &self.expression
    }
    #[must_use]
    pub const fn logic(&self) -> &Logic {
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

    fn missing_condition_range(range: &(i32, i32, i32, i32)) -> bool {
        *range == (range.0, -1, range.0, -1)
    }

    fn is_compatible_with(&self, other: &Self) -> bool {
        self.key == other.key
            && self.path == other.path
            && self.line == other.line
            && self.end_line == other.end_line
            && self.kind == other.kind
            && self.source_hash == other.source_hash
            && self.expression == other.expression
            && self.logic == other.logic
            && self.condition_lines == other.condition_lines
            && self.condition_ranges.len() == other.condition_ranges.len()
            && self
                .condition_ranges
                .iter()
                .zip(&other.condition_ranges)
                .all(|(left, right)| {
                    left == right
                        || Self::missing_condition_range(left)
                        || Self::missing_condition_range(right)
                })
    }

    fn merge_condition_ranges(&mut self, other: &Self) {
        // Files without vfy_condition retain only each condition's starting line.
        for (range, incoming) in self
            .condition_ranges
            .iter_mut()
            .zip(&other.condition_ranges)
        {
            if Self::missing_condition_range(range) {
                *range = *incoming;
            }
        }
    }

    /// Construct a definition whose identity, ranges, and condition arity are consistent.
    ///
    /// # Errors
    ///
    /// Returns an error when any persisted field violates the semantic model.
    #[expect(
        clippy::too_many_arguments,
        reason = "Mirrors one persisted definition row plus its children"
    )]
    pub fn validated(
        key: String,
        path: String,
        line: i32,
        end_line: i32,
        kind: String,
        source_hash: String,
        expression: String,
        logic: Logic,
        condition_lines: Vec<i32>,
        condition_ranges: Vec<(i32, i32, i32, i32)>,
    ) -> Result<Self, SemanticValidationError> {
        let count = logic.condition_count();
        if key.is_empty() {
            return Err(SemanticValidationError("empty decision key"));
        }
        if path.is_empty() {
            return Err(SemanticValidationError("empty decision path"));
        }
        if line <= 0 || end_line < line {
            return Err(SemanticValidationError("invalid decision line range"));
        }
        if !matches!(
            kind.as_str(),
            "if" | "elif" | "while" | "assert" | "match_guard" | "if_expr" | "comprehension"
        ) {
            return Err(SemanticValidationError("unsupported boolean decision kind"));
        }
        if count == 0
            || !logic.has_consistent_condition_numbering()
            || condition_lines.len() != count
            || condition_ranges.len() != count
        {
            return Err(SemanticValidationError("inconsistent condition arity"));
        }
        if condition_ranges.iter().zip(&condition_lines).any(
            |(&(sl, sc, el, ec), &condition_line)| {
                sl < line
                    || sc < -1
                    || el < sl
                    || el > end_line
                    || ec < -1
                    || (sc == -1) != (ec == -1)
                    || (sl == el && ec < sc)
                    || condition_line != sl
            },
        ) {
            return Err(SemanticValidationError("invalid condition range"));
        }
        Ok(Self {
            key,
            path,
            line,
            end_line,
            kind,
            source_hash,
            expression,
            logic,
            condition_lines,
            condition_ranges,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct SemanticObservation {
    pub(crate) context: String,
    pub(crate) decision: String,
    pub(crate) vector: String,
    pub(crate) outcome: bool,
}

impl SemanticObservation {
    #[must_use]
    pub fn context(&self) -> &str {
        &self.context
    }
    #[must_use]
    pub fn decision(&self) -> &str {
        &self.decision
    }
    #[must_use]
    pub fn vector(&self) -> &str {
        &self.vector
    }
    #[must_use]
    pub const fn outcome(&self) -> bool {
        self.outcome
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PathOutcomeDefinition {
    pub(crate) label: String,
    pub(crate) range: (i32, i32, i32, i32),
    pub(crate) role: PathOutcomeRole,
}

impl PathOutcomeDefinition {
    #[must_use]
    pub fn label(&self) -> &str {
        &self.label
    }
    #[must_use]
    pub const fn range(&self) -> (i32, i32, i32, i32) {
        self.range
    }
    #[must_use]
    pub const fn role(&self) -> PathOutcomeRole {
        self.role
    }

    /// Construct a path outcome with a valid label, source range, and role body.
    ///
    /// # Errors
    ///
    /// Returns an error when a persisted field violates the semantic model.
    pub fn validated(
        label: String,
        range: (i32, i32, i32, i32),
        role: PathOutcomeRole,
    ) -> Result<Self, SemanticValidationError> {
        if label.is_empty() {
            return Err(SemanticValidationError("empty path outcome label"));
        }
        let (start_line, start_col, end_line, end_col) = range;
        if start_line <= 0
            || start_col < 0
            || end_line < start_line
            || end_col < 0
            || (start_line == end_line && end_col < start_col)
        {
            return Err(SemanticValidationError("invalid path outcome range"));
        }
        if role
            .body()
            .is_some_and(|(start, end)| start <= 0 || end < start)
        {
            return Err(SemanticValidationError("invalid path outcome body range"));
        }
        Ok(Self { label, range, role })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PathOutcomeRole {
    Normal,
    Handler { body: (i32, i32) },
    MatchCase { body: (i32, i32) },
    Unhandled,
    NoMatch,
    Cancelled,
    Operand,
}

impl PathOutcomeRole {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Normal => "normal",
            Self::Handler { .. } => "handler",
            Self::MatchCase { .. } => "match_case",
            Self::Unhandled => "unhandled",
            Self::NoMatch => "no_match",
            Self::Cancelled => "cancelled",
            Self::Operand => "operand",
        }
    }

    #[must_use]
    pub fn parse(value: &str, body: Option<(i32, i32)>) -> Option<Self> {
        match (value, body) {
            ("normal", None) => Some(Self::Normal),
            ("handler", Some(body)) => Some(Self::Handler { body }),
            ("match_case", Some(body)) => Some(Self::MatchCase { body }),
            ("unhandled", None) => Some(Self::Unhandled),
            ("no_match", None) => Some(Self::NoMatch),
            ("cancelled", None) => Some(Self::Cancelled),
            ("operand", None) => Some(Self::Operand),
            _ => None,
        }
    }

    #[must_use]
    pub const fn handler_body(self) -> Option<(i32, i32)> {
        match self {
            Self::Handler { body } => Some(body),
            _ => None,
        }
    }

    #[must_use]
    pub const fn body(self) -> Option<(i32, i32)> {
        match self {
            Self::Handler { body } | Self::MatchCase { body } => Some(body),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PathDecisionKind {
    Try,
    TryStar,
    AsyncCancellation,
    ValueAnd,
    ValueOr,
    Match,
}

impl PathDecisionKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Try => "try",
            Self::TryStar => "try_star",
            Self::AsyncCancellation => "async_cancellation",
            Self::ValueAnd => "value_and",
            Self::ValueOr => "value_or",
            Self::Match => "match",
        }
    }

    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "try" => Some(Self::Try),
            "try_star" => Some(Self::TryStar),
            "async_cancellation" => Some(Self::AsyncCancellation),
            "value_and" => Some(Self::ValueAnd),
            "value_or" => Some(Self::ValueOr),
            "match" => Some(Self::Match),
            _ => None,
        }
    }

    #[must_use]
    pub const fn is_try(self) -> bool {
        matches!(self, Self::Try | Self::TryStar)
    }

    #[must_use]
    pub const fn is_value(self) -> bool {
        matches!(self, Self::ValueAnd | Self::ValueOr)
    }

    #[must_use]
    pub const fn is_match(self) -> bool {
        matches!(self, Self::Match)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PathDecisionDefinition {
    pub(crate) key: String,
    pub(crate) path: String,
    pub(crate) line: i32,
    pub(crate) end_line: i32,
    pub(crate) kind: PathDecisionKind,
    pub(crate) source_hash: String,
    pub(crate) expression: String,
    pub(crate) body_start: i32,
    pub(crate) body_end: i32,
    pub(crate) range: (i32, i32, i32, i32),
    pub(crate) outcomes: Vec<PathOutcomeDefinition>,
}

impl PathDecisionDefinition {
    #[must_use]
    pub fn key(&self) -> &str {
        &self.key
    }
    #[must_use]
    pub fn path(&self) -> &str {
        &self.path
    }
    #[must_use]
    pub const fn line(&self) -> i32 {
        self.line
    }
    #[must_use]
    pub const fn end_line(&self) -> i32 {
        self.end_line
    }
    #[must_use]
    pub const fn kind(&self) -> PathDecisionKind {
        self.kind
    }
    #[must_use]
    pub fn source_hash(&self) -> &str {
        &self.source_hash
    }
    #[must_use]
    pub fn expression(&self) -> &str {
        &self.expression
    }
    #[must_use]
    pub const fn body_start(&self) -> i32 {
        self.body_start
    }
    #[must_use]
    pub const fn body_end(&self) -> i32 {
        self.body_end
    }
    #[must_use]
    pub const fn range(&self) -> (i32, i32, i32, i32) {
        self.range
    }
    #[must_use]
    pub fn outcomes(&self) -> &[PathOutcomeDefinition] {
        &self.outcomes
    }

    fn has_header_range(&self) -> bool {
        // Schema-7 path rows retain line bounds, but omit the header range.
        self.range != (self.line, 0, self.end_line, 0)
    }

    fn is_compatible_with(&self, other: &Self) -> bool {
        self.key == other.key
            && self.path == other.path
            && self.line == other.line
            && self.end_line == other.end_line
            && self.kind == other.kind
            && self.source_hash == other.source_hash
            && self.expression == other.expression
            && self.body_start == other.body_start
            && self.body_end == other.body_end
            && self.outcomes == other.outcomes
            && (self.range == other.range || !self.has_header_range() || !other.has_header_range())
    }

    fn merge_header_range(&mut self, other: &Self) {
        if !self.has_header_range() {
            self.range = other.range;
        }
    }

    /// Construct a path definition with valid source, body, and outcome ranges.
    ///
    /// # Errors
    ///
    /// Returns an error when any persisted field violates the semantic model.
    #[expect(
        clippy::too_many_arguments,
        reason = "Mirrors one persisted definition row plus its children"
    )]
    pub fn validated(
        key: String,
        path: String,
        line: i32,
        end_line: i32,
        kind: PathDecisionKind,
        source_hash: String,
        expression: String,
        body_start: i32,
        body_end: i32,
        range: (i32, i32, i32, i32),
        outcomes: Vec<PathOutcomeDefinition>,
    ) -> Result<Self, SemanticValidationError> {
        if key.is_empty() || path.is_empty() {
            return Err(SemanticValidationError("empty path decision identity"));
        }
        if line <= 0 || end_line < line || body_start <= 0 || body_end < body_start {
            return Err(SemanticValidationError("invalid path decision range"));
        }
        let (range_start_line, range_start_col, range_end_line, range_end_col) = range;
        if range_start_line != line
            || range_start_col < 0
            || range_end_line < range_start_line
            || range_end_line > end_line
            || range_end_col < 0
            || (range_start_line == range_end_line && range_end_col < range_start_col)
        {
            return Err(SemanticValidationError(
                "invalid path decision source range",
            ));
        }
        if outcomes.len() < 2 {
            return Err(SemanticValidationError(
                "path decision has fewer than two outcomes",
            ));
        }
        if outcomes.iter().any(|outcome| outcome.label.is_empty()) {
            return Err(SemanticValidationError("empty path outcome label"));
        }
        if outcomes.iter().any(|outcome| {
            outcome
                .role
                .body()
                .is_some_and(|(start, end)| start <= 0 || end < start)
        }) {
            return Err(SemanticValidationError("invalid path outcome body range"));
        }
        if outcomes.iter().any(|outcome| {
            let (outcome_start_line, start_col, outcome_end_line, end_col) = outcome.range;
            outcome_start_line < line
                || start_col < 0
                || outcome_end_line < outcome_start_line
                || outcome_end_line > end_line
                || end_col < 0
                || (outcome_start_line == outcome_end_line && end_col < start_col)
        }) {
            return Err(SemanticValidationError("invalid path outcome range"));
        }
        Ok(Self {
            key,
            path,
            line,
            end_line,
            kind,
            source_hash,
            expression,
            body_start,
            body_end,
            range,
            outcomes,
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct PathObservation {
    pub(crate) context: String,
    pub(crate) decision: String,
    pub(crate) outcome: usize,
}

impl PathObservation {
    #[must_use]
    pub fn context(&self) -> &str {
        &self.context
    }
    #[must_use]
    pub fn decision(&self) -> &str {
        &self.decision
    }
    #[must_use]
    pub const fn outcome(&self) -> usize {
        self.outcome
    }
}

pub type PathReportRecord = (
    String,
    String,
    i32,
    String,
    String,
    usize,
    usize,
    Vec<String>,
);

pub type SemanticReportRecord = (
    String,
    String,
    i32,
    String,
    String,
    usize,
    usize,
    usize,
    usize,
    usize,
    usize,
    Vec<String>,
);

pub type SemanticEvidenceRecord = (String, usize, String, String, bool, String, String, bool);

#[derive(Clone, Debug, Default)]
pub struct SemanticData {
    pub(crate) definitions: BTreeMap<String, DecisionDefinition>,
    pub(crate) observations: BTreeSet<SemanticObservation>,
    pub(crate) path_definitions: BTreeMap<String, PathDecisionDefinition>,
    pub(crate) path_observations: BTreeSet<PathObservation>,
}

impl SemanticData {
    #[must_use]
    pub const fn definitions(&self) -> &BTreeMap<String, DecisionDefinition> {
        &self.definitions
    }
    #[must_use]
    pub const fn observations(&self) -> &BTreeSet<SemanticObservation> {
        &self.observations
    }
    #[must_use]
    pub const fn path_definitions(&self) -> &BTreeMap<String, PathDecisionDefinition> {
        &self.path_definitions
    }
    #[must_use]
    pub const fn path_observations(&self) -> &BTreeSet<PathObservation> {
        &self.path_observations
    }
    #[must_use]
    pub fn has_data(&self) -> bool {
        !self.definitions.is_empty() || !self.path_definitions.is_empty()
    }

    /// Insert one observation after validating its definition and vector.
    ///
    /// # Errors
    ///
    /// Returns an error when the decision is absent or the observation is inconsistent.
    pub fn insert_observation(
        &mut self,
        context: String,
        decision: String,
        vector: String,
        outcome: bool,
    ) -> Result<(), SemanticValidationError> {
        let Some(definition) = self.definitions.get(&decision) else {
            return Err(SemanticValidationError(
                "observation references an absent decision",
            ));
        };
        if !valid_observation(&definition.logic, &vector, outcome) {
            return Err(SemanticValidationError("invalid semantic observation"));
        }
        self.observations.insert(SemanticObservation {
            context,
            decision,
            vector,
            outcome,
        });
        Ok(())
    }

    /// Insert one named-path observation after validating its outcome index.
    ///
    /// # Errors
    ///
    /// Returns an error when the decision is absent or the outcome is out of range.
    pub fn insert_path_observation(
        &mut self,
        context: String,
        decision: String,
        outcome: usize,
    ) -> Result<(), SemanticValidationError> {
        let Some(definition) = self.path_definitions.get(&decision) else {
            return Err(SemanticValidationError(
                "path observation references an absent decision",
            ));
        };
        if outcome >= definition.outcomes.len() {
            return Err(SemanticValidationError("invalid path observation outcome"));
        }
        self.path_observations.insert(PathObservation {
            context,
            decision,
            outcome,
        });
        Ok(())
    }

    /// Insert one uniquely identified Boolean decision.
    ///
    /// # Errors
    ///
    /// Returns an error when the decision key is already present.
    pub fn insert_definition(
        &mut self,
        definition: DecisionDefinition,
    ) -> Result<(), SemanticValidationError> {
        if self.definitions.contains_key(&definition.key) {
            return Err(SemanticValidationError("duplicate decision key"));
        }
        self.definitions.insert(definition.key.clone(), definition);
        Ok(())
    }

    /// Insert one uniquely identified path decision.
    ///
    /// # Errors
    ///
    /// Returns an error when the decision key is already present.
    pub fn insert_path_definition(
        &mut self,
        definition: PathDecisionDefinition,
    ) -> Result<(), SemanticValidationError> {
        if self.path_definitions.contains_key(&definition.key) {
            return Err(SemanticValidationError("duplicate path decision key"));
        }
        self.path_definitions
            .insert(definition.key.clone(), definition);
        Ok(())
    }

    /// Validate that another model can be merged without mixing source revisions.
    ///
    /// # Errors
    ///
    /// Returns an error for mixed source revisions or conflicting definitions outside
    /// the paths explicitly being replaced.
    pub(crate) fn validate_merge(
        &self,
        other: &Self,
        replaced_paths: &BTreeSet<String>,
    ) -> Result<(), &'static str> {
        Self::validate_source_revisions(
            self.source_revisions()
                .filter(|(path, _)| !replaced_paths.contains(*path))
                .chain(other.source_revisions()),
        )
        .map_err(|(_, invariant)| invariant)?;
        let conflicting_definition = other.definitions.iter().any(|(key, definition)| {
            self.definitions.get(key).is_some_and(|existing| {
                !replaced_paths.contains(&existing.path)
                    && existing.source_hash != definition.source_hash
            })
        });
        let conflicting_path_definition = other.path_definitions.iter().any(|(key, definition)| {
            self.path_definitions.get(key).is_some_and(|existing| {
                !replaced_paths.contains(&existing.path)
                    && existing.source_hash != definition.source_hash
            })
        });
        if conflicting_definition || conflicting_path_definition {
            return Err("cannot combine semantic data from different source revisions");
        }
        if other.definitions.iter().any(|(key, definition)| {
            self.definitions.get(key).is_some_and(|existing| {
                !replaced_paths.contains(&existing.path) && !existing.is_compatible_with(definition)
            })
        }) || other.path_definitions.iter().any(|(key, definition)| {
            self.path_definitions.get(key).is_some_and(|existing| {
                !replaced_paths.contains(&existing.path) && !existing.is_compatible_with(definition)
            })
        }) {
            return Err("conflicting semantic definitions");
        }
        Ok(())
    }

    pub(crate) fn source_revisions(&self) -> impl Iterator<Item = (&str, &str)> {
        self.definitions
            .values()
            .map(|definition| (definition.path.as_str(), definition.source_hash.as_str()))
            .chain(
                self.path_definitions
                    .values()
                    .map(|definition| (definition.path.as_str(), definition.source_hash.as_str())),
            )
    }

    fn validate_source_revisions<'a>(
        revisions: impl Iterator<Item = (&'a str, &'a str)>,
    ) -> Result<(), (String, &'static str)> {
        let mut hashes = BTreeMap::new();
        for (path, hash) in revisions {
            if hashes
                .insert(path, hash)
                .is_some_and(|previous| previous != hash)
            {
                return Err((
                    path.to_owned(),
                    "cannot combine semantic data from different source revisions",
                ));
            }
        }
        Ok(())
    }

    pub(crate) fn merge_validated(&mut self, other: Self) {
        for definition in other.definitions.into_values() {
            if let Some(existing) = self.definitions.get_mut(&definition.key)
                && existing.is_compatible_with(&definition)
            {
                existing.merge_condition_ranges(&definition);
            } else {
                self.definitions.insert(definition.key.clone(), definition);
            }
        }
        self.observations.extend(other.observations);
        for definition in other.path_definitions.into_values() {
            if let Some(existing) = self.path_definitions.get_mut(&definition.key)
                && existing.is_compatible_with(&definition)
            {
                existing.merge_header_range(&definition);
            } else {
                self.path_definitions
                    .insert(definition.key.clone(), definition);
            }
        }
        self.path_observations.extend(other.path_observations);
    }

    /// Validate and merge another semantic model atomically.
    ///
    /// # Errors
    ///
    /// Returns an error without mutation for mixed source revisions or conflicting definitions.
    pub fn merge(&mut self, other: Self) -> Result<(), &'static str> {
        self.validate_merge(&other, &BTreeSet::new())?;
        self.merge_validated(other);
        Ok(())
    }

    pub(crate) fn normalize_generated_keys(self) -> Result<Self, (String, &'static str)> {
        self.remap_paths(&BTreeMap::new())
    }

    pub(crate) fn remap_paths(
        mut self,
        paths: &BTreeMap<String, String>,
    ) -> Result<Self, (String, &'static str)> {
        let mut namespaces = BTreeMap::new();
        let mut decision_keys = BTreeMap::new();
        let mut definitions: BTreeMap<String, DecisionDefinition> = BTreeMap::new();
        for mut definition in self.definitions.into_values() {
            let previous = definition.key.clone();
            if let Some(path) = paths.get(&definition.path) {
                definition.path.clone_from(path);
            }
            definition.key =
                discovery::remap_semantic_key(&definition.key, &definition.path, &mut namespaces);
            if previous != definition.key {
                decision_keys.insert(previous.clone(), definition.key.clone());
            }
            if let Some(existing) = definitions.get_mut(&definition.key) {
                if existing.source_hash != definition.source_hash {
                    return Err((
                        previous,
                        "cannot combine semantic data from different source revisions",
                    ));
                }
                if !existing.is_compatible_with(&definition) {
                    return Err((previous, "conflicting semantic definitions"));
                }
                existing.merge_condition_ranges(&definition);
            } else {
                definitions.insert(definition.key.clone(), definition);
            }
        }
        self.definitions = definitions;
        if !decision_keys.is_empty() {
            self.observations = self
                .observations
                .into_iter()
                .map(|mut observation| {
                    if let Some(key) = decision_keys.get(&observation.decision) {
                        observation.decision.clone_from(key);
                    }
                    observation
                })
                .collect();
        }

        let mut path_keys = BTreeMap::new();
        let mut path_definitions: BTreeMap<String, PathDecisionDefinition> = BTreeMap::new();
        for mut definition in self.path_definitions.into_values() {
            let previous = definition.key.clone();
            if let Some(path) = paths.get(&definition.path) {
                definition.path.clone_from(path);
            }
            definition.key =
                discovery::remap_semantic_key(&definition.key, &definition.path, &mut namespaces);
            if previous != definition.key {
                path_keys.insert(previous.clone(), definition.key.clone());
            }
            if let Some(existing) = path_definitions.get_mut(&definition.key) {
                if existing.source_hash != definition.source_hash {
                    return Err((
                        previous,
                        "cannot combine semantic data from different source revisions",
                    ));
                }
                if !existing.is_compatible_with(&definition) {
                    return Err((previous, "conflicting semantic definitions"));
                }
                existing.merge_header_range(&definition);
            } else {
                path_definitions.insert(definition.key.clone(), definition);
            }
        }
        self.path_definitions = path_definitions;
        if !path_keys.is_empty() {
            self.path_observations = self
                .path_observations
                .into_iter()
                .map(|mut observation| {
                    if let Some(key) = path_keys.get(&observation.decision) {
                        observation.decision.clone_from(key);
                    }
                    observation
                })
                .collect();
        }
        Self::validate_source_revisions(self.source_revisions())?;
        Ok(self)
    }

    #[must_use]
    #[expect(
        clippy::type_complexity,
        reason = "Stable batched storage-boundary shape"
    )]
    pub fn path_records(
        &self,
    ) -> Vec<(String, String, i32, i32, String, String, String, i32, i32)> {
        self.path_definitions
            .values()
            .map(|definition| {
                (
                    definition.key.clone(),
                    definition.path.clone(),
                    definition.line,
                    definition.end_line,
                    definition.kind.as_str().to_owned(),
                    definition.source_hash.clone(),
                    definition.expression.clone(),
                    definition.body_start,
                    definition.body_end,
                )
            })
            .collect()
    }

    #[must_use]
    pub fn path_outcome_records(&self) -> Vec<(String, usize, String, i32, i32, i32, i32)> {
        self.path_definitions
            .values()
            .flat_map(|definition| {
                definition
                    .outcomes
                    .iter()
                    .enumerate()
                    .map(move |(index, outcome)| {
                        (
                            definition.key.clone(),
                            index,
                            outcome.label.clone(),
                            outcome.range.0,
                            outcome.range.1,
                            outcome.range.2,
                            outcome.range.3,
                        )
                    })
            })
            .collect()
    }

    #[must_use]
    pub fn path_observation_records(&self) -> Vec<(String, String, usize)> {
        self.path_observations
            .iter()
            .map(|observation| {
                (
                    observation.context.clone(),
                    observation.decision.clone(),
                    observation.outcome,
                )
            })
            .collect()
    }

    #[must_use]
    pub fn path_report_records(
        &self,
        selected: &[String],
        contexts: &[String],
    ) -> Vec<PathReportRecord> {
        let selected: BTreeSet<&str> = selected.iter().map(String::as_str).collect();
        let contexts: BTreeSet<&str> = contexts.iter().map(String::as_str).collect();
        let observations = self.selected_path_observations(&selected, &contexts);
        self.path_definitions
            .values()
            .filter(|definition| selected.is_empty() || selected.contains(definition.key.as_str()))
            .map(|definition| {
                let covered: BTreeSet<_> = observations
                    .get(definition.key.as_str())
                    .into_iter()
                    .flatten()
                    .map(|observation| observation.outcome)
                    .collect();
                let missing = definition
                    .outcomes
                    .iter()
                    .enumerate()
                    .filter(|(index, _)| !covered.contains(index))
                    .map(|(_, outcome)| outcome.label.clone())
                    .collect();
                (
                    definition.key.clone(),
                    definition.path.clone(),
                    definition.line,
                    definition.expression.clone(),
                    definition.kind.as_str().to_owned(),
                    covered.len(),
                    definition.outcomes.len(),
                    missing,
                )
            })
            .collect()
    }

    #[must_use]
    #[expect(
        clippy::type_complexity,
        reason = "Stable batched storage-boundary shape"
    )]
    pub fn records(&self) -> Vec<(String, String, i32, i32, String, String, String, String)> {
        self.definitions
            .values()
            .map(|definition| {
                (
                    definition.key.clone(),
                    definition.path.clone(),
                    definition.line,
                    definition.end_line,
                    definition.kind.clone(),
                    definition.source_hash.clone(),
                    definition.expression.clone(),
                    definition.logic.render(),
                )
            })
            .collect()
    }

    #[must_use]
    pub fn observation_records(&self) -> Vec<(String, String, String, bool)> {
        self.observations
            .iter()
            .map(|observation| {
                (
                    observation.context.clone(),
                    observation.decision.clone(),
                    observation.vector.clone(),
                    observation.outcome,
                )
            })
            .collect()
    }

    #[must_use]
    pub fn condition_records(&self) -> Vec<(String, usize, i32, i32, i32, i32)> {
        self.definitions
            .values()
            .flat_map(|definition| {
                definition.condition_ranges.iter().enumerate().map(
                    move |(index, &(line, start, end_line, end))| {
                        (definition.key.clone(), index, line, start, end_line, end)
                    },
                )
            })
            .collect()
    }

    fn selected_path_observations(
        &self,
        selected: &BTreeSet<&str>,
        contexts: &BTreeSet<&str>,
    ) -> BTreeMap<&str, Vec<&PathObservation>> {
        let mut grouped: BTreeMap<&str, Vec<&PathObservation>> = BTreeMap::new();
        let single = selected.first().copied().filter(|_| selected.len() == 1);
        for observation in &self.path_observations {
            if single.map_or_else(
                || selected.is_empty() || selected.contains(observation.decision.as_str()),
                |decision| observation.decision == decision,
            ) && (contexts.is_empty() || contexts.contains(observation.context.as_str()))
            {
                grouped
                    .entry(observation.decision.as_str())
                    .or_default()
                    .push(observation);
            }
        }
        grouped
    }

    fn selected_observations(
        &self,
        selected: &BTreeSet<&str>,
        contexts: &BTreeSet<&str>,
    ) -> BTreeMap<&str, Vec<&SemanticObservation>> {
        let mut grouped: BTreeMap<&str, Vec<&SemanticObservation>> = BTreeMap::new();
        let single = selected.first().copied().filter(|_| selected.len() == 1);
        for observation in &self.observations {
            if single.map_or_else(
                || selected.is_empty() || selected.contains(observation.decision.as_str()),
                |decision| observation.decision == decision,
            ) && (contexts.is_empty() || contexts.contains(observation.context.as_str()))
            {
                grouped
                    .entry(observation.decision.as_str())
                    .or_default()
                    .push(observation);
            }
        }
        grouped
    }

    #[must_use]
    pub fn report_records(
        &self,
        selected: &[String],
        contexts: &[String],
    ) -> Vec<SemanticReportRecord> {
        let selected: BTreeSet<&str> = selected.iter().map(String::as_str).collect();
        let contexts: BTreeSet<&str> = contexts.iter().map(String::as_str).collect();
        let observations = self.selected_observations(&selected, &contexts);
        self.definitions
            .values()
            .filter(|definition| selected.is_empty() || selected.contains(definition.key.as_str()))
            .map(|definition| {
                let condition_count = definition.logic.condition_count();
                let mut condition_values = vec![BTreeSet::new(); condition_count];
                let proof = McdcProofInputs::from_encoded(
                    &definition.logic,
                    observations
                        .get(definition.key.as_str())
                        .into_iter()
                        .flatten()
                        .map(|observation| (observation.vector.as_str(), observation.outcome)),
                );
                for vector in proof.vectors() {
                    for (index, value) in vector.values.iter().enumerate().take(condition_count) {
                        if let Some(value) = value {
                            condition_values[index].insert(*value);
                        }
                    }
                }
                let outcomes: BTreeSet<bool> = proof
                    .vectors()
                    .iter()
                    .map(|vector| vector.outcome)
                    .collect();
                let mcdc = proof.covered_indices();
                let mut missing = Vec::new();
                if !outcomes.contains(&true) {
                    missing.push("decision=true".to_owned());
                }
                if !outcomes.contains(&false) {
                    missing.push("decision=false".to_owned());
                }
                for (index, values) in condition_values.iter().enumerate() {
                    if !values.contains(&true) {
                        missing.push(format!("condition {}=true", index + 1));
                    }
                    if !values.contains(&false) {
                        missing.push(format!("condition {}=false", index + 1));
                    }
                }
                for index in 0..condition_count {
                    if !mcdc.contains(&index) {
                        missing.push(format!("condition {} lacks independent effect", index + 1));
                    }
                }
                (
                    definition.key.clone(),
                    definition.path.clone(),
                    definition.line,
                    definition.expression.clone(),
                    definition.kind.clone(),
                    outcomes.len(),
                    2,
                    condition_values.iter().map(BTreeSet::len).sum(),
                    condition_count * 2,
                    mcdc.len(),
                    condition_count,
                    missing,
                )
            })
            .collect()
    }

    #[must_use]
    pub fn evidence_records(
        &self,
        selected: &[String],
        contexts: &[String],
    ) -> Vec<SemanticEvidenceRecord> {
        let selected: BTreeSet<&str> = selected.iter().map(String::as_str).collect();
        let contexts: BTreeSet<&str> = contexts.iter().map(String::as_str).collect();
        let observations = self.selected_observations(&selected, &contexts);
        let mut records = BTreeSet::new();
        for definition in self
            .definitions
            .values()
            .filter(|definition| selected.is_empty() || selected.contains(definition.key.as_str()))
        {
            let by_vector: BTreeMap<(&str, bool), Vec<_>> = observations
                .get(definition.key.as_str())
                .into_iter()
                .flatten()
                .fold(BTreeMap::new(), |mut grouped, observation| {
                    grouped
                        .entry((observation.vector.as_str(), observation.outcome))
                        .or_insert_with(Vec::new)
                        .push(observation);
                    grouped
                });
            let proof = McdcProofInputs::from_encoded(&definition.logic, by_vector.keys().copied());
            proof.for_each_pair(|target, left, right| {
                let left_vector = &proof.vectors()[left];
                let right_vector = &proof.vectors()[right];
                if let (Some(left_group), Some(right_group)) = (
                    left_vector
                        .encoded
                        .and_then(|encoded| by_vector.get(&(encoded, left_vector.outcome))),
                    right_vector
                        .encoded
                        .and_then(|encoded| by_vector.get(&(encoded, right_vector.outcome))),
                ) {
                    for &left_observation in left_group {
                        for &right_observation in right_group {
                            let (first, second) = if left_observation <= right_observation {
                                (left_observation, right_observation)
                            } else {
                                (right_observation, left_observation)
                            };
                            records.insert((
                                definition.key.clone(),
                                target,
                                first.context.clone(),
                                first.vector.clone(),
                                first.outcome,
                                second.context.clone(),
                                second.vector.clone(),
                                second.outcome,
                            ));
                        }
                    }
                }
            });
        }
        records.into_iter().collect()
    }
}

#[cfg(test)]
mod tests;
