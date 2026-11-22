use super::test_name;

#[test]
#[cfg(unix)]
fn hardlink_collection_aliases_union_lines_without_merging_unrelated_files() {
    use std::collections::{BTreeMap, BTreeSet};

    let directory = tempfile::tempdir().expect("real collection sources");
    let source = directory.path().join("app.py");
    let alias = directory.path().join("alias.py");
    let unrelated = directory.path().join("unrelated.py");
    let missing = directory.path().join("missing.py");
    let bytes = "def value():\n    return 1\n";
    std::fs::write(&source, bytes).expect("primary source");
    std::fs::hard_link(&source, &alias).expect("distinct names for the same physical source");
    std::fs::write(&unrelated, bytes).expect("equal bytes do not establish a source alias");
    let path = |path: &std::path::Path| crate::glob_path(&crate::absolute_path(path));
    let mut data = crate::data::CoverageData::new(true);
    data.add_lines("", &path(&source), vec![1]);
    data.add_arcs("", &path(&alias), vec![(2, 3), (3, -2)]);
    data.add_lines("test_app.py::test_value|run", &path(&alias), vec![99]);
    data.add_lines("", &path(&unrelated), vec![7]);
    assert_eq!(
        super::mutation_unattributed_lines(&data),
        BTreeMap::from([
            (path(&source), BTreeSet::from([1, 2, 3])),
            (path(&alias), BTreeSet::from([1, 2, 3])),
            (path(&unrelated), BTreeSet::from([7])),
        ])
    );
    data.add_lines("", &path(&missing), vec![8]);
    assert!(super::mutation_unattributed_lines(&data).is_empty());
}

#[test]
fn locations_preserve_pytest_paths_node_ids_and_windows_drive_prefixes() {
    for target in [
        None,
        Some(""),
        Some("tests"),
        Some("tests/test_app.py"),
        Some("tests/test_app.py::test_value[param:2]"),
        Some(r"C:\tests\test_app.py"),
        Some("C:/tests/test_app.py"),
        Some(r"C:\tests\test_app.py::test_value"),
    ] {
        assert_eq!(super::mutation_location(target), Ok(None), "{target:?}");
    }
    for (target, path, line) in [
        ("app.py:2", "app.py", 2),
        ("app.py:0002", "app.py", 2),
        (r"C:\src\app.py:2", r"C:\src\app.py", 2),
    ] {
        assert_eq!(
            super::mutation_location(Some(target)),
            Ok(Some((
                crate::absolute_path(std::path::Path::new(path)),
                line
            ))),
        );
    }
    for target in [
        "app.py:0",
        "app.py:-2",
        "app.py:bad",
        ":2",
        "app.py:2147483648",
    ] {
        assert_eq!(
            super::mutation_location(Some(target)),
            Err(format!("invalid mutation location: {target}")),
        );
    }
}

#[test]
fn contexts_group_compact_lines_and_arc_endpoints_without_losing_attribution() {
    let directory = tempfile::tempdir().expect("source directory");
    let path = directory.path().join("module.py");
    let source = path.to_string_lossy();
    let mut data = crate::data::CoverageData::new(true);
    let parameterized = "nightly|tests/test_app.py::test_value[param|edge]|run";
    data.add_lines(parameterized, &source, vec![3, 3]);
    data.add_arcs(parameterized, &source, vec![(-1, 3), (3, 4)]);
    data.add_arcs("tests/test_app.py::test_other|setup", &source, vec![(4, 5)]);
    data.add_lines("tests/test_empty.py::test_case|teardown", &source, vec![]);
    data.add_lines("unattributed", &source, vec![9]);

    let (locations, tests) = super::mutation_contexts(&data);
    let path = crate::glob_path(&crate::absolute_path(&path));
    assert_eq!(
        locations,
        std::collections::BTreeMap::from([
            (
                format!("{path}:3"),
                std::collections::BTreeSet::from([
                    "tests/test_app.py::test_value[param|edge]".to_owned(),
                ]),
            ),
            (
                format!("{path}:4"),
                std::collections::BTreeSet::from([
                    "tests/test_app.py::test_other".to_owned(),
                    "tests/test_app.py::test_value[param|edge]".to_owned(),
                ]),
            ),
            (
                format!("{path}:5"),
                std::collections::BTreeSet::from(["tests/test_app.py::test_other".to_owned()]),
            ),
        ])
    );
    assert_eq!(
        tests,
        std::collections::BTreeSet::from([
            "tests/test_app.py::test_other".to_owned(),
            "tests/test_app.py::test_value[param|edge]".to_owned(),
            "tests/test_empty.py::test_case".to_owned(),
        ])
    );
}

fn mutation_suite_state(
    root: &std::path::Path,
    tests: &[String],
    arguments: &[String],
) -> Vec<(String, String)> {
    super::mutation_suite_state(root, tests, arguments)
        .into_iter()
        .map(|(path, digest)| (path, digest.expect("readable test evidence")))
        .collect()
}

#[test]
fn parameter_pipes_survive_static_context_prefix_removal() {
    assert_eq!(
        test_name("tests/test_app.py::test_value[param|edge]|run"),
        Some("tests/test_app.py::test_value[param|edge]")
    );
    assert_eq!(
        test_name("nightly|tests/test_app.py::test_value|teardown"),
        Some("tests/test_app.py::test_value")
    );
    assert_eq!(test_name("unattributed"), None);
}

#[test]
fn suite_identity_tracks_imported_helpers_and_missing_config_creation() {
    let directory = tempfile::tempdir().expect("suite directory");
    let root = directory.path();
    std::fs::write(root.join("helper.py"), "VALUE = 1\n").expect("helper source");
    let before = mutation_suite_state(root, &[], &[]);
    std::fs::write(root.join("helper.py"), "VALUE = 2\n").expect("changed helper");
    let changed = mutation_suite_state(root, &[], &[]);
    assert_ne!(before, changed);
    std::fs::write(root.join("pytest.ini"), "[pytest]\n").expect("new configuration");
    assert_ne!(changed, mutation_suite_state(root, &[], &[]));
}

fn commit(root: &std::path::Path, message: &str) {
    super::git(root, &["add", "."]).expect("stage project");
    super::git(root, &["commit", "-qm", message]).expect("commit project");
}

#[test]
fn base_scope_uses_merge_base_and_current_paths_from_a_subdirectory() {
    let directory = tempfile::tempdir().expect("Git project");
    let root = directory.path().canonicalize().expect("canonical root");
    super::git(&root, &["init", "-qb", "main"]).expect("initialize Git");
    super::git(&root, &["config", "user.email", "test@example.invalid"]).expect("email");
    super::git(&root, &["config", "user.name", "Test"]).expect("name");
    for name in [
        "committed.py",
        "staged.py",
        "unstaged.py",
        "old name.py",
        "deleted.py",
        "unchanged.py",
    ] {
        std::fs::write(root.join(name), "VALUE = 1\n").expect("initial source");
    }
    std::fs::write(root.join(".gitignore"), "ignored.py\n").expect("ignore rule");
    commit(&root, "baseline");
    let merge_base = super::git_text(&root, &["rev-parse", "HEAD"]).expect("baseline commit");
    super::git(&root, &["checkout", "-qb", "topic"]).expect("topic branch");
    std::fs::write(root.join("committed.py"), "VALUE = 2\n").expect("committed edit");
    commit(&root, "topic edit");
    super::git(&root, &["checkout", "-q", "main"]).expect("base branch");
    std::fs::write(root.join("unchanged.py"), "VALUE = 99\n").expect("upstream-only edit");
    commit(&root, "upstream edit");
    let base_commit = super::git_text(&root, &["rev-parse", "HEAD"]).expect("base commit");
    super::git(&root, &["checkout", "-q", "topic"]).expect("topic branch");
    std::fs::write(root.join("staged.py"), "VALUE = 3\n").expect("staged edit");
    super::git(&root, &["add", "staged.py"]).expect("stage edit");
    std::fs::write(root.join("unstaged.py"), "VALUE = 4\n").expect("unstaged edit");
    super::git(&root, &["mv", "old name.py", "new name.py"]).expect("rename source");
    std::fs::remove_file(root.join("deleted.py")).expect("delete source");
    std::fs::write(root.join("added.py"), "VALUE = 5\n").expect("added source");
    std::fs::write(root.join("ignored.py"), "VALUE = 6\n").expect("ignored source");
    std::fs::create_dir(root.join("subdir")).expect("invocation directory");
    let selected = super::changed_sources(&root.join("subdir"), Some("main")).expect("base scope");
    let expected: Vec<_> = [
        "added.py",
        "committed.py",
        "new name.py",
        "staged.py",
        "unstaged.py",
    ]
    .iter()
    .map(|name| root.join(name).to_string_lossy().into_owned())
    .collect();
    assert_eq!(selected.paths, expected);
    let evidence = selected.scope.expect("Git evidence");
    assert_eq!(evidence.base_commit, base_commit);
    assert_eq!(evidence.merge_base_commit, merge_base);
    assert_eq!(evidence.selected_files, expected);
    assert!(super::changed_sources(&root, Some("missing-ref")).is_err());
    assert!(super::changed_sources(&root, Some("--all")).is_err());
}

#[test]
fn source_filters_ignore_directory_names_above_the_project() {
    let directory = tempfile::tempdir().expect("temporary directory");
    let project = directory.path().join("tests").join(".venv").join("project");
    std::fs::create_dir_all(project.join("tests")).expect("project layout");
    std::fs::create_dir_all(project.join(".venv")).expect("environment layout");
    for name in [
        "module.py",
        "test_module.py",
        "tests/helper.py",
        ".venv/site.py",
    ] {
        std::fs::write(project.join(name), "value = 1\n").expect("source");
    }
    let roots = [project.to_string_lossy().into_owned()];
    let found = super::mutation_sources_below(&roots, false, Some(&project));
    assert_eq!(found, [project.join("module.py").to_string_lossy()]);
    let all = super::mutation_sources_below(&roots, true, Some(&project));
    assert_eq!(all.len(), 3, "{all:?}");
}
