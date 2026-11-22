use proptest::prelude::*;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc as Shared;

use crate::semantic::{
    DecisionDefinition, Logic, PathDecisionDefinition, PathDecisionKind, PathOutcomeDefinition,
    PathOutcomeRole, SemanticData,
};

use super::{CoverageData, encode_lines};

type LineRecords = Vec<(String, String, Vec<i32>)>;
type ArcRecords = Vec<(String, String, Vec<(i32, i32)>)>;

fn data(has_arcs: bool, lines: LineRecords, arcs: ArcRecords) -> CoverageData {
    data_with(has_arcs, lines, arcs, Vec::new(), SemanticData::default())
}

fn data_with(
    has_arcs: bool,
    lines: LineRecords,
    arcs: ArcRecords,
    tracers: Vec<(String, String)>,
    semantic: SemanticData,
) -> CoverageData {
    CoverageData::from_trace_records(has_arcs, lines, arcs, tracers, semantic)
}

fn decision(hash: &str, path: &str) -> DecisionDefinition {
    DecisionDefinition::validated(
        format!("decision:{path}"),
        path.to_owned(),
        1,
        1,
        "if".to_owned(),
        hash.to_owned(),
        "value".to_owned(),
        Logic::Condition(0),
        vec![1],
        vec![(1, 0, 1, 5)],
    )
    .expect("valid decision fixture")
}

#[test]
fn shared_snapshots_detach_on_success_and_survive_failed_mutation() {
    let original = data(
        false,
        vec![("first".to_owned(), "source.py".to_owned(), vec![1])],
        Vec::new(),
    );
    let mut changed = original.clone();
    assert!(Shared::ptr_eq(&original.inner, &changed.inner));

    changed.add_lines("second", "source.py", vec![2]);
    assert!(!Shared::ptr_eq(&original.inner, &changed.inner));
    assert_eq!(original.lines_for("source.py"), Some(vec![1]));
    assert_eq!(changed.lines_for("source.py"), Some(vec![1, 2]));

    let mut unchanged = original.clone();
    let incompatible = data(
        true,
        Vec::new(),
        vec![(String::new(), "source.py".to_owned(), vec![(1, 2)])],
    );
    assert_eq!(
        unchanged.merge_from(incompatible, false),
        Err("cannot combine line and branch coverage data")
    );
    assert!(Shared::ptr_eq(&original.inner, &unchanged.inner));
    assert_eq!(unchanged.line_records(), original.line_records());
}

#[test]
fn purging_retains_file_identity_without_ordinary_or_semantic_evidence() {
    let definition = decision("revision", "source.py");
    let named = path_decision("revision", "source.py");
    let decision_key = definition.key().to_owned();
    let path_key = named.key().to_owned();
    let mut semantic = SemanticData::default();
    semantic
        .insert_definition(definition)
        .expect("unique definition");
    semantic
        .insert_path_definition(named)
        .expect("unique path definition");
    semantic
        .insert_observation("test".to_owned(), decision_key, "1".to_owned(), true)
        .expect("valid observation");
    semantic
        .insert_path_observation("test".to_owned(), path_key, 0)
        .expect("valid path observation");
    let mut measured = data_with(
        true,
        Vec::new(),
        vec![("test".to_owned(), "source.py".to_owned(), vec![(1, 2)])],
        Vec::new(),
        semantic,
    );
    measured.purge_files(vec!["source.py".to_owned()]);
    assert_eq!(measured.measured_files(), vec!["source.py"]);
    assert_eq!(measured.lines_for("source.py"), Some(Vec::new()));
    assert_eq!(measured.measured_contexts(), vec!["test"]);
    assert!(measured.semantic_data().definitions().is_empty());
    assert!(measured.semantic_data().observations().is_empty());
    assert!(measured.semantic_data().path_definitions().is_empty());
    assert!(measured.semantic_data().path_observations().is_empty());
}

#[test]
fn mapped_collisions_union_measurements_and_reject_tracer_conflicts() {
    let measured = data(
        false,
        vec![
            ("first".to_owned(), "first.py".to_owned(), vec![1]),
            ("second".to_owned(), "second.py".to_owned(), vec![2]),
        ],
        Vec::new(),
    );
    let paths = [
        ("first.py".to_owned(), "same.py".to_owned()),
        ("second.py".to_owned(), "same.py".to_owned()),
    ]
    .into_iter()
    .collect();
    let merged = measured
        .remapped_files(&paths)
        .expect("compatible mappings");
    assert_eq!(merged.lines_for("same.py"), Some(vec![1, 2]));
    assert_eq!(merged.line_records().len(), 2);
    let conflicting = data_with(
        false,
        vec![
            ("first".to_owned(), "first.py".to_owned(), vec![1]),
            ("second".to_owned(), "second.py".to_owned(), vec![2]),
        ],
        Vec::new(),
        vec![
            ("first.py".to_owned(), "first.plugin".to_owned()),
            ("second.py".to_owned(), "second.plugin".to_owned()),
        ],
        SemanticData::default(),
    );
    assert!(conflicting.remapped_files(&paths).is_err());
}

#[test]
fn mapped_collisions_reject_incompatible_semantic_source_revisions() {
    let mut semantic = SemanticData::default();
    for (hash, path) in [("original", "first.py"), ("changed", "second.py")] {
        semantic
            .insert_definition(decision(hash, path))
            .expect("unique definition");
    }
    let measured = data_with(
        false,
        vec![
            ("first".to_owned(), "first.py".to_owned(), vec![1]),
            ("second".to_owned(), "second.py".to_owned(), vec![2]),
        ],
        Vec::new(),
        Vec::new(),
        semantic,
    );
    let paths = [
        ("first.py".to_owned(), "same.py".to_owned()),
        ("second.py".to_owned(), "same.py".to_owned()),
    ]
    .into_iter()
    .collect();
    assert!(matches!(
        measured.remapped_files(&paths),
        Err("cannot combine semantic data from different source revisions")
    ));
}

fn path_decision(hash: &str, path: &str) -> PathDecisionDefinition {
    let outcomes = [
        ("normal", PathOutcomeRole::Normal),
        ("unhandled", PathOutcomeRole::Unhandled),
    ]
    .into_iter()
    .map(|(label, role)| {
        PathOutcomeDefinition::validated(label.to_owned(), (2, 0, 2, 3), role)
            .expect("valid outcome fixture")
    })
    .collect();
    PathDecisionDefinition::validated(
        format!("path:{path}"),
        path.to_owned(),
        2,
        4,
        PathDecisionKind::Try,
        hash.to_owned(),
        "try".to_owned(),
        3,
        3,
        (2, 0, 2, 3),
        outcomes,
    )
    .expect("valid path decision fixture")
}

#[expect(
    clippy::type_complexity,
    reason = "This tuple is the complete persisted observable state."
)]
fn observable(
    data: &CoverageData,
) -> (
    Vec<(String, String)>,
    Vec<String>,
    Vec<(String, String)>,
    LineRecords,
    ArcRecords,
    Vec<(String, String, i32, i32, String, String, String, String)>,
    Vec<(String, String, String, bool)>,
    Vec<(String, String, i32, i32, String, String, String, i32, i32)>,
    Vec<(String, usize, String, i32, i32, i32, i32)>,
    Vec<(String, String, usize)>,
) {
    (
        data.metadata_records(),
        data.measured_files(),
        data.tracer_records(),
        data.line_records(),
        data.arc_records(),
        data.semantic_records(),
        data.semantic_observation_records(),
        data.semantic_path_records(),
        data.semantic_path_outcome_records(),
        data.semantic_path_observation_records(),
    )
}

#[test]
fn line_and_arc_records_are_deduplicated_and_report_inputs_merge_endpoints() {
    let coverage = data(
        true,
        vec![
            ("ctx".to_owned(), "module.py".to_owned(), vec![1, 1]),
            ("other".to_owned(), "module.py".to_owned(), vec![5]),
        ],
        vec![(
            "ctx".to_owned(),
            "module.py".to_owned(),
            vec![(-1, 1), (1, 2), (1, 2), (2, -1)],
        )],
    );

    assert_eq!(coverage.lines_for("module.py"), Some(vec![1, 5]));
    assert_eq!(
        coverage.arcs_for("module.py"),
        Some(vec![(-1, 1), (1, 2), (2, -1)])
    );
    assert_eq!(coverage.lines_for("missing.py"), None);
    assert_eq!(
        coverage.report_inputs(),
        vec![(
            "module.py".to_owned(),
            vec![1, 2, 5],
            vec![(-1, 1), (1, 2), (2, -1)],
            vec![
                ("ctx".to_owned(), vec![1, 2]),
                ("other".to_owned(), vec![5]),
            ],
            vec![("ctx".to_owned(), vec![(-1, 1), (1, 2), (2, -1)],)],
        )]
    );
}

#[test]
fn select_files_keeps_only_selected_measurements() {
    let coverage = data(
        false,
        vec![
            ("ctx".to_owned(), "keep.py".to_owned(), vec![1]),
            ("ctx".to_owned(), "drop.py".to_owned(), vec![2]),
        ],
        Vec::new(),
    );
    let selected = coverage.select_files(vec!["keep.py".to_owned()]);
    assert_eq!(selected.measured_files(), vec!["keep.py".to_owned()]);
    assert_eq!(selected.lines_for("keep.py"), Some(vec![1]));
    assert_eq!(selected.lines_for("drop.py"), None);
}

#[test]
fn merge_rejects_mode_and_tracer_conflicts_before_partial_merge() {
    let mut lines = data(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![1])],
        Vec::new(),
    );
    let branch = data(
        true,
        Vec::new(),
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![(1, 2)])],
    );
    assert_eq!(
        lines.merge_from(branch, false),
        Err("cannot combine line and branch coverage data")
    );
    assert_eq!(lines.lines_for("module.py"), Some(vec![1]));

    let mut left = data(
        false,
        vec![("existing|run".to_owned(), "existing.py".to_owned(), vec![1])],
        Vec::new(),
    );
    left.set_metadata("run_id", "old");
    left.set_tracer("module.py", "first");
    let mut right = data(
        false,
        vec![("incoming|run".to_owned(), "incoming.py".to_owned(), vec![2])],
        Vec::new(),
    );
    right.set_metadata("new_key", "new_value");
    right.set_tracer("module.py", "second");
    let before = observable(&left);
    assert_eq!(
        left.merge_from(right, false),
        Err("cannot combine conflicting file tracers")
    );
    assert_eq!(observable(&left), before);
}

#[test]
fn semantic_conflicts_leave_metadata_files_measurements_and_evidence_unchanged() {
    let old_definition = decision("old-hash", "existing.py");
    let old_path_definition = path_decision("old-hash", "existing.py");
    let mut left_semantic = SemanticData::default();
    left_semantic
        .insert_definition(old_definition)
        .expect("unique decision definition");
    left_semantic
        .insert_observation(
            "existing|run".to_owned(),
            "decision:existing.py".to_owned(),
            "1".to_owned(),
            true,
        )
        .expect("valid decision observation");
    left_semantic
        .insert_path_definition(old_path_definition)
        .expect("unique path definition");
    left_semantic
        .insert_path_observation("existing|run".to_owned(), "path:existing.py".to_owned(), 0)
        .expect("valid path observation");
    let mut left = data_with(
        false,
        vec![("existing|run".to_owned(), "existing.py".to_owned(), vec![1])],
        Vec::new(),
        Vec::new(),
        left_semantic,
    );
    left.set_metadata("run_id", "old");

    let new_existing = decision("new-hash", "existing.py");
    let new_incoming = decision("incoming-hash", "incoming.py");
    let new_path = path_decision("new-hash", "existing.py");
    let mut right_semantic = SemanticData::default();
    right_semantic
        .insert_definition(new_existing)
        .expect("unique existing definition");
    right_semantic
        .insert_definition(new_incoming)
        .expect("unique incoming definition");
    right_semantic
        .insert_path_definition(new_path)
        .expect("unique path definition");
    let mut right = data_with(
        false,
        vec![("incoming|run".to_owned(), "incoming.py".to_owned(), vec![2])],
        Vec::new(),
        Vec::new(),
        right_semantic,
    );
    right.set_metadata("new_key", "new_value");
    let right_for_replace = right.clone();
    let before = observable(&left);
    assert_eq!(
        left.merge_from(right, false),
        Err("cannot combine semantic data from different source revisions")
    );
    assert_eq!(observable(&left), before);

    left.merge_from(right_for_replace, true)
        .expect("replacement merge accepts a new source revision");
    assert_eq!(
        left.semantic_data().definitions()["decision:existing.py"].source_hash(),
        "new-hash"
    );
    assert_eq!(
        left.semantic_data().path_definitions()["path:existing.py"].source_hash(),
        "new-hash"
    );
}

#[test]
fn replay_replaces_invalidated_contexts_and_changed_files() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary replay directory");
    let changed = directory.path().join("changed.py");
    let untouched = directory.path().join("untouched.py");
    let changed = changed.to_string_lossy().into_owned();
    let untouched = untouched.to_string_lossy().into_owned();
    let baseline = data(
        false,
        vec![
            (
                "tests/test_a.py::test_a|run".to_owned(),
                changed.clone(),
                vec![1, 2],
            ),
            (
                "tests/test_b.py::test_b|run".to_owned(),
                untouched.clone(),
                vec![1, 2],
            ),
        ],
        Vec::new(),
    );
    let fresh = data(
        false,
        vec![(
            "tests/test_a.py::test_a|run".to_owned(),
            changed.clone(),
            vec![1, 3],
        )],
        Vec::new(),
    );

    let baseline_path = directory.path().join("baseline.coverage");
    baseline.write_path(&baseline_path).expect("write baseline");

    pyo3::Python::attach(|py| {
        let fresh = pyo3::Py::new(py, fresh).expect("fresh replay data");
        let replayed = CoverageData::replay_baseline(
            &baseline_path.to_string_lossy(),
            fresh.bind(py).borrow(),
            vec!["tests/test_a.py::test_a".to_owned()],
            vec![changed.clone()],
            BTreeMap::new(),
        )
        .expect("replay merge");
        assert_eq!(
            replayed.line_records(),
            vec![
                (
                    "tests/test_a.py::test_a|run".to_owned(),
                    changed,
                    vec![1, 3],
                ),
                (
                    "tests/test_b.py::test_b|run".to_owned(),
                    untouched,
                    vec![1, 2],
                ),
            ]
        );
    });
}

#[test]
fn replay_baseline_reports_unreadable_and_conflicting_baselines_without_changing_them() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary replay directory");
    let missing = directory.path().join("missing.coverage");
    let corrupt = directory.path().join("corrupt.coverage");
    std::fs::write(&corrupt, b"not a coverage database").expect("corrupt baseline");
    let branch = directory.path().join("branch.coverage");
    data(
        true,
        Vec::new(),
        vec![(
            "tests/test_a.py::test_a|run".to_owned(),
            "/project/a.py".to_owned(),
            vec![(-1, 1), (1, -1)],
        )],
    )
    .write_path(&branch)
    .expect("write branch baseline");
    let branch_bytes = std::fs::read(&branch).expect("read branch baseline");

    pyo3::Python::attach(|py| {
        let fresh = pyo3::Py::new(py, CoverageData::new(false)).expect("fresh replay data");
        let replay = |baseline: &std::path::Path| {
            CoverageData::replay_baseline(
                &baseline.to_string_lossy(),
                fresh.bind(py).borrow(),
                Vec::new(),
                Vec::new(),
                BTreeMap::new(),
            )
            .err()
            .expect("replay fails")
        };
        for unreadable in [&missing, &corrupt] {
            let error = replay(unreadable);
            assert!(error.is_instance_of::<pyo3::exceptions::PyValueError>(py));
            assert!(
                error.to_string().contains(&format!(
                    "failed to read Coverage.py schema-7 data from {}",
                    unreadable.display()
                )),
                "{error}"
            );
        }
        let error = replay(&branch);
        assert!(error.is_instance_of::<pyo3::exceptions::PyValueError>(py));
        assert_eq!(
            error.to_string(),
            "ValueError: cannot replay line and branch coverage together"
        );
    });
    assert!(!missing.exists());
    assert_eq!(
        std::fs::read(&corrupt).expect("read corrupt baseline"),
        b"not a coverage database"
    );
    assert_eq!(
        std::fs::read(&branch).expect("read branch baseline"),
        branch_bytes
    );
}

#[cfg(unix)]
#[test]
fn replay_matches_changed_files_through_symlinks() {
    use std::os::unix::fs::symlink;

    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary replay directory");
    let source = directory.path().join("source");
    let alias = directory.path().join("alias");
    std::fs::create_dir(&source).expect("source directory");
    std::fs::write(source.join("changed.py"), "value = 1\n").expect("changed source");
    symlink(&source, &alias).expect("source alias");
    let baseline_path = alias.join("changed.py").to_string_lossy().into_owned();
    let changed_path = source.join("changed.py").to_string_lossy().into_owned();
    let baseline = data(
        false,
        vec![(
            "tests/test_a.py::test_value|run".to_owned(),
            baseline_path,
            vec![1],
        )],
        Vec::new(),
    );

    let stored_baseline = directory.path().join("baseline.coverage");
    baseline
        .write_path(&stored_baseline)
        .expect("write baseline");

    pyo3::Python::attach(|py| {
        let fresh = pyo3::Py::new(py, CoverageData::new(false)).expect("fresh replay data");
        let replayed = CoverageData::replay_baseline(
            &stored_baseline.to_string_lossy(),
            fresh.bind(py).borrow(),
            Vec::new(),
            vec![changed_path],
            BTreeMap::new(),
        )
        .expect("replay merge");
        assert!(replayed.measured_files().is_empty());
    });
}

#[test]
fn equal_data_merges_idempotently_and_preserves_sorted_records() {
    let original = data(
        true,
        vec![("z".to_owned(), "b.py".to_owned(), vec![3, 1])],
        vec![("z".to_owned(), "b.py".to_owned(), vec![(3, 1)])],
    );
    let mut merged = original.clone();
    merged
        .merge_from(original.clone(), false)
        .expect("equal data merges");
    assert_eq!(merged.line_records(), original.line_records());
    assert_eq!(merged.arc_records(), original.arc_records());
    assert_eq!(merged.measured_files(), vec!["b.py".to_owned()]);
}

#[test]
fn combine_validates_every_input_before_replacing_or_removing_shards() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary combine directory");
    let output = directory.path().join(".coverage");
    let shard = directory.path().join("valid.coverage");
    let corrupt = directory.path().join("corrupt.coverage");
    let original = data(
        false,
        vec![("old".to_owned(), "old.py".to_owned(), vec![1])],
        Vec::new(),
    );
    original.write_path(&output).expect("write existing output");
    data(
        false,
        vec![("new".to_owned(), "new.py".to_owned(), vec![2])],
        Vec::new(),
    )
    .write_path(&shard)
    .expect("write valid shard");
    std::fs::write(&corrupt, b"not a schema-seven database").expect("write corrupt shard");

    let Err(error) = pyo3::Python::attach(|py| {
        CoverageData::combine_transaction(
            py,
            vec![
                shard.to_string_lossy().into_owned(),
                corrupt.to_string_lossy().into_owned(),
            ],
            output.to_str().expect("output path"),
            None,
            false,
            true,
            true,
            Vec::new(),
            false,
        )
    }) else {
        panic!("corrupt input must abort the transaction");
    };
    assert!(
        error
            .to_string()
            .contains("failed to read Coverage.py schema-7 data")
    );
    let restored = CoverageData::read_path(&output).expect("old output remains readable");
    assert_eq!(restored.line_records(), original.line_records());
    assert!(shard.exists(), "valid shard is removed only after commit");
    assert!(corrupt.exists(), "corrupt shard is never removed");

    let (combined, count) = pyo3::Python::attach(|py| {
        CoverageData::combine_transaction(
            py,
            vec![shard.to_string_lossy().into_owned()],
            output.to_str().expect("output path"),
            None,
            false,
            true,
            true,
            Vec::new(),
            false,
        )
    })
    .expect("valid input commits");
    assert_eq!(count, 1);
    assert!(!shard.exists(), "committed shard is cleaned up");
    let combined = combined.expect("combined data");
    assert_eq!(
        combined.line_records(),
        vec![
            ("new".to_owned(), "new.py".to_owned(), vec![2]),
            ("old".to_owned(), "old.py".to_owned(), vec![1]),
        ]
    );
    assert_eq!(
        CoverageData::read_path(&output)
            .expect("committed output")
            .line_records(),
        combined.line_records()
    );
}

#[test]
fn combine_staging_failure_preserves_every_input_shard() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary staging directory");
    let parent = directory.path().join("parent-file");
    std::fs::write(&parent, b"parent remains a file").expect("parent file");
    let output = parent.join(".coverage");
    let shard = directory.path().join("worker.coverage");
    data(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![1])],
        Vec::new(),
    )
    .write_path(&shard)
    .expect("write shard");

    let Err(error) = pyo3::Python::attach(|py| {
        CoverageData::combine_transaction(
            py,
            vec![shard.to_string_lossy().into_owned()],
            output.to_str().expect("output path"),
            None,
            false,
            true,
            false,
            Vec::new(),
            false,
        )
    }) else {
        panic!("a file cannot contain the output parent directory");
    };
    assert!(
        error
            .to_string()
            .contains("failed to write combined coverage data")
    );
    assert_eq!(
        std::fs::read(&parent).expect("parent remains"),
        b"parent remains a file"
    );
    assert!(shard.exists(), "the shard is retained when staging fails");
}

#[test]
fn combine_replacement_failure_preserves_existing_output_and_shard() {
    pyo3::Python::initialize();
    let directory = tempfile::tempdir().expect("temporary replacement directory");
    let output = directory.path().join("output-directory");
    std::fs::create_dir(&output).expect("output directory");
    std::fs::write(output.join("sentinel"), b"unchanged").expect("output sentinel");
    let shard = directory.path().join("worker.coverage");
    data(
        false,
        vec![("ctx".to_owned(), "module.py".to_owned(), vec![1])],
        Vec::new(),
    )
    .write_path(&shard)
    .expect("write shard");

    let Err(error) = pyo3::Python::attach(|py| {
        CoverageData::combine_transaction(
            py,
            vec![shard.to_string_lossy().into_owned()],
            output.to_str().expect("output path"),
            None,
            false,
            true,
            false,
            Vec::new(),
            false,
        )
    }) else {
        panic!("a directory cannot be replaced by a coverage database");
    };
    assert!(
        error
            .to_string()
            .contains("failed to write combined coverage data")
    );
    assert_eq!(
        std::fs::read(output.join("sentinel")).expect("sentinel remains"),
        b"unchanged"
    );
    assert!(
        shard.exists(),
        "the shard is retained when replacement fails"
    );
}

proptest! {
    #[test]
    fn encoded_lines_round_trip_for_arbitrary_signed_inputs(values in prop::collection::vec(-32_i32..512, 0..100)) {
        let expected: BTreeSet<i32> = values.into_iter().filter(|line| *line >= 0).collect();
        let encoded = encode_lines(&expected);
        let mut decoded = BTreeSet::new();
        for (byte_index, byte) in encoded.iter().copied().enumerate() {
            for bit in 0..8 {
                if byte & (1 << bit) != 0 {
                    decoded.insert(
                        i32::try_from(byte_index * 8 + bit).expect("generated bitmap fits i32"),
                    );
                }
            }
        }
        prop_assert_eq!(decoded, expected);
    }

    #[test]
    fn merging_measurements_preserves_the_union_and_algebra(
        branch in any::<bool>(),
        shards in prop::collection::vec(
            prop::collection::vec((0_u8..3, 0_u8..3, 1_i32..20, -1_i32..20), 0..30), 3..=3),
    ) {
        let mut expected: BTreeMap<(String, String), BTreeSet<(i32, i32)>> = BTreeMap::new();
        let inputs: Vec<_> = shards.into_iter().map(|rows| {
            let mut lines = Vec::new();
            let mut arcs = Vec::new();
            for (file, context, start, end) in rows {
                let path = format!("file-{file}.py");
                let context = format!("context-{context}");
                expected.entry((context.clone(), path.clone())).or_default()
                    .insert((start, if branch {end} else {start}));
                if branch { arcs.push((context, path, vec![(start, end)])); }
                else { lines.push((context, path, vec![start])); }
            }
            data(branch, lines, arcs)
        }).collect();
        let merge = |mut left: CoverageData, right: CoverageData| {
            left.merge_from(right, false).expect("compatible measurements");
            left
        };
        let left = merge(merge(inputs[0].clone(), inputs[1].clone()), inputs[2].clone());
        let right = merge(inputs[0].clone(), merge(inputs[1].clone(), inputs[2].clone()));
        let reversed = merge(merge(inputs[2].clone(), inputs[1].clone()), inputs[0].clone());
        prop_assert_eq!(observable(&left), observable(&right));
        prop_assert_eq!(observable(&left), observable(&reversed));
        prop_assert_eq!(observable(&left), observable(&merge(left.clone(), left.clone())));
        if branch {
            let expected: ArcRecords = expected.into_iter().map(|((context, path), arcs)|
                (context, path, arcs.into_iter().collect())).collect();
            prop_assert_eq!(left.arc_records(), expected);
        } else {
            let expected: LineRecords = expected.into_iter().map(|((context, path), lines)|
                (context, path, lines.into_iter().map(|(line, _)| line).collect())).collect();
            prop_assert_eq!(left.line_records(), expected);
        }
    }
}

#[test]
fn empty_update_only_ignores_an_uninitialized_measurement_mode() {
    let mut branch = data(
        true,
        Vec::new(),
        vec![("test".to_owned(), "source.py".to_owned(), vec![(1, 2)])],
    );
    branch
        .merge_from(CoverageData::empty(), false)
        .expect("uninitialized update");
    let mut lines = CoverageData::empty();
    lines.set_metadata("has_arcs", "0");
    assert!(branch.merge_from(lines, false).is_err());
    assert_eq!(branch.arcs_for("source.py"), Some(vec![(1, 2)]));
}
