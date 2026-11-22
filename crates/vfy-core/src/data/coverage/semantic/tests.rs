use std::collections::BTreeMap;

use crate::Logic;
use crate::semantic::{
    DecisionDefinition, PathDecisionDefinition, PathDecisionKind, PathObservation,
    PathOutcomeDefinition, PathOutcomeRole, SemanticData, SemanticObservation,
    analyze_semantic_source_data,
};

use super::super::CoverageData;

fn discovered_coverage(
    path: &str,
    source: &str,
    vector: &str,
    outcome: bool,
    path_outcome: usize,
) -> CoverageData {
    let (definitions, paths) =
        analyze_semantic_source_data(path, source).expect("valid semantic test source");
    assert_eq!((definitions.len(), paths.len()), (1, 1));
    let mut semantic = SemanticData::default();
    for definition in definitions {
        let key = definition.key().to_owned();
        semantic
            .insert_definition(definition)
            .expect("unique Boolean definition");
        semantic
            .insert_observation("test|run".to_owned(), key, vector.to_owned(), outcome)
            .expect("valid Boolean observation");
    }
    for definition in paths {
        let key = definition.key().to_owned();
        semantic
            .insert_path_definition(definition)
            .expect("unique path definition");
        semantic
            .insert_path_observation("test|run".to_owned(), key, path_outcome)
            .expect("valid path observation");
    }
    CoverageData::from_trace_records(
        false,
        vec![("test|run".to_owned(), path.to_owned(), vec![1, 2])],
        Vec::new(),
        Vec::new(),
        semantic,
    )
}

fn rekey_coverage(mut data: CoverageData, transform: impl Fn(&str) -> String) -> CoverageData {
    data.semantic.definitions = data
        .semantic
        .definitions
        .into_values()
        .map(|mut definition| {
            definition.key = transform(&definition.key);
            (definition.key.clone(), definition)
        })
        .collect();
    data.semantic.observations = data
        .semantic
        .observations
        .into_iter()
        .map(|mut observation| {
            observation.decision = transform(&observation.decision);
            observation
        })
        .collect();
    data.semantic.path_definitions = data
        .semantic
        .path_definitions
        .into_values()
        .map(|mut definition| {
            definition.key = transform(&definition.key);
            (definition.key.clone(), definition)
        })
        .collect();
    data.semantic.path_observations = data
        .semantic
        .path_observations
        .into_iter()
        .map(|mut observation| {
            observation.decision = transform(&observation.decision);
            observation
        })
        .collect();
    data
}

fn unscoped_key(key: &str) -> String {
    format!("vfy:{}", key.rsplit(':').next().expect("generated digest"))
}

#[test]
fn unscoped_schema_and_materialized_records_merge_with_fresh_keys() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let path = "module.py";
    let old = rekey_coverage(
        discovered_coverage(path, source, "0-", false, 0),
        unscoped_key,
    );
    let temporary = tempfile::tempdir().expect("temporary coverage directory");
    let database = temporary.path().join("coverage.sqlite");
    old.write_path(&database)
        .expect("write unscoped schema-7 records");
    let restored = CoverageData::read_path(&database).expect("read unscoped records");
    let materialized = CoverageData::from_materialized_records(
        false,
        old.report_inputs(),
        BTreeMap::new(),
        BTreeMap::new(),
        Some(&old),
    )
    .expect("materialize unscoped records");
    assert!(
        old.semantic_data()
            .definitions()
            .keys()
            .all(|key| key.matches(':').count() == 1)
    );
    for mut accepted in [restored, materialized] {
        accepted
            .merge_from(discovered_coverage(path, source, "11", true, 1), false)
            .expect("merge complementary fresh observations");
        let semantic = accepted.semantic_data();
        assert_eq!(semantic.definitions().len(), 1);
        assert_eq!(semantic.path_definitions().len(), 1);
        assert_eq!(semantic.observations().len(), 2);
        assert_eq!(semantic.path_observations().len(), 2);
        let (definitions, paths) =
            analyze_semantic_source_data(path, source).expect("valid semantic test source");
        assert!(semantic.definitions().contains_key(definitions[0].key()));
        assert!(semantic.path_definitions().contains_key(paths[0].key()));
        accepted
            .write_path(&database)
            .expect("persist normalized records");
        let round_trip = CoverageData::read_path(&database).expect("read normalized records");
        assert_eq!(
            round_trip.semantic_data().observation_records(),
            semantic.observation_records()
        );
        assert_eq!(
            round_trip.semantic_data().path_observation_records(),
            semantic.path_observation_records()
        );
    }
}

#[test]
fn schema_key_normalization_preserves_opaque_and_malformed_keys() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let temporary = tempfile::tempdir().expect("temporary coverage directory");
    let database = temporary.path().join("coverage.sqlite");
    for key in [
        "external-key".to_owned(),
        format!("vfy:{}", "G".repeat(64)),
        format!("vfy:{}:extra", "a".repeat(64)),
    ] {
        let old = rekey_coverage(
            discovered_coverage("module.py", source, "0-", false, 0),
            |_| key.clone(),
        );
        old.write_path(&database).expect("write opaque key");
        let restored = CoverageData::read_path(&database).expect("read opaque key");
        assert_eq!(
            restored.semantic_data().records(),
            old.semantic_data().records()
        );
        assert_eq!(
            restored.semantic_data().path_records(),
            old.semantic_data().path_records()
        );
        assert_eq!(
            restored.semantic_data().observation_records(),
            old.semantic_data().observation_records()
        );
        assert_eq!(
            restored.semantic_data().path_observation_records(),
            old.semantic_data().path_observation_records()
        );
    }
}

#[test]
fn generated_key_normalization_rejects_source_conflicts_before_coalescing() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    for is_path in [false, true] {
        let fresh = discovered_coverage("module.py", source, "11", true, 1);
        let mut old = rekey_coverage(
            discovered_coverage("module.py", source, "0-", false, 0),
            unscoped_key,
        );
        if is_path {
            old.semantic
                .path_definitions
                .values_mut()
                .next()
                .expect("path definition")
                .source_hash = "different revision".to_owned();
        } else {
            old.semantic
                .definitions
                .values_mut()
                .next()
                .expect("Boolean definition")
                .source_hash = "different revision".to_owned();
        }
        old.semantic.merge_validated(fresh.semantic);
        let temporary = tempfile::tempdir().expect("temporary coverage directory");
        let database = temporary.path().join("coverage.sqlite");
        old.write_path(&database)
            .expect("write conflicting key formats");
        let error = CoverageData::read_path(&database)
            .err()
            .expect("reject source conflict");
        assert!(error.to_string().contains("different source revisions"));
        let before = old.semantic_data().records();
        let before_paths = old.semantic_data().path_records();
        let result = CoverageData::from_materialized_records(
            false,
            old.report_inputs(),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(&old),
        );
        assert!(
            result
                .err()
                .expect("reject materialized conflict")
                .to_string()
                .contains("different source revisions")
        );
        assert_eq!(old.semantic_data().records(), before);
        assert_eq!(old.semantic_data().path_records(), before_paths);
    }
}

fn change_definition_shape(data: &mut CoverageData, is_path: bool) {
    if is_path {
        let definition = data
            .semantic
            .path_definitions
            .values_mut()
            .next()
            .expect("path definition");
        let mut extra = definition.outcomes[0].clone();
        extra.label = "extra".to_owned();
        definition.outcomes.push(extra);
        data.semantic.path_observations = std::mem::take(&mut data.semantic.path_observations)
            .into_iter()
            .map(|mut observation| {
                observation.outcome = 2;
                observation
            })
            .collect();
    } else {
        let definition = data
            .semantic
            .definitions
            .values_mut()
            .next()
            .expect("Boolean definition");
        definition.logic = Logic::Condition(0);
        definition.condition_lines.truncate(1);
        definition.condition_ranges.truncate(1);
        data.semantic.observations = std::mem::take(&mut data.semantic.observations)
            .into_iter()
            .map(|mut observation| {
                observation.vector = "0".to_owned();
                observation
            })
            .collect();
    }
}

#[test]
fn generated_key_normalization_rejects_definition_conflicts_before_coalescing() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    for is_path in [false, true] {
        let fresh = discovered_coverage("module.py", source, "11", true, 1);
        let mut old = rekey_coverage(
            discovered_coverage("module.py", source, "0-", false, 0),
            unscoped_key,
        );
        change_definition_shape(&mut old, is_path);
        old.semantic.merge_validated(fresh.semantic);
        let temporary = tempfile::tempdir().expect("temporary coverage directory");
        let database = temporary.path().join("coverage.sqlite");
        old.write_path(&database)
            .expect("write independently valid definitions with conflicting canonical keys");
        let before = std::fs::read(&database).expect("original database bytes");
        let error = CoverageData::read_path(&database)
            .err()
            .expect("reject conflicting canonical definitions");
        assert!(
            error
                .to_string()
                .contains("conflicting semantic definitions"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(&database).expect("unchanged database"),
            before
        );
        let error = CoverageData::from_materialized_records(
            false,
            old.report_inputs(),
            BTreeMap::new(),
            BTreeMap::new(),
            Some(&old),
        )
        .err()
        .expect("reject conflicting materialized definitions");
        assert!(
            error
                .to_string()
                .contains("conflicting semantic definitions"),
            "{error}"
        );
    }
}

#[test]
fn equivalent_generated_key_definitions_coalesce_and_round_trip() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let fresh = discovered_coverage("module.py", source, "11", true, 1);
    let mut old = rekey_coverage(
        discovered_coverage("module.py", source, "0-", false, 0),
        unscoped_key,
    );
    old.semantic.merge_validated(fresh.semantic);
    let temporary = tempfile::tempdir().expect("temporary coverage directory");
    let database = temporary.path().join("coverage.sqlite");
    old.write_path(&database)
        .expect("write equivalent definitions");
    let restored = CoverageData::read_path(&database).expect("coalesce equivalent definitions");
    assert_eq!(restored.semantic.definitions.len(), 1);
    assert_eq!(restored.semantic.path_definitions.len(), 1);
    assert_eq!(restored.semantic.observations.len(), 2);
    assert_eq!(restored.semantic.path_observations.len(), 2);
    restored
        .write_path(&database)
        .expect("write normalized database");
    let round_trip = CoverageData::read_path(&database).expect("reread normalized database");
    assert_eq!(round_trip.semantic.records(), restored.semantic.records());
    assert_eq!(
        round_trip.semantic.path_records(),
        restored.semantic.path_records()
    );
    assert_eq!(
        round_trip.semantic.observation_records(),
        restored.semantic.observation_records()
    );
    assert_eq!(
        round_trip.semantic.path_observation_records(),
        restored.semantic.path_observation_records()
    );
}

#[test]
fn semantic_path_aliases_rekey_observations_and_round_trip_without_duplicate_decisions() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let mut combined = discovered_coverage("worker-a/module.py", source, "0-", false, 0);
    combined
        .merge_from(
            discovered_coverage("worker-b/module.py", source, "11", true, 1),
            false,
        )
        .expect("distinct files can be combined");
    assert_eq!(combined.semantic_data().definitions().len(), 2);
    assert_eq!(combined.semantic_data().path_definitions().len(), 2);

    let target = "src/module.py".to_owned();
    let mapped = combined
        .remapped_files(&BTreeMap::from([
            ("worker-a/module.py".to_owned(), target.clone()),
            ("worker-b/module.py".to_owned(), target.clone()),
        ]))
        .expect("same source revisions collapse under path aliases");
    let semantic = mapped.semantic_data();
    let (expected, expected_paths) =
        analyze_semantic_source_data(&target, source).expect("valid semantic test source");
    assert_eq!(semantic.definitions().len(), 1);
    assert_eq!(semantic.path_definitions().len(), 1);
    assert_eq!(semantic.observations().len(), 2);
    assert_eq!(semantic.path_observations().len(), 2);
    assert!(semantic.definitions().contains_key(expected[0].key()));
    assert!(
        semantic
            .path_definitions()
            .contains_key(expected_paths[0].key())
    );
    assert!(
        semantic
            .observations()
            .iter()
            .all(|observation| observation.decision() == expected[0].key())
    );
    assert!(
        semantic
            .path_observations()
            .iter()
            .all(|observation| observation.decision() == expected_paths[0].key())
    );

    let idempotent = mapped
        .clone()
        .remapped_files(&BTreeMap::from([(target.clone(), target)]))
        .expect("remapping an unchanged path is idempotent");
    let temporary = tempfile::tempdir().expect("temporary coverage directory");
    let database = temporary.path().join("coverage.sqlite");
    idempotent
        .write_path(&database)
        .expect("persist rekeyed observations");
    let restored = CoverageData::read_path(&database).expect("read rekeyed observations");
    for actual in [idempotent.semantic_data(), restored.semantic_data()] {
        assert_eq!(actual.records(), semantic.records());
        assert_eq!(actual.condition_records(), semantic.condition_records());
        assert_eq!(actual.observation_records(), semantic.observation_records());
        assert_eq!(actual.path_records(), semantic.path_records());
        assert_eq!(
            actual.path_outcome_records(),
            semantic.path_outcome_records()
        );
        assert_eq!(
            actual.path_observation_records(),
            semantic.path_observation_records()
        );
    }
}

#[test]
fn live_and_persisted_path_definitions_merge_without_losing_header_ranges() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let temporary = tempfile::tempdir().expect("temporary semantic data directory");
    let database = temporary.path().join("coverage.sqlite");
    for aliases in [false, true] {
        for disk_first in [false, true] {
            let first_path = "first.py";
            let second_path = if aliases { "second.py" } else { first_path };
            let first = discovered_coverage(first_path, source, "0-", false, 0);
            let second = discovered_coverage(second_path, source, "11", true, 1);
            let (disk, live) = if disk_first {
                (first, second)
            } else {
                (second, first)
            };
            disk.write_path(&database).expect("persist one collection");
            let disk = CoverageData::read_path(&database).expect("read persisted collection");
            let (mut merged, incoming) = if disk_first {
                (disk, live)
            } else {
                (live, disk)
            };
            merged
                .merge_from(incoming, false)
                .expect("merge live and persisted definitions");
            let target = "target.py".to_owned();
            let merged = merged
                .remapped_files(&BTreeMap::from([
                    (first_path.to_owned(), target.clone()),
                    (second_path.to_owned(), target.clone()),
                ]))
                .expect("coalesce compatible aliased definitions");
            let semantic = merged.semantic_data();
            let (expected, expected_paths) =
                analyze_semantic_source_data(&target, source).expect("valid semantic test source");
            assert_eq!(semantic.definitions.len(), 1);
            assert_eq!(semantic.path_definitions.len(), 1);
            assert_eq!(semantic.definitions[expected[0].key()], expected[0]);
            assert_eq!(
                semantic.path_definitions[expected_paths[0].key()],
                expected_paths[0]
            );
            assert_eq!(semantic.observations.len(), 2);
            assert_eq!(semantic.path_observations.len(), 2);
            merged
                .write_path(&database)
                .expect("persist merged evidence");
            let restored = CoverageData::read_path(&database).expect("read merged evidence");
            assert_eq!(restored.semantic.records(), semantic.records());
            assert_eq!(restored.semantic.path_records(), semantic.path_records());
            assert_eq!(
                restored.semantic.observation_records(),
                semantic.observation_records()
            );
            assert_eq!(
                restored.semantic.path_observation_records(),
                semantic.path_observation_records()
            );
        }
    }
}

#[test]
fn semantic_aliases_reject_conflicting_source_revisions() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    let mut combined = discovered_coverage("worker-a/module.py", source, "0-", false, 0);
    combined
        .merge_from(
            discovered_coverage(
                "worker-b/module.py",
                "value = left or right\nif left and right:\n    value = 1\n",
                "11",
                true,
                1,
            ),
            false,
        )
        .expect("different files can contain different source revisions");
    let result = combined.remapped_files(&BTreeMap::from([
        ("worker-a/module.py".to_owned(), "src/module.py".to_owned()),
        ("worker-b/module.py".to_owned(), "src/module.py".to_owned()),
    ]));
    assert!(matches!(
        result,
        Err("cannot combine semantic data from different source revisions")
    ));
}

#[test]
fn semantic_alias_collisions_preserve_the_output_and_input_shard() {
    let source = "value = left or right\nif left and right:\n    pass\n";
    for is_path in [false, true] {
        let temporary = tempfile::tempdir().expect("temporary alias transaction");
        let target_root = temporary.path().join("src");
        std::fs::create_dir(&target_root).expect("target source directory");
        let target = target_root.join("module.py");
        std::fs::write(&target, source).expect("alias target exists");
        let first_root = temporary.path().join("worker-a");
        let second_root = temporary.path().join("worker-b");
        let first = first_root.join("module.py").to_string_lossy().into_owned();
        let second = second_root.join("module.py").to_string_lossy().into_owned();
        let mut combined = discovered_coverage(&first, source, "0-", false, 0);
        change_definition_shape(&mut combined, is_path);
        combined
            .merge_from(discovered_coverage(&second, source, "11", true, 1), false)
            .expect("distinct original paths have independent definitions");
        let shard = temporary.path().join(".coverage.worker");
        combined
            .write_path(&shard)
            .expect("write independent definitions");
        let accepted =
            CoverageData::read_path(&shard).expect("both definitions validate before aliasing");
        assert_eq!(accepted.semantic.definitions.len(), 2);
        assert_eq!(accepted.semantic.path_definitions.len(), 2);

        let output = temporary.path().join(".coverage");
        discovered_coverage(target.to_string_lossy().as_ref(), source, "11", true, 1)
            .write_path(&output)
            .expect("write original destination");
        let before_output = std::fs::read(&output).expect("original destination bytes");
        let before_shard = std::fs::read(&shard).expect("original shard bytes");
        let error = crate::data::combine_transaction(
            vec![shard.to_string_lossy().into_owned()],
            &output,
            None,
            crate::data::CombineOptions {
                aliases: vec![vec![
                    target_root.to_string_lossy().into_owned(),
                    first_root.to_string_lossy().into_owned(),
                    second_root.to_string_lossy().into_owned(),
                ]],
                ..crate::data::CombineOptions::default()
            },
        )
        .err()
        .expect("reject unequal definitions with a shared aliased key");
        assert!(
            error
                .to_string()
                .contains("conflicting semantic definitions"),
            "{error}"
        );
        assert_eq!(
            std::fs::read(&output).expect("destination retained"),
            before_output
        );
        assert_eq!(std::fs::read(&shard).expect("shard retained"), before_shard);
        CoverageData::read_path(&output).expect("original destination remains valid");
    }
}

fn semantic_data() -> SemanticData {
    let mut semantic = SemanticData::default();
    semantic.definitions.insert(
        "decision-1".to_owned(),
        DecisionDefinition {
            key: "decision-1".to_owned(),
            path: "module.py".to_owned(),
            line: 4,
            end_line: 4,
            kind: "if".to_owned(),
            source_hash: "hash-1".to_owned(),
            expression: "left and right".to_owned(),
            logic: Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]),
            condition_lines: vec![4, 4],
            condition_ranges: vec![(4, 3, 4, 7), (4, 12, 4, 17)],
        },
    );
    semantic.observations.insert(SemanticObservation {
        context: "test_one|run".to_owned(),
        decision: "decision-1".to_owned(),
        vector: "11".to_owned(),
        outcome: true,
    });
    semantic.path_definitions.insert(
        "path-1".to_owned(),
        PathDecisionDefinition {
            key: "path-1".to_owned(),
            path: "module.py".to_owned(),
            line: 8,
            end_line: 10,
            kind: PathDecisionKind::Try,
            source_hash: "hash-1".to_owned(),
            expression: "try".to_owned(),
            body_start: 9,
            body_end: 10,
            range: (8, 0, 8, 3),
            outcomes: vec![
                PathOutcomeDefinition {
                    label: "normal".to_owned(),
                    range: (8, 0, 8, 3),
                    role: PathOutcomeRole::Normal,
                },
                PathOutcomeDefinition {
                    label: "handler".to_owned(),
                    range: (9, 0, 10, 5),
                    role: PathOutcomeRole::Handler { body: (9, 10) },
                },
            ],
        },
    );
    semantic.path_observations.insert(PathObservation {
        context: "test_one|run".to_owned(),
        decision: "path-1".to_owned(),
        outcome: 0,
    });
    semantic
}

#[test]
fn semantic_tables_round_trip_boolean_and_named_path_evidence() {
    let temporary = tempfile::tempdir().expect("temporary data directory");
    let path = temporary.path().join("coverage.sqlite");
    let original = CoverageData::from_trace_records(
        true,
        vec![("test_one|run".to_owned(), "module.py".to_owned(), vec![4])],
        vec![(
            "test_one|run".to_owned(),
            "module.py".to_owned(),
            vec![(4, 8)],
        )],
        Vec::new(),
        semantic_data(),
    );
    original.write_path(&path).expect("write semantic data");
    let loaded = CoverageData::read_path(&path).expect("read semantic data");

    let expected = original.semantic_data();
    let actual = loaded.semantic_data();
    assert_eq!(actual.records(), expected.records());
    assert_eq!(actual.condition_records(), expected.condition_records());
    assert_eq!(actual.observation_records(), expected.observation_records());
    assert_eq!(actual.path_records(), expected.path_records());
    assert_eq!(
        actual.path_outcome_records(),
        expected.path_outcome_records()
    );
    assert_eq!(
        actual.path_observation_records(),
        expected.path_observation_records()
    );
    for (key, definition) in &expected.definitions {
        assert_eq!(
            actual.definitions[key].condition_lines,
            definition.condition_lines
        );
    }
    for (key, definition) in &expected.path_definitions {
        assert_eq!(actual.path_definitions[key].outcomes, definition.outcomes);
        // Schema 7's extension stores path line bounds without header columns.
        assert_eq!(
            actual.path_definitions[key].range,
            (definition.line, 0, definition.end_line, 0)
        );
    }
}

#[test]
fn invalid_semantic_observation_is_rejected_on_read() {
    let temporary = tempfile::tempdir().expect("temporary data directory");
    let path = temporary.path().join("coverage.sqlite");
    let original = CoverageData::from_trace_records(
        false,
        Vec::new(),
        Vec::new(),
        Vec::new(),
        semantic_data(),
    );
    original.write_path(&path).expect("write semantic data");
    let connection = rusqlite::Connection::open(&path).expect("open data");
    connection
        .execute(
            "UPDATE vfy_observation SET vector = '10-' WHERE decision = 'decision-1'",
            [],
        )
        .expect("corrupt observation");
    drop(connection);
    assert!(CoverageData::read_path(&path).is_err());
}

#[test]
fn semantic_reader_reads_path_tables_when_boolean_tables_are_absent() {
    let connection = rusqlite::Connection::open_in_memory().expect("open in-memory database");
    connection
        .execute_batch(
            "CREATE TABLE vfy_path_decision (key text primary key, path text, line integer, end_line integer, kind text, source_hash text, expression text, body_start integer, body_end integer);
             CREATE TABLE vfy_path_outcome (decision text, index_no integer, label text, start_line integer, start_col integer, end_line integer, end_col integer, role text, body_start integer, body_end integer);
             CREATE TABLE vfy_path_observation (context text, decision text, outcome integer);
             INSERT INTO vfy_path_decision VALUES ('path', 'module.py', 1, 1, 'value_and', 'hash', 'a and b', 1, 1);
             INSERT INTO vfy_path_outcome VALUES ('path', 0, 'true', 1, 0, 1, 1, 'normal', NULL, NULL);
             INSERT INTO vfy_path_outcome VALUES ('path', 1, 'false', 1, 0, 1, 1, 'unhandled', NULL, NULL);
             INSERT INTO vfy_path_observation VALUES ('test|run', 'path', 0);",
        )
        .expect("create path semantic tables");
    let mut data = CoverageData::from_trace_records(
        false,
        Vec::new(),
        Vec::new(),
        Vec::new(),
        SemanticData::default(),
    );
    data.touch_file("module.py");
    super::data_read_semantic(&mut data, &connection).expect("read path semantic tables");
    assert_eq!(data.semantic_data().path_definitions.len(), 1);
    assert_eq!(data.semantic_data().path_observations.len(), 1);
}

#[test]
fn semantic_records_are_sorted_across_definitions_and_contexts() {
    let mut semantic = SemanticData::default();
    for suffix in ["z", "a", "m"] {
        let mut entry = semantic_data();
        let mut definition = entry
            .definitions
            .remove("decision-1")
            .expect("boolean fixture");
        definition.key = format!("decision-{suffix}");
        let mut path = entry
            .path_definitions
            .remove("path-1")
            .expect("path fixture");
        path.key = format!("path-{suffix}");
        for context in ["z-context", "a-context"] {
            semantic.observations.insert(SemanticObservation {
                context: context.to_owned(),
                decision: definition.key.clone(),
                vector: "11".to_owned(),
                outcome: true,
            });
            semantic.path_observations.insert(PathObservation {
                context: context.to_owned(),
                decision: path.key.clone(),
                outcome: 0,
            });
        }
        semantic
            .definitions
            .insert(definition.key.clone(), definition);
        semantic.path_definitions.insert(path.key.clone(), path);
    }
    assert_eq!(
        semantic
            .records()
            .into_iter()
            .map(|row| row.0)
            .collect::<Vec<_>>(),
        ["decision-a", "decision-m", "decision-z"]
    );
    assert_eq!(
        semantic
            .path_records()
            .into_iter()
            .map(|row| row.0)
            .collect::<Vec<_>>(),
        ["path-a", "path-m", "path-z"]
    );
    let expected = [
        ("a-context", "a"),
        ("a-context", "m"),
        ("a-context", "z"),
        ("z-context", "a"),
        ("z-context", "m"),
        ("z-context", "z"),
    ];
    assert_eq!(
        semantic.observation_records(),
        expected.map(|(context, suffix)| (
            context.to_owned(),
            format!("decision-{suffix}"),
            "11".to_owned(),
            true
        ))
    );
    assert_eq!(
        semantic.path_observation_records(),
        expected.map(|(context, suffix)| (context.to_owned(), format!("path-{suffix}"), 0))
    );
}

#[test]
fn malformed_semantic_definitions_preserve_the_destination() {
    for sql in [
        "UPDATE vfy_decision SET kind = 'unknown'",
        "UPDATE vfy_decision SET kind = ''",
        "UPDATE vfy_condition SET start_col = 10, end_col = 2 WHERE index_no = 0",
        "UPDATE vfy_decision SET condition_lines = '0,4'",
        "UPDATE vfy_decision SET condition_lines = '4'",
        "UPDATE vfy_path_outcome SET body_end = 0 WHERE role = 'handler'",
        "UPDATE vfy_path_outcome SET start_line = 8, end_line = 8, start_col = 10, end_col = 2 WHERE index_no = 0",
    ] {
        let temporary = tempfile::tempdir().expect("temporary database directory");
        let path = temporary.path().join("coverage.sqlite");
        let mut destination = CoverageData::from_trace_records(
            false,
            Vec::new(),
            Vec::new(),
            Vec::new(),
            semantic_data(),
        );
        destination.touch_file("module.py");
        destination
            .write_path(&path)
            .expect("write valid semantic data");
        let connection = rusqlite::Connection::open(&path).expect("open database");
        connection.execute(sql, []).expect("corrupt semantic range");
        let before = destination.semantic.records();
        assert!(
            super::data_read_semantic(&mut destination, &connection).is_err(),
            "accepted {sql}"
        );
        assert_eq!(destination.semantic.records(), before);
    }
}
