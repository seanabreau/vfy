use super::{
    CollectionArgumentPolicy, CollectionCandidate, CollectionFile, CollectionSettings,
    CollectionSpecification, CollectionVisitor, DiscoveryError,
};
use std::convert::Infallible;
use std::fs;

#[test]
fn kept_targets_retain_argument_positions_and_selector_boundaries() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::create_dir(root.path().join("suite")).expect("test directory");
    fs::write(root.path().join("suite/test_case.py"), "").expect("test source");
    let arguments = [
        "suite/test_case.py::TestGroup::test_case",
        "suite",
        "suite/test_case.py",
        "suite/test_case.py::TestGroup::test_case",
    ]
    .map(str::to_owned);
    let mut settings =
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
    settings.argument_policy = CollectionArgumentPolicy::Keep;
    let specification = CollectionSpecification::from_resolved(settings, &arguments, &[], &[])
        .expect("kept target specification");
    assert_eq!(
        specification
            .targets()
            .map(|target| (target.original_index, target.argument))
            .collect::<Vec<_>>(),
        arguments
            .iter()
            .enumerate()
            .map(|(index, argument)| (index, argument.as_str()))
            .collect::<Vec<_>>()
    );
    let first = specification.targets().next().expect("first selector");
    assert_eq!(first.selector.parts(), ["TestGroup", "test_case"]);
    let normalized = CollectionSpecification::defaults(root.path(), &arguments)
        .expect("normalized target specification");
    assert_eq!(
        normalized
            .targets()
            .map(|target| target.original_index)
            .collect::<Vec<_>>(),
        [1]
    );
}

#[test]
fn kept_targets_still_validate_every_original_argument() {
    let root = tempfile::tempdir().expect("temporary project");
    fs::write(root.path().join("test_case.py"), "").expect("test source");
    for invalid in ["missing.py", "test_case.py::", ".::test_case"] {
        let mut settings =
            CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
        settings.argument_policy = CollectionArgumentPolicy::Keep;
        assert!(
            CollectionSpecification::from_resolved(
                settings,
                &["test_case.py".to_owned(), invalid.to_owned()],
                &[],
                &[],
            )
            .is_err(),
            "invalid target {invalid}"
        );
    }
}

#[test]
fn flat_projections_reject_kept_occurrences_before_any_visitor_callback() {
    struct NoCallbacks;
    impl CollectionVisitor for NoCallbacks {
        type Error = Infallible;
        fn enter_directory(&mut self, _: &std::path::Path) -> Result<(), Self::Error> {
            panic!("flat projection must reject before entering a directory")
        }
        fn ignore(&mut self, _: &CollectionCandidate<'_>) -> Result<bool, Self::Error> {
            panic!("flat projection must reject before candidate filtering")
        }
        fn collect_file(&mut self, _: &CollectionFile) -> Result<(), Self::Error> {
            panic!("flat projection must reject before collecting a file")
        }
    }
    let root = tempfile::tempdir().expect("temporary project");
    fs::write(root.path().join("test_case.py"), "").expect("test source");
    let mut settings =
        CollectionSettings::defaults(root.path().to_path_buf(), root.path().to_path_buf());
    settings.argument_policy = CollectionArgumentPolicy::Keep;
    let specification = CollectionSpecification::from_resolved(
        settings,
        &["test_case.py".to_owned(), "test_case.py".to_owned()],
        &[],
        &[],
    )
    .expect("kept target specification");
    assert!(matches!(
        specification.walk(&mut NoCallbacks),
        Err(DiscoveryError::CollectionArenaRequired)
    ));
    let nodeids = [
        "test_case.py::test_case".to_owned(),
        "test_case.py::test_case".to_owned(),
    ];
    assert!(specification.order_nodeids(&nodeids).is_err());
    assert!(specification.discover().is_err());
}
