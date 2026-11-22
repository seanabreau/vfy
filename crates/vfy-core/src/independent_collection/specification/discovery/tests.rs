use super::*;
use crate::independent_collection::{
    CollectionExclusions, CollectionIgnoreSettings, CollectionSettings, ConftestPathValue,
};

#[derive(Default)]
struct Visitor {
    root: PathBuf,
    events: Vec<String>,
    override_ignored: bool,
    failure: Option<&'static str>,
    recover: bool,
    selectors: Vec<Vec<String>>,
}

impl Visitor {
    fn relative(&self, path: &Path) -> String {
        path.strip_prefix(&self.root)
            .expect("project path")
            .to_string_lossy()
            .into_owned()
    }
}

impl CollectionVisitor for Visitor {
    type Error = &'static str;

    fn enter_directory(&mut self, path: &Path) -> Result<(), Self::Error> {
        self.events.push(format!("enter {}", self.relative(path)));
        if self.failure == Some("enter") && path.ends_with("branch") {
            return Err("original import failure");
        }
        Ok(())
    }

    fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> Result<bool, Self::Error> {
        self.events
            .push(format!("ignore {}", self.relative(candidate.path)));
        if self.failure == Some("ignore") && candidate.path.ends_with("test_branch.py") {
            return Err("original hook failure");
        }
        Ok(!self.override_ignored && candidate.default_ignored)
    }

    fn collect_file(&mut self, file: &CollectionFile) -> Result<(), Self::Error> {
        self.events.push(format!("file {}", file.nodeid));
        self.selectors.push(file.selectors.clone());
        Ok(())
    }

    fn collection_error(
        &mut self,
        path: &Path,
        error: DiscoveryError<Self::Error>,
    ) -> Result<(), DiscoveryError<Self::Error>> {
        if self.recover {
            self.events
                .push(format!("error {}: {error}", self.relative(path)));
            Ok(())
        } else {
            Err(error)
        }
    }
}

fn project(names: &[&str]) -> tempfile::TempDir {
    let root = tempfile::tempdir().expect("project");
    for name in names {
        let path = root.path().join(name);
        fs::create_dir_all(path.parent().expect("parent")).expect("directory");
        fs::write(path, "").expect("source");
    }
    root
}

#[test]
fn directory_decisions_finish_before_children_are_imported() {
    let root = project(&["a/test_a.py", "test_z.py"]);
    let specification = CollectionSpecification::defaults(root.path(), &[]).expect("specification");
    let mut visitor = Visitor {
        root: root.path().to_path_buf(),
        ..Visitor::default()
    };
    specification.walk(&mut visitor).expect("collection");
    assert_eq!(
        visitor.events,
        [
            "enter ",
            "ignore a",
            "ignore test_z.py",
            "enter a",
            "ignore a/test_a.py",
            "file a/test_a.py",
            "file test_z.py",
        ]
    );
}

#[test]
fn callback_can_override_defaults_but_explicit_targets_bypass_callbacks() {
    let root = project(&[".hidden/test_a.py", "test_z.py"]);
    for (arguments, override_ignored, expected) in [
        (vec![], false, vec!["test_z.py"]),
        (vec![], true, vec![".hidden/test_a.py", "test_z.py"]),
        (
            vec![".hidden/test_a.py".to_owned()],
            false,
            vec![".hidden/test_a.py"],
        ),
    ] {
        let specification =
            CollectionSpecification::defaults(root.path(), &arguments).expect("specification");
        let mut visitor = Visitor {
            root: root.path().to_path_buf(),
            override_ignored,
            ..Visitor::default()
        };
        let files = specification.walk(&mut visitor).expect("collection");
        assert_eq!(
            files
                .iter()
                .map(|file| file.nodeid.as_str())
                .collect::<Vec<_>>(),
            expected
        );
        if !arguments.is_empty() {
            assert!(
                !visitor
                    .events
                    .iter()
                    .any(|event| event.starts_with("ignore "))
            );
        }
    }
}

#[test]
fn original_visitor_error_is_retained_and_directory_recovery_keeps_siblings() {
    let root = project(&["branch/test_branch.py", "test_z.py"]);
    let specification = CollectionSpecification::defaults(root.path(), &[]).expect("specification");
    for failure in ["enter", "ignore"] {
        let mut visitor = Visitor {
            root: root.path().to_path_buf(),
            failure: Some(failure),
            ..Visitor::default()
        };
        let error = specification.walk(&mut visitor).expect_err("typed failure");
        let DiscoveryError::Visitor(original) = error else {
            panic!("visitor error changed kind");
        };
        assert_eq!(
            original,
            if failure == "enter" {
                "original import failure"
            } else {
                "original hook failure"
            }
        );
        visitor.events.clear();
        visitor.recover = true;
        let files = specification
            .walk(&mut visitor)
            .expect("sibling collection");
        assert_eq!(
            files
                .iter()
                .map(|file| file.nodeid.as_str())
                .collect::<Vec<_>>(),
            ["test_z.py"]
        );
        assert!(
            visitor
                .events
                .iter()
                .any(|event| event.starts_with("error branch:"))
        );
    }
}

#[test]
fn a_file_callback_receives_all_nonredundant_selectors_once() {
    let root = project(&["test_a.py"]);
    let specification = CollectionSpecification::defaults(
        root.path(),
        &[
            "test_a.py::test_two".to_owned(),
            "test_a.py::test_one".to_owned(),
        ],
    )
    .expect("specification");
    let mut visitor = Visitor {
        root: root.path().to_path_buf(),
        ..Visitor::default()
    };
    specification.walk(&mut visitor).expect("collection");
    assert_eq!(visitor.selectors, [vec!["test_two", "test_one"]]);
}

#[test]
fn initial_preload_anchors_are_shallow_and_retain_redundant_arguments() {
    let root = project(&[
        "conftest.py",
        "tests/conftest.py",
        "tests/deep/conftest.py",
        "branch/conftest.py",
        "branch/test_a.py",
    ]);
    let initial =
        initial_conftest_anchors(root.path(), root.path(), &[], &[]).expect("initial anchors");
    let fallback = [root.path().to_path_buf(), root.path().join("tests")];
    assert_eq!(initial, fallback);
    let initial = initial_conftest_anchors(
        root.path(),
        root.path(),
        &[
            ".".to_owned(),
            "branch/test_a.py::test_one".to_owned(),
            "missing.py::test_later".to_owned(),
            "branch/../branch/test_a.py::test_two".to_owned(),
        ],
        &[],
    )
    .expect("raw anchors");
    assert_eq!(
        initial,
        [
            root.path().to_path_buf(),
            root.path().join("tests"),
            root.path().join("branch/test_a.py"),
            root.path().join("branch/test_a.py"),
        ]
    );
    let initial = initial_conftest_anchors(root.path(), root.path(), &[], &["branch".to_owned()])
        .expect("configured anchor");
    assert_eq!(initial, [root.path().join("branch")]);
    let initial =
        initial_conftest_anchors(root.path(), root.path(), &["missing.py".to_owned()], &[])
            .expect("missing anchor fallback");
    assert_eq!(initial, fallback);
}

#[test]
fn conftest_strings_resolve_at_source_but_pathlike_values_keep_spelling() {
    let root = project(&[
        "branch/test_string.py",
        "branch/test_relative.py",
        "branch/test_absolute.py",
    ]);
    let mut exclusions = CollectionExclusions::from_resolved(CollectionIgnoreSettings {
        invocation_dir: root.path().to_path_buf(),
        ignore: vec![],
        ignore_glob: vec![],
        norecursedirs: vec![],
        collect_in_virtualenv: false,
    })
    .expect("exclusions");
    exclusions
        .add_paths(
            &root.path().join("branch"),
            &[
                ConftestPathValue::String("test_string.py".to_owned()),
                ConftestPathValue::PathLike(PathBuf::from("test_relative.py")),
                ConftestPathValue::PathLike(root.path().join("branch/test_absolute.py")),
            ],
        )
        .expect("paths");
    assert!(exclusions.is_ignored(&root.path().join("branch/test_string.py")));
    assert!(!exclusions.is_ignored(&root.path().join("branch/test_relative.py")));
    assert!(exclusions.is_ignored(&root.path().join("branch/test_absolute.py")));
}

#[test]
fn live_naming_update_does_not_reselect_testpaths() {
    let root = project(&["test_a.py", "check_a.py", "other/check_a.py"]);
    let mut settings =
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
    settings.testpaths = vec!["test_a.py".to_owned()];
    let mut specification =
        CollectionSpecification::from_resolved(settings, &[], &[], &[]).expect("specification");
    specification
        .update_naming(
            vec!["check_*.py".to_owned()],
            vec!["Spec".to_owned()],
            vec!["check".to_owned()],
        )
        .expect("live patterns");
    assert_eq!(
        specification.discover().expect("fixed target")[0].nodeid,
        "test_a.py"
    );
    assert!(specification.matches_function("check_one"));
}
