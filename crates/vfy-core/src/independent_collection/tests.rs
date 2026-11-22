use super::{CollectionSettings, CollectionSpecification, conftests, discover};
use std::fs;

#[test]
fn discovery_is_sorted_deduplicated_and_does_not_enter_hidden_directories() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::create_dir(root.path().join(".venv")).expect("hidden directory");
    for name in [
        "test_z.py",
        "a_test.py",
        "helper.py",
        ".venv/test_hidden.py",
    ] {
        fs::write(root.path().join(name), "").expect("test source");
    }
    let files = discover(
        root.path(),
        &[".".to_owned(), "test_z.py::test_one".to_owned()],
    )
    .expect("discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["a_test.py", "test_z.py"]
    );
    assert!(files[1].selectors.is_empty());
    assert!(discover(root.path(), &["missing.py".to_owned()]).is_err());
}

#[test]
fn explicit_files_and_ancestor_conftests_preserve_scope_order() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::create_dir(root.path().join("tests")).expect("tests directory");
    for name in ["conftest.py", "tests/conftest.py", "tests/check.py"] {
        fs::write(root.path().join(name), "").expect("source");
    }
    let files = discover(
        root.path(),
        &["tests/check.py::TestCase::test_one".to_owned()],
    )
    .expect("explicit selection");
    assert_eq!(files[0].selectors, ["TestCase::test_one"]);
    assert_eq!(
        conftests(root.path(), &files[0].path),
        [
            root.path().join("conftest.py"),
            root.path().join("tests/conftest.py")
        ]
    );
}

#[test]
fn explicit_non_python_file_is_a_collection_candidate() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::write(root.path().join("example.txt"), "example").expect("test source");
    assert!(
        discover(root.path(), &[])
            .expect("directory discovery")
            .is_empty()
    );
    let files = discover(root.path(), &["example.txt".to_owned()]).expect("explicit file");
    assert_eq!(files.len(), 1);
    assert_eq!(files[0].nodeid, "example.txt");
}

#[test]
fn conftest_cutoff_excludes_ancestors_without_excluding_other_trees() {
    let root = tempfile::tempdir().expect("temporary project");
    for directory in ["configuration", "tests", "tests/nested"] {
        fs::create_dir_all(root.path().join(directory)).expect("source directory");
        fs::write(root.path().join(directory).join("conftest.py"), "").expect("conftest source");
    }
    fs::write(root.path().join("conftest.py"), "").expect("ancestor conftest");
    assert_eq!(
        conftests(
            &root.path().join("configuration"),
            &root.path().join("tests/nested/test_body.py"),
        ),
        [
            root.path().join("tests/conftest.py"),
            root.path().join("tests/nested/conftest.py"),
        ]
    );
}

#[test]
fn configured_discovery_replaces_defaults_and_applies_ignore_options() {
    let root = tempfile::tempdir().expect("temporary project");
    for directory in ["cases", "cases/.hidden", "cases/excluded", "other"] {
        fs::create_dir_all(root.path().join(directory)).expect("test directory");
    }
    for name in [
        "cases/check_z.py",
        "cases/check_a.py",
        "cases/.hidden/check_h.py",
        "cases/excluded/check_e.py",
        "cases/test_default.py",
        "other/check_o.py",
    ] {
        fs::write(root.path().join(name), "").expect("test source");
    }
    let specification = CollectionSpecification::from_resolved(
        CollectionSettings {
            argument_policy: super::CollectionArgumentPolicy::Normalize,
            rootdir: root.path().to_path_buf(),
            invocation_dir: root.path().to_path_buf(),
            testpaths: vec!["cases".to_owned()],
            python_files: vec!["check_*.py".to_owned()],
            python_classes: vec!["Spec*".to_owned()],
            python_functions: vec!["check".to_owned()],
            norecursedirs: vec!["excluded".to_owned()],
        },
        &[],
        &["cases/check_z.py".to_owned()],
        &["**/check_a.py".to_owned()],
    )
    .expect("discovery specification");
    let files = specification.discover().expect("configured discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["cases/.hidden/check_h.py"]
    );
    assert!(specification.matches_class("Special"));
    assert!(!specification.matches_class("TestDefault"));
    assert!(specification.matches_function("check_without_underscore"));
    assert!(!specification.matches_function("test_default"));
}

#[test]
fn target_order_and_selector_order_survive_deduplication() {
    let root = tempfile::tempdir().expect("temporary project");
    for name in ["test_a.py", "test_z.py"] {
        fs::write(root.path().join(name), "").expect("test source");
    }
    let files = discover(
        root.path(),
        &[
            "test_z.py::test_2".to_owned(),
            "test_z.py::test_1".to_owned(),
            "test_a.py".to_owned(),
            "test_z.py::test_2".to_owned(),
        ],
    )
    .expect("ordered discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["test_z.py", "test_a.py"]
    );
    assert_eq!(files[0].selectors, ["test_2", "test_1"]);
}

#[test]
fn final_case_selection_preserves_interleaved_targets_and_bare_file_precedence() {
    let root = tempfile::tempdir().expect("temporary project");
    for name in ["test_a.py", "test_b.py"] {
        fs::write(root.path().join(name), "").expect("test source");
    }
    let nodeids = [
        "test_a.py::test_one",
        "test_a.py::test_two[0]",
        "test_b.py::test_one",
    ]
    .map(str::to_owned);
    for (targets, expected) in [
        (
            vec!["test_a.py::test_two", "test_b.py", "test_a.py::test_one"],
            vec![1, 2, 0],
        ),
        (
            vec!["test_a.py::test_two", "test_b.py", "test_a.py"],
            vec![2, 0, 1],
        ),
    ] {
        let targets: Vec<_> = targets.into_iter().map(str::to_owned).collect();
        let specification = CollectionSpecification::defaults(root.path(), &targets)
            .expect("selection specification");
        assert_eq!(
            specification
                .order_nodeids(&nodeids)
                .expect("normalized ordering"),
            expected
        );
    }
    let specification = CollectionSpecification::from_resolved(
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf()),
        &["test_a.py".to_owned(), "test_b.py".to_owned()],
        &[],
        &[],
    )
    .expect("file target specification");
    assert_eq!(
        specification
            .order_nodeids(&nodeids)
            .expect("normalized ordering"),
        [0, 1, 2]
    );
}

#[test]
fn overlapping_directory_and_class_targets_normalize_before_discovery() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::create_dir(root.path().join("suite")).expect("test directory");
    fs::create_dir(root.path().join("suite/__pycache__")).expect("bytecode directory");
    fs::write(root.path().join("suite/__pycache__/test_a.pyc"), "").expect("cached bytecode");
    for name in ["suite/helper.py", "suite/test_a.py"] {
        fs::write(root.path().join(name), "").expect("test source");
    }
    let mut settings =
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
    settings.testpaths = vec!["suite/**".to_owned()];
    let specification = CollectionSpecification::from_resolved(settings, &[], &[], &[])
        .expect("glob specification");
    assert_eq!(
        specification
            .discover()
            .expect("glob discovery")
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["suite/test_a.py"]
    );
    let specification = CollectionSpecification::defaults(
        root.path(),
        &[
            "suite/test_a.py::Test::test_second[0]".to_owned(),
            "suite/test_a.py::test_other".to_owned(),
            "suite/test_a.py::Test".to_owned(),
        ],
    )
    .expect("class specification");
    let nodeids = [
        "suite/test_a.py::Test::test_first",
        "suite/test_a.py::Test::test_second[0]",
        "suite/test_a.py::test_other",
    ]
    .map(str::to_owned);
    assert_eq!(
        specification
            .order_nodeids(&nodeids)
            .expect("normalized ordering"),
        [2, 0, 1]
    );
    assert!(
        specification
            .unmatched_selectors(&nodeids.iter().map(String::as_str))
            .is_empty()
    );
    assert_eq!(
        specification.unmatched_selectors(&nodeids[..1].iter().map(String::as_str)),
        ["suite/test_a.py::test_other"]
    );
    assert_eq!(
        specification.unmatched_selectors(&std::iter::empty()),
        ["suite/test_a.py::test_other", "suite/test_a.py::Test"]
    );
}

#[test]
fn naming_globs_use_python_fnmatch_character_class_semantics() {
    let root = tempfile::tempdir().expect("temporary project");
    // Oracle: CPython 3.12 fnmatch.fnmatchcase. Python treats caret literally
    // and accepts descending ranges and unclosed brackets.
    for (pattern, name, expected) in [
        ("[^a]", "a", true),
        ("[^a]", "^", true),
        ("[^a]", "b", false),
        ("[z-a]", "z", false),
        ("[!z-a]", "x", true),
        ("[]]", "]", true),
        ("[a--b]", "b", true),
        ("[a--b]", "a", false),
        ("[b-a!]", "a", true),
        ("[b-a!a]", "a", false),
        ("[b-a!a]", "b", true),
        ("[!]", "!", false),
        ("{Test}", "Test", false),
        ("a/**/b", "a/x/y/b", true),
    ] {
        let mut settings =
            CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
        settings.python_classes = vec![pattern.to_owned()];
        let specification = CollectionSpecification::from_resolved(settings, &[], &[], &[])
            .expect("naming specification");
        assert_eq!(
            specification.matches_class(name),
            expected,
            "pattern {pattern:?}, name {name:?}"
        );
    }
}

#[test]
fn testpaths_globs_and_invocation_subdirectory_select_their_own_roots() {
    let root = tempfile::tempdir().expect("temporary project");
    for directory in ["suites/b", "suites/a", "local"] {
        fs::create_dir_all(root.path().join(directory)).expect("test directory");
        fs::write(root.path().join(directory).join("test_case.py"), "").expect("test source");
    }
    let specification = CollectionSpecification::from_resolved(
        {
            let mut settings =
                CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
            settings.testpaths = vec!["suites/*".to_owned()];
            settings
        },
        &[],
        &[],
        &[],
    )
    .expect("discovery specification");
    let files = specification.discover().expect("testpaths discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["suites/a/test_case.py", "suites/b/test_case.py"]
    );
    let specification = CollectionSpecification::from_resolved(
        {
            let mut settings =
                CollectionSettings::defaults(root.path().to_path_buf(), root.path().join("local"));
            settings.testpaths = vec!["suites/*".to_owned()];
            settings
        },
        &[],
        &[],
        &[],
    )
    .expect("subdirectory specification");
    assert_eq!(
        specification.discover().expect("subdirectory discovery")[0].nodeid,
        "local/test_case.py"
    );
}

#[test]
fn explicit_targets_bypass_ignore_checks_and_default_recursion_includes_target() {
    let root = tempfile::tempdir().expect("temporary project");
    for directory in ["target", ".hidden", "__pycache__", "environment"] {
        fs::create_dir(root.path().join(directory)).expect("test directory");
        fs::write(root.path().join(directory).join("test_case.py"), "").expect("test source");
    }
    fs::write(root.path().join("environment/pyvenv.cfg"), "").expect("virtual environment marker");
    let files = discover(root.path(), &[]).expect("default discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["target/test_case.py"]
    );
    let specification = CollectionSpecification::from_resolved(
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf()),
        &[".hidden".to_owned()],
        &[".hidden".to_owned()],
        &[],
    )
    .expect("explicit directory");
    assert_eq!(
        specification.discover().expect("explicit discovery")[0].nodeid,
        ".hidden/test_case.py"
    );
}

#[cfg(unix)]
#[test]
fn symlink_aliases_remain_distinct_and_cycles_fail() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().expect("temporary project");
    fs::create_dir(root.path().join("cases")).expect("test directory");
    fs::write(root.path().join("cases/test_case.py"), "").expect("test source");
    symlink("cases", root.path().join("link")).expect("symlink alias");
    let files = discover(root.path(), &[]).expect("alias discovery");
    assert_eq!(
        files
            .iter()
            .map(|file| file.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["cases/test_case.py", "link/test_case.py"]
    );
    symlink("..", root.path().join("cases/loop")).expect("directory cycle");
    assert!(
        discover(root.path(), &[])
            .expect_err("cycle must fail")
            .contains("recursive symlink directory")
    );
}
