use super::*;

#[test]
fn changed_collection_turns_subset_into_full_plan() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = temporary.path().join("request.json");
    let output = temporary.path().join("plan.json");
    let mut selection = full_plan("recorded evidence");
    selection.fallback_full = false;
    selection.all_tests = vec!["test_a".to_owned(), "test_b".to_owned()];
    selection.selected = vec!["test_a".to_owned()];
    write_plan_request(PlanRequest {
        path: &request,
        baseline_commit: None,
        target_commit: String::new(),
        source_identity: String::new(),
        selection,
    })
    .expect("persist request");
    let plan = finalize_plan(
        &request,
        vec!["test_a".to_owned(), "test_new".to_owned()],
        &output,
    )
    .expect("finalize changed inventory");
    assert!(plan.selection.fallback_full);
    assert_eq!(plan.selection.selected, vec!["test_a", "test_new"]);
    assert!(
        plan.selection
            .reasons
            .iter()
            .any(|reason| reason.contains("inventory changed"))
    );
}

#[test]
fn duplicate_collection_preserves_existing_plan() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = temporary.path().join("request.json");
    let output = temporary.path().join("plan.json");
    write_fallback_plan(&request, "commit", "missing evidence").expect("persist request");
    std::fs::write(&output, b"old valid artifact").expect("existing output");
    assert!(
        finalize_plan(
            &request,
            vec!["test_a".to_owned(), "test_a".to_owned()],
            &output
        )
        .is_err()
    );
    assert_eq!(
        std::fs::read(&output).expect("preserved output"),
        b"old valid artifact"
    );
}

#[test]
fn independent_repeated_display_ids_keep_every_case_in_a_full_plan() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = temporary.path().join("request.json");
    let output = temporary.path().join("plan.json");
    let collection = temporary.path().join("collection.json");
    let mut selection = full_plan("recorded evidence");
    selection.fallback_full = false;
    selection.all_tests = vec!["shared".to_owned(), "other".to_owned()];
    selection.selected = vec!["other".to_owned()];
    write_plan_request(PlanRequest {
        path: &request,
        baseline_commit: None,
        target_commit: String::new(),
        source_identity: String::new(),
        selection,
    })
    .expect("persist request");
    let collected = vec!["shared".to_owned(), "other".to_owned(), "shared".to_owned()];
    let IndependentCollection::Finalized(plan) =
        finalize_independent_collection(&request, collected.clone(), None, &output, &collection)
            .expect("finalize repeated display identities")
    else {
        panic!("an unscoped collection always finalizes");
    };
    assert!(plan.selection.fallback_full);
    assert_eq!(plan.selection.selected, collected);
    assert_eq!(plan.selection.all_tests, collected);
    assert_eq!(
        read_json::<Vec<String>>(&collection).expect("published full collection"),
        collected,
    );
}

#[test]
fn audit_keeps_omitted_setup_failure_and_marks_incomplete_execution() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = temporary.path().join("request.json");
    let output = temporary.path().join("plan.json");
    let results = temporary.path().join("results.json");
    let mut selection = full_plan("recorded evidence");
    selection.fallback_full = false;
    selection.all_tests = vec!["test_a".to_owned(), "test_b".to_owned()];
    selection.selected = vec!["test_a".to_owned()];
    write_plan_request(PlanRequest {
        path: &request,
        baseline_commit: None,
        target_commit: String::new(),
        source_identity: String::new(),
        selection,
    })
    .expect("persist request");
    finalize_plan(
        &request,
        vec!["test_a".to_owned(), "test_b".to_owned()],
        &output,
    )
    .expect("finalize inventory");
    super::super::write_test_reports(
        &results,
        vec![(
            "test_b".to_owned(),
            "setup".to_owned(),
            "failed".to_owned(),
            1.0,
        )],
        false,
    )
    .expect("record setup failure");
    let audit = audit_plan(&output, &results, 1, &output).expect("audit execution");
    assert!(!audit.complete);
    assert_eq!(audit.omitted_failures, vec!["test_b"]);
}

#[test]
fn portable_identity_normalizes_unicode_json_paths_without_merging_neighbor_roots() {
    let old = vec![r#"["/repo/\u00e9/src", "/repo/\u00e9-other"]"#.to_owned()];
    let relocated = vec![r#"["/checkout/src", "/repo/\u00e9-other"]"#.to_owned()];
    assert_eq!(
        portable_fingerprint(&old, "/repo/é"),
        portable_fingerprint(&relocated, "/checkout")
    );
    let changed_external = vec![r#"["/checkout/src", "/different-other"]"#.to_owned()];
    assert_ne!(
        portable_fingerprint(&old, "/repo/é"),
        portable_fingerprint(&changed_external, "/checkout")
    );
}

#[test]
fn history_json_limit_precedes_parsing_with_a_small_limit() {
    let directory = tempfile::tempdir().expect("CI history directory");
    let path = directory.path().join("history.json");
    std::fs::write(&path, b"{invalid JSON larger than the test limit")
        .expect("oversized invalid history");
    let error = read_history(&path, 8).expect_err("reject size before JSON parsing");
    assert!(error.to_string().contains("artifact exceeds 128 MiB"));
    let error = read_history(&path, 1024).expect_err("parse fitting malformed history");
    assert!(!error.to_string().contains("artifact exceeds"));
}

#[test]
fn baseline_coverage_round_trip_preserves_lines() {
    let mut data = CoverageData::new(false);
    data.add_lines("test_a", "source.py", vec![1, 4]);
    let bytes = coverage_bytes(&data).expect("encode baseline coverage");
    let restored = read_coverage(&bytes).expect("decode baseline coverage");
    assert_eq!(restored.line_records(), data.line_records());
}

fn scoped_request(directory: &Path) -> std::path::PathBuf {
    let request = directory.join("request.json");
    let mut selection = full_plan("recorded evidence");
    selection.fallback_full = false;
    selection.all_tests = vec![
        "tests/test_a.py::test_one".to_owned(),
        "tests/test_a.py::test_two".to_owned(),
        "tests/test_b.py::test_three".to_owned(),
    ];
    selection.selected = vec!["tests/test_a.py::test_one".to_owned()];
    selection.collection_files = Some(BTreeSet::from(["tests/test_a.py".to_owned()]));
    write_plan_request(PlanRequest {
        path: &request,
        baseline_commit: None,
        target_commit: String::new(),
        source_identity: String::new(),
        selection,
    })
    .expect("persist request");
    request
}

#[test]
fn scoped_collection_keeps_baseline_identities_of_skipped_files() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = scoped_request(temporary.path());
    let output = temporary.path().join("plan.json");
    let collection = temporary.path().join("collection.json");
    let scope = collection_scope(&request)
        .expect("read scope")
        .expect("scoped request");
    assert!(scope.skips("tests/test_b.py"));
    assert!(!scope.skips("tests/test_a.py"));
    assert!(!scope.skips("tests/test_unlisted.py"));

    let collected = vec![
        "tests/test_a.py::test_one".to_owned(),
        "tests/test_a.py::test_two".to_owned(),
    ];
    let IndependentCollection::Finalized(plan) =
        finalize_independent_collection(&request, collected, Some(&scope), &output, &collection)
            .expect("finalize scoped collection")
    else {
        panic!("matching identities finalize the plan");
    };
    assert!(
        !plan.selection.fallback_full,
        "{:?}",
        plan.selection.reasons
    );
    assert_eq!(plan.selection.selected, ["tests/test_a.py::test_one"]);
    assert_eq!(
        read_json::<Vec<String>>(&collection).expect("published collection"),
        [
            "tests/test_a.py::test_one",
            "tests/test_a.py::test_two",
            "tests/test_b.py::test_three",
        ],
    );
    assert!(output.is_file());
}

#[test]
fn changed_scoped_collection_publishes_nothing_and_removes_the_scope() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = scoped_request(temporary.path());
    let output = temporary.path().join("plan.json");
    let collection = temporary.path().join("collection.json");
    let scope = collection_scope(&request)
        .expect("read scope")
        .expect("scoped request");
    let collected = vec![
        "tests/test_a.py::test_one".to_owned(),
        "tests/test_a.py::test_renamed".to_owned(),
    ];
    assert!(matches!(
        finalize_independent_collection(&request, collected, Some(&scope), &output, &collection)
            .expect("finalize scoped collection"),
        IndependentCollection::ScopeChanged
    ));
    assert!(!output.exists());
    assert!(!collection.exists());
    assert!(collection_scope(&request).expect("read request").is_none());
    let request: CiPlan = read_json(&request).expect("rewritten request");
    assert!(!request.selection.fallback_full);
    assert_eq!(request.selection.selected, ["tests/test_a.py::test_one"]);
}

#[test]
fn scoped_collection_that_also_collects_a_retained_file_removes_the_scope() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = scoped_request(temporary.path());
    let output = temporary.path().join("plan.json");
    let collection = temporary.path().join("collection.json");
    let scope = collection_scope(&request)
        .expect("read scope")
        .expect("scoped request");
    // A file the scope retains must not also be collected: its tests would repeat.
    let collected = vec![
        "tests/test_a.py::test_one".to_owned(),
        "tests/test_a.py::test_two".to_owned(),
        "tests/test_b.py::test_three".to_owned(),
    ];
    assert!(matches!(
        finalize_independent_collection(&request, collected, Some(&scope), &output, &collection)
            .expect("finalize scoped collection"),
        IndependentCollection::ScopeChanged
    ));
    assert!(!output.exists());
    assert!(!collection.exists());
}

#[test]
fn full_requests_never_scope_collection() {
    let temporary = tempfile::tempdir().expect("temporary evidence directory");
    let request = scoped_request(temporary.path());
    let mut plan: CiPlan = read_json(&request).expect("scoped request");
    plan.selection.fallback_full = true;
    write_json(&request, &plan).expect("rewrite request");
    assert!(collection_scope(&request).expect("read request").is_none());
}

#[test]
fn bytecode_caches_are_not_source_inputs() {
    for path in [
        "__pycache__/conftest.cpython-313.pyc",
        "tests/__pycache__/test_a.cpython-313-vfy-0.0.1-f8310693.pyc",
        "src/pkg/__pycache__/__init__.cpython-313.pyc",
    ] {
        assert!(!source_input(path), "{path}");
    }
    // A bytecode file outside `__pycache__` can be imported without source.
    for path in [
        "pkg/module.pyc",
        "pkg/pycache/data.bin",
        "pkg/__pycache__/fixture.txt",
        "__pycache__.py",
    ] {
        assert!(source_input(path), "{path}");
    }
}
