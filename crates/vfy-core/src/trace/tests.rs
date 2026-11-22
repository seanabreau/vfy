use super::{TraceOptions, TraceStore};

use crate::data::CoverageData;
use crate::{
    PathDecisionDefinition, PathDecisionKind, PathOutcomeDefinition, PathOutcomeRole, SemanticData,
};

#[test]
fn deduplicates_and_orders_context_partitioned_events() {
    let mut store = TraceStore::new(true);
    store.add_line("test_b", "b.py", 4);
    store.add_line("test_a", "a.py", 2);
    store.add_line("test_a", "a.py", 1);
    store.add_line("test_a", "a.py", 1);
    store.add_arc("test_a", "a.py", 2, 3);
    store.add_arc("test_a", "a.py", 2, 3);
    store.add_arc("test_a", "a.py", 2, 2);

    assert_eq!(
        store.line_records(),
        vec![
            ("test_a".to_owned(), "a.py".to_owned(), vec![1, 2]),
            ("test_b".to_owned(), "b.py".to_owned(), vec![4]),
        ]
    );
    assert_eq!(
        store.arc_records(),
        vec![("test_a".to_owned(), "a.py".to_owned(), vec![(2, 3)])]
    );
}

#[test]
fn sparse_line_chunks_preserve_distant_lines_and_chunk_boundaries() {
    let mut store = TraceStore::new(false);
    for line in [65_537, 64, 63, 0, 64, 65_536] {
        store.add_line("unit", "generated.py", line);
    }

    assert_eq!(
        store.line_records(),
        vec![(
            "unit".to_owned(),
            "generated.py".to_owned(),
            vec![0, 63, 64, 65_536, 65_537]
        )]
    );
}

#[test]
fn line_mode_rejects_arc_storage() {
    let mut store = TraceStore::new(false);
    store.add_arc("", "a.py", 1, 2);
    assert!(store.arc_records().is_empty());
}

#[test]
fn owns_frame_lifecycle_and_generator_resumption() {
    let mut store = TraceStore::new(true);
    store.start_frame(17);
    store.line_event("unit", "source.py", 17, 10, 10);
    store.line_event("unit", "source.py", 17, 10, 11);
    store.suspend_frame(17);
    store.resume_frame(17, Some(11));
    store.line_event("unit", "source.py", 17, 10, 14);
    store.end_frame("unit", Some("source.py"), 17, 10, None);

    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![(-10, 10), (10, 11), (11, 14), (14, -10)]
        )]
    );
}

#[test]
fn repeated_code_executions_partition_branch_arcs_by_dynamic_source_path() {
    let mut store = TraceStore::new(true);
    for context in ["", "unit"] {
        for (path, line) in [("first.py", 11), ("second.py", 20), ("first.py", 12)] {
            store.start_frame(17);
            store.line_event(context, path, 17, 10, line);
            store.end_frame(context, Some(path), 17, 10, None);
        }
    }

    assert_eq!(
        store.arc_records(),
        ["", "unit"]
            .into_iter()
            .flat_map(|context| {
                [
                    (
                        context.to_owned(),
                        "first.py".to_owned(),
                        vec![(-10, 11), (-10, 12), (11, -10), (12, -10)],
                    ),
                    (
                        context.to_owned(),
                        "second.py".to_owned(),
                        vec![(-10, 20), (20, -10)],
                    ),
                ]
            })
            .collect::<Vec<_>>()
    );
}

#[test]
fn dynamic_path_transitions_preserve_measured_lines_and_frame_exit_attribution() {
    for return_path in [Some("other.py"), None] {
        let mut branches = TraceStore::new(true);
        let mut lines = TraceStore::new(false);
        for context in ["", "unit"] {
            for store in [&mut branches, &mut lines] {
                store.start_frame(17);
                for (path, line) in [
                    ("first.py", 11),
                    ("first.py", 12),
                    ("second.py", 20),
                    ("second.py", 21),
                    ("first.py", 13),
                ] {
                    store.line_event(context, path, 17, 10, line);
                }
                store.end_frame(context, return_path, 17, 10, None);
            }
        }

        let branch_data = CoverageData::from_trace_records(
            true,
            branches.line_records(),
            branches.arc_records(),
            Vec::new(),
            SemanticData::default(),
        );
        let line_data = CoverageData::from_trace_records(
            false,
            lines.line_records(),
            lines.arc_records(),
            Vec::new(),
            SemanticData::default(),
        );
        assert_eq!(branch_data.measured_files(), line_data.measured_files());
        for path in line_data.measured_files() {
            assert_eq!(branch_data.lines_for(&path), line_data.lines_for(&path));
        }
        assert_eq!(
            branches.arc_records(),
            ["", "unit"]
                .into_iter()
                .flat_map(|context| {
                    [
                        (
                            context.to_owned(),
                            "first.py".to_owned(),
                            vec![(-10, 11), (-10, 13), (11, 12), (13, -10)],
                        ),
                        (
                            context.to_owned(),
                            "second.py".to_owned(),
                            vec![(-10, 20), (20, 21)],
                        ),
                    ]
                })
                .collect::<Vec<_>>()
        );
    }
}

#[test]
fn line_tracer_branch_mode_preserves_repeated_line_transitions() {
    let mut store = TraceStore::new(true);
    store.start_frame(17);
    store.line_event("unit", "source.py", 17, 10, 10);
    store.line_event("unit", "source.py", 17, 10, 10);
    store.end_frame("unit", Some("source.py"), 17, 10, None);

    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![(-10, 10), (10, -10), (10, 10)]
        )]
    );
}

#[test]
fn backward_same_line_edge_restores_only_the_missing_self_arc() {
    let mut store = TraceStore::new(true);
    store.add_arc("unit", "source.py", 10, 10);
    assert!(store.arc_records().is_empty());

    store.add_repeated_arc("unit", "source.py", 10);
    assert_eq!(
        store.arc_records(),
        vec![("unit".to_owned(), "source.py".to_owned(), vec![(10, 10)])]
    );
}

#[test]
fn suppressed_exit_belongs_to_one_active_frame() {
    let mut store = TraceStore::new(true);
    store.start_frame(17);
    store.line_event("unit", "source.py", 17, 10, 10);
    store.suppress_frame_exit(17);

    store.start_frame(17);
    store.line_event("unit", "source.py", 17, 10, 20);
    store.end_frame("unit", Some("source.py"), 17, 10, None);
    store.end_frame("unit", Some("source.py"), 17, 10, None);

    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![(-10, 10), (-10, 20), (20, -10)]
        )]
    );
}

#[test]
fn suspension_clears_the_frame_owned_exit_suppression() {
    let mut store = TraceStore::new(true);
    store.start_frame(17);
    store.line_event("unit", "source.py", 17, 10, 10);
    store.suppress_frame_exit(17);
    store.suspend_frame(17);

    store.resume_frame(17, Some(10));
    store.line_event("unit", "source.py", 17, 10, 11);
    store.end_frame("unit", Some("source.py"), 17, 10, None);

    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![(-10, 10), (10, 11), (11, -10)]
        )]
    );
}

#[test]
fn a_line_after_an_injected_exit_restores_normal_return_evidence() {
    let mut store = TraceStore::new(true);
    store.resume_frame(17, Some(10));
    store.suppress_frame_exit(17);
    store.line_event("unit", "source.py", 17, 10, 11);
    store.end_frame("unit", Some("source.py"), 17, 10, None);

    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![(10, 11), (11, -10)]
        )]
    );
}

#[test]
fn clearing_measurements_retains_live_frame_continuity() {
    let mut store = TraceStore::new(true);
    store.start_frame(1);
    store.line_event("test", "source.py", 1, 1, 1);
    store.clear_records();
    assert!(store.line_records().is_empty());
    assert!(store.arc_records().is_empty());
    store.line_event("test", "source.py", 1, 1, 2);
    store.end_frame("test", Some("source.py"), 1, 1, None);
    assert_eq!(
        store.arc_records(),
        vec![(
            "test".to_owned(),
            "source.py".to_owned(),
            vec![(1, 2), (2, -1)]
        )]
    );
}

#[test]
fn clearing_measurements_retains_only_live_frame_source_identities() {
    let mut store = TraceStore::new(true);
    store.line_event("", "discarded.py", 0, 1, 1);
    store.end_frame("", Some("discarded.py"), 0, 1, None);
    store.start_frame(1);
    store.line_event("", "first.py", 1, 1, 1);
    store.start_frame(2);
    store.line_event("", "second.py", 2, 5, 7);
    store.clear_records();
    store.start_frame(3);
    store.line_event("", "third.py", 3, 10, 10);
    store.end_frame("", Some("third.py"), 3, 10, None);
    store.end_frame("", Some("unmeasured.py"), 2, 5, None);
    store.line_event("", "fourth.py", 1, 1, 20);
    store.end_frame("", Some("fourth.py"), 1, 1, None);

    assert_eq!(
        store.arc_records(),
        vec![
            (
                String::new(),
                "fourth.py".to_owned(),
                vec![(-1, 20), (20, -1)]
            ),
            (String::new(), "second.py".to_owned(), vec![(7, -5)]),
            (
                String::new(),
                "third.py".to_owned(),
                vec![(-10, 10), (10, -10)]
            ),
        ]
    );
    assert_eq!(store.interner.paths.len(), 4);
}

#[test]
fn clearing_measurements_reclaims_interned_contexts_and_paths() {
    let mut store = TraceStore::new(true);
    for cycle in 0..100 {
        let context = format!("test_{cycle}");
        let path = format!("generated_{cycle}.py");
        store.add_line(&context, &path, cycle);
        assert_eq!(store.interner.contexts.len(), 1);
        assert_eq!(store.interner.paths.len(), 1);
        store.reset_records();
        assert!(store.interner.contexts.is_empty());
        assert!(store.interner.paths.is_empty());
    }

    store.start_frame(7);
    store.line_event("after-reset", "source.py", 7, 10, 10);
    store.line_event("after-reset", "source.py", 7, 10, 11);
    store.end_frame("after-reset", Some("source.py"), 7, 10, None);
    assert_eq!(
        store.arc_records(),
        vec![(
            "after-reset".to_owned(),
            "source.py".to_owned(),
            vec![(-10, 10), (10, 11), (11, -10)]
        )]
    );
}

#[test]
fn revisiting_an_earlier_context_preserves_later_context_partitions() {
    let mut store = TraceStore::new(false);
    store.add_line("first", "source.py", 1);
    store.add_line("second", "source.py", 2);
    store.add_line("first", "source.py", 3);

    assert_eq!(
        store.line_records(),
        vec![
            ("first".to_owned(), "source.py".to_owned(), vec![1, 3]),
            ("second".to_owned(), "source.py".to_owned(), vec![2]),
        ]
    );
}

#[test]
fn semantic_registration_retries_after_parse_failure_and_allows_missing_source() {
    let temporary = tempfile::tempdir().expect("semantic source directory");
    let path = temporary.path().join("decisions.py");
    let name = path.to_string_lossy();
    let mut store = TraceStore::with_options(TraceOptions {
        branch: true,
        decisions: true,
        ..TraceOptions::default()
    });
    let missing = temporary.path().join("missing.py");
    store
        .register_semantic_path(&missing.to_string_lossy())
        .expect("missing source is allowed");
    std::fs::write(&path, "if broken(:\n    pass\n").expect("malformed source");
    let error = store
        .register_semantic_path(&name)
        .expect_err("parse failure");
    assert!(error.to_string().contains("decisions.py"));
    std::fs::write(&path, "if flag:\n    pass\n").expect("valid source");
    store
        .register_semantic_path(&name)
        .expect("retry valid source");
    assert_eq!(store.semantic_data().definitions.len(), 1);
}

#[test]
fn checkpoints_keep_incomplete_semantics_but_reset_discards_them() {
    let temporary = tempfile::tempdir().expect("semantic source directory");
    let path = temporary.path().join("decisions.py");
    std::fs::write(&path, "if left and right:\n    pass\n").expect("semantic source");
    let path = path.to_string_lossy().into_owned();
    let mut store = TraceStore::with_options(TraceOptions {
        branch: true,
        decisions: true,
        ..TraceOptions::default()
    });
    store
        .register_semantic_path(&path)
        .expect("register semantic test source");
    store.start_frame(1);
    store.observe_semantic("test", &path, 1, -1, 0, true);
    store.clear_records();
    store.observe_semantic("test", &path, 1, -1, 1, false);
    store.flush_semantic_current();
    assert_eq!(store.semantic_data().observations.len(), 1);
    store.observe_semantic("test", &path, 1, -1, 0, true);
    store.reset_records();
    store.observe_semantic("test", &path, 1, -1, 1, true);
    store.flush_semantic_current();
    assert!(store.semantic_data().observations.is_empty());
}

#[test]
fn sysmon_arc_mode_keeps_line_events_and_self_arcs_separate_from_line_mode() {
    let mut sysmon = TraceStore::with_options(TraceOptions {
        branch: true,
        sysmon_arcs: true,
        ..TraceOptions::default()
    });
    sysmon.line_event("ctx", "source.py", 1, 10, 10);
    sysmon.line_event("ctx", "source.py", 1, 10, 11);
    assert_eq!(
        sysmon.line_records(),
        vec![("ctx".to_owned(), "source.py".to_owned(), vec![10, 11])]
    );
    assert_eq!(
        sysmon.arc_records(),
        vec![(
            "ctx".to_owned(),
            "source.py".to_owned(),
            vec![(10, 10), (11, 11)]
        )]
    );

    let mut line_only = TraceStore::with_options(TraceOptions::default());
    line_only.line_event("ctx", "source.py", 1, 10, 10);
    line_only.end_frame("ctx", Some("source.py"), 1, 10, None);
    assert_eq!(
        line_only.line_records(),
        vec![("ctx".to_owned(), "source.py".to_owned(), vec![10])]
    );
    assert!(line_only.arc_records().is_empty());
}

#[test]
fn ending_a_nested_frame_does_not_drop_the_parent_arc_state() {
    let mut store = TraceStore::new(true);
    store.start_frame(1);
    store.line_event("ctx", "source.py", 1, 1, 1);
    store.start_frame(2);
    store.line_event("ctx", "source.py", 2, 20, 20);
    store.end_frame("ctx", Some("source.py"), 2, 20, None);
    store.line_event("ctx", "source.py", 1, 1, 2);
    store.end_frame("ctx", Some("source.py"), 1, 1, None);
    assert_eq!(
        store.arc_records(),
        vec![(
            "ctx".to_owned(),
            "source.py".to_owned(),
            vec![(-20, 20), (-1, 1), (1, 2), (2, -1), (20, -20)]
        )]
    );
}

#[test]
fn completed_semantic_vectors_are_committed_only_when_the_frame_flushes() {
    let temporary = tempfile::tempdir().expect("semantic source directory");
    let path = temporary.path().join("decisions.py");
    std::fs::write(&path, "if left and right:\n    pass\n").expect("semantic source");
    let path = path.to_string_lossy().into_owned();

    let mut store = TraceStore::with_options(TraceOptions {
        decisions: true,
        ..TraceOptions::default()
    });
    store
        .register_semantic_path(&path)
        .expect("register semantic test source");
    store.start_frame(1);
    store.observe_semantic("test|run", &path, 1, -1, 0, true);
    store.observe_semantic("test|run", &path, 1, -1, 1, false);
    assert!(store.semantic_data().observations.is_empty());
    store.flush_semantic_current();
    let observations = &store.semantic_data().observations;
    assert_eq!(observations.len(), 1);
    let observation = observations.iter().next().expect("semantic observation");
    assert_eq!(observation.context, "test|run");
    assert_eq!(observation.vector, "10");
    assert!(!observation.outcome);
}

#[test]
fn incomplete_semantic_vectors_are_discarded_at_frame_end() {
    let temporary = tempfile::tempdir().expect("semantic source directory");
    let path = temporary.path().join("decisions.py");
    std::fs::write(&path, "if left and right:\n    pass\n").expect("semantic source");
    let path = path.to_string_lossy().into_owned();
    let mut store = TraceStore::with_options(TraceOptions {
        decisions: true,
        ..TraceOptions::default()
    });
    store
        .register_semantic_path(&path)
        .expect("register semantic test source");
    store.start_frame(1);
    store.observe_semantic("test|run", &path, 1, -1, 0, true);
    store.end_frame("test|run", Some(&path), 1, 1, None);
    assert!(store.semantic_data().observations.is_empty());
}

#[test]
fn semantic_path_state_machine_records_match_outcome() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/semantic_sample.py")
        .to_string_lossy()
        .into_owned();

    let mut match_store = TraceStore::with_options(TraceOptions {
        decisions: true,
        ..TraceOptions::default()
    });
    match_store
        .register_semantic_path(&path)
        .expect("register semantic test source");
    let (match_key, match_line, match_end, match_body_line) = match_store
        .semantic
        .path_definitions
        .values()
        .find(|definition| definition.kind == PathDecisionKind::Match)
        .map(|definition| {
            (
                definition.key.clone(),
                definition.line,
                definition.end_line,
                definition.outcomes[0].role.body().expect("match body").0,
            )
        })
        .expect("match definition");
    match_store.start_frame(1);
    match_store.observe_path_line("match|run", &path, 1, match_line);
    match_store.observe_path_line("match|run", &path, 1, match_body_line);
    match_store.observe_path_line("match|run", &path, 1, match_end + 1);
    match_store.finish_paths("match|run", 1, false);
    assert_eq!(
        match_store
            .semantic
            .path_observations
            .iter()
            .map(|observation| (observation.decision.clone(), observation.outcome))
            .collect::<Vec<_>>(),
        vec![(match_key, 0)]
    );
}

#[test]
fn semantic_path_state_machine_records_exception_outcomes() {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/semantic_sample.py")
        .to_string_lossy()
        .into_owned();
    let scenarios = [("normal|run", false, false), ("handler|run", true, false)];
    for (context, raises, cancelled) in scenarios {
        let mut store = TraceStore::with_options(TraceOptions {
            decisions: true,
            ..TraceOptions::default()
        });
        store
            .register_semantic_path(&path)
            .expect("register semantic test source");
        let (try_key, try_line, try_end, handler_line) = store
            .semantic
            .path_definitions
            .values()
            .find(|definition| definition.kind == PathDecisionKind::Try)
            .map(|definition| {
                (
                    definition.key.clone(),
                    definition.line,
                    definition.end_line,
                    definition.outcomes[1]
                        .role
                        .handler_body()
                        .expect("handler body")
                        .0,
                )
            })
            .expect("try definition");
        store.start_frame(2);
        store.observe_path_line(context, &path, 2, try_line + 1);
        if raises {
            store.observe_path_raise(context, &path, 2, try_line + 1, cancelled);
            store.observe_path_line(context, &path, 2, handler_line);
        } else {
            store.observe_path_line(context, &path, 2, try_end + 1);
        }
        store.finish_paths(context, 2, false);
        let observation = store
            .semantic
            .path_observations
            .iter()
            .next()
            .expect("path outcome");
        assert_eq!(observation.decision, try_key);
        assert_eq!(observation.outcome, usize::from(raises));
    }

    let mut unhandled = TraceStore::with_options(TraceOptions {
        decisions: true,
        ..TraceOptions::default()
    });
    unhandled
        .register_semantic_path(&path)
        .expect("register semantic test source");
    let (try_key, try_line, try_end) = unhandled
        .semantic
        .path_definitions
        .values()
        .find(|definition| definition.kind == PathDecisionKind::Try)
        .map(|definition| (definition.key.clone(), definition.line, definition.end_line))
        .expect("try definition");
    unhandled.start_frame(3);
    unhandled.observe_path_line("unhandled|run", &path, 3, try_line + 1);
    unhandled.observe_path_raise("unhandled|run", &path, 3, try_line + 1, false);
    unhandled.observe_path_line("unhandled|run", &path, 3, try_end + 1);
    unhandled.finish_paths("unhandled|run", 3, true);
    let observation = unhandled
        .semantic
        .path_observations
        .iter()
        .next()
        .expect("unhandled path outcome");
    assert_eq!(observation.decision, try_key);
    assert_eq!(observation.outcome, 2);
}

#[test]
fn short_circuit_path_observations_commit_selected_and_final_operands() {
    for (kind, first_value, expected_outcome) in [
        (PathDecisionKind::ValueAnd, false, 0),
        (PathDecisionKind::ValueOr, true, 0),
    ] {
        let path = "value.py".to_owned();
        let key = format!("{}-decision", kind.as_str());
        let definition = PathDecisionDefinition {
            key: key.clone(),
            path: path.clone(),
            line: 1,
            end_line: 1,
            kind,
            source_hash: "hash".to_owned(),
            expression: "first and second".to_owned(),
            body_start: 1,
            body_end: 1,
            range: (1, 0, 1, 16),
            outcomes: vec![
                PathOutcomeDefinition {
                    label: "operand 1 selected".to_owned(),
                    range: (1, 0, 1, 5),
                    role: PathOutcomeRole::Operand,
                },
                PathOutcomeDefinition {
                    label: "operand 2 selected".to_owned(),
                    range: (1, 10, 1, 16),
                    role: PathOutcomeRole::Operand,
                },
            ],
        };
        let mut selected = TraceStore::with_options(TraceOptions {
            decisions: true,
            ..TraceOptions::default()
        });
        selected
            .semantic
            .path_definitions
            .insert(key.clone(), definition);
        selected
            .path_definition_keys
            .insert(path.clone(), vec![key.clone()]);
        selected.observe_path_branch("selected|run", &path, 1, (1, 0, 1, 16), 0, first_value);
        let observations = selected.semantic.path_observations;
        assert_eq!(
            observations
                .iter()
                .map(|observation| observation.outcome)
                .collect::<Vec<_>>(),
            vec![expected_outcome]
        );

        let mut final_operand = TraceStore::with_options(TraceOptions {
            decisions: true,
            ..TraceOptions::default()
        });
        final_operand
            .semantic
            .path_definitions
            .insert(key.clone(), selected_definition(kind, &key, &path));
        final_operand
            .path_definition_keys
            .insert(path.clone(), vec![key.clone()]);
        final_operand.start_frame(1);
        final_operand.observe_path_branch("final|run", &path, 1, (1, 0, 1, 16), 0, !first_value);
        final_operand.flush_path_current("final|run", 1);
        let observations = final_operand.semantic.path_observations;
        assert_eq!(
            observations
                .iter()
                .map(|observation| observation.outcome)
                .collect::<Vec<_>>(),
            vec![1]
        );
    }
}

fn selected_definition(kind: PathDecisionKind, key: &str, path: &str) -> PathDecisionDefinition {
    PathDecisionDefinition {
        key: key.to_owned(),
        path: path.to_owned(),
        line: 1,
        end_line: 1,
        kind,
        source_hash: "hash".to_owned(),
        expression: "first and second".to_owned(),
        body_start: 1,
        body_end: 1,
        range: (1, 0, 1, 16),
        outcomes: vec![
            PathOutcomeDefinition {
                label: "operand 1 selected".to_owned(),
                range: (1, 0, 1, 5),
                role: PathOutcomeRole::Operand,
            },
            PathOutcomeDefinition {
                label: "operand 2 selected".to_owned(),
                range: (1, 10, 1, 16),
                role: PathOutcomeRole::Operand,
            },
        ],
    }
}

#[test]
fn generator_resumption_uses_its_own_position_with_shared_code() {
    let mut store = TraceStore::new(true);
    for line in [11, 12] {
        store.start_frame(17);
        store.line_event("unit", "source.py", 17, 10, line);
        store.suspend_frame(17);
    }
    for (previous, next) in [(11, 14), (12, 15)] {
        store.resume_frame(17, Some(previous));
        store.line_event("unit", "source.py", 17, 10, next);
        store.end_frame("unit", Some("source.py"), 17, 10, None);
    }
    assert_eq!(
        store.arc_records(),
        vec![(
            "unit".to_owned(),
            "source.py".to_owned(),
            vec![
                (-10, 11),
                (-10, 12),
                (11, 14),
                (12, 15),
                (14, -10),
                (15, -10)
            ]
        )]
    );
}

fn line_set<'a>(
    lines: &'a super::TraceLines,
    context: &str,
    path: &str,
) -> &'a crate::evidence::SparseLines {
    lines
        .0
        .iter()
        .find(|(record_context, record_path, _)| {
            record_context.as_ref() == context && record_path.as_ref() == path
        })
        .map(|(_, _, lines)| lines)
        .expect("recorded line set")
}

#[test]
fn finished_contexts_share_equal_line_sets_and_copy_before_reopening() {
    let mut store = TraceStore::new(false);
    for context in ["test_a", "test_b", "test_c"] {
        store.add_line(context, "a.py", 1);
        store.add_line(context, "a.py", 70);
        store.share_finished_lines();
    }
    store.add_line("test_c", "b.py", 2);
    // Reopening a finished context must not change contexts sharing its set.
    store.add_line("test_a", "a.py", 3);
    store.share_finished_lines();

    let lines = store.copy_lines();
    let a = line_set(&lines, "test_a", "a.py");
    let b = line_set(&lines, "test_b", "a.py");
    let c = line_set(&lines, "test_c", "a.py");
    assert!(b.shares_allocation(c));
    assert!(!a.shares_allocation(b));
    assert_eq!(
        store.line_records(),
        vec![
            ("test_a".to_owned(), "a.py".to_owned(), vec![1, 3, 70]),
            ("test_b".to_owned(), "a.py".to_owned(), vec![1, 70]),
            ("test_c".to_owned(), "a.py".to_owned(), vec![1, 70]),
            ("test_c".to_owned(), "b.py".to_owned(), vec![2]),
        ]
    );
}

#[test]
fn moved_and_copied_lines_build_the_same_coverage_data_as_line_records() {
    let mut store = TraceStore::new(false);
    for (context, path, line) in [
        ("test_b", "b.py", 4),
        ("test_a", "b.py", 130),
        ("test_a", "a.py", 2),
        ("", "a.py", 1),
        ("test_b", "b.py", 64),
    ] {
        store.add_line(context, path, line);
        store.share_finished_lines();
    }
    store.add_line("test_b", "b.py", 5);
    let expected = CoverageData::from_trace_records(
        false,
        store.line_records(),
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );

    let copied = CoverageData::from_trace(
        false,
        store.copy_lines(),
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );
    let moved = CoverageData::from_trace(
        false,
        store.take_lines(),
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );

    let expected = expected.serialize().expect("expected payload");
    assert_eq!(copied.serialize().expect("copied payload"), expected);
    assert_eq!(moved.serialize().expect("moved payload"), expected);
    assert!(store.line_records().is_empty());
}
