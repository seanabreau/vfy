use std::collections::BTreeSet;
use std::fmt;
use std::ops::ControlFlow;

const MAX_LOGIC_LENGTH: usize = 64 * 1024;
const MAX_LOGIC_DEPTH: usize = 64;
const MAX_CONDITIONS: usize = 4096;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LogicParseError {
    Syntax,
    Value,
    Length,
    ResourceLimit,
}

impl fmt::Display for LogicParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Syntax => "invalid logic syntax",
            Self::Value => "invalid logic value",
            Self::Length => "logic text is too long",
            Self::ResourceLimit => "logic exceeds the supported resource limit",
        })
    }
}

impl std::error::Error for LogicParseError {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Logic {
    Condition(usize),
    And(Vec<Logic>),
    Or(Vec<Logic>),
    Not(Box<Logic>),
}

impl Logic {
    #[must_use]
    pub fn has_consistent_condition_numbering(&self) -> bool {
        fn visit(logic: &Logic, indices: &mut Vec<usize>) {
            match logic {
                Logic::Condition(index) => indices.push(*index),
                Logic::And(values) | Logic::Or(values) => {
                    for value in values {
                        visit(value, indices);
                    }
                }
                Logic::Not(value) => visit(value, indices),
            }
        }
        let mut indices = Vec::new();
        visit(self, &mut indices);
        indices.sort_unstable();
        indices.iter().copied().eq(0..indices.len())
    }

    #[must_use]
    pub fn condition_count(&self) -> usize {
        match self {
            Self::Condition(index) => index.saturating_add(1),
            Self::And(values) | Self::Or(values) => {
                values.iter().map(Self::condition_count).max().unwrap_or(0)
            }
            Self::Not(value) => value.condition_count(),
        }
    }

    #[must_use]
    pub fn render(&self) -> String {
        match self {
            Self::Condition(index) => format!("c{index}"),
            Self::And(values) => format!(
                "and({})",
                values
                    .iter()
                    .map(Self::render)
                    .collect::<Vec<_>>()
                    .join(",")
            ),
            Self::Or(values) => format!(
                "or({})",
                values
                    .iter()
                    .map(Self::render)
                    .collect::<Vec<_>>()
                    .join(",")
            ),
            Self::Not(value) => format!("not({})", value.render()),
        }
    }

    /// Evaluate an observation vector using Python's left-to-right
    /// short-circuit execution model.
    ///
    /// An unevaluated condition stops the current expression: `0-` is valid for
    /// `and(c0,c1)`, but `-0` is not. MC/DC may treat a missing value as masked
    /// only after this check establishes that short-circuiting explains it.
    #[must_use]
    pub fn evaluate_observation(&self, values: &[Option<bool>]) -> Option<bool> {
        let mut visited = vec![false; self.condition_count()];
        let outcome = self.evaluate_observation_inner(values, &mut visited)?;
        // A persisted vector must not contain a value for an operand that
        // Python skipped after a short-circuit boundary (for example `0-1`
        // is not a possible vector for `and(c0,c1,c2)`).
        if values
            .iter()
            .enumerate()
            .any(|(index, value)| value.is_some() && !visited.get(index).copied().unwrap_or(false))
        {
            return None;
        }
        Some(outcome)
    }

    fn evaluate_observation_inner(
        &self,
        values: &[Option<bool>],
        visited: &mut [bool],
    ) -> Option<bool> {
        match self {
            Self::Condition(index) => {
                let value = values.get(*index).copied().flatten();
                if value.is_some()
                    && let Some(slot) = visited.get_mut(*index)
                {
                    *slot = true;
                }
                value
            }
            Self::And(nodes) => {
                for node in nodes {
                    match node.evaluate_observation_inner(values, visited) {
                        Some(false) => return Some(false),
                        Some(true) => {}
                        None => return None,
                    }
                }
                Some(true)
            }
            Self::Or(nodes) => {
                for node in nodes {
                    match node.evaluate_observation_inner(values, visited) {
                        Some(true) => return Some(true),
                        Some(false) => {}
                        None => return None,
                    }
                }
                Some(false)
            }
            Self::Not(value) => value
                .evaluate_observation_inner(values, visited)
                .map(|value| !value),
        }
    }

    /// Return the decision outcomes that are possible for a pair of test
    /// observations when `target` is the condition under proof.
    ///
    /// Each non-target condition takes a value observed by either test. Combine
    /// the four possible Boolean pairs through each `and`, `or`, and `not` node
    /// to find a shared assignment where changing only `target` changes the outcome.
    fn possible_pairs(&self, target: usize, target_values: (bool, bool), domains: &[u8]) -> u8 {
        const FALSE_FALSE: u8 = 1;
        const TRUE_TRUE: u8 = 1 << 3;

        fn pair_bit(left: bool, right: bool) -> u8 {
            1 << (u8::from(left) * 2 + u8::from(right))
        }

        fn combine(left: u8, right: u8, is_and: bool) -> u8 {
            let mut output = 0;
            for left_bit in 0..4 {
                if left & (1 << left_bit) == 0 {
                    continue;
                }
                let left_value = left_bit >= 2;
                let left_other = left_bit % 2 == 1;
                for right_bit in 0..4 {
                    if right & (1 << right_bit) == 0 {
                        continue;
                    }
                    let right_value = right_bit >= 2;
                    let right_other = right_bit % 2 == 1;
                    let value = if is_and {
                        (left_value && right_value, left_other && right_other)
                    } else {
                        (left_value || right_value, left_other || right_other)
                    };
                    output |= pair_bit(value.0, value.1);
                }
            }
            output
        }

        match self {
            Self::Condition(index) if *index == target => {
                pair_bit(target_values.0, target_values.1)
            }
            Self::Condition(index) => {
                let domain = domains.get(*index).copied().unwrap_or(0);
                let mut output = 0;
                if domain & 1 != 0 {
                    output |= FALSE_FALSE;
                }
                if domain & 2 != 0 {
                    output |= TRUE_TRUE;
                }
                output
            }
            Self::And(nodes) => nodes.iter().fold(TRUE_TRUE, |acc, node| {
                combine(
                    acc,
                    node.possible_pairs(target, target_values, domains),
                    true,
                )
            }),
            Self::Or(nodes) => nodes.iter().fold(FALSE_FALSE, |acc, node| {
                combine(
                    acc,
                    node.possible_pairs(target, target_values, domains),
                    false,
                )
            }),
            Self::Not(value) => {
                let pairs = value.possible_pairs(target, target_values, domains);
                let mut output = 0;
                for bit in 0..4 {
                    if pairs & (1 << bit) == 0 {
                        continue;
                    }
                    let left = bit >= 2;
                    let right = bit % 2 == 1;
                    output |= pair_bit(!left, !right);
                }
                output
            }
        }
    }

    /// Prove a masking MC/DC pair for one atomic condition.
    #[must_use]
    pub fn proves_independent_effect(
        &self,
        left: &[Option<bool>],
        left_outcome: bool,
        right: &[Option<bool>],
        right_outcome: bool,
        target: usize,
    ) -> bool {
        let count = self.condition_count();
        if left.len() != count || right.len() != count || target >= count {
            return false;
        }
        if left_outcome == right_outcome
            || left[target].is_none()
            || right[target] != left[target].map(|value| !value)
        {
            return false;
        }
        // Public callers may supply incomplete or impossible observations;
        // only a validated proof set can skip this short-circuit check.
        if self.evaluate_observation(left) != Some(left_outcome)
            || self.evaluate_observation(right) != Some(right_outcome)
        {
            return false;
        }
        self.proves_validated_pair(left, left_outcome, right, right_outcome, target)
    }

    fn proves_validated_pair(
        &self,
        left: &[Option<bool>],
        left_outcome: bool,
        right: &[Option<bool>],
        right_outcome: bool,
        target: usize,
    ) -> bool {
        let Some(left_value) = left[target] else {
            return false;
        };
        let Some(right_value) = right[target] else {
            return false;
        };
        if left_value == right_value || left_outcome == right_outcome {
            return false;
        }
        let domains: Vec<u8> = (0..self.condition_count())
            .map(|index| match (left[index], right[index]) {
                (Some(false) | None, Some(false)) | (Some(false), None) => 1,
                (Some(true) | None, Some(true)) | (Some(true), None) => 2,
                (Some(false), Some(true)) | (Some(true), Some(false)) | (None, None) => 3,
            })
            .collect();
        let pair = self.possible_pairs(target, (left_value, right_value), &domains);
        pair & (1 << (u8::from(left_outcome) * 2 + u8::from(right_outcome))) != 0
    }
}

pub(super) struct McdcProofInputs<'logic, 'observation> {
    logic: &'logic Logic,
    vectors: Vec<ProofVector<'observation>>,
}

pub(super) struct ProofVector<'a> {
    pub(super) encoded: Option<&'a str>,
    pub(super) values: Vec<Option<bool>>,
    pub(super) outcome: bool,
}

impl<'logic, 'observation> McdcProofInputs<'logic, 'observation> {
    pub(super) fn from_encoded(
        logic: &'logic Logic,
        observations: impl IntoIterator<Item = (&'observation str, bool)>,
    ) -> Self {
        let vectors = observations
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .filter_map(|(encoded, outcome)| {
                if encoded
                    .bytes()
                    .any(|value| !matches!(value, b'0' | b'1' | b'-'))
                {
                    return None;
                }
                let values = parse_vector(encoded);
                (values.len() == logic.condition_count()
                    && logic.evaluate_observation(&values) == Some(outcome))
                .then_some(ProofVector {
                    encoded: Some(encoded),
                    values,
                    outcome,
                })
            })
            .collect();
        Self { logic, vectors }
    }

    pub(super) fn vectors(&self) -> &[ProofVector<'observation>] {
        &self.vectors
    }

    fn visit_target_pairs(
        &self,
        target: usize,
        mut witness: impl FnMut(usize, usize) -> ControlFlow<()>,
    ) {
        let mut buckets: [[Vec<usize>; 2]; 2] =
            std::array::from_fn(|_| std::array::from_fn(|_| Vec::new()));
        for (index, vector) in self.vectors.iter().enumerate() {
            if let Some(value) = vector.values[target] {
                buckets[usize::from(vector.outcome)][usize::from(value)].push(index);
            }
        }
        for (left_bucket, right_bucket) in [
            (&buckets[0][0], &buckets[1][1]),
            (&buckets[0][1], &buckets[1][0]),
        ] {
            for &first_index in left_bucket {
                for &second_index in right_bucket {
                    let (left_index, right_index) =
                        (first_index.min(second_index), first_index.max(second_index));
                    let left = &self.vectors[left_index];
                    let right = &self.vectors[right_index];
                    if self.logic.proves_validated_pair(
                        &left.values,
                        left.outcome,
                        &right.values,
                        right.outcome,
                        target,
                    ) && witness(left_index, right_index).is_break()
                    {
                        return;
                    }
                }
            }
        }
    }

    pub(super) fn covered_indices(&self) -> BTreeSet<usize> {
        (0..self.logic.condition_count())
            .filter(|&target| {
                let mut covered = false;
                self.visit_target_pairs(target, |_, _| {
                    covered = true;
                    ControlFlow::Break(())
                });
                covered
            })
            .collect()
    }

    pub(super) fn for_each_pair(&self, mut witness: impl FnMut(usize, usize, usize)) {
        for target in 0..self.logic.condition_count() {
            self.visit_target_pairs(target, |left, right| {
                witness(target, left, right);
                ControlFlow::Continue(())
            });
        }
    }
}

impl<'logic> McdcProofInputs<'logic, 'static> {
    fn from_decoded(logic: &'logic Logic, observations: &[(Vec<Option<bool>>, bool)]) -> Self {
        let vectors = observations
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .filter_map(|(values, outcome)| {
                (values.len() == logic.condition_count()
                    && logic.evaluate_observation(&values) == Some(outcome))
                .then_some(ProofVector {
                    encoded: None,
                    values,
                    outcome,
                })
            })
            .collect();
        Self { logic, vectors }
    }
}

#[must_use]
pub fn render_vector(values: &[Option<bool>]) -> String {
    values
        .iter()
        .map(|value| match value {
            Some(true) => '1',
            Some(false) => '0',
            None => '-',
        })
        .collect()
}

#[must_use]
pub fn parse_vector(vector: &str) -> Vec<Option<bool>> {
    vector
        .chars()
        .map(|value| match value {
            '1' => Some(true),
            '0' => Some(false),
            _ => None,
        })
        .collect()
}

/// Validate one persisted observation against its decision structure.
#[must_use]
pub fn valid_observation(logic: &Logic, vector: &str, outcome: bool) -> bool {
    if vector
        .chars()
        .any(|value| !matches!(value, '0' | '1' | '-'))
    {
        return false;
    }
    let values = parse_vector(vector);
    values.len() == logic.condition_count() && logic.evaluate_observation(&values) == Some(outcome)
}

#[must_use]
pub fn mcdc_indices(logic: &Logic, vectors: &[(Vec<Option<bool>>, bool)]) -> BTreeSet<usize> {
    McdcProofInputs::from_decoded(logic, vectors).covered_indices()
}

/// Parse the stable persisted logic representation within engine resource bounds.
///
/// # Errors
///
/// Returns [`LogicParseError`] for malformed syntax, invalid values, oversized
/// text, excessive nesting, or condition indices beyond the supported limit.
pub fn parse_logic(text: &str) -> Result<Logic, LogicParseError> {
    fn parse(text: &str, depth: usize) -> Result<Logic, LogicParseError> {
        if depth > MAX_LOGIC_DEPTH {
            return Err(LogicParseError::ResourceLimit);
        }
        if text.is_empty() || text.trim() != text {
            return Err(LogicParseError::Syntax);
        }
        if let Some(value) = text.strip_prefix('c') {
            if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
                return Err(LogicParseError::Syntax);
            }
            let index = value.parse::<usize>().map_err(|_| LogicParseError::Value)?;
            return (index < MAX_CONDITIONS)
                .then_some(Logic::Condition(index))
                .ok_or(LogicParseError::ResourceLimit);
        }
        let open = text.find('(').ok_or(LogicParseError::Syntax)?;
        let name = &text[..open];
        let inner = text
            .get(open + 1..text.len().checked_sub(1).ok_or(LogicParseError::Syntax)?)
            .filter(|_| text.ends_with(')'))
            .ok_or(LogicParseError::Syntax)?;
        let mut level = 0_usize;
        let mut start = 0;
        let mut parts = Vec::new();
        for (index, byte) in inner.bytes().enumerate() {
            match byte {
                b'(' => level = level.checked_add(1).ok_or(LogicParseError::ResourceLimit)?,
                b')' => level = level.checked_sub(1).ok_or(LogicParseError::Syntax)?,
                b',' if level == 0 => {
                    parts.push(parse(&inner[start..index], depth + 1)?);
                    start = index + 1;
                }
                _ => {}
            }
        }
        if level != 0 {
            return Err(LogicParseError::Syntax);
        }
        parts.push(parse(&inner[start..], depth + 1)?);
        match (name, parts.len()) {
            ("not", 1) => Ok(Logic::Not(Box::new(
                parts.pop().ok_or(LogicParseError::Syntax)?,
            ))),
            ("and", 2..) => Ok(Logic::And(parts)),
            ("or", 2..) => Ok(Logic::Or(parts)),
            ("not" | "and" | "or", _) => Err(LogicParseError::Value),
            _ => Err(LogicParseError::Syntax),
        }
    }
    if text.len() > MAX_LOGIC_LENGTH {
        return Err(LogicParseError::Length);
    }
    parse(text, 0)
}

#[cfg(test)]
mod tests;
