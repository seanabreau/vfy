use super::{MutationOutput, MutationPublication};
use std::path::Path;

fn baseline_request(coverage: &Path) -> String {
    serde_json::json!({
        "tests": ["test_subject.py::test_value"],
        "pytest_args": [],
        "suite_state": [["test_subject.py", "actual-source"]],
        "data_file": coverage,
        "data_hash": "untrusted supplied hash"
    })
    .to_string()
}

#[test]
fn rejected_destination_change_preserves_every_prior_artifact() {
    let root = tempfile::tempdir().expect("project artifacts");
    let coverage = root.path().join("coverage");
    let cache = root.path().join("cache");
    std::fs::write(&coverage, b"old coverage").expect("prior coverage");
    std::fs::write(&cache, b"old cache").expect("prior cache");
    let publication = MutationPublication::new(&[
        (MutationOutput::Coverage, coverage.clone()),
        (MutationOutput::Cache, cache.clone()),
    ])
    .expect("stage prior artifacts");
    std::fs::write(
        publication
            .staged(MutationOutput::Coverage)
            .expect("coverage resource"),
        b"new coverage",
    )
    .expect("stage new coverage");
    std::fs::write(
        publication
            .staged(MutationOutput::Cache)
            .expect("cache resource"),
        b"new cache",
    )
    .expect("stage new cache");
    std::fs::write(&cache, b"caller changed cache").expect("concurrent caller write");
    assert!(publication.commit().is_err());
    assert_eq!(
        std::fs::read(&coverage).expect("retained coverage"),
        b"old coverage"
    );
    assert_eq!(
        std::fs::read(&cache).expect("retained caller write"),
        b"caller changed cache"
    );
}

#[test]
fn baseline_binds_new_staged_coverage_without_reading_prior_public_bytes() {
    let root = tempfile::tempdir().expect("project artifacts");
    let coverage = root.path().join("coverage");
    let baseline = root.path().join("baseline");
    std::fs::write(&coverage, b"old coverage").expect("prior coverage");
    std::fs::write(&baseline, b"old baseline").expect("prior baseline");
    let mut publication = MutationPublication::new(&[
        (MutationOutput::Coverage, coverage.clone()),
        (MutationOutput::Baseline, baseline.clone()),
    ])
    .expect("stage prior artifacts");
    let staged = publication
        .staged(MutationOutput::Coverage)
        .expect("coverage resource");
    std::fs::write(staged, b"new coverage").expect("stage coverage");
    let actual = crate::output::file_digest(staged).expect("staged digest");
    let request = baseline_request(&coverage);
    assert!(
        !publication
            .baseline_valid(&request)
            .expect("prior baseline mismatch")
    );
    publication
        .write_baseline(&request)
        .expect("prepare bound baseline");
    assert!(
        publication
            .baseline_valid(&request)
            .expect("actual staged binding")
    );
    let metadata: serde_json::Value = serde_json::from_slice(
        &std::fs::read(
            publication
                .staged(MutationOutput::Baseline)
                .expect("baseline resource"),
        )
        .expect("prepared baseline"),
    )
    .expect("baseline JSON");
    assert_eq!(metadata["data_hash"], actual);
    let canonical_coverage = crate::canonical_path(&coverage).expect("canonical coverage path");
    assert_eq!(
        metadata["data_file"],
        canonical_coverage.to_string_lossy().as_ref()
    );
    assert_eq!(
        std::fs::read(&coverage).expect("unpublished coverage"),
        b"old coverage"
    );
    assert_eq!(
        std::fs::read(&baseline).expect("unpublished baseline"),
        b"old baseline"
    );
    publication.commit().expect("publish bound artifacts");
    assert_eq!(
        std::fs::read(&coverage).expect("published coverage"),
        b"new coverage"
    );
    assert_eq!(
        crate::output::file_digest(&coverage).expect("published digest"),
        actual
    );
}

#[test]
fn foreign_baseline_destination_preserves_staged_and_public_artifacts() {
    let root = tempfile::tempdir().expect("project artifacts");
    let coverage = root.path().join("coverage");
    let baseline = root.path().join("baseline");
    std::fs::write(&coverage, b"old coverage").expect("prior coverage");
    std::fs::write(&baseline, b"old baseline").expect("prior baseline");
    let mut publication = MutationPublication::new(&[
        (MutationOutput::Coverage, coverage.clone()),
        (MutationOutput::Baseline, baseline.clone()),
    ])
    .expect("stage artifacts");
    let request = baseline_request(&root.path().join("foreign"));
    assert!(publication.baseline_valid(&request).is_err());
    assert!(publication.write_baseline(&request).is_err());
    assert_eq!(
        std::fs::read(
            publication
                .staged(MutationOutput::Baseline)
                .expect("baseline resource")
        )
        .expect("retained stage"),
        b"old baseline"
    );
    assert_eq!(
        std::fs::read(&coverage).expect("retained coverage"),
        b"old coverage"
    );
    assert_eq!(
        std::fs::read(&baseline).expect("retained baseline"),
        b"old baseline"
    );
}

#[test]
fn prepared_baseline_stage_changes_reject_before_any_public_replacement() {
    for changed in [MutationOutput::Coverage, MutationOutput::Baseline] {
        let root = tempfile::tempdir().expect("project artifacts");
        let coverage = root.path().join("coverage");
        let baseline = root.path().join("baseline");
        let cache = root.path().join("cache");
        for (path, bytes) in [
            (&coverage, b"old coverage".as_slice()),
            (&baseline, b"old baseline".as_slice()),
            (&cache, b"old cache".as_slice()),
        ] {
            std::fs::write(path, bytes).expect("prior artifact");
        }
        let mut publication = MutationPublication::new(&[
            (MutationOutput::Coverage, coverage.clone()),
            (MutationOutput::Baseline, baseline.clone()),
            (MutationOutput::Cache, cache.clone()),
        ])
        .expect("stage artifacts");
        std::fs::write(
            publication
                .staged(MutationOutput::Coverage)
                .expect("coverage resource"),
            b"new coverage",
        )
        .expect("stage coverage");
        std::fs::write(
            publication
                .staged(MutationOutput::Cache)
                .expect("cache resource"),
            b"new cache",
        )
        .expect("stage cache");
        publication
            .write_baseline(&baseline_request(&coverage))
            .expect("prepare baseline");
        std::fs::write(
            publication.staged(changed).expect("changed resource"),
            b"changed after preparation",
        )
        .expect("late stage change");
        assert!(publication.commit().is_err());
        assert_eq!(
            std::fs::read(&coverage).expect("retained coverage"),
            b"old coverage"
        );
        assert_eq!(
            std::fs::read(&baseline).expect("retained baseline"),
            b"old baseline"
        );
        assert_eq!(std::fs::read(&cache).expect("retained cache"), b"old cache");
    }
}

#[test]
fn missing_owned_baseline_roles_reject_without_changing_existing_outputs() {
    let root = tempfile::tempdir().expect("project artifacts");
    let coverage = root.path().join("coverage");
    let baseline = root.path().join("baseline");
    std::fs::write(&coverage, b"old coverage").expect("prior coverage");
    std::fs::write(&baseline, b"old baseline").expect("prior baseline");
    for (kind, path) in [
        (MutationOutput::Coverage, &coverage),
        (MutationOutput::Baseline, &baseline),
    ] {
        let mut publication =
            MutationPublication::new(&[(kind, path.clone())]).expect("single generic output");
        let request = baseline_request(&coverage);
        assert!(publication.baseline_valid(&request).is_err());
        assert!(publication.write_baseline(&request).is_err());
        assert_eq!(
            std::fs::read(path).expect("retained public file"),
            if kind == MutationOutput::Coverage {
                b"old coverage".as_slice()
            } else {
                b"old baseline".as_slice()
            }
        );
    }
}

#[test]
fn reused_valid_baseline_retains_its_binding_until_publication() {
    let root = tempfile::tempdir().expect("project artifacts");
    let coverage = root.path().join("coverage");
    let baseline = root.path().join("baseline");
    let outputs = [
        (MutationOutput::Coverage, coverage.clone()),
        (MutationOutput::Baseline, baseline.clone()),
    ];
    let mut first = MutationPublication::new(&outputs).expect("first publication");
    std::fs::write(
        first
            .staged(MutationOutput::Coverage)
            .expect("coverage resource"),
        b"published coverage",
    )
    .expect("initial coverage");
    let request = baseline_request(&coverage);
    first.write_baseline(&request).expect("initial baseline");
    first.commit().expect("initial publication");
    let previous_coverage = std::fs::read(&coverage).expect("published coverage");
    let previous_baseline = std::fs::read(&baseline).expect("published baseline");
    let mut reused = MutationPublication::new(&outputs).expect("reused publication");
    assert!(
        reused
            .baseline_valid(&request)
            .expect("actual reused binding")
    );
    std::fs::write(
        reused
            .staged(MutationOutput::Coverage)
            .expect("coverage resource"),
        b"changed after successful validation",
    )
    .expect("late change");
    assert!(reused.commit().is_err());
    assert_eq!(
        std::fs::read(&coverage).expect("retained coverage"),
        previous_coverage
    );
    assert_eq!(
        std::fs::read(&baseline).expect("retained baseline"),
        previous_baseline
    );
}
