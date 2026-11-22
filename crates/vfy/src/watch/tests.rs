#[cfg(unix)]
#[test]
fn staging_via_a_symlink_is_idempotent() {
    use super::stage_watch_outputs;

    let root = tempfile::tempdir().expect("root");
    let stage = root.path().join("stage");
    std::fs::create_dir(&stage).expect("stage");
    let alias = root.path().join("alias");
    std::os::unix::fs::symlink(&stage, &alias).expect("stage alias");
    let original = root.path().join("coverage.json");
    let first = stage_watch_outputs(
        alias.to_string_lossy().into_owned(),
        vec![("json".to_owned(), original.to_string_lossy().into_owned())],
    )
    .expect("initial staging");
    let second = stage_watch_outputs(alias.to_string_lossy().into_owned(), first.clone())
        .expect("pytest resolves staged options");
    assert_eq!(first, second);
    let manifest: std::collections::BTreeMap<String, String> = serde_json::from_slice(
        &std::fs::read(stage.join("resolved-outputs.json")).expect("manifest"),
    )
    .expect("valid manifest");
    assert_eq!(manifest.len(), 1);
    assert_eq!(
        manifest.values().next().expect("destination"),
        &vfy_core::absolute_path(&original).to_string_lossy()
    );
}

#[test]
fn pytest_html_stages_the_report_and_sibling_assets_together() {
    use super::stage_watch_outputs;

    let root = tempfile::tempdir().expect("root");
    let stage = root.path().join("stage");
    std::fs::create_dir(&stage).expect("stage");
    let destination = root.path().join("reports/results.html");
    let staged = stage_watch_outputs(
        stage.to_string_lossy().into_owned(),
        vec![(
            "pytest-html".to_owned(),
            destination.to_string_lossy().into_owned(),
        )],
    )
    .expect("stage pytest HTML report");
    let target = std::path::PathBuf::from(&staged[0].1);
    assert_eq!(target.file_name().expect("filename"), "results.html");

    let manifest: std::collections::BTreeMap<String, String> = serde_json::from_slice(
        &std::fs::read(stage.join("resolved-outputs.json")).expect("manifest"),
    )
    .expect("valid manifest");
    assert_eq!(manifest.len(), 2);
    assert_eq!(
        manifest[&target.to_string_lossy().into_owned()],
        vfy_core::absolute_path(&destination).to_string_lossy()
    );
    assert_eq!(
        manifest[&target
            .parent()
            .expect("target parent")
            .join("assets")
            .to_string_lossy()
            .into_owned()],
        vfy_core::absolute_path(
            &destination
                .parent()
                .expect("destination parent")
                .join("assets")
        )
        .to_string_lossy()
    );
}
