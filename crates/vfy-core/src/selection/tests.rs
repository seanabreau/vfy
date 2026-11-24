use std::collections::{BTreeMap, BTreeSet};

use proptest::prelude::*;

use super::{
    ArcSelectionRecord, FileSelectionEvidence, LineSelectionRecord, SelectionEvidence,
    SelectionInput, SelectionMode, SelectionRequest, TestAttempt, TestObservation, TestOutcome,
    build_selection_plan, build_selection_plan_from_data, normalize_test_results, parse_diff,
    plan_selection, rank_test_contexts, read_git_replay_state_at, read_green_git_head_at,
    read_test_history, record_test_run, record_test_run_artifact, replay_fingerprint,
    static_dependents, write_test_history, write_test_reports,
};

#[test]
fn history_skips_negative_and_non_numeric_durations() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");
    std::fs::write(
        &path,
        r#"{"tests":{"negative":{"duration_ms":-1},"zero":{"duration_ms":0},"positive":{"duration_ms":1.5},"text":{"duration_ms":"2"},"missing":{}}}"#,
    )
    .expect("write timing history");
    assert_eq!(
        read_test_history(&path).durations,
        [("positive".to_owned(), 1.5), ("zero".to_owned(), 0.0)].into()
    );
}

fn canonical_history_durations(bytes: &[u8]) -> BTreeMap<String, f64> {
    let Ok(serde_json::Value::Object(root)) = serde_json::from_slice(bytes) else {
        return BTreeMap::new();
    };
    let Some(serde_json::Value::Object(tests)) = root.get("tests") else {
        return BTreeMap::new();
    };
    tests
        .iter()
        .filter_map(|(nodeid, value)| {
            let duration = value.get("duration_ms")?.as_f64()?;
            (duration.is_finite() && duration >= 0.0).then_some((nodeid.clone(), duration))
        })
        .collect()
}

#[test]
fn history_duration_projection_matches_value_shapes_duplicates_and_validation() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");
    let cases = [
        "null",
        "17",
        "[]",
        r#"[{"tests":{"a":{"duration_ms":1}}}]"#,
        r#"{"tests":null}"#,
        r#"{"tests":[{"duration_ms":1}]}"#,
        r#"{"tests":{"a":1,"b":null,"c":[],"d":"duration_ms","e":true}}"#,
        r#"{"tests":{"zero":{"duration_ms":-0.0},"finite":{"duration_ms":1.25e2},"negative":{"duration_ms":-1},"text":{"duration_ms":"2"},"null":{"duration_ms":null}}}"#,
        r#"{"tests":{"a":{"duration_ms":-0.0},"b":{"duration_ms":1e2},"c":{"duration_ms":1e999},"d":{"duration_ms":18446744073709551616},"e":{"duration_ms":-1}}}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"tests":null}"#,
        r#"{"tests":null,"tests":{"a":{"duration_ms":2}}}"#,
        r#"{"tests":{"a":{"duration_ms":1},"a":{"duration_ms":2},"b":{"duration_ms":3},"b":null}}"#,
        r#"{"tests":{"a":{"duration_ms":1,"duration_ms":null},"b":{"duration_ms":null,"duration_ms":2},"c":{"duration_ms":3,"duration_ms":{"nested":4}}}}"#,
        r#"{"custom":{"tests":{"ignored":{"duration_ms":42}}},"tests":{"測試":{"duration_ms":2.5,"observations":[{"duration_ms":17,"extra":[false,null,"雪",{"duration_ms":91}]}]}}}"#,
        r#"{"tests":{"a":{"duration_ms":1}}} trailing"#,
        r#"{"tests":{"a":{"duration_ms":1}}} {}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":[1,2,]}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":"\ud800"}"#,
        r#"{"$serde_json::private::Number":"4","tests":{"a":{"duration_ms":1}}}"#,
        r#"{"$serde_json::private::Number":"invalid","tests":{"a":{"duration_ms":1}}}"#,
        r#"{"tests":{"$serde_json::private::Number":"4","a":{"duration_ms":1}}}"#,
        r#"{"tests":{"a":{"$serde_json::private::Number":"4","duration_ms":1}}}"#,
        r#"{"tests":{"a":{"duration_ms":{"$serde_json::private::Number":"4"}}}}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":{"$serde_json::private::Number":"invalid"}}"#,
        r#"{"tests":{"a":{"duration_ms":1,"observations":[{"$serde_json::private::Number":false}]}}}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":{"\u0024serde_json::private::Number":"invalid"}}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":{"ordinary":null,"$serde_json::private::Number":"invalid"}}"#,
        r#"{"tests":{"a":{"duration_ms":1}},"custom":[{"$serde_json::private::Number":"4"}]}"#,
        "{\"tests\":{\"a\":{\"duration_ms\":1}}}\n\t ",
    ];
    for text in cases {
        std::fs::write(&path, text).expect("write history projection case");
        let expected = canonical_history_durations(text.as_bytes());
        let actual = read_test_history(&path).durations;
        let bits = |durations: BTreeMap<String, f64>| {
            durations
                .into_iter()
                .map(|(nodeid, duration)| (nodeid, duration.to_bits()))
                .collect::<BTreeMap<_, _>>()
        };
        assert_eq!(bits(actual), bits(expected), "history: {text}");
    }
    let invalid_utf8 = b"{\"tests\":{\"a\":{\"duration_ms\":1}},\"custom\":\"\xff\"}";
    std::fs::write(&path, invalid_utf8).expect("write invalid UTF-8 history");
    assert_eq!(
        read_test_history(&path).durations,
        canonical_history_durations(invalid_utf8)
    );
    let deep_custom = format!(
        "{{\"tests\":{{\"a\":{{\"duration_ms\":1}}}},\"custom\":{}0{}}}",
        "[".repeat(150),
        "]".repeat(150)
    );
    std::fs::write(&path, &deep_custom).expect("write deeply nested history");
    assert_eq!(
        read_test_history(&path).durations,
        canonical_history_durations(deep_custom.as_bytes())
    );
    std::fs::remove_file(&path).expect("remove history fixture");
    assert!(read_test_history(&path).durations.is_empty());
    assert!(read_test_history(directory.path()).durations.is_empty());
}

#[test]
fn length_delimiting_keeps_replay_inputs_distinct() {
    assert_ne!(
        replay_fingerprint(["ab", "c"]),
        replay_fingerprint(["a", "bc"])
    );
}

#[test]
fn history_write_round_trips_ordered_valid_records() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");

    write_test_history(&path, [("test_b", 2.0), ("test_a", 1.0)]).expect("valid timing history");

    assert_eq!(
        read_test_history(&path)
            .durations
            .into_iter()
            .collect::<Vec<_>>(),
        [("test_a".to_owned(), 1.0), ("test_b".to_owned(), 2.0)]
    );
}

#[test]
fn invalid_history_records_are_filtered_before_mutation() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");
    write_test_history(&path, [("test_a", 1.0)]).expect("initial timing history");
    write_test_history(&path, [("test_b", 2.0), ("test_c", f64::NAN)])
        .expect("invalid durations are ignored by the history contract");
    assert_eq!(
        read_test_history(&path).durations,
        [("test_a".to_owned(), 1.0), ("test_b".to_owned(), 2.0)].into()
    );
}

#[test]
fn parses_added_lines_without_counting_context_or_deletions() {
    let files = parse_diff("+++ b/src/a.py\n@@ -1,2 +2,2 @@\n-old\n+x = 1\n+y = 2\n");
    assert_eq!(files[0].added, BTreeSet::from([2, 3]));
}

#[test]
fn diff_parser_handles_replacements_multiple_hunks_deletions_and_unicode_renames() {
    let files = parse_diff(
        "diff --git a/one.py b/one.py\n+++ b/one.py\n@@ -1 +1,2 @@\n-old\n+first\n context\n@@ -8,0 +9,1 @@\n+second\ndiff --git a/two.py b/two.py\n+++ b/two.py\n@@ -1 +1,0 @@\n-old\ndiff --git a/naive.py b/naïve.py\nrename from naive.py\nrename to naïve.py\n--- a/naive.py\n+++ b/naïve.py\n@@ -0,0 +1 @@\n+value\n",
    );
    let one = files
        .iter()
        .find(|file| file.path.ends_with("/one.py"))
        .expect("one.py");
    assert_eq!(one.added, BTreeSet::from([1, 9]));
    let two = files
        .iter()
        .find(|file| file.path.ends_with("/two.py"))
        .expect("two.py");
    assert!(two.added.is_empty());
    assert!(
        files
            .iter()
            .any(|file| { file.path.ends_with("/naïve.py") && file.added == BTreeSet::from([1]) })
    );
    let malformed = parse_diff("+++ b/bad.py\n@@ no header\n+value\n");
    assert!(malformed[0].added.is_empty());
}

#[test]
fn diff_parser_keeps_header_like_source_lines_and_rejects_incomplete_hunks() {
    let files = parse_diff("+++ b/source.py\n@@ -1,0 +2,2 @@\n+++ b/phantom.py\n+value\n");
    assert_eq!(files.len(), 1);
    assert!(files[0].path.ends_with("/source.py"));
    assert_eq!(files[0].added, BTreeSet::from([2, 3]));
    assert!(!files[0].malformed_hunks);

    for patch in [
        "+++ b/source.py\n@@ -1 +1 @@\n-old\n",
        "+++ b/source.py\n@@ -1 +1 @@\n-old\n@@ -3 +3 @@\n-before\n+after\n",
        "+++ b/source.py\n@@ -1 +1 @@\n-old\ndiff --git a/next.py b/next.py\n+++ b/next.py\n@@ -1 +1 @@\n-before\n+after\n",
        "+++ b/source.py\n@@ -1 +1 @@\né\n",
    ] {
        let files = parse_diff(patch);
        assert!(files.iter().any(|file| file.malformed_hunks), "{patch}");
    }
}

fn git_at(root: &std::path::Path, arguments: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .current_dir(root)
        .args(arguments)
        .output()
        .expect("run fixture Git");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("UTF-8 fixture Git output")
}

#[test]
fn diff_parser_decodes_real_git_quoted_paths_and_resets_sections() {
    let directory = tempfile::tempdir().expect("Git patch directory");
    let root = directory.path().canonicalize().expect("root");
    git_at(&root, &["init", "-q"]);
    let mut names = vec!["aï.py", "middle.py", "zï.py"];
    if cfg!(unix) {
        names.extend(["tab\t.py", "line\n.py", "slash\\.py", "quote\".py"]);
    }
    for name in &names {
        std::fs::write(root.join(name), "value = 1\n").expect("baseline source");
    }
    git_at(&root, &["add", "."]);
    for name in &names {
        std::fs::write(root.join(name), "value = 222\n").expect("changed source");
    }
    let patch = git_at(
        &root,
        &[
            "-c",
            "core.quotePath=true",
            "diff",
            "--no-ext-diff",
            "--unified=0",
        ],
    );
    assert!(patch.contains("+++ \"b/a\\303\\257.py\""), "{patch}");
    let files = super::plan::parse_diff_at(&patch, &root);
    let actual: BTreeMap<_, _> = files
        .into_iter()
        .map(|file| {
            assert!(!file.malformed_hunks, "{}", file.path);
            (file.path, file.added)
        })
        .collect();
    let expected = names
        .into_iter()
        .map(|name| (crate::glob_path(&root.join(name)), BTreeSet::from([1])))
        .collect();
    assert_eq!(actual, expected);

    let unsupported = super::plan::parse_diff_at(
        "+++ b/middle.py\n@@ -1 +1 @@\n-old\n+new\ndiff --git a/bad.py b/bad.py\n+++ \"b/bad\\q.py\"\n@@ -1 +1 @@\n-old\n+other\n",
        &root,
    );
    assert_eq!(unsupported.len(), 2);
    let middle = unsupported
        .iter()
        .find(|file| file.path.ends_with("/middle.py"))
        .expect("ordinary section");
    assert_eq!(middle.hunks.len(), 1);
    assert!(unsupported.iter().any(|file| file.malformed_hunks));
    for mode in [
        SelectionMode::Safe,
        SelectionMode::Balanced,
        SelectionMode::Aggressive,
    ] {
        let plan = plan_selection(SelectionRequest {
            mode,
            all_tests: BTreeSet::from(["first".to_owned(), "second".to_owned()]),
            durations: BTreeMap::new(),
            changed: unsupported.clone(),
            observations: BTreeMap::from([(
                "first".to_owned(),
                TestObservation {
                    files: BTreeMap::from([(middle.path.clone(), BTreeSet::from([1]))]),
                },
            )]),
            file_evidence: BTreeMap::new(),
            static_files: BTreeSet::new(),
            static_reasons: Vec::new(),
            function_spans: BTreeMap::new(),
            location: None,
        });
        assert!(plan.fallback_full);
        assert_eq!(plan.selected, ["first", "second"]);
        assert!(
            plan.reasons
                .iter()
                .any(|reason| reason.contains("patch section"))
        );
    }

    let binary = super::plan::parse_diff_at(
        "diff --git a/image.png b/image.png\nBinary files differ\n",
        &root,
    );
    assert_eq!(binary.len(), 1);
    assert!(binary[0].malformed_hunks);
}

#[test]
fn selection_falls_back_when_safe_evidence_is_incomplete() {
    let changed = parse_diff("+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n");
    let path = changed[0].path.clone();
    let plan = plan_selection(SelectionRequest {
        mode: SelectionMode::Safe,
        all_tests: BTreeSet::from(["tests/test_a.py::test_a".to_owned()]),
        durations: BTreeMap::from([("tests/test_a.py::test_a".to_owned(), 10.0)]),
        changed,
        observations: BTreeMap::from([(
            "tests/test_a.py::test_a".to_owned(),
            TestObservation {
                files: BTreeMap::from([(path, BTreeSet::from([1]))]),
            },
        )]),
        file_evidence: BTreeMap::new(),
        static_files: BTreeSet::new(),
        static_reasons: Vec::new(),
        function_spans: BTreeMap::new(),
        location: None,
    });
    assert!(plan.fallback_full);
    assert_eq!(plan.selected, ["tests/test_a.py::test_a"]);
}

#[test]
fn selection_uses_static_dependents_without_falling_back() {
    let changed = parse_diff("+++ b/src/base.py\n@@ -1 +1 @@\n-old\n+new\n");
    let path = changed[0].path.clone();
    let test = "tests/test_dependent.py::test_value".to_owned();
    let plan = plan_selection(SelectionRequest {
        mode: SelectionMode::Balanced,
        all_tests: BTreeSet::from([test.clone()]),
        durations: BTreeMap::new(),
        changed,
        observations: BTreeMap::from([(
            test.clone(),
            TestObservation {
                files: BTreeMap::from([(
                    "/project/src/dependent.py".to_owned(),
                    BTreeSet::from([1]),
                )]),
            },
        )]),
        file_evidence: BTreeMap::from([(
            path,
            FileSelectionEvidence {
                executable_added: BTreeSet::from([1]),
                affected_functions: 1,
                affected_branches: 0,
            },
        )]),
        static_files: BTreeSet::from(["/project/src/dependent.py".to_owned()]),
        static_reasons: Vec::new(),
        function_spans: BTreeMap::new(),
        location: None,
    });
    assert!(!plan.fallback_full);
    assert_eq!(plan.selected, [test]);
}

#[test]
fn result_normalization_and_recording_preserve_confirmation_duration() {
    let directory = tempfile::tempdir().expect("history directory");
    let history = directory.path().join("history.json");
    let primary = normalize_test_results(
        [(
            "test_one".to_owned(),
            "call".to_owned(),
            "passed".to_owned(),
            5.0,
        )],
        false,
    )
    .expect("primary run");
    record_test_run(&history, &primary, "one", TestAttempt::Primary).expect("record primary");
    let confirmation = normalize_test_results(
        [(
            "test_one".to_owned(),
            "call".to_owned(),
            "failed".to_owned(),
            99.0,
        )],
        false,
    )
    .expect("confirmation run");
    let recorded = record_test_run(&history, &confirmation, "one", TestAttempt::Confirmation)
        .expect("record confirmation");
    assert_eq!(recorded.outcomes["test_one"], TestOutcome::Failed);
    assert!((read_test_history(&history).durations["test_one"] - 5.0).abs() < f64::EPSILON);
}

#[test]
fn affected_graph_read_does_not_replace_green_baseline() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let dependent = root.join("dependent.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&dependent, "import base\n").expect("dependent");
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let before = std::fs::read(&cache).expect("baseline cache");
    std::fs::write(&dependent, "def broken(:\n").expect("broken changed source");
    let changed = BTreeSet::from([crate::glob_path(&dependent)]);
    assert!(
        !static_dependents(&changed, &[root], &cache, "green")
            .reasons
            .is_empty()
    );
    assert_eq!(std::fs::read(&cache).expect("preserved cache"), before);
}

#[test]
fn git_replay_state_reads_each_requested_repository_and_its_live_changes() {
    let first = tempfile::tempdir().expect("first repository");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let first_path = crate::path::absolute_path(first.path());
    let second = tempfile::tempdir().expect("second repository");
    let second_path = crate::path::absolute_path(second.path());
    for (root, contents) in [
        (first_path.as_path(), "first\n"),
        (second_path.as_path(), "second\n"),
    ] {
        git_at(root, &["init", "-q"]);
        git_at(root, &["config", "user.email", "vfy@example.invalid"]);
        git_at(root, &["config", "user.name", "vfy test"]);
        std::fs::write(root.join("source.py"), contents).expect("source");
        git_at(root, &["add", "source.py"]);
        git_at(root, &["commit", "-qm", "baseline"]);
    }
    std::fs::write(first_path.join("source.py"), "first changed\n").expect("first change");
    let first_state = read_git_replay_state_at(first_path.as_path(), &[]).expect("first state");
    let second_state = read_git_replay_state_at(second_path.as_path(), &[]).expect("second state");
    assert_ne!(first_state.head, second_state.head);
    assert_eq!(
        first_state.changed,
        [crate::glob_path(&first_path.join("source.py"))]
    );
    assert!(second_state.changed.is_empty());

    std::fs::write(second_path.join("source.py"), "second changed\n").expect("second change");
    let second_changed =
        read_git_replay_state_at(second_path.as_path(), &[]).expect("live second state");
    assert_eq!(second_changed.head, second_state.head);
    assert_eq!(
        second_changed.changed,
        [crate::glob_path(&second_path.join("source.py"))]
    );
    assert_eq!(
        read_git_replay_state_at(first_path.as_path(), &[]).expect("first state again"),
        first_state
    );
}

#[test]
fn git_replay_state_filters_ignored_changes_without_leaking_status_flags() {
    let directory = tempfile::tempdir().expect("Git directory");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let directory_path = crate::path::absolute_path(directory.path());
    let root = directory_path.as_path();
    for args in [
        &["init", "-q"][..],
        &["config", "user.email", "vfy@example.invalid"][..],
        &["config", "user.name", "vfy test"][..],
    ] {
        assert!(
            std::process::Command::new("git")
                .current_dir(root)
                .args(args)
                .status()
                .expect("run Git")
                .success()
        );
    }
    std::fs::write(root.join("tracked.py"), "value = 1\n").expect("tracked source");
    assert!(
        std::process::Command::new("git")
            .current_dir(root)
            .args(["add", "tracked.py"])
            .status()
            .expect("Git add")
            .success()
    );
    assert!(
        std::process::Command::new("git")
            .current_dir(root)
            .args(["commit", "-qm", "baseline"])
            .status()
            .expect("Git commit")
            .success()
    );
    std::fs::write(root.join("ignored.tmp"), "temporary\n").expect("ignored source");
    let state = read_git_replay_state_at(
        root,
        &[root.join("ignored.tmp").to_string_lossy().into_owned()],
    )
    .expect("Git replay state");
    assert!(state.changed.is_empty());
    assert!(!state.untracked);
    assert!(!state.deleted);
    let ignored = [root.join("ignored.tmp").to_string_lossy().into_owned()];
    assert_eq!(
        read_green_git_head_at(root, &ignored).expect("clean green identity"),
        Some(state.head)
    );

    std::fs::write(root.join("tracked.py"), "value = 2\n").expect("changed source");
    let nested = root.join("nested");
    std::fs::create_dir(&nested).expect("subdirectory");
    let nested_state = read_git_replay_state_at(&nested, &ignored).expect("subdirectory replay");
    assert_eq!(
        nested_state.changed,
        [crate::glob_path(&root.join("tracked.py"))]
    );
    assert_eq!(
        super::plan::parse_diff_at(&nested_state.patch, root)[0].path,
        nested_state.changed[0]
    );
    assert_eq!(
        read_green_git_head_at(&nested, &ignored).expect("dirty nested checkout"),
        None
    );
    let ignored_all = [
        ignored[0].clone(),
        crate::glob_path(&root.join("tracked.py")),
    ];
    assert!(
        read_git_replay_state_at(&nested, &ignored_all)
            .expect("ignored root changes")
            .changed
            .is_empty()
    );
    assert_eq!(
        read_green_git_head_at(&nested, &ignored_all).expect("ignored nested checkout"),
        Some(nested_state.head)
    );

    assert_eq!(
        read_green_git_head_at(root, &ignored).expect("changed green identity"),
        None
    );
    std::fs::write(root.join("tracked.py"), "value = 1\n").expect("restored source");
    git_at(root, &["mv", "tracked.py", "renamed.py"]);
    let renamed = read_git_replay_state_at(&nested, &ignored).expect("nested rename");
    assert_eq!(
        renamed.changed,
        [
            crate::glob_path(&root.join("renamed.py")),
            crate::glob_path(&root.join("tracked.py"))
        ]
    );
    git_at(root, &["reset", "--hard", "-q"]);
    std::fs::write(root.join("new.py"), "value = 3\n").expect("untracked source");
    assert_eq!(
        read_green_git_head_at(root, &ignored).expect("untracked green identity"),
        None
    );
}

#[test]
fn import_graph_finds_transitive_dependents_and_terminates_cycles() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let first = root.join("first.py");
    let second = root.join("second.py");
    let leaf = root.join("leaf.py");
    std::fs::write(&first, "import second\n").expect("first");
    std::fs::write(&second, "import first\n").expect("second");
    std::fs::write(&leaf, "import second\n").expect("leaf");
    let cache = root.join("graph.json");
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&first)]),
        &[root],
        &cache,
        "green",
    );
    assert!(
        result
            .dependents
            .iter()
            .any(|path| path.ends_with("/second.py"))
    );
    assert!(
        result
            .dependents
            .iter()
            .any(|path| path.ends_with("/leaf.py"))
    );
}

#[test]
fn import_graph_resolves_relative_imports_at_package_boundaries() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let package = root.join("pkg");
    std::fs::create_dir(&package).expect("package");
    let base = package.join("base.py");
    let sibling = package.join("sibling.py");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&sibling, "from . import base\n").expect("sibling");
    let initializer = package.join("__init__.py");
    std::fs::write(
        &initializer,
        "from .base import VALUE\nfrom . import base\n",
    )
    .expect("package init");
    let nested = package.join("nested");
    std::fs::create_dir(&nested).expect("nested package");
    let nested_initializer = nested.join("__init__.py");
    std::fs::write(&nested_initializer, "from .. import base\n").expect("nested package init");
    let cache = root.join("graph.json");
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    assert!(result.reasons.is_empty(), "{:?}", result.reasons);
    assert_eq!(
        result.dependents,
        BTreeSet::from([
            crate::glob_path(&sibling),
            crate::glob_path(&initializer),
            crate::glob_path(&nested_initializer),
        ])
    );
    let mut old_cache: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&cache).expect("graph bytes")).expect("graph JSON");
    old_cache["version"] = serde_json::json!(6);
    old_cache["reverse"] = serde_json::json!({});
    std::fs::write(
        &cache,
        serde_json::to_vec(&old_cache).expect("old graph JSON"),
    )
    .expect("old cache");
    let upgraded = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    assert_eq!(upgraded.dependents, result.dependents);
}

#[test]
fn malformed_graph_cache_is_rescanned_without_replacing_affected_baseline() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let dependent = root.join("dependent.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&dependent, "import base\n").expect("dependent");
    let malformed = b"not json";
    std::fs::write(&cache, malformed).expect("malformed cache");
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        &[root],
        &cache,
        "green",
    );
    assert!(result.dependents.contains(&crate::glob_path(&dependent)));
    assert_eq!(std::fs::read(&cache).expect("preserved cache"), malformed);
}

#[test]
fn git_declared_malformed_source_is_reparsed_on_every_affected_run() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let source = root.join("broken.py");
    let cache = root.join("graph.json");
    std::fs::write(&source, "def broken(:\n").expect("malformed source");
    let changed = BTreeSet::from([crate::glob_path(&source)]);
    for _ in 0..2 {
        let result = static_dependents(&changed, std::slice::from_ref(&root), &cache, "green");
        assert!(result.reasons.iter().any(|reason| reason.contains("parse")));
    }
}

#[test]
fn graph_cache_rejects_another_green_source_identity() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let dependent = root.join("dependent.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&dependent, "import base\n").expect("dependent");
    let _ = static_dependents(&BTreeSet::new(), std::slice::from_ref(&root), &cache, "old");
    std::fs::write(&dependent, "VALUE = 2\n").expect("new baseline source");
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        &[root],
        &cache,
        "new",
    );
    assert!(result.dependents.is_empty());
}

#[test]
fn test_results_use_failure_then_pass_then_skip_precedence() {
    let run = normalize_test_results(
        [
            (
                "failed".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
                2.0,
            ),
            (
                "failed".to_owned(),
                "teardown".to_owned(),
                "failed".to_owned(),
                3.0,
            ),
            (
                "passed".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
                4.0,
            ),
            (
                "skipped".to_owned(),
                "setup".to_owned(),
                "skipped".to_owned(),
                5.0,
            ),
        ],
        false,
    )
    .expect("normalized results");
    assert_eq!(run.tests.len(), 3);
    assert_eq!(run.tests[0].outcome, TestOutcome::Failed);
    assert_eq!(run.tests[1].outcome, TestOutcome::Passed);
    assert_eq!(run.tests[2].outcome, TestOutcome::Skipped);
}

#[test]
fn recorded_observations_are_bounded_and_preserve_custom_fields() {
    let directory = tempfile::tempdir().expect("history directory");
    let history = directory.path().join("history.json");
    std::fs::write(
        &history,
        r#"{"custom":"keep","tests":{"test_one":{"custom":"keep"}}}"#,
    )
    .expect("history fixture");
    for index in 0..21 {
        let run = normalize_test_results(
            [(
                "test_one".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
                f64::from(index),
            )],
            false,
        )
        .expect("run");
        record_test_run(
            &history,
            &run,
            &format!("run-{index}"),
            TestAttempt::Primary,
        )
        .expect("record run");
    }
    let value: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&history).expect("history")).expect("history JSON");
    assert_eq!(value["custom"], "keep");
    assert_eq!(value["tests"]["test_one"]["custom"], "keep");
    let observations = value["tests"]["test_one"]["observations"]
        .as_array()
        .expect("observations");
    assert_eq!(observations.len(), 20);
    assert_eq!(observations[0]["run_id"], "run-1");
    assert_eq!(observations[19]["run_id"], "run-20");
}

#[test]
fn malformed_history_entry_does_not_replace_existing_bytes() {
    let directory = tempfile::tempdir().expect("history directory");
    let history = directory.path().join("history.json");
    let run = normalize_test_results(
        [
            ("a".to_owned(), "call".to_owned(), "passed".to_owned(), 1.0),
            ("z".to_owned(), "call".to_owned(), "passed".to_owned(), 1.0),
        ],
        false,
    )
    .expect("run");
    for (before, message) in [
        (r#"{"tests":[]}"#, "invalid tests object"),
        (
            r#"{"tests":{"a":{"duration_ms":1},"z":[]}}"#,
            "must be an object",
        ),
        (
            r#"{"tests":{"a":{"duration_ms":1},"z":{"observations":{}}}}"#,
            "must be an array",
        ),
    ] {
        std::fs::write(&history, before).expect("history fixture");
        let error = record_test_run(&history, &run, "run", TestAttempt::Primary)
            .expect_err("invalid history");
        assert!(error.to_string().contains(message), "{error}");
        assert_eq!(
            std::fs::read_to_string(&history).expect("preserved history"),
            before
        );
    }
}

#[test]
fn duplicate_result_entries_keep_order_and_duration_beyond_observation_limit() {
    let directory = tempfile::tempdir().expect("history directory");
    let initial = serde_json::json!({
        "version": 1,
        "z_custom": {"z": 2, "a": 1},
        "tests": {
            "unchanged": ["custom", {"z": 2, "a": 1}],
            "test_é": {
                "duration_ms": 99.0,
                "custom": {"z": 2, "a": 1},
                "observations": [{"custom": true}]
            }
        }
    });
    let run = super::TestRun {
        infrastructure_failed: false,
        tests: (0..23)
            .map(|index| super::TestReport {
                nodeid: "test_é".to_owned(),
                outcome: if index == 22 {
                    TestOutcome::Failed
                } else {
                    TestOutcome::Passed
                },
                failed_phase: (index == 22).then_some(super::FailedPhase::Call),
                duration_ms: f64::from(index),
                // This duration survives even though its observation is dropped.
                call_duration_ms: (index == 1).then_some(7.0),
            })
            .collect(),
    };
    for attempt in [TestAttempt::Primary, TestAttempt::Confirmation] {
        let history = directory.path().join(format!("{attempt:?}.json"));
        std::fs::write(&history, initial.to_string()).expect("initial history");
        let recorded = record_test_run(&history, &run, "run", attempt).expect("record duplicates");
        assert_eq!(recorded.failed, ["test_é"]);
        assert_eq!(recorded.outcomes["test_é"], TestOutcome::Failed);
        let attempt_name = match attempt {
            TestAttempt::Primary => "primary",
            TestAttempt::Confirmation => "confirmation",
        };
        let observations = (3..23)
            .map(|index| {
                let mut observation = serde_json::json!({
                    "attempt": attempt_name,
                    "duration_ms": f64::from(index),
                    "outcome": if index == 22 { "failed" } else { "passed" },
                    "run_id": "run"
                });
                if index == 22 {
                    observation["failed_phase"] = serde_json::json!("call");
                }
                observation
            })
            .collect::<Vec<_>>();
        let mut expected = serde_json::json!({
            "z_custom": {"z": 2, "a": 1},
            "tests": {
                "unchanged": ["custom", {"z": 2, "a": 1}],
                "test_é": {
                    "duration_ms": if attempt == TestAttempt::Primary { 7.0 } else { 99.0 },
                    "custom": {"z": 2, "a": 1},
                    "observations": observations
                }
            }
        });
        expected.sort_all_objects();
        assert_eq!(
            std::fs::read(&history).expect("recorded history"),
            serde_json::to_vec_pretty(&expected).expect("expected sorted JSON")
        );
    }
}

#[test]
fn history_serialization_is_independent_of_record_order() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let first = directory.path().join("first.json");
    let second = directory.path().join("second.json");
    write_test_history(&first, [("test_b", 2.0), ("test_a", 1.0)]).expect("first timing history");
    write_test_history(&second, [("test_a", 1.0), ("test_b", 2.0)]).expect("second timing history");
    assert_eq!(
        std::fs::read(first).expect("first history bytes"),
        std::fs::read(second).expect("second history bytes")
    );
}

#[test]
fn invalid_history_shape_preserves_existing_artifact() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");
    let original = br#"{"version":1,"tests":[]}"#;
    std::fs::write(&path, original).expect("invalid history fixture");
    assert!(matches!(
        write_test_history(&path, [("test_a", 1.0)]),
        Err(crate::CoreError::InvalidInput(_))
    ));
    assert_eq!(std::fs::read(path).expect("preserved history"), original);
}

#[test]
fn history_preserves_empty_node_ids_accepted_by_the_adapter() {
    let directory = tempfile::tempdir().expect("temporary history directory");
    let path = directory.path().join("history.json");
    write_test_history(&path, [("", 1.0)]).expect("timing history");
    assert_eq!(
        read_test_history(&path).durations,
        [(String::new(), 1.0)].into()
    );
}

#[test]
fn context_ranking_deduplicates_phases_and_orders_by_duration() {
    let ranked = rank_test_contexts(
        vec![vec![
            "slow|run".to_owned(),
            "fast|setup".to_owned(),
            "slow|teardown".to_owned(),
            "manual".to_owned(),
        ]],
        &BTreeMap::from([("slow".to_owned(), 9.0), ("fast".to_owned(), 1.0)]),
    );
    assert_eq!(
        ranked,
        vec![vec![
            ("fast".to_owned(), Some(1.0)),
            ("slow".to_owned(), Some(9.0))
        ]]
    );
}

proptest! {
    #[test]
    fn test_ranking_is_a_deterministic_permutation_of_equivalent_inputs(
        values in prop::collection::vec(0_u8..8, 0..24),
    ) {
        let first: Vec<String> = values
            .iter()
            .map(|value| format!("test-{value}|run"))
            .collect();
        let mut second = first.clone();
        second.reverse();
        let durations: BTreeMap<String, f64> = (0_u8..8)
            .map(|value| (format!("test-{value}"), f64::from(value)))
            .collect();
        let expected = values
            .iter()
            .copied()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .map(|value| (format!("test-{value}"), Some(f64::from(value))))
            .collect::<Vec<_>>();
        prop_assert_eq!(
            rank_test_contexts(vec![first], &durations),
            vec![expected.clone()]
        );
        prop_assert_eq!(rank_test_contexts(vec![second], &durations), vec![expected]);
    }
}

#[test]
fn batched_selection_evidence_maps_arc_endpoints_and_source_regions() {
    let directory = tempfile::tempdir().expect("selection directory");
    let source = directory.path().join("module.py");
    std::fs::write(&source, "def value():\n    return 2\n").expect("source");
    let source_path = crate::glob_path(&crate::absolute_path(&source));
    let patch = format!("+++ b/{source_path}\n@@ -2 +2 @@\n-    return 1\n+    return 2\n");
    let test = "tests/test_module.py::test_value".to_owned();
    let plan = build_selection_plan(SelectionEvidence {
        mode: SelectionMode::Aggressive,
        history_path: directory.path().join("history.json"),
        source_roots: vec![directory.path().to_path_buf()],
        patch,
        location: None,
        source_identity: "green".to_owned(),
        branch_mode: true,
        semantic_evidence: false,
        python_semantics: crate::analysis::PythonSemantics {
            class_bare_annotations_execute: true,
            debug_enabled: true,
        },
        durations: BTreeMap::new(),
        lines: Vec::new(),
        arcs: vec![ArcSelectionRecord {
            context: format!("{test}|run"),
            path: source_path,
            arcs: vec![(1, 2)],
        }],
    })
    .expect("selection plan");
    assert_eq!(plan.selected, [test]);
    assert_eq!(plan.affected_functions, 1);
    assert!(!plan.fallback_full);
}

#[test]
fn compact_selection_preserves_inventory_and_global_import_attribution() {
    let directory = tempfile::tempdir().expect("selection directory");
    let source = directory.path().join("module.py");
    let other = directory.path().join("other.py");
    std::fs::write(&source, "def value():\n    return 2\n").expect("source");
    std::fs::write(&other, "VALUE = 1\n").expect("other source");
    let source_path = crate::glob_path(&crate::absolute_path(&source));
    let other_path = crate::glob_path(&crate::absolute_path(&other));
    let patch = format!("+++ b/{source_path}\n@@ -2 +2 @@\n-    return 1\n+    return 2\n");
    let alpha = "tests/test_module.py::test_alpha";
    let beta = "tests/test_module.py::test_beta";
    let gamma = "tests/test_other.py::test_gamma";
    let data = crate::data::CoverageData::from_trace_records(
        false,
        vec![
            (format!("{alpha}|run"), source_path.clone(), vec![2]),
            (format!("{alpha}|run"), other_path.clone(), vec![1]),
            (format!("{beta}|run"), source_path.clone(), vec![1]),
            (format!("{gamma}|teardown"), other_path, vec![2]),
        ],
        Vec::new(),
        Vec::new(),
        crate::semantic::SemanticData::default(),
    );
    for mode in [
        SelectionMode::Safe,
        SelectionMode::Balanced,
        SelectionMode::Aggressive,
    ] {
        for location in [None, Some(format!("{source_path}:2"))] {
            let input = || SelectionInput {
                mode,
                history_path: directory.path().join("history.json"),
                source_roots: vec![directory.path().to_path_buf()],
                patch: patch.clone(),
                location: location.clone(),
                source_identity: "green".to_owned(),
                branch_mode: false,
                semantic_evidence: false,
                python_semantics: crate::analysis::PythonSemantics {
                    class_bare_annotations_execute: true,
                    debug_enabled: true,
                },
                durations: BTreeMap::new(),
            };
            let compact = build_selection_plan_from_data(&data, input()).expect("compact plan");
            let old = input();
            let expanded = build_selection_plan(SelectionEvidence {
                mode: old.mode,
                history_path: old.history_path,
                source_roots: old.source_roots,
                patch: old.patch,
                location: old.location,
                source_identity: old.source_identity,
                branch_mode: old.branch_mode,
                semantic_evidence: old.semantic_evidence,
                python_semantics: old.python_semantics,
                durations: old.durations,
                lines: data
                    .line_measurements()
                    .map(|(context, path, lines)| LineSelectionRecord {
                        context: context.to_owned(),
                        path: path.to_owned(),
                        lines,
                    })
                    .collect(),
                arcs: Vec::new(),
            })
            .expect("expanded plan");
            assert_eq!(compact, expanded, "{mode:?} {location:?}");
            if location.is_none() && mode == SelectionMode::Safe {
                assert_eq!(compact.selected, [alpha, beta]);
                assert_eq!(compact.all_tests, [alpha, beta, gamma]);
            }
        }
    }
}

#[test]
fn compact_selection_reads_arc_endpoints_for_relevant_paths() {
    let directory = tempfile::tempdir().expect("selection directory");
    let source = directory.path().join("module.py");
    std::fs::write(&source, "def value():\n    return 2\n").expect("source");
    let path = crate::glob_path(&crate::absolute_path(&source));
    let test = "tests/test_module.py::test_value";
    let mut data = crate::data::CoverageData::from_trace_records(
        true,
        Vec::new(),
        vec![(format!("{test}|run"), path.clone(), vec![(1, 2)])],
        Vec::new(),
        crate::semantic::SemanticData::default(),
    );
    let input = || SelectionInput {
        mode: SelectionMode::Aggressive,
        history_path: directory.path().join("history.json"),
        source_roots: vec![directory.path().to_path_buf()],
        patch: format!("+++ b/{path}\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"),
        location: None,
        source_identity: "green".to_owned(),
        branch_mode: true,
        semantic_evidence: false,
        python_semantics: crate::analysis::PythonSemantics {
            class_bare_annotations_execute: true,
            debug_enabled: true,
        },
        durations: [("deleted.py::test_old".to_owned(), 1.0)].into(),
    };
    let skipped = "tests/test_module.py::test_skipped";
    data.set_metadata(
        "vfy_green_test_collection",
        &serde_json::to_string(&[test, skipped]).expect("green inventory"),
    );
    let plan = build_selection_plan_from_data(&data, input()).expect("compact selection plan");
    assert_eq!(plan.selected, [test]);
    assert_eq!(
        plan.all_tests.iter().cloned().collect::<BTreeSet<_>>(),
        [test.to_owned(), skipped.to_owned()].into()
    );
    assert_eq!(plan.full_seconds, None);
    assert!(plan.durations.is_empty());
    assert_eq!(plan.affected_functions, 1);
    data.set_metadata(
        "vfy_green_test_collection",
        &serde_json::to_string(&[test, test, skipped]).expect("repeated green inventory"),
    );
    assert_eq!(
        build_selection_plan_from_data(&data, input()).expect("repeated inventory"),
        plan
    );
    for invalid in ["null", "[\"\"]"] {
        data.set_metadata("vfy_green_test_collection", invalid);
        assert!(
            build_selection_plan_from_data(&data, input()).is_err(),
            "{invalid}"
        );
    }
}

#[test]
fn compact_selection_reselects_contextless_tests_after_body_and_module_edits() {
    let directory = tempfile::tempdir().expect("selection directory");
    let source = directory.path().join("module.py");
    std::fs::write(&source, "LIMIT = 2\ndef value():\n    return LIMIT\n").expect("source");
    let path = crate::glob_path(&crate::absolute_path(&source));
    let (reached, unrelated, skipped) = (
        "test_module.py::test_reached",
        "test_other.py::test_unrelated",
        "test_skips.py::test_skipped",
    );
    let mut data = crate::data::CoverageData::from_trace_records(
        false,
        vec![
            (String::new(), path.clone(), vec![1, 2]),
            (format!("{reached}|run"), path.clone(), vec![3]),
            (format!("{unrelated}|run"), "other.py".to_owned(), vec![1]),
        ],
        Vec::new(),
        Vec::new(),
        crate::semantic::SemanticData::default(),
    );
    data.set_metadata(
        "vfy_green_test_collection",
        &serde_json::to_string(&[reached, unrelated, skipped]).expect("green inventory"),
    );
    let input = |patch: &str| SelectionInput {
        mode: SelectionMode::Safe,
        history_path: directory.path().join("history.json"),
        source_roots: vec![directory.path().to_path_buf()],
        patch: format!("+++ b/{path}\n{patch}"),
        location: None,
        source_identity: "green".to_owned(),
        branch_mode: false,
        semantic_evidence: false,
        python_semantics: crate::analysis::PythonSemantics {
            class_bare_annotations_execute: true,
            debug_enabled: true,
        },
        durations: BTreeMap::new(),
    };

    // Missing contexts cannot rule out an unmeasured consumer of the edited body.
    let body = build_selection_plan_from_data(
        &data,
        input("@@ -3 +3 @@\n-    return 1\n+    return LIMIT\n"),
    )
    .expect("body edit");
    assert!(!body.fallback_full, "{:?}", body.reasons);
    assert_eq!(body.selected, [reached, skipped]);
    assert_eq!(
        body.collection_files,
        Some(BTreeSet::from([
            "test_module.py".to_owned(),
            "test_skips.py".to_owned(),
        ]))
    );

    // Module state can feed a skip condition, so the skipped test runs again and
    // every file is collected.
    let module =
        build_selection_plan_from_data(&data, input("@@ -1 +1 @@\n-LIMIT = 1\n+LIMIT = 2\n"))
            .expect("module edit");
    assert!(!module.fallback_full, "{:?}", module.reasons);
    assert_eq!(module.selected, [reached, skipped]);
    assert_eq!(module.collection_files, None);

    let unchanged = SelectionInput {
        patch: String::new(),
        ..input("")
    };
    let plan = build_selection_plan_from_data(&data, unchanged).expect("unchanged");
    assert!(plan.selected.is_empty(), "{:?}", plan.selected);
    assert_eq!(plan.collection_files, None);
}

#[test]
fn compact_selection_collects_every_file_unless_all_edits_are_body_edits() {
    let directory = tempfile::tempdir().expect("selection directory");
    let module = directory.path().join("module.py");
    let sibling = directory.path().join("sibling.py");
    std::fs::write(&module, "def value():\n    return 2\n").expect("module");
    std::fs::write(&sibling, "NAME = 2\n").expect("sibling");
    let module_path = crate::glob_path(&crate::absolute_path(&module));
    let sibling_path = crate::glob_path(&crate::absolute_path(&sibling));
    let (reached, unrelated) = ("test_module.py::test_reached", "test_other.py::test_other");
    let mut data = crate::data::CoverageData::from_trace_records(
        false,
        vec![
            (String::new(), module_path.clone(), vec![1]),
            (String::new(), sibling_path.clone(), vec![1]),
            (format!("{reached}|run"), module_path.clone(), vec![2]),
            (format!("{unrelated}|run"), "other.py".to_owned(), vec![1]),
        ],
        Vec::new(),
        Vec::new(),
        crate::semantic::SemanticData::default(),
    );
    data.set_metadata(
        "vfy_green_test_collection",
        &serde_json::to_string(&[reached, unrelated]).expect("green inventory"),
    );
    let plan_for = |patch: String| {
        build_selection_plan_from_data(
            &data,
            SelectionInput {
                mode: SelectionMode::Safe,
                history_path: directory.path().join("history.json"),
                source_roots: vec![directory.path().to_path_buf()],
                patch,
                location: None,
                source_identity: "green".to_owned(),
                branch_mode: false,
                semantic_evidence: false,
                python_semantics: crate::analysis::PythonSemantics {
                    class_bare_annotations_execute: true,
                    debug_enabled: true,
                },
                durations: BTreeMap::new(),
            },
        )
        .expect("plan")
    };
    let body = format!("+++ b/{module_path}\n@@ -2 +2 @@\n-    return 1\n+    return 2\n");
    let module_edit = format!("+++ b/{sibling_path}\n@@ -1 +1 @@\n-NAME = 1\n+NAME = 2\n");

    let alone = plan_for(body.clone());
    assert!(!alone.fallback_full, "{:?}", alone.reasons);
    assert_eq!(
        alone.collection_files,
        Some(BTreeSet::from(["test_module.py".to_owned()]))
    );

    // One edit outside a function body keeps every file in the collection.
    let mixed = plan_for(format!("{body}{module_edit}"));
    assert!(!mixed.fallback_full, "{:?}", mixed.reasons);
    assert_eq!(mixed.selected, [reached]);
    assert_eq!(mixed.collection_files, None);
}

#[test]
fn compact_selection_unmapped_contexts_keep_the_full_collected_inventory() {
    let directory = tempfile::tempdir().expect("selection directory");
    let test = "test_module.py::test_value";
    for (context, inventory) in [
        (format!("label|{test}|run"), vec![test]),
        (format!("{test}|run"), vec!["repeated", "repeated"]),
        (format!("{test}|run"), Vec::new()),
    ] {
        let mut data = crate::data::CoverageData::from_trace_records(
            false,
            vec![(context, "module.py".to_owned(), vec![1])],
            Vec::new(),
            Vec::new(),
            crate::semantic::SemanticData::default(),
        );
        data.set_metadata(
            "vfy_green_test_collection",
            &serde_json::to_string(&inventory).expect("green inventory"),
        );
        let plan = build_selection_plan_from_data(
            &data,
            SelectionInput {
                mode: SelectionMode::Safe,
                history_path: directory.path().join("history.json"),
                source_roots: Vec::new(),
                patch: String::new(),
                location: None,
                source_identity: "green".to_owned(),
                branch_mode: false,
                semantic_evidence: false,
                python_semantics: crate::analysis::PythonSemantics {
                    class_bare_annotations_execute: true,
                    debug_enabled: true,
                },
                durations: [(test.to_owned(), 4.0)].into(),
            },
        )
        .expect("valid coverage contexts");
        assert!(plan.fallback_full);
        assert_eq!(plan.selected, plan.all_tests);
        assert!(plan.scoped_files.is_empty());
        assert_eq!(plan.selected_seconds, plan.full_seconds);
        assert!(
            plan.reasons
                .iter()
                .any(|reason| reason.contains("coverage contexts"))
        );
        let request = directory.path().join("request.json");
        super::ci::write_plan_request(super::ci::PlanRequest {
            path: &request,
            baseline_commit: None,
            target_commit: String::new(),
            source_identity: String::new(),
            selection: plan,
        })
        .expect("plan request");
        let collected = vec![test.to_owned(), "test_module.py::test_skipped".to_owned()];
        let super::ci::IndependentCollection::Finalized(finalized) =
            super::ci::finalize_independent_collection(
                &request,
                collected.clone(),
                None,
                &directory.path().join("plan.json"),
                &directory.path().join("collection.json"),
            )
            .expect("full collection fallback")
        else {
            panic!("an unscoped collection always finalizes");
        };
        let expected = vec!["test_module.py::test_skipped".to_owned(), test.to_owned()];
        assert_eq!(finalized.collected, collected);
        assert_eq!(finalized.selection.selected, expected);
        assert_eq!(finalized.selection.all_tests, expected);
        assert!(finalized.selection.fallback_full);
        assert!(finalized.selection.scoped_files.is_empty());
        assert_eq!(finalized.selection.selected_seconds, None);
        assert_eq!(finalized.selection.full_seconds, None);
    }
}

/// Build a single-hunk `--unified=0` patch between two sources.
fn one_hunk_patch(path: &str, old: &str, new: &str) -> String {
    let old: Vec<&str> = old.lines().collect();
    let new: Vec<&str> = new.lines().collect();
    let prefix = old
        .iter()
        .zip(&new)
        .take_while(|(left, right)| left == right)
        .count();
    let suffix = old[prefix..]
        .iter()
        .rev()
        .zip(new[prefix..].iter().rev())
        .take_while(|(left, right)| left == right)
        .count();
    let removed = &old[prefix..old.len() - suffix];
    let added = &new[prefix..new.len() - suffix];
    let range = |lines: &[&str]| {
        if lines.is_empty() {
            format!("{prefix},0")
        } else {
            format!("{},{}", prefix + 1, lines.len())
        }
    };
    let mut diff = format!(
        "+++ b/{path}\n@@ -{} +{} @@\n",
        range(removed),
        range(added)
    );
    for line in removed {
        diff.extend(["-", line, "\n"]);
    }
    for line in added {
        diff.extend(["+", line, "\n"]);
    }
    diff
}

fn function_spans(old: &str, new: &str) -> Option<Vec<(i32, i32)>> {
    let changed = parse_diff(&one_hunk_patch("module.py", old, new));
    super::scope::changed_function_spans(new, &changed[0].hunks)
}

const SCOPED_MODULE: &str = r#"import functools

LIMIT = 3


def helper():
    """Return one."""
    return 1


class Box:
    size = 2

    def total(self, value):
        def inner():
            return value

        return inner() + self.size

    @property
    def doubled(self):
        return self.size * 2


@functools.cache
def cached():
    return 4


def one_line(): return 5


def numbers():
    yield 1
"#;

#[test]
fn function_body_edits_map_to_baseline_body_spans() {
    let edit = |from: &str, to: &str| {
        assert!(SCOPED_MODULE.contains(from), "fixture lacks {from:?}");
        function_spans(SCOPED_MODULE, &SCOPED_MODULE.replacen(from, to, 1))
    };
    assert_eq!(
        edit("    return 1\n", "    return 10\n"),
        Some(vec![(8, 8)])
    );
    assert_eq!(
        edit("            return value\n", "            return -value\n"),
        Some(vec![(16, 16)])
    );
    assert_eq!(
        edit("        return inner()", "        return 0 + inner()"),
        Some(vec![(15, 18)])
    );
    assert_eq!(
        edit("        def inner():\n", "        def inner(extra=1):\n"),
        Some(vec![(15, 18)])
    );
    assert_eq!(
        edit(
            "        return self.size * 2\n",
            "        return self.size * 3\n"
        ),
        Some(vec![(22, 22)])
    );
    assert_eq!(
        edit("    return 1\n", "    print()\n    return 1\n"),
        Some(vec![(8, 8)])
    );
    assert_eq!(
        edit(
            "        return inner()",
            "        def extra():\n            return 0\n\n        return extra() + inner()"
        ),
        Some(vec![(15, 18)])
    );
    assert_eq!(
        edit("    yield 1\n", "    yield 1\n    yield 2\n"),
        Some(vec![(34, 34)])
    );
    for (from, to) in [
        ("LIMIT = 3", "LIMIT = 4"),
        ("    \"\"\"Return one.\"\"\"", "    \"\"\"Return 1.\"\"\""),
        ("def helper():", "def helper(value=1):"),
        ("    size = 2", "    size = 3"),
        ("    return 4", "    return 40"),
        ("def one_line(): return 5", "def one_line(): return 6"),
        ("    yield 1\n", "    return [1]\n"),
        (
            "def numbers():\n",
            "def numbers():\n    pass\n\n\ndef added():\n",
        ),
    ] {
        assert_eq!(edit(from, to), None, "{from:?} -> {to:?}");
    }
}

fn scoped_selection(edit: (&str, &str), lines: &[(&str, &[i32])]) -> super::SelectionPlan {
    let directory = tempfile::tempdir().expect("selection directory");
    let source = directory.path().join("module.py");
    let (from, to) = edit;
    let current = SCOPED_MODULE.replacen(from, to, 1);
    std::fs::write(&source, &current).expect("source");
    let source_path = crate::glob_path(&crate::absolute_path(&source));
    let patch = one_hunk_patch(&source_path, SCOPED_MODULE, &current);
    build_selection_plan(SelectionEvidence {
        mode: SelectionMode::Safe,
        history_path: directory.path().join("history.json"),
        source_roots: vec![directory.path().to_path_buf()],
        patch,
        location: None,
        source_identity: "green".to_owned(),
        branch_mode: false,
        semantic_evidence: false,
        python_semantics: crate::analysis::PythonSemantics {
            class_bare_annotations_execute: true,
            debug_enabled: true,
        },
        durations: BTreeMap::new(),
        lines: lines
            .iter()
            .map(|(context, lines)| LineSelectionRecord {
                context: (*context).to_owned(),
                path: source_path.clone(),
                lines: lines.iter().copied().collect(),
            })
            .collect(),
        arcs: Vec::new(),
    })
    .expect("selection plan")
}

#[test]
fn safe_selection_limits_body_edits_to_tests_that_executed_the_body() {
    const IMPORT: (&str, &[i32]) = ("", &[1, 3, 6, 11, 12, 14, 20, 21, 25, 26, 30, 33]);
    const HELPER: (&str, &[i32]) = ("tests/test_module.py::test_helper|run", &[8]);
    const TOTAL: (&str, &[i32]) = ("tests/test_module.py::test_total|run", &[15, 16, 18]);
    let body = ("    return 1\n", "    return 10\n");
    let plan = scoped_selection(body, &[IMPORT, HELPER, TOTAL]);
    assert!(!plan.fallback_full, "{:?}", plan.reasons);
    assert_eq!(plan.selected, ["tests/test_module.py::test_helper"]);
    assert_eq!(plan.scoped_files.len(), 1);

    let deletion = ("        def inner():\n            return value\n\n", "");
    let plan = scoped_selection(deletion, &[IMPORT, HELPER, TOTAL]);
    assert!(!plan.fallback_full, "{:?}", plan.reasons);
    assert_eq!(plan.selected, ["tests/test_module.py::test_total"]);

    let both = [
        "tests/test_module.py::test_helper",
        "tests/test_module.py::test_total",
    ];
    let module = ("LIMIT = 3", "LIMIT = 4");
    let plan = scoped_selection(module, &[IMPORT, HELPER, TOTAL]);
    assert_eq!(plan.selected, both);
    assert!(plan.scoped_files.is_empty());
    for shared in [
        ("", &[1, 3, 6, 8, 11][..]),
        ("tests/test_module.py::test_total|setup", &[8][..]),
        ("tests/test_module.py::test_total|run", &[1, 8, 15][..]),
    ] {
        let plan = scoped_selection(body, &[IMPORT, HELPER, TOTAL, shared]);
        assert_eq!(plan.selected, both, "{shared:?}");
        assert!(plan.scoped_files.is_empty(), "{shared:?}");
    }
}

#[test]
fn graph_cache_restores_reverted_imports_outside_current_diff() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let dependent = root.join("dependent.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&dependent, "import base\n").expect("dependent");
    let base_path = crate::glob_path(&base);
    let dependent_path = crate::glob_path(&dependent);
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let before = std::fs::read(&cache).expect("baseline graph");
    std::fs::write(&dependent, "value = 1\n").expect("temporary edit");
    let _ = static_dependents(
        &BTreeSet::from([dependent_path.clone()]),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    assert_eq!(std::fs::read(&cache).expect("unchanged graph"), before);
    std::fs::write(&dependent, "import base\n").expect("restore source");
    let result = static_dependents(&BTreeSet::from([base_path]), &[root], &cache, "green");
    assert!(result.dependents.contains(&dependent_path));
}

#[test]
fn incomplete_or_unknown_graph_cache_rescans_sources() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let dependent = root.join("dependent.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&dependent, "import base\n").expect("dependent");
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let mut value: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&cache).expect("graph cache")).expect("graph JSON");
    value["version"] = serde_json::json!(0);
    std::fs::write(
        &cache,
        serde_json::to_vec(&value).expect("unknown version JSON"),
    )
    .expect("unknown graph version");
    std::fs::write(&dependent, "def broken(:\n").expect("broken source");
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        &[root],
        &cache,
        "green",
    );
    assert!(result.reasons.iter().any(|reason| reason.contains("parse")));
}

#[test]
fn import_graph_decodes_non_utf8_cookie_and_bom_sources() {
    let directory = tempfile::tempdir().expect("graph directory");
    let root = directory.path().canonicalize().expect("root");
    let base = root.join("base.py");
    let cookie = root.join("cookie.py");
    let bom = root.join("bom.py");
    let cache = root.join("graph.json");
    std::fs::write(&base, "VALUE = 1\n").expect("base");
    std::fs::write(&cookie, b"# coding: latin-1\n# caf\xe9\nimport base\n").expect("cookie source");
    std::fs::write(&bom, b"\xef\xbb\xbfimport cookie\n").expect("BOM source");
    let _ = static_dependents(
        &BTreeSet::new(),
        std::slice::from_ref(&root),
        &cache,
        "green",
    );
    let result = static_dependents(
        &BTreeSet::from([crate::glob_path(&base)]),
        &[root],
        &cache,
        "green",
    );
    assert!(result.dependents.contains(&crate::glob_path(&cookie)));
    assert!(result.dependents.contains(&crate::glob_path(&bom)));
}

#[test]
fn invalid_result_batch_preserves_previous_artifact() {
    let directory = tempfile::tempdir().expect("result directory");
    let result = directory.path().join("result.json");
    let before = br#"{"infrastructure_failed":false,"tests":[]}"#;
    std::fs::write(&result, before).expect("previous result");
    for (phase, outcome, duration, message) in [
        ("collect", "passed", 1.0, "invalid phase"),
        ("call", "unknown", 1.0, "invalid outcome"),
        ("call", "passed", -1.0, "finite non-negative"),
        ("call", "passed", f64::NAN, "finite non-negative"),
        ("call", "passed", f64::INFINITY, "finite non-negative"),
    ] {
        std::fs::write(&result, before).expect("previous result");
        let error = write_test_reports(
            &result,
            [
                (
                    "valid".to_owned(),
                    "call".to_owned(),
                    "passed".to_owned(),
                    1.0,
                ),
                (
                    "bad".to_owned(),
                    phase.to_owned(),
                    outcome.to_owned(),
                    duration,
                ),
            ],
            false,
        )
        .expect_err("invalid report");
        assert!(error.to_string().contains(message), "{error}");
        assert_eq!(std::fs::read(&result).expect("preserved result"), before);
    }
}

#[test]
fn invalid_result_artifact_preserves_history_bytes() {
    let directory = tempfile::tempdir().expect("history directory");
    let history = directory.path().join("history.json");
    let result = directory.path().join("result.json");
    let before = br#"{"tests":{"test_one":{"duration_ms":1}}}"#;
    std::fs::write(&history, before).expect("history");
    for (field, value, message) in [
        ("outcome", serde_json::json!("unknown"), "invalid outcome"),
        (
            "failed_phase",
            serde_json::json!("collect"),
            "invalid phase",
        ),
        ("duration_ms", serde_json::json!(-1), "invalid duration"),
        (
            "call_duration_ms",
            serde_json::json!(-1),
            "invalid call duration",
        ),
    ] {
        std::fs::write(&history, before).expect("history");
        let mut entry =
            serde_json::json!({"nodeid":"test_one", "outcome":"passed", "duration_ms":1});
        entry[field] = value;
        std::fs::write(
            &result,
            serde_json::json!({"infrastructure_failed":false,"tests":[entry]}).to_string(),
        )
        .expect("invalid result");
        let error = record_test_run_artifact(&history, &result, "run", TestAttempt::Primary)
            .expect_err("invalid result artifact");
        assert!(error.to_string().contains(message), "{error}");
        assert_eq!(std::fs::read(&history).expect("preserved history"), before);
    }
}

fn history_records(path: &std::path::Path) -> serde_json::Value {
    serde_json::from_slice(&std::fs::read(path).expect("history bytes")).expect("history JSON")
}

#[test]
fn history_write_drops_the_legacy_version_field() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join(".vfy").join("test-history.json");
    std::fs::create_dir(path.parent().expect("parent")).expect("history parent");
    std::fs::write(
        &path,
        r#"{"version":1,"tests":{"old_test":{"duration_ms":1.0}}}"#,
    )
    .expect("legacy history");

    write_test_history(&path, [("test_node", 1.5)]).expect("history update");

    let content = history_records(&path);
    assert!(content.get("version").is_none());
    assert_eq!(content["tests"]["test_node"]["duration_ms"], 1.5);
    assert_eq!(content["tests"]["old_test"]["duration_ms"], 1.0);
}

#[test]
fn run_history_round_trip_preserves_custom_fields_and_sorted_bytes() {
    let directory = tempfile::tempdir().expect("history directory");
    let result = directory.path().join("result.json");
    let history = directory.path().join("history.json");
    std::fs::write(
        &history,
        r#"{"z_custom":{"z":2,"a":1},"version":1,"tests":{"test_é":{"custom":{"z":2,"a":1},"duration_ms":99}}}"#,
    )
    .expect("history fixture");
    write_test_reports(
        &result,
        [
            (
                "test_é".to_owned(),
                "setup".to_owned(),
                "passed".to_owned(),
                1.0,
            ),
            (
                "test_é".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
                2.0,
            ),
        ],
        false,
    )
    .expect("result artifact");

    let recorded = record_test_run_artifact(&history, &result, "run", TestAttempt::Primary)
        .expect("recorded run");
    assert!(recorded.failed.is_empty());
    assert!(!recorded.infrastructure_failed);
    assert_eq!(
        recorded.outcomes,
        BTreeMap::from([("test_é".to_owned(), TestOutcome::Passed)])
    );
    let expected = serde_json::json!({
        "tests": {
            "test_é": {
                "custom": {"a": 1, "z": 2},
                "duration_ms": 2.0,
                "observations": [
                    {"attempt": "primary", "duration_ms": 3.0, "outcome": "passed", "run_id": "run"}
                ]
            }
        },
        "z_custom": {"a": 1, "z": 2}
    });
    assert_eq!(
        std::fs::read_to_string(&history).expect("history bytes"),
        serde_json::to_string_pretty(&expected).expect("expected bytes")
    );

    let original: &[u8] = b"{\"tests\":{\"test_\\u00e9\":{\"observations\":{}}}}";
    std::fs::write(&history, original).expect("invalid observations");
    let error = record_test_run_artifact(&history, &result, "run", TestAttempt::Primary)
        .expect_err("observations must be an array");
    assert!(error.to_string().contains("must be an array"), "{error}");
    assert_eq!(
        std::fs::read(&history).expect("preserved history"),
        original
    );
}

/// Source files and baseline evidence for the Python-facing selection scenarios.
struct PlanProject {
    directory: tempfile::TempDir,
    lines: Vec<LineSelectionRecord>,
}

impl PlanProject {
    fn new() -> Self {
        let directory = tempfile::tempdir().expect("selection directory");
        std::fs::create_dir(directory.path().join("src")).expect("source directory");
        Self {
            directory,
            lines: Vec::new(),
        }
    }

    fn write(&self, relative: &str, text: &str) -> String {
        let path = self.directory.path().join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("parent directory");
        std::fs::write(&path, text).expect("source file");
        crate::glob_path(&crate::absolute_path(&path))
    }

    fn payments() -> &'static str {
        "def refund(amount):\n    return amount\n\ndef untouched():\n    return 0\n"
    }

    fn with_payments() -> (Self, String) {
        let mut project = Self::new();
        let source = project.write("src/payments.py", Self::payments());
        project.record("tests/test_payments.py::test_refund|run", &source, &[1, 2]);
        project.record(
            "tests/test_payments.py::test_refund|teardown",
            &source,
            &[1],
        );
        project.record("tests/test_other.py::test_untouched|run", &source, &[5, 6]);
        (project, source)
    }

    fn record(&mut self, context: &str, path: &str, lines: &[i32]) {
        self.lines.push(LineSelectionRecord {
            context: context.to_owned(),
            path: path.to_owned(),
            lines: lines.iter().copied().collect(),
        });
    }

    fn history(&self) -> std::path::PathBuf {
        self.directory.path().join("history.json")
    }

    fn plan(
        &self,
        mode: SelectionMode,
        patch: String,
        location: Option<String>,
        durations: BTreeMap<String, f64>,
    ) -> super::SelectionPlan {
        build_selection_plan(SelectionEvidence {
            mode,
            history_path: self.history(),
            // The command runs at the project root, which resolves package imports.
            source_roots: vec![
                self.directory.path().to_path_buf(),
                self.directory.path().join("src"),
            ],
            patch,
            location,
            source_identity: "green".to_owned(),
            branch_mode: false,
            semantic_evidence: false,
            python_semantics: crate::analysis::PythonSemantics {
                class_bare_annotations_execute: true,
                debug_enabled: true,
            },
            durations,
            lines: self.lines.clone(),
            arcs: Vec::new(),
        })
        .expect("selection plan")
    }
}

fn added_line_patch(path: &str, hunk: &str, line: &str) -> String {
    format!("+++ b/{path}\n@@ {hunk} @@\n+{line}\n")
}

#[test]
fn balanced_selection_uses_contexts_and_history() {
    let (project, source) = PlanProject::with_payments();
    let durations = BTreeMap::from([
        ("tests/test_payments.py::test_refund".to_owned(), 12.0),
        ("tests/test_other.py::test_untouched".to_owned(), 30.0),
    ]);
    let patch = added_line_patch(&source, "-1,0 +2,1", "    return amount + 1");

    let plan = project.plan(SelectionMode::Balanced, patch, None, durations);

    assert_eq!(plan.selected, ["tests/test_payments.py::test_refund"]);
    assert_eq!(
        plan.all_tests,
        [
            "tests/test_payments.py::test_refund",
            "tests/test_other.py::test_untouched"
        ]
    );
    assert_eq!(plan.selected_seconds, Some(0.012));
    assert_eq!(plan.full_seconds, Some(0.042));
    assert!(!plan.fallback_full);
}

#[test]
fn safe_mode_falls_back_when_changed_file_has_no_evidence() {
    let mut project = PlanProject::new();
    let new = project.write("src/new.py", "value = 1\n");
    let existing = project.write("src/existing.py", "value = 1\n");
    project.record("tests/test_existing.py::test_one|run", &existing, &[1]);
    let patch = added_line_patch(&new, "-1,0 +1,1", "value = 2");

    let plan = project.plan(SelectionMode::Safe, patch, None, BTreeMap::new());

    assert!(plan.fallback_full);
    assert_eq!(plan.selected, ["tests/test_existing.py::test_one"]);
}

#[test]
fn safe_mode_invalidates_every_test_that_touched_a_changed_file() {
    let (mut project, source) = PlanProject::with_payments();
    project.record("tests/test_summary.py::test_summary|run", &source, &[1]);
    let patch = added_line_patch(&source, "-1,0 +2,1", "    return amount + 1");

    let plan = project.plan(SelectionMode::Safe, patch, None, BTreeMap::new());

    assert_eq!(
        plan.selected,
        [
            "tests/test_other.py::test_untouched",
            "tests/test_payments.py::test_refund",
            "tests/test_summary.py::test_summary"
        ]
    );
}

#[test]
fn safe_body_scope_preserves_global_imports_and_unrelated_test_inventory() {
    let mut project = PlanProject::new();
    let changed = project.write(
        "src/value.py",
        "def value():\n    return 2\n\ndef other():\n    return 3\n",
    );
    let importer = project.write("src/importer.py", "marker = 1\n");
    let unrelated = project.write("src/unrelated.py", "marker = 1\n");
    project.record("collection", &changed, &[1]);
    project.record("tests/test_a.py::test_a|run", &changed, &[2]);
    project.record("tests/test_b.py::test_b|run", &changed, &[2]);
    project.record("tests/test_c.py::test_c|run", &changed, &[5]);
    project.record(
        "tests/test_unrelated.py::test_unrelated|setup",
        &unrelated,
        &[1],
    );
    let patch = format!("+++ b/{changed}\n@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n");

    let scoped = project.plan(SelectionMode::Safe, patch.clone(), None, BTreeMap::new());
    assert_eq!(
        scoped.selected,
        ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
    );
    assert_eq!(
        scoped.all_tests,
        [
            "tests/test_a.py::test_a",
            "tests/test_b.py::test_b",
            "tests/test_c.py::test_c",
            "tests/test_unrelated.py::test_unrelated"
        ]
    );

    project.record("tests/test_b.py::test_b|run", &importer, &[1]);
    let widened = project.plan(SelectionMode::Safe, patch, None, BTreeMap::new());
    assert_eq!(
        widened.selected,
        [
            "tests/test_a.py::test_a",
            "tests/test_b.py::test_b",
            "tests/test_c.py::test_c"
        ]
    );
    assert_eq!(widened.all_tests, scoped.all_tests);
}

#[test]
fn safe_mode_rebuilds_when_test_discovery_changes() {
    let (project, _) = PlanProject::with_payments();
    let new_test = project.write("tests/test_new.py", "def test_new(): pass\n");
    let patch = added_line_patch(&new_test, "-0,0 +1,1", "def test_new(): pass");

    let plan = project.plan(SelectionMode::Safe, patch, None, BTreeMap::new());

    assert!(plan.fallback_full);
    assert!(
        plan.reasons
            .iter()
            .any(|reason| reason == "test discovery or fixtures changed"),
        "{:?}",
        plan.reasons
    );
}

#[test]
fn selection_falls_back_when_import_evidence_is_incomplete() {
    let (project, source) = PlanProject::with_payments();
    project.write("src/broken.py", "def broken(:\n");
    let patch = added_line_patch(&source, "-1,0 +2,1", "    return amount + 1");

    for _ in 0..2 {
        let plan = project.plan(
            SelectionMode::Balanced,
            patch.clone(),
            None,
            BTreeMap::new(),
        );
        assert!(plan.fallback_full);
        assert!(
            plan.reasons
                .iter()
                .any(|reason| reason.contains("failed to parse import source")),
            "{:?}",
            plan.reasons
        );
    }
}

#[test]
fn aggressive_selection_uses_direct_evidence_without_building_import_graph() {
    let (project, source) = PlanProject::with_payments();
    project.write("src/broken.py", "def broken(:\n");
    let patch = added_line_patch(&source, "-1,0 +2,1", "    return amount + 1");

    let plan = project.plan(SelectionMode::Aggressive, patch, None, BTreeMap::new());

    assert_eq!(plan.selected, ["tests/test_payments.py::test_refund"]);
    assert!(!plan.fallback_full);
    assert!(!project.directory.path().join("import-graph.json").exists());
}

#[test]
fn location_query_orders_by_history() {
    let (project, source) = PlanProject::with_payments();
    let durations = BTreeMap::from([
        ("tests/test_payments.py::test_refund".to_owned(), 20.0),
        ("tests/test_other.py::test_untouched".to_owned(), 5.0),
    ]);

    let plan = project.plan(
        SelectionMode::Balanced,
        String::new(),
        Some(format!("{source}:2")),
        durations,
    );

    assert_eq!(plan.all_tests, ["tests/test_payments.py::test_refund"]);
    assert_eq!(plan.selected, ["tests/test_payments.py::test_refund"]);
    assert!((plan.durations["tests/test_payments.py::test_refund"] - 20.0).abs() < f64::EPSILON);
}

#[test]
fn balanced_selection_adds_transitive_static_importers() {
    let mut project = PlanProject::new();
    let changed = project.write(
        "src/payments.py",
        "def refund(amount):\n    return amount + 1\n",
    );
    let service = project.write("src/service.py", "from src.payments import refund\n");
    project.write("tests/test_service.py", "from src.service import refund\n");
    project.record("tests/test_service.py::test_refund|run", &service, &[1]);
    let patch = added_line_patch(&changed, "-1,0 +1,1", "    return amount + 2");

    let plan = project.plan(SelectionMode::Balanced, patch, None, BTreeMap::new());

    assert_eq!(plan.selected, ["tests/test_service.py::test_refund"]);
    assert!(!plan.fallback_full, "{:?}", plan.reasons);
    assert!(!project.directory.path().join("import-graph.json").exists());
}

#[test]
fn empty_selection_estimates_positive_zero_seconds() {
    let estimate = super::plan::seconds(&[], &BTreeMap::new()).expect("no missing durations");
    assert!(
        estimate.is_sign_positive(),
        "{estimate:?} formats as -0.00s"
    );
    assert_eq!(format!("{estimate:.2}"), "0.00");
}

#[test]
fn git_replay_patch_ignores_user_diff_and_color_configuration() {
    let directory = tempfile::tempdir().expect("Git directory");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let directory_path = crate::path::absolute_path(directory.path());
    let root = directory_path.as_path();
    git_at(root, &["init", "-q"]);
    git_at(root, &["config", "user.email", "vfy@example.invalid"]);
    git_at(root, &["config", "user.name", "vfy test"]);
    git_at(root, &["config", "diff.noprefix", "true"]);
    git_at(root, &["config", "color.ui", "always"]);
    git_at(root, &["config", "diff.renames", "false"]);
    let spaced = root.join("space dir").join("naïve file.py");
    std::fs::create_dir_all(spaced.parent().expect("parent")).expect("directory");
    std::fs::write(&spaced, "value = 1\n").expect("source");
    git_at(root, &["add", "."]);
    git_at(root, &["commit", "-qm", "baseline"]);
    git_at(root, &["checkout", "-q", "--detach"]);
    std::fs::write(&spaced, "value = 2\n").expect("change");

    let state = read_git_replay_state_at(root, &[]).expect("detached replay state");
    assert!(!state.patch.contains('\u{1b}'), "{:?}", state.patch);
    let files = super::plan::parse_diff_at(&state.patch, root);
    assert_eq!(files.len(), 1, "{:?}", state.patch);
    assert_eq!(files[0].path, crate::glob_path(&spaced));
    assert_eq!(state.changed, [crate::glob_path(&spaced)]);
}

#[test]
fn git_replay_state_reports_changes_below_generated_directory_names() {
    let directory = tempfile::tempdir().expect("Git directory");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let directory_path = crate::path::absolute_path(directory.path());
    let root = directory_path
        .as_path()
        .join("target")
        .join(".cache")
        .join("repo");
    std::fs::create_dir_all(root.join("build")).expect("repository layout");
    git_at(&root, &["init", "-q"]);
    git_at(&root, &["config", "user.email", "vfy@example.invalid"]);
    git_at(&root, &["config", "user.name", "vfy test"]);
    std::fs::write(root.join("module.py"), "value = 1\n").expect("source");
    git_at(&root, &["add", "."]);
    git_at(&root, &["commit", "-qm", "baseline"]);
    std::fs::write(root.join("module.py"), "value = 2\n").expect("change");
    std::fs::write(root.join("build").join("__pycache__.tmp"), "x").expect("untracked");
    std::fs::create_dir(root.join("__pycache__")).expect("generated directory");
    std::fs::write(root.join("__pycache__").join("module.pyc"), "x").expect("bytecode");

    let state = read_git_replay_state_at(&root, &[]).expect("replay state");
    let module = crate::glob_path(&root.join("module.py"));
    assert!(state.changed.contains(&module), "{:?}", state.changed);
    assert!(
        state
            .changed
            .iter()
            .all(|path| !path.contains("/__pycache__/")),
        "{:?}",
        state.changed
    );
    assert_eq!(
        read_green_git_head_at(&root, &[]).expect("green head"),
        None
    );
}

#[test]
fn git_replay_state_reports_empty_repositories_and_linked_worktrees() {
    let directory = tempfile::tempdir().expect("Git directory");
    // Expect resolved paths, as on macOS where temporary paths are symlinks.
    let directory_path = crate::path::absolute_path(directory.path());
    let main = directory_path.join("main repo");
    std::fs::create_dir(&main).expect("repository directory");
    git_at(&main, &["init", "-q"]);
    git_at(&main, &["config", "user.email", "vfy@example.invalid"]);
    git_at(&main, &["config", "user.name", "vfy test"]);
    let error = read_git_replay_state_at(&main, &[]).expect_err("no commit has a HEAD");
    assert!(!error.is_empty());
    assert!(read_green_git_head_at(&main, &[]).is_err());

    std::fs::write(main.join("module.py"), "value = 1\n").expect("source");
    git_at(&main, &["add", "."]);
    git_at(&main, &["commit", "-qm", "baseline"]);
    let linked = directory_path.join("linked ünï");
    git_at(
        &main,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            &linked.to_string_lossy(),
        ],
    );
    std::fs::write(linked.join("module.py"), "value = 2\n").expect("linked change");
    let state = read_git_replay_state_at(&linked, &[]).expect("linked worktree state");
    assert_eq!(state.changed, [crate::glob_path(&linked.join("module.py"))]);
    assert!(
        read_git_replay_state_at(&main, &[])
            .expect("main state")
            .changed
            .is_empty()
    );
}

#[test]
fn git_failures_name_the_operation_on_one_line() {
    let directory = tempfile::tempdir().expect("create empty repository");
    let status = std::process::Command::new("git")
        .args(["init", "-q"])
        .current_dir(directory.path())
        .status()
        .expect("run git init");
    assert!(status.success());

    let error = read_git_replay_state_at(directory.path(), &[])
        .expect_err("a repository without commits has no HEAD");

    assert!(error.starts_with("failed to read Git HEAD: "), "{error}");
    assert!(!error.contains('\n'), "{error}");
}
