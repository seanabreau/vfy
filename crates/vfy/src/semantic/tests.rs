use super::{
    Logic, PathDecisionKind, analyze_path_source, analyze_source, mcdc_indices, render_vector,
};

use proptest::prelude::*;

#[test]
fn tri_state_logic_preserves_short_circuiting() {
    let and = Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]);
    assert_eq!(and.evaluate_observation(&[Some(false), None]), Some(false));
    assert_eq!(
        and.evaluate_observation(&[Some(true), Some(false)]),
        Some(false)
    );
    assert_eq!(
        and.evaluate_observation(&[Some(true), Some(true)]),
        Some(true)
    );
    assert_eq!(and.evaluate_observation(&[Some(true), None]), None);

    let or = Logic::Or(vec![Logic::Condition(0), Logic::Condition(1)]);
    assert_eq!(or.evaluate_observation(&[Some(true), None]), Some(true));
    assert_eq!(
        or.evaluate_observation(&[Some(false), Some(true)]),
        Some(true)
    );
    assert_eq!(
        or.evaluate_observation(&[Some(false), Some(false)]),
        Some(false)
    );
    assert_eq!(or.evaluate_observation(&[Some(false), None]), None);

    let not = Logic::Not(Box::new(Logic::Condition(0)));
    assert_eq!(not.evaluate_observation(&[Some(true)]), Some(false));
    assert_eq!(not.evaluate_observation(&[Some(false)]), Some(true));
    assert_eq!(not.evaluate_observation(&[None]), None);
}

#[test]
fn masking_mcdc_requires_an_independent_effect_pair() {
    let complete = vec![
        (vec![Some(false), None], false),
        (vec![Some(true), Some(false)], false),
        (vec![Some(true), Some(true)], true),
    ];
    assert_eq!(
        mcdc_indices(
            &Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]),
            &complete
        )
        .into_iter()
        .collect::<Vec<_>>(),
        [0, 1]
    );

    let decision_and_condition_only = vec![
        (vec![Some(false), None], false),
        (vec![Some(true), Some(true)], true),
    ];
    assert_eq!(
        mcdc_indices(
            &Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]),
            &decision_and_condition_only,
        )
        .into_iter()
        .collect::<Vec<_>>(),
        [0]
    );
}

#[test]
fn masking_mcdc_rejects_an_invalid_short_circuit_vector() {
    let logic = Logic::And(vec![
        Logic::Condition(0),
        Logic::Condition(1),
        Logic::Condition(2),
    ]);
    // Condition 0 short-circuits the first vector before conditions 1 and 2. Treating
    // `None` as an unconditional mask would incorrectly accept condition 0.
    assert!(!logic.proves_independent_effect(
        &[Some(false), None, Some(true)],
        false,
        &[Some(true), Some(true), Some(true)],
        true,
        0,
    ));
    assert!(logic.proves_independent_effect(
        &[Some(false), None, None],
        false,
        &[Some(true), Some(true), Some(true)],
        true,
        0,
    ));
}

#[expect(
    clippy::too_many_lines,
    reason = "One fixture exercises the complete semantic report pipeline."
)]
fn assert_logic_matches_reference(logic: &Logic) {
    fn reference_eval(logic: &Logic, values: &[bool]) -> bool {
        match logic {
            Logic::Condition(index) => values[*index],
            Logic::And(nodes) => nodes.iter().all(|node| reference_eval(node, values)),
            Logic::Or(nodes) => nodes.iter().any(|node| reference_eval(node, values)),
            Logic::Not(value) => !reference_eval(value, values),
        }
    }

    fn reference_trace(logic: &Logic, values: &[bool], vector: &mut [Option<bool>]) -> bool {
        match logic {
            Logic::Condition(index) => {
                vector[*index] = Some(values[*index]);
                values[*index]
            }
            Logic::And(nodes) => {
                for node in nodes {
                    if !reference_trace(node, values, vector) {
                        return false;
                    }
                }
                true
            }
            Logic::Or(nodes) => {
                for node in nodes {
                    if reference_trace(node, values, vector) {
                        return true;
                    }
                }
                false
            }
            Logic::Not(value) => !reference_trace(value, values, vector),
        }
    }

    fn reference_observation(logic: &Logic, vector: &[Option<bool>], outcome: bool) -> bool {
        let count = logic.condition_count();
        (0..(1usize << count)).any(|bits| {
            let values: Vec<bool> = (0..count).map(|index| bits & (1 << index) != 0).collect();
            let mut observed = vec![None; count];
            reference_trace(logic, &values, &mut observed) == outcome && observed == vector
        })
    }

    fn reference_pair(
        logic: &Logic,
        left: &[Option<bool>],
        left_outcome: bool,
        right: &[Option<bool>],
        right_outcome: bool,
        target: usize,
    ) -> bool {
        let count = logic.condition_count();
        let (Some(left_target), Some(right_target)) = (left[target], right[target]) else {
            return false;
        };
        if left_target == right_target
            || left_outcome == right_outcome
            || !reference_observation(logic, left, left_outcome)
            || !reference_observation(logic, right, right_outcome)
        {
            return false;
        }
        (0..(1usize << count)).any(|bits| {
            let mut values = vec![false; count];
            for index in 0..count {
                values[index] = bits & (1 << index) != 0;
                if index == target {
                    continue;
                }
                match (left[index], right[index]) {
                    (Some(value), Some(other)) if value == other && values[index] != value => {
                        return false;
                    }
                    (Some(value), None) | (None, Some(value)) if values[index] != value => {
                        return false;
                    }
                    _ => {}
                }
            }
            values[target] = left_target;
            if reference_eval(logic, &values) != left_outcome {
                return false;
            }
            values[target] = right_target;
            reference_eval(logic, &values) == right_outcome
        })
    }

    let count = logic.condition_count();
    let mut observations = Vec::new();
    for bits in 0..(1usize << count) {
        let values: Vec<bool> = (0..count).map(|index| bits & (1 << index) != 0).collect();
        let mut vector = vec![None; count];
        let outcome = reference_trace(logic, &values, &mut vector);
        if !observations.contains(&(vector.clone(), outcome)) {
            observations.push((vector, outcome));
        }
    }
    for (left_index, (left, left_outcome)) in observations.iter().enumerate() {
        for (right, right_outcome) in observations.iter().skip(left_index + 1) {
            for target in 0..count {
                assert_eq!(
                    logic.proves_independent_effect(
                        left,
                        *left_outcome,
                        right,
                        *right_outcome,
                        target,
                    ),
                    reference_pair(logic, left, *left_outcome, right, *right_outcome, target,),
                    "{}: target {target}, {left:?}/{right:?}",
                    logic.render(),
                );
            }
        }
    }
}

fn small_logic_trees() -> impl Strategy<Value = Logic> {
    fn number_conditions(logic: &mut Logic, next: &mut usize) {
        match logic {
            Logic::Condition(index) => {
                *index = *next;
                *next += 1;
            }
            Logic::Not(child) => number_conditions(child, next),
            Logic::And(children) | Logic::Or(children) => {
                for child in children {
                    number_conditions(child, next);
                }
            }
        }
    }
    Just(Logic::Condition(0))
        .prop_recursive(4, 8, 3, |inner| {
            prop_oneof![
                inner.clone().prop_map(|child| Logic::Not(Box::new(child))),
                prop::collection::vec(inner.clone(), 2..=3).prop_map(Logic::And),
                prop::collection::vec(inner, 2..=3).prop_map(Logic::Or),
            ]
        })
        .prop_filter_map("at most four distinct conditions", |mut logic| {
            let mut count = 0;
            number_conditions(&mut logic, &mut count);
            (count <= 4).then_some(logic)
        })
}

proptest! {
    #[test]
    fn generated_logic_matches_independent_mcdc_oracle(logic in small_logic_trees()) {
        assert_logic_matches_reference(&logic);
    }
}

#[test]
fn parser_fixture_discovers_every_supported_decision_kind() {
    let source = "\
def sample(value, flag):
    chosen = 1 if flag else 0
    values = [item for item in value if item and flag]
    while flag and value:
        break
    match chosen:
        case 1 if flag:
            pass
        case _:
            pass
    assert flag
    return values
";
    let mut definitions = analyze_source("fixture.py", source);
    definitions.sort_by(|left, right| left.kind().cmp(right.kind()));
    let kinds: Vec<_> = definitions
        .iter()
        .map(super::DecisionDefinition::kind)
        .collect();
    assert_eq!(
        kinds,
        ["assert", "comprehension", "if_expr", "match_guard", "while"]
    );
    assert_eq!(
        definitions
            .iter()
            .find(|definition| definition.kind() == "while")
            .unwrap()
            .logic()
            .render(),
        "and(c0,c1)"
    );
    assert!(
        definitions
            .iter()
            .all(|definition| definition.source_hash().len() == 64)
    );
}

#[test]
fn parser_discovers_named_path_decisions_without_duplicating_control_tests() {
    let source = "\
from asyncio import CancelledError as CE

async def sample(first, second, awaitable):
    value = first or second
    if first and second:
        pass
    try:
        await awaitable
    except CE:
        return value
";
    let definitions = analyze_path_source("fixture.py", source);
    let kinds: Vec<_> = definitions
        .iter()
        .map(|definition| definition.kind().as_str())
        .collect();
    assert!(kinds.contains(&"value_or"));
    assert!(kinds.contains(&"try"));
    assert!(kinds.contains(&"async_cancellation"));
    assert!(
        !definitions
            .iter()
            .any(|definition| definition.expression() == "first and second")
    );
    let handler = definitions
        .iter()
        .find(|definition| definition.kind() == PathDecisionKind::Try)
        .and_then(|definition| definition.outcomes().get(1))
        .expect("try handler outcome");
    assert_eq!(handler.label(), "handler 1 CE (CancelledError)");
}

#[test]
fn parser_discovers_match_case_outcomes_and_exhaustiveness() {
    let source = "\
def classify(value, enabled):
    match value:
        case 1 | 2:
            return \"small\"
        case int() if enabled:
            return \"integer\"
        case _:
            return \"other\"

def maybe(value, enabled):
    match value:
        case int() if enabled:
            return \"integer\"

def guarded(value, enabled):
    match value:
        case _ if enabled:
            return \"enabled\"
";
    let definitions = analyze_path_source("fixture.py", source);
    let mut matches: Vec<_> = definitions
        .iter()
        .filter(|definition| definition.kind() == PathDecisionKind::Match)
        .collect();
    matches.sort_by_key(|definition| definition.line());
    assert_eq!(matches.len(), 3);
    assert_eq!(matches[0].outcomes().len(), 3);
    assert!(
        matches[0]
            .outcomes()
            .iter()
            .all(|outcome| outcome.role() != super::PathOutcomeRole::NoMatch)
    );
    assert_eq!(matches[1].outcomes().len(), 2);
    assert_eq!(
        matches[1]
            .outcomes()
            .last()
            .map(super::PathOutcomeDefinition::label),
        Some("no case matched")
    );
    assert_eq!(matches[2].outcomes().len(), 2);
    assert_eq!(
        matches[2]
            .outcomes()
            .last()
            .map(super::PathOutcomeDefinition::label),
        Some("no case matched")
    );
}

#[test]
fn parser_applies_path_exclusions_and_omits_unreachable_unhandled_outcomes() {
    let source = "\
def excluded(first, second):
    value = first or second  # pragma: no decision
    try:  # pragma: no decision
        return value
    except RuntimeError:
        return None

def catches_everything():
    try:
        raise RuntimeError()
    except:
        return None
";
    let definitions = analyze_path_source("fixture.py", source);
    assert_eq!(definitions.len(), 1);
    let definition = &definitions[0];
    assert_eq!(definition.kind(), PathDecisionKind::Try);
    assert_eq!(
        definition
            .outcomes()
            .iter()
            .map(super::PathOutcomeDefinition::label)
            .collect::<Vec<_>>(),
        ["normal completion", "handler 1 bare"]
    );
}

#[test]
fn parser_fixture_honors_semantic_exclusions() {
    let source = "\
if first:  # pragma: no decision
    pass
if second:  # pragma: no cover
    pass
if third:
    pass
";
    let definitions = analyze_source("fixture.py", source);
    assert_eq!(definitions.len(), 1);
    assert_eq!(definitions[0].expression(), "third");
}

#[test]
fn parser_condition_ranges_anchor_each_atomic_expression() {
    let source = "if first and second:\n    pass\n";
    let definitions = analyze_source("fixture.py", source);
    assert_eq!(definitions.len(), 1);
    assert_eq!(
        definitions[0].condition_ranges(),
        vec![(1, 3, 1, 8), (1, 13, 1, 19)]
    );
}

#[test]
fn vector_rendering_is_stable_and_explicit_about_unvisited_conditions() {
    assert_eq!(render_vector(&[Some(true), None, Some(false)]), "1-0");
}

#[test]
fn universal_newlines_keep_decision_locations_and_exclusions() {
    for newline in ["\n", "\r\n", "\r"] {
        let source = ["if a: pass  # pragma: no decision", "if é: pass", ""].join(newline);
        let definitions = analyze_source("newlines.py", &source);
        assert_eq!(definitions.len(), 1);
        assert_eq!(definitions[0].condition_ranges(), [(2, 3, 2, 5)]);
    }
}
