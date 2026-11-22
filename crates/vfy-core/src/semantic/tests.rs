use std::collections::BTreeMap;

use super::{
    DecisionDefinition, Logic, PathDecisionDefinition, PathDecisionKind, PathObservation,
    PathOutcomeDefinition, PathOutcomeRole, SemanticData, SemanticObservation,
    analyze_semantic_source_data,
};

#[test]
fn identical_source_in_distinct_files_has_independent_decision_keys() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let (first, first_paths) =
        analyze_semantic_source_data("first.py", source).expect("valid semantic test source");
    let (second, second_paths) =
        analyze_semantic_source_data("second.py", source).expect("valid semantic test source");
    assert_eq!((first.len(), first_paths.len()), (1, 1));
    assert_eq!((second.len(), second_paths.len()), (1, 1));
    assert_ne!(first[0].key(), second[0].key());
    assert_ne!(first_paths[0].key(), second_paths[0].key());
    assert_eq!(first[0].source_hash(), second[0].source_hash());
}

#[test]
fn semantic_exclusions_only_match_comment_tokens() {
    for pragma in [
        "pragma: no cover",
        "pragma: no branch",
        "pragma: no decision",
    ] {
        let source = format!("if value == {pragma:?}:\n    pass\nresult = value or {pragma:?}\n");
        let (definitions, paths) =
            analyze_semantic_source_data("source.py", &source).expect("valid semantic test source");
        assert_eq!((definitions.len(), paths.len()), (1, 1), "{pragma}");

        let source =
            format!("if value:  # {pragma}\n    pass\nresult = value or other  # {pragma}\n");
        let (definitions, paths) =
            analyze_semantic_source_data("source.py", &source).expect("valid semantic test source");
        assert!(definitions.is_empty(), "{pragma}");
        assert!(paths.is_empty(), "{pragma}");
    }
}

#[test]
fn unsupported_boolean_kinds_are_rejected_by_the_validated_model() {
    for kind in ["", "unknown", "value_or"] {
        let definition = definition("hash");
        let error = DecisionDefinition::validated(
            definition.key,
            definition.path,
            definition.line,
            definition.end_line,
            kind.to_owned(),
            definition.source_hash,
            definition.expression,
            definition.logic,
            definition.condition_lines,
            definition.condition_ranges,
        )
        .expect_err("unsupported boolean kind must be rejected");
        assert_eq!(error.0, "unsupported boolean decision kind");
    }
}

#[test]
fn remapping_preserves_opaque_keys_and_qualifies_unscoped_generated_keys() {
    let source = "if value:\n    pass\n";
    let (original, _) =
        analyze_semantic_source_data("first.py", source).expect("valid semantic test source");
    let (mapped, _) =
        analyze_semantic_source_data("mapped.py", source).expect("valid semantic test source");
    let content_key = original[0]
        .key()
        .rsplit_once(':')
        .expect("generated key contains a content digest")
        .1;
    let unscoped = format!("vfy:{content_key}");
    for (key, expected) in [
        ("opaque".to_owned(), "opaque".to_owned()),
        ("vfy:opaque".to_owned(), "vfy:opaque".to_owned()),
        (
            format!("vfy:{content_key}:extra"),
            format!("vfy:{content_key}:extra"),
        ),
        (unscoped, mapped[0].key().to_owned()),
    ] {
        let mut definition = original[0].clone();
        definition.key.clone_from(&key);
        let mut data = SemanticData::default();
        data.insert_definition(definition)
            .expect("unique decision key");
        data.insert_observation("test|run".to_owned(), key, "1".to_owned(), true)
            .expect("valid observation");
        let remapped = data
            .remap_paths(&BTreeMap::from([(
                "first.py".to_owned(),
                "mapped.py".to_owned(),
            )]))
            .expect("remap a valid definition");
        assert_eq!(remapped.definitions[&expected].path(), "mapped.py");
        assert_eq!(
            remapped
                .observations()
                .first()
                .expect("retained observation")
                .decision(),
            expected
        );
    }
}

fn definition(hash: &str) -> DecisionDefinition {
    DecisionDefinition {
        key: "decision".to_owned(),
        path: "source.py".to_owned(),
        line: 2,
        end_line: 2,
        kind: "if".to_owned(),
        source_hash: hash.to_owned(),
        expression: "left and right".to_owned(),
        logic: Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]),
        condition_lines: vec![2, 2],
        condition_ranges: vec![(2, 3, 2, 7), (2, 12, 2, 17)],
    }
}

fn path_definition() -> PathDecisionDefinition {
    PathDecisionDefinition {
        key: "path".to_owned(),
        path: "source.py".to_owned(),
        line: 5,
        end_line: 8,
        kind: PathDecisionKind::Try,
        source_hash: "hash".to_owned(),
        expression: "try".to_owned(),
        body_start: 6,
        body_end: 7,
        range: (5, 0, 5, 3),
        outcomes: vec![
            PathOutcomeDefinition {
                label: "normal".to_owned(),
                range: (5, 0, 5, 3),
                role: PathOutcomeRole::Normal,
            },
            PathOutcomeDefinition {
                label: "handler".to_owned(),
                range: (7, 0, 7, 5),
                role: PathOutcomeRole::Handler { body: (7, 7) },
            },
        ],
    }
}

#[test]
fn path_definition_rejects_a_source_range_outside_its_record() {
    let definition = path_definition();
    let error = PathDecisionDefinition::validated(
        definition.key,
        definition.path,
        definition.line,
        definition.end_line,
        definition.kind,
        definition.source_hash,
        definition.expression,
        definition.body_start,
        definition.body_end,
        (4, 0, 5, 0),
        definition.outcomes,
    )
    .expect_err("reject source range outside record");
    assert_eq!(
        error,
        super::SemanticValidationError("invalid path decision source range")
    );
}

#[test]
fn definitions_reject_child_ranges_outside_their_parent() {
    let decision_error = DecisionDefinition::validated(
        "decision".to_owned(),
        "source.py".to_owned(),
        1,
        1,
        "if".to_owned(),
        "hash".to_owned(),
        "value".to_owned(),
        Logic::Condition(0),
        vec![2],
        vec![(2, 0, 2, 1)],
    )
    .expect_err("condition range outside its definition");
    assert_eq!(
        decision_error,
        super::SemanticValidationError("invalid condition range")
    );

    let definition = path_definition();
    let mut outcomes = definition.outcomes;
    outcomes[1] =
        PathOutcomeDefinition::validated("late".to_owned(), (9, 0, 9, 1), PathOutcomeRole::Normal)
            .expect("standalone outcome range");
    let path_error = PathDecisionDefinition::validated(
        definition.key,
        definition.path,
        definition.line,
        definition.end_line,
        definition.kind,
        definition.source_hash,
        definition.expression,
        definition.body_start,
        definition.body_end,
        definition.range,
        outcomes,
    )
    .expect_err("outcome range outside its definition");
    assert_eq!(
        path_error,
        super::SemanticValidationError("invalid path outcome range")
    );
}

#[test]
fn domain_enums_round_trip_known_storage_values() {
    for (value, body, expected) in [
        ("normal", None, PathOutcomeRole::Normal),
        (
            "handler",
            Some((1, 2)),
            PathOutcomeRole::Handler { body: (1, 2) },
        ),
        (
            "match_case",
            Some((2, 3)),
            PathOutcomeRole::MatchCase { body: (2, 3) },
        ),
        ("unhandled", None, PathOutcomeRole::Unhandled),
        ("no_match", None, PathOutcomeRole::NoMatch),
        ("cancelled", None, PathOutcomeRole::Cancelled),
        ("operand", None, PathOutcomeRole::Operand),
    ] {
        assert_eq!(PathOutcomeRole::parse(value, body), Some(expected));
    }
    assert!(PathOutcomeRole::parse("handler", None).is_none());
    assert_eq!(
        PathDecisionKind::parse("try_star"),
        Some(PathDecisionKind::TryStar)
    );
    assert!(PathDecisionKind::parse("unknown").is_none());
}

#[test]
fn observation_insertion_rejects_invalid_references_without_mutation() {
    let mut data = SemanticData::default();
    data.insert_definition(definition("hash"))
        .expect("valid decision definition");
    data.insert_path_definition(path_definition())
        .expect("valid path definition");

    assert!(
        data.insert_observation(
            "test|run".to_owned(),
            "missing".to_owned(),
            "11".to_owned(),
            true,
        )
        .is_err()
    );
    assert!(
        data.insert_observation(
            "test|run".to_owned(),
            "decision".to_owned(),
            "00".to_owned(),
            false,
        )
        .is_err()
    );
    assert!(
        data.insert_path_observation("test|run".to_owned(), "path".to_owned(), 2)
            .is_err()
    );
    assert!(data.observations().is_empty());
    assert!(data.path_observations().is_empty());

    data.insert_observation(
        "test|run".to_owned(),
        "decision".to_owned(),
        "11".to_owned(),
        true,
    )
    .expect("consistent decision observation");
    data.insert_path_observation("test|run".to_owned(), "path".to_owned(), 1)
        .expect("in-range path observation");
    assert_eq!(data.observations().len(), 1);
    assert_eq!(data.path_observations().len(), 1);
}

#[test]
fn semantic_merge_deduplicates_observations_and_rejects_conflicting_revisions() {
    let mut left = SemanticData::default();
    left.definitions
        .insert("decision".to_owned(), definition("hash"));
    left.observations.insert(SemanticObservation {
        context: "test|run".to_owned(),
        decision: "decision".to_owned(),
        vector: "00".to_owned(),
        outcome: false,
    });
    let mut right = left.clone();
    right.observations.insert(SemanticObservation {
        context: "test|run".to_owned(),
        decision: "decision".to_owned(),
        vector: "11".to_owned(),
        outcome: true,
    });
    left.merge(right).expect("same revision merge");
    assert_eq!(left.observations.len(), 2);

    let mut conflict = SemanticData::default();
    conflict
        .definitions
        .insert("decision".to_owned(), definition("new-hash"));
    assert_eq!(
        left.merge(conflict),
        Err("cannot combine semantic data from different source revisions")
    );
    assert_eq!(left.definitions["decision"].source_hash, "hash");
}

#[test]
fn semantic_merge_preserves_known_condition_ranges_in_both_orders() {
    let known = definition("hash");
    let mut unknown = known.clone();
    unknown.condition_ranges[0] = (2, -1, 2, -1);
    for (first, second) in [(&known, &unknown), (&unknown, &known)] {
        let mut data = SemanticData::default();
        data.insert_definition(first.clone())
            .expect("unique definition");
        let mut incoming = SemanticData::default();
        incoming
            .insert_definition(second.clone())
            .expect("unique incoming definition");
        data.merge(incoming).expect("compatible condition ranges");
        assert_eq!(data.definitions["decision"], known);
    }
}

#[test]
fn semantic_merge_rejects_same_revision_boolean_conflicts_atomically() {
    let mut different_logic = definition("hash");
    different_logic.logic = Logic::Or(vec![Logic::Condition(0), Logic::Condition(1)]);
    let mut different_range = definition("hash");
    different_range.condition_ranges[0].1 += 1;
    for conflicting in [different_logic, different_range] {
        let mut data = SemanticData::default();
        data.insert_definition(definition("hash"))
            .expect("valid Boolean definition");
        data.insert_observation(
            "left".to_owned(),
            "decision".to_owned(),
            "0-".to_owned(),
            false,
        )
        .expect("valid observation");
        let before = data.clone();
        let mut incoming = SemanticData::default();
        let mut added = definition("hash");
        added.key = "added".to_owned();
        incoming.insert_definition(added).expect("new definition");
        incoming
            .insert_definition(conflicting)
            .expect("conflicting definition");

        assert_eq!(
            data.merge(incoming),
            Err("conflicting semantic definitions")
        );
        assert_eq!(data.definitions, before.definitions);
        assert_eq!(data.observations, before.observations);
    }
}

#[test]
fn aliases_coalesce_complementary_condition_ranges_and_observations() {
    let source = "if left and (right\n             + 1):\n    pass\n";
    let (expected, _) =
        analyze_semantic_source_data("target.py", source).expect("valid semantic test source");
    assert_eq!(expected[0].condition_ranges, [(1, 3, 1, 7), (1, 13, 2, 16)]);
    for unknown_index in [0, 1] {
        let mut data = SemanticData::default();
        for (path, index, vector, outcome) in [
            ("first.py", unknown_index, "0-", false),
            ("second.py", 1 - unknown_index, "11", true),
        ] {
            let (mut definitions, _) =
                analyze_semantic_source_data(path, source).expect("valid semantic test source");
            let mut definition = definitions.remove(0);
            let line = definition.condition_lines[index];
            definition.condition_ranges[index] = (line, -1, line, -1);
            let key = definition.key.clone();
            data.insert_definition(definition)
                .expect("distinct source identity");
            data.insert_observation("test".to_owned(), key, vector.to_owned(), outcome)
                .expect("valid observation");
        }
        let mapped = data
            .remap_paths(&BTreeMap::from([
                ("first.py".to_owned(), "target.py".to_owned()),
                ("second.py".to_owned(), "target.py".to_owned()),
            ]))
            .expect("compatible aliased condition ranges");
        assert_eq!(mapped.definitions.len(), 1);
        assert_eq!(mapped.definitions[expected[0].key()], expected[0]);
        assert_eq!(mapped.observations.len(), 2);
        assert!(
            mapped
                .observations
                .iter()
                .all(|observation| observation.decision == expected[0].key())
        );
    }
}

#[test]
fn semantic_merge_rejection_is_atomic_when_a_late_path_definition_conflicts() {
    let mut left = SemanticData::default();
    left.definitions
        .insert("decision".to_owned(), definition("hash"));
    left.path_definitions
        .insert("path".to_owned(), path_definition());
    let before = (
        left.records(),
        left.observation_records(),
        left.path_records(),
        left.path_outcome_records(),
        left.path_observation_records(),
    );

    let mut incoming = SemanticData::default();
    let mut added = definition("added-hash");
    added.key = "added".to_owned();
    added.path = "added.py".to_owned();
    incoming.definitions.insert(added.key.clone(), added);
    incoming.observations.insert(SemanticObservation {
        context: "new|run".to_owned(),
        decision: "added".to_owned(),
        vector: "11".to_owned(),
        outcome: true,
    });
    let mut conflicting_path = path_definition();
    conflicting_path.source_hash = "new-hash".to_owned();
    incoming
        .path_definitions
        .insert(conflicting_path.key.clone(), conflicting_path);

    assert_eq!(
        left.merge(incoming),
        Err("cannot combine semantic data from different source revisions")
    );
    assert_eq!(
        (
            left.records(),
            left.observation_records(),
            left.path_records(),
            left.path_outcome_records(),
            left.path_observation_records(),
        ),
        before
    );
}

#[test]
fn semantic_merge_rejects_same_revision_path_conflicts_atomically() {
    for change in ["outcome", "header", "path"] {
        let mut left = SemanticData::default();
        left.insert_path_definition(path_definition())
            .expect("valid path definition");
        left.insert_path_observation("left".to_owned(), "path".to_owned(), 0)
            .expect("valid path observation");
        let before = left.clone();
        let mut conflicting = path_definition();
        match change {
            "outcome" => conflicting.outcomes[0].label = "different outcome".to_owned(),
            "header" => conflicting.range.3 += 1,
            "path" => conflicting.path = "another.py".to_owned(),
            _ => unreachable!("known test mutation"),
        }
        let mut incoming = SemanticData::default();
        incoming
            .insert_definition(definition("hash"))
            .expect("new Boolean definition");
        incoming
            .insert_path_definition(conflicting)
            .expect("individually valid conflicting definition");
        incoming
            .insert_path_observation("right".to_owned(), "path".to_owned(), 1)
            .expect("valid incoming path observation");

        assert_eq!(
            left.merge(incoming),
            Err("conflicting semantic definitions")
        );
        assert_eq!(left.definitions, before.definitions);
        assert_eq!(left.path_definitions, before.path_definitions);
        assert_eq!(left.observations, before.observations);
        assert_eq!(left.path_observations, before.path_observations);
    }
}

#[test]
fn remapping_updates_boolean_and_named_path_definitions() {
    let mut data = SemanticData::default();
    data.definitions
        .insert("decision".to_owned(), definition("hash"));
    data.path_definitions
        .insert("path".to_owned(), path_definition());
    data = data
        .remap_paths(&BTreeMap::from([(
            "source.py".to_owned(),
            "mapped/source.py".to_owned(),
        )]))
        .expect("remap valid semantic evidence");
    assert_eq!(data.definitions["decision"].path, "mapped/source.py");
    assert_eq!(data.path_definitions["path"].path, "mapped/source.py");
}

#[test]
fn report_records_compute_boolean_and_path_obligations_with_context_filtering() {
    let mut data = SemanticData::default();
    data.definitions
        .insert("decision".to_owned(), definition("hash"));
    data.observations.extend([
        SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "0-".to_owned(),
            outcome: false,
        },
        SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "10".to_owned(),
            outcome: false,
        },
        SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "11".to_owned(),
            outcome: true,
        },
        SemanticObservation {
            context: "b|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "1x".to_owned(),
            outcome: true,
        },
    ]);
    let record = data.report_records(&["decision".to_owned()], &["a|run".to_owned()])[0].clone();
    assert_eq!(record.5, 2);
    assert_eq!(record.6, 2);
    assert_eq!(record.7, 4);
    assert_eq!(record.8, 4);
    assert_eq!(record.9, 2);
    assert!(record.11.is_empty());

    data.path_definitions
        .insert("path".to_owned(), path_definition());
    data.path_observations.insert(PathObservation {
        context: "a|run".to_owned(),
        decision: "path".to_owned(),
        outcome: 0,
    });
    let path_record =
        data.path_report_records(&["path".to_owned()], &["a|run".to_owned()])[0].clone();
    assert_eq!(path_record.5, 1);
    assert_eq!(path_record.6, 2);
    assert_eq!(path_record.7, vec!["handler".to_owned()]);
}

#[test]
fn evidence_records_ignore_invalid_vectors_and_are_sorted() {
    let mut data = SemanticData::default();
    data.definitions
        .insert("decision".to_owned(), definition("hash"));
    data.observations.extend([
        SemanticObservation {
            context: "z|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "0-".to_owned(),
            outcome: false,
        },
        SemanticObservation {
            context: "bad|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "10-".to_owned(),
            outcome: false,
        },
    ]);
    let evidence = data.evidence_records(&[], &[]);
    assert_eq!(evidence.len(), 0);
    data.observations.insert(SemanticObservation {
        context: "a|run".to_owned(),
        decision: "decision".to_owned(),
        vector: "11".to_owned(),
        outcome: true,
    });
    let evidence = data.evidence_records(&[], &[]);
    assert_eq!(evidence.len(), 1);
    assert_eq!(evidence[0].0, "decision");
    assert_eq!(evidence[0].2, "a|run");
}

fn partitioned_report_data() -> SemanticData {
    let mut data = SemanticData::default();
    for key in ["alpha", "beta", "unseen"] {
        data.insert_definition(
            DecisionDefinition::validated(
                key.to_owned(),
                "source.py".to_owned(),
                2,
                2,
                "if".to_owned(),
                "hash".to_owned(),
                "value".to_owned(),
                Logic::Condition(0),
                vec![2],
                vec![(2, 0, 2, 5)],
            )
            .expect("valid decision"),
        )
        .expect("unique decision");
        let mut path = path_definition();
        path.key = key.to_owned();
        data.insert_path_definition(path)
            .expect("unique path decision");
    }
    for (key, left) in [("alpha", false), ("beta", true)] {
        for (context, outcome) in [("left", left), ("right", !left)] {
            data.insert_observation(
                context.to_owned(),
                key.to_owned(),
                u8::from(outcome).to_string(),
                outcome,
            )
            .expect("valid observation");
            data.insert_path_observation(context.to_owned(), key.to_owned(), usize::from(outcome))
                .expect("valid path observation");
        }
    }
    data
}

#[test]
fn report_selection_keeps_decisions_and_contexts_separate() {
    let data = partitioned_report_data();
    let all = data.report_records(&[], &[]);
    assert_eq!(
        all.iter().map(|row| row.0.as_str()).collect::<Vec<_>>(),
        ["alpha", "beta", "unseen"]
    );
    for row in &all[..2] {
        assert_eq!((row.5, row.7, row.9), (2, 2, 1));
        assert!(row.11.is_empty());
    }
    assert_eq!((all[2].5, all[2].7, all[2].9), (0, 0, 0));
    assert_eq!(all[2].11.len(), 5);
    let selected = data.report_records(&["alpha".to_owned()], &["left".to_owned()]);
    assert_eq!(selected.len(), 1);
    assert_eq!(selected[0].0, "alpha");
    assert_eq!((selected[0].5, selected[0].7, selected[0].9), (1, 1, 0));
    assert_eq!(
        selected[0].11,
        [
            "decision=true",
            "condition 1=true",
            "condition 1 lacks independent effect"
        ]
    );
    assert!(data.report_records(&["absent".to_owned()], &[]).is_empty());
    let evidence = data.evidence_records(&[], &[]);
    let expected: Vec<_> = [
        ("alpha", "0", false, "1", true),
        ("beta", "1", true, "0", false),
    ]
    .into_iter()
    .map(|(key, left, left_outcome, right, right_outcome)| {
        (
            key.to_owned(),
            0,
            "left".to_owned(),
            left.to_owned(),
            left_outcome,
            "right".to_owned(),
            right.to_owned(),
            right_outcome,
        )
    })
    .collect();
    assert_eq!(evidence, expected);
    assert_eq!(
        data.evidence_records(&["beta".to_owned()], &[]),
        expected[1..]
    );
    assert!(data.evidence_records(&[], &["left".to_owned()]).is_empty());
}

#[test]
fn repeated_vectors_keep_witness_contexts_and_filtered_mcdc_obligations() {
    let mut data = SemanticData::default();
    data.insert_definition(definition("hash"))
        .expect("valid Boolean definition");
    for (context, vector, outcome) in [
        ("a|run", "0-", false),
        ("b|run", "10", false),
        ("c|run", "11", true),
        ("d|run", "0-", false),
        ("e|run", "11", true),
    ] {
        data.insert_observation(
            context.to_owned(),
            "decision".to_owned(),
            vector.to_owned(),
            outcome,
        )
        .expect("valid short-circuit observation");
    }
    data.insert_observation(
        "a|run".to_owned(),
        "decision".to_owned(),
        "0-".to_owned(),
        false,
    )
    .expect("duplicate observation is idempotent");
    // Persisted rows from an external source may be malformed. Reporting must
    // ignore them without changing the public evidence or missing obligations.
    data.observations.insert(SemanticObservation {
        context: "bad|run".to_owned(),
        decision: "decision".to_owned(),
        vector: "0-".to_owned(),
        outcome: true,
    });

    let expected: Vec<_> = [
        (0, "a|run", "0-", false, "c|run", "11", true),
        (0, "a|run", "0-", false, "e|run", "11", true),
        (0, "c|run", "11", true, "d|run", "0-", false),
        (0, "d|run", "0-", false, "e|run", "11", true),
        (1, "b|run", "10", false, "c|run", "11", true),
        (1, "b|run", "10", false, "e|run", "11", true),
    ]
    .into_iter()
    .map(
        |(
            index,
            left_context,
            left_vector,
            left_outcome,
            right_context,
            right_vector,
            right_outcome,
        )| {
            (
                "decision".to_owned(),
                index,
                left_context.to_owned(),
                left_vector.to_owned(),
                left_outcome,
                right_context.to_owned(),
                right_vector.to_owned(),
                right_outcome,
            )
        },
    )
    .collect();
    assert_eq!(data.evidence_records(&[], &[]), expected);
    let all = &data.report_records(&[], &[])[0];
    assert_eq!(
        (all.5, all.7, all.9, all.11.as_slice()),
        (2, 4, 2, [].as_slice())
    );

    let selected_contexts = ["b|run".to_owned(), "e|run".to_owned()];
    assert_eq!(
        data.evidence_records(&[], &selected_contexts),
        expected[5..].to_vec()
    );
    let selected = &data.report_records(&[], &selected_contexts)[0];
    assert_eq!((selected.5, selected.7, selected.9), (2, 3, 1));
    assert_eq!(
        selected.11,
        ["condition 1=false", "condition 1 lacks independent effect"]
    );

    let true_contexts = ["c|run".to_owned(), "e|run".to_owned()];
    assert!(data.evidence_records(&[], &true_contexts).is_empty());
    let true_only = &data.report_records(&[], &true_contexts)[0];
    assert_eq!((true_only.5, true_only.7, true_only.9), (1, 2, 0));
    assert_eq!(
        true_only.11,
        [
            "decision=false",
            "condition 1=false",
            "condition 2=false",
            "condition 1 lacks independent effect",
            "condition 2 lacks independent effect",
        ]
    );
}

#[test]
fn path_report_selection_keeps_unobserved_definitions() {
    let data = partitioned_report_data();
    let all = data.path_report_records(&[], &[]);
    assert_eq!(
        all.iter().map(|row| row.0.as_str()).collect::<Vec<_>>(),
        ["alpha", "beta", "unseen"]
    );
    assert_eq!((all[0].5, all[1].5, all[2].5), (2, 2, 0));
    assert!(all[0].7.is_empty());
    assert!(all[1].7.is_empty());
    assert_eq!(all[2].7, ["normal", "handler"]);
    let selected = data.path_report_records(&["beta".to_owned()], &["left".to_owned()]);
    assert_eq!(selected.len(), 1);
    assert_eq!(selected[0].0, "beta");
    assert_eq!(selected[0].5, 1);
    assert_eq!(selected[0].7, ["normal"]);
    assert!(
        data.path_report_records(&["absent".to_owned()], &[])
            .is_empty()
    );
}
