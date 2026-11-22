use super::{
    Logic, LogicParseError, MAX_CONDITIONS, MAX_LOGIC_DEPTH, MAX_LOGIC_LENGTH, mcdc_indices,
    parse_logic, valid_observation,
};
use std::collections::BTreeSet;

#[test]
fn parses_and_renders_nested_logic_deterministically() {
    let logic = parse_logic("and(c0,or(c1,not(c2)))").expect("valid logic");
    assert_eq!(logic.render(), "and(c0,or(c1,not(c2)))");
    assert_eq!(logic.condition_count(), 3);
}

#[test]
fn parser_rejects_bounded_failure_classes() {
    assert_eq!(parse_logic("and(c0,)"), Err(LogicParseError::Syntax));
    assert_eq!(parse_logic("not(c0,c1)"), Err(LogicParseError::Value));
    assert_eq!(
        parse_logic(&"x".repeat(MAX_LOGIC_LENGTH + 1)),
        Err(LogicParseError::Length)
    );
    assert_eq!(
        parse_logic(&format!("c{MAX_CONDITIONS}")),
        Err(LogicParseError::ResourceLimit)
    );
}

#[test]
fn parser_is_bounded_for_arbitrarily_nested_inputs() {
    for excess in 1..=8 {
        let depth = MAX_LOGIC_DEPTH + excess;
        let text = format!("{}c0{}", "not(".repeat(depth), ")".repeat(depth));
        assert_eq!(parse_logic(&text), Err(LogicParseError::ResourceLimit));
    }
}

#[test]
fn parser_rejects_overflow_and_unbalanced_operators() {
    assert_eq!(
        parse_logic("c999999999999999999999999999999999999"),
        Err(LogicParseError::Value)
    );
    for text in ["and(c0)", "or()", "xor(c0,c1)", "and(c0,c1))", "(c0)"] {
        assert!(parse_logic(text).is_err(), "accepted {text}");
    }
}

#[test]
fn rejects_values_after_a_short_circuit_boundary() {
    let logic = Logic::And(vec![
        Logic::Condition(0),
        Logic::Condition(1),
        Logic::Condition(2),
    ]);
    assert!(valid_observation(&logic, "0--", false));
    assert!(!valid_observation(&logic, "0-1", false));
}

#[test]
fn proves_masking_mcdc_for_each_independent_condition() {
    let logic = parse_logic("and(c0,c1)").expect("valid logic");
    let vectors = vec![
        (vec![Some(false), None], false),
        (vec![Some(true), Some(false)], false),
        (vec![Some(true), Some(true)], true),
    ];
    assert_eq!(mcdc_indices(&logic, &vectors), [0, 1].into_iter().collect());
}

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
    if left.len() != count || right.len() != count || target >= count {
        return false;
    }
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

// All binary and/or shapes with one to three uniquely indexed operands, with
// optional negation at every operand and internal node. No production parser
// or evaluator participates in generating the oracle.
fn logic_trees(start: usize, count: usize) -> Vec<Logic> {
    if count == 1 {
        return vec![
            Logic::Condition(start),
            Logic::Not(Box::new(Logic::Condition(start))),
        ];
    }
    let mut trees = Vec::new();
    for left_count in 1..count {
        for left in logic_trees(start, left_count) {
            for right in logic_trees(start + left_count, count - left_count) {
                for tree in [
                    Logic::And(vec![left.clone(), right.clone()]),
                    Logic::Or(vec![left.clone(), right]),
                ] {
                    trees.push(Logic::Not(Box::new(tree.clone())));
                    trees.push(tree);
                }
            }
        }
    }
    trees
}

fn vectors(count: usize) -> Vec<Vec<Option<bool>>> {
    let mut vectors = vec![Vec::new()];
    for _ in 0..count {
        vectors = vectors
            .into_iter()
            .flat_map(|prefix| {
                [None, Some(false), Some(true)]
                    .into_iter()
                    .map(move |value| {
                        let mut vector = prefix.clone();
                        vector.push(value);
                        vector
                    })
            })
            .collect();
    }
    vectors
}

#[test]
fn generated_trees_accept_exactly_the_reference_short_circuit_observations() {
    for count in 1..=3 {
        for logic in logic_trees(0, count) {
            for vector in vectors(count) {
                let text = super::render_vector(&vector);
                for outcome in [false, true] {
                    assert_eq!(
                        valid_observation(&logic, &text, outcome),
                        reference_observation(&logic, &vector, outcome),
                        "{}: {text}/{outcome}",
                        logic.render()
                    );
                    assert!(!valid_observation(&logic, &format!("{text}0"), outcome));
                    assert!(!valid_observation(&logic, &text[..text.len() - 1], outcome));
                    assert!(!valid_observation(
                        &logic,
                        &text.replacen(['0', '1', '-'], "x", 1),
                        outcome
                    ));
                }
            }
        }
    }
}

#[test]
fn generated_mcdc_pairs_match_the_independent_exhaustive_oracle() {
    for count in 1..=3 {
        for logic in logic_trees(0, count) {
            let observations: Vec<_> = vectors(count)
                .into_iter()
                .flat_map(|vector| [(vector.clone(), false), (vector, true)])
                .collect();
            // Include impossible observations and wrong outcomes, not only
            // vectors produced by the reference trace.
            for (left, left_outcome) in &observations {
                for (right, right_outcome) in &observations {
                    for target in 0..count {
                        assert_eq!(
                            logic.proves_independent_effect(
                                left,
                                *left_outcome,
                                right,
                                *right_outcome,
                                target
                            ),
                            reference_pair(
                                &logic,
                                left,
                                *left_outcome,
                                right,
                                *right_outcome,
                                target
                            ),
                            "{}: target {target}, {left:?}/{left_outcome} / {right:?}/{right_outcome}",
                            logic.render()
                        );
                    }
                }
            }
        }
    }
}

fn reference_mcdc_indices(
    logic: &Logic,
    observations: &[(Vec<Option<bool>>, bool)],
) -> BTreeSet<usize> {
    (0..logic.condition_count())
        .filter(|&target| {
            observations.iter().enumerate().any(|(left_index, left)| {
                observations
                    .iter()
                    .skip(left_index + 1)
                    .any(|right| reference_pair(logic, &left.0, left.1, &right.0, right.1, target))
            })
        })
        .collect()
}

#[test]
fn mcdc_sets_match_the_reference_for_every_subset_of_short_circuit_traces() {
    for text in [
        "and(c0,or(c1,c2))",
        "or(c0,and(not(c1),c2))",
        "and(c0,c1,c2)",
    ] {
        let logic = parse_logic(text).expect("valid bounded test logic");
        let observations: Vec<_> = (0..(1 << logic.condition_count()))
            .map(|bits| {
                let values: Vec<bool> = (0..logic.condition_count())
                    .map(|index| bits & (1 << index) != 0)
                    .collect();
                let mut vector = vec![None; logic.condition_count()];
                let outcome = reference_trace(&logic, &values, &mut vector);
                (vector, outcome)
            })
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        for mask in 0..(1 << observations.len()) {
            let selected: Vec<_> = observations
                .iter()
                .enumerate()
                .filter(|(index, _)| mask & (1 << index) != 0)
                .map(|(_, observation)| observation.clone())
                .collect();
            assert_eq!(
                mcdc_indices(&logic, &selected),
                reference_mcdc_indices(&logic, &selected),
                "{text}: selected {selected:?}"
            );
        }
    }
}

#[test]
fn mostly_unique_observations_keep_late_witnesses_and_ignore_bad_rows() {
    let logic = parse_logic("or(and(c0,c1),and(c2,c3),and(c4,c5),and(c6,c7))")
        .expect("valid bounded test logic");
    let valid: BTreeSet<_> = (0..256)
        .map(|bits| {
            let values: Vec<bool> = (0..8).map(|index| bits & (1 << index) != 0).collect();
            let mut vector = vec![None; 8];
            let outcome = reference_trace(&logic, &values, &mut vector);
            (vector, outcome)
        })
        .collect();
    let mut observations: Vec<_> = valid.iter().filter(|row| !row.1).take(7).cloned().collect();
    observations.extend(valid.iter().filter(|row| row.1).take(7).cloned());
    assert_eq!(observations.len(), 14);
    observations.push(observations[0].clone());
    observations.push((vec![None; 8], true));
    observations.push((vec![Some(true); 7], true));

    let expected = reference_mcdc_indices(&logic, &observations);
    assert!(!expected.is_empty(), "fixture must contain a witness");
    assert_eq!(mcdc_indices(&logic, &observations), expected);
}
