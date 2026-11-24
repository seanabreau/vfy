use super::{
    DecisionDefinition, SemanticData, SemanticReportOptions, build_semantic_report,
    empty_semantic_row, render_semantic_json, selected_path, shell_word, validate_semantic_sources,
};
use crate::{Logic, mutation::source_hash, path::glob_set};

#[test]
fn malformed_discovered_source_fails_even_with_valid_decisions() {
    let directory = tempfile::tempdir().expect("semantic sources");
    let good = directory.path().join("good.py");
    let bad = directory.path().join("bad.py");
    std::fs::write(&good, "if flag:\n    pass\n").expect("write valid source");
    std::fs::write(&bad, "if flag:\n    pass\nif broken(:\n    pass\n")
        .expect("write malformed source");
    let error = build_semantic_report(
        SemanticData::default(),
        &SemanticReportOptions {
            source_roots: vec![good.display().to_string(), bad.display().to_string()],
            ..SemanticReportOptions::default()
        },
    )
    .expect_err("malformed source must fail the report");
    let message = error.to_string();
    assert!(
        message.contains("failed to parse semantic source"),
        "{message}"
    );
    assert!(message.contains("bad.py"), "{message}");
}

#[test]
fn directory_discovery_reports_read_errors() {
    let directory = tempfile::tempdir().expect("semantic sources");
    let file = directory.path().join("not-a-directory");
    std::fs::write(&file, "").expect("write file");
    let error = super::discover_python_sources(&file, &mut std::collections::BTreeSet::new())
        .expect_err("reading a file as a directory must fail");
    assert!(error.to_string().contains("not-a-directory"));
}

fn definition() -> DecisionDefinition {
    DecisionDefinition {
        key: "decision".to_owned(),
        path: "pkg/module.py".to_owned(),
        line: 2,
        end_line: 2,
        kind: "if".to_owned(),
        source_hash: "hash".to_owned(),
        expression: "left and right".to_owned(),
        logic: Logic::And(vec![Logic::Condition(0), Logic::Condition(1)]),
        condition_lines: vec![2, 2],
        condition_ranges: vec![(2, 3, 2, 7), (2, 12, 2, 17)],
    }
}

#[test]
fn selected_path_applies_include_and_omit_patterns() {
    let includes = glob_set(&["pkg/*.py".to_owned()]).expect("include patterns");
    let omits = glob_set(&["pkg/generated.py".to_owned()]).expect("omit patterns");
    assert!(selected_path(
        "pkg/module.py",
        includes.as_ref(),
        omits.as_ref()
    ));
    assert!(!selected_path(
        "pkg/generated.py",
        includes.as_ref(),
        omits.as_ref()
    ));
    assert!(!selected_path(
        "other/module.py",
        includes.as_ref(),
        omits.as_ref()
    ));
}

#[test]
fn empty_semantic_rows_list_every_boolean_obligation() {
    let row = empty_semantic_row(&definition());
    assert_eq!(row.decisions_covered, 0);
    assert_eq!(row.decisions_total, 2);
    assert_eq!(row.conditions_total, 4);
    assert_eq!(row.mcdc_total, 2);
    assert_eq!(row.missing.len(), 8);
}

#[test]
fn rust_report_filters_contexts_and_preserves_unvisited_obligations() {
    let mut data = SemanticData::default();
    data.definitions.insert("decision".to_owned(), definition());
    data.observations.extend([
        crate::semantic::SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "0-".to_owned(),
            outcome: false,
        },
        crate::semantic::SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "10".to_owned(),
            outcome: false,
        },
        crate::semantic::SemanticObservation {
            context: "a|run".to_owned(),
            decision: "decision".to_owned(),
            vector: "11".to_owned(),
            outcome: true,
        },
    ]);
    let report = build_semantic_report(
        data,
        &SemanticReportOptions {
            contexts: vec!["^missing\\|run$".to_owned()],
            ..SemanticReportOptions::default()
        },
    )
    .expect("build semantic report");
    assert_eq!(report.rows.len(), 1);
    assert_eq!(report.rows[0].decisions_covered, 0);
    assert_eq!(report.rows[0].vectors.len(), 0);
    assert_eq!(report.rows[0].missing.len(), 8);
}

#[test]
fn report_json_keeps_every_context_for_repeated_vectors_and_exact_missing_effects() {
    let mut data = SemanticData::default();
    data.insert_definition(definition())
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
        .expect("valid observation");
    }
    let report = build_semantic_report(data.clone(), &SemanticReportOptions::default())
        .expect("complete semantic report");
    let payload: serde_json::Value =
        serde_json::from_str(&render_semantic_json(&report, false).expect("render semantic JSON"))
            .expect("valid semantic JSON");
    assert_eq!((report.mcdc_covered(), report.mcdc_total()), (2, 2));
    assert_eq!(
        payload["files"][0]["vectors"],
        serde_json::json!([
            {"context": "a|run", "vector": "0-", "outcome": false},
            {"context": "b|run", "vector": "10", "outcome": false},
            {"context": "c|run", "vector": "11", "outcome": true},
            {"context": "d|run", "vector": "0-", "outcome": false},
            {"context": "e|run", "vector": "11", "outcome": true},
        ])
    );

    let true_only = build_semantic_report(
        data,
        &SemanticReportOptions {
            contexts: vec!["^[ce]\\|run$".to_owned()],
            ..SemanticReportOptions::default()
        },
    )
    .expect("filtered semantic report");
    assert_eq!(
        (
            true_only.decisions_covered(),
            true_only.conditions_covered(),
            true_only.mcdc_covered()
        ),
        (1, 2, 0)
    );
    assert_eq!(
        true_only.rows()[0].missing(),
        [
            "decision=false",
            "condition 1=false",
            "condition 2=false",
            "condition 1 lacks independent effect",
            "condition 2 lacks independent effect",
        ]
    );
    assert_eq!(
        true_only.rows()[0].vectors(),
        [
            ("c|run".to_owned(), "11".to_owned(), true),
            ("e|run".to_owned(), "11".to_owned(), true),
        ]
    );
}

#[test]
fn semantic_source_validation_rejects_conflicting_digests_for_one_file() {
    let directory = tempfile::tempdir().expect("semantic source directory");
    let path = directory.path().join("module.py");
    let source = "if value:\n    pass\n";
    std::fs::write(&path, source).expect("source file");
    let digest = source_hash(source);
    let path = path.to_str().expect("path");
    let error = validate_semantic_sources([(path, digest.as_str()), (path, "wrong")])
        .expect_err("a valid first decision must not hide a stale later decision");
    assert!(error.to_string().contains("stale"));
}

#[test]
fn semantic_reports_propagate_discovered_source_decode_errors() {
    let directory = tempfile::tempdir().expect("semantic source directory");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let directory_path = crate::path::absolute_path(directory.path());
    let path = directory_path.join("invalid.py");
    std::fs::write(&path, b"if '\xff':\n    pass\n").expect("invalid UTF-8 source file");
    for root in [&path, &directory_path] {
        let error = build_semantic_report(
            SemanticData::default(),
            &SemanticReportOptions {
                source_roots: vec![root.to_string_lossy().into_owned()],
                ..SemanticReportOptions::default()
            },
        )
        .expect_err("discovered source decoding must not be silently skipped");
        let crate::CoreError::ArtifactIo {
            path: failed_path,
            operation,
            source,
        } = error
        else {
            panic!("expected semantic source I/O error");
        };
        assert_eq!(failed_path, path);
        assert_eq!(operation, "read semantic source");
        assert_eq!(source.kind(), std::io::ErrorKind::InvalidData);
    }
}

#[test]
fn suggested_node_ids_are_quoted_only_when_the_shell_would_interpret_them() {
    assert_eq!(
        shell_word("tests/test_a.py::test_b"),
        "tests/test_a.py::test_b"
    );
    assert_eq!(
        shell_word("tests/test_a.py::test_b[x y]"),
        "'tests/test_a.py::test_b[x y]'"
    );
    assert_eq!(
        shell_word("test_a.py::test_b[it's]"),
        r"'test_a.py::test_b[it'\''s]'"
    );
}
