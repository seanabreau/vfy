use super::*;

#[test]
fn a_failed_rollback_does_not_prevent_other_restorations() {
    let root = tempfile::tempdir().expect("root");
    let original = root.path().join("original");
    std::fs::write(&original, "accepted evidence").expect("original");
    let previous = prepare(&original, root.path()).expect("backup");
    std::fs::write(&original, "obsolete evidence").expect("replacement");
    let blocked = root.path().join("blocked");
    std::fs::create_dir(&blocked).expect("blocked destination");
    let files = vec![
        PublishedFile {
            destination: original.clone(),
            previous: Some(previous),
        },
        PublishedFile {
            destination: blocked.clone(),
            previous: None,
        },
    ];
    let error = restore(files).expect_err("directory cannot be removed as a file");
    assert!(error.to_string().contains(&blocked.display().to_string()));
    assert_eq!(
        std::fs::read_to_string(original).expect("restored"),
        "accepted evidence"
    );
}

#[test]
fn a_failed_restoration_retains_the_original_evidence_on_disk() {
    let root = tempfile::tempdir().expect("root");
    let original = root.path().join("source");
    std::fs::write(&original, "accepted evidence").expect("source");
    let previous = prepare(&original, root.path()).expect("backup");
    let backup = previous.path().to_path_buf();
    let destination = root.path().join("blocked");
    std::fs::create_dir(&destination).expect("blocked destination");
    let error = restore(vec![PublishedFile {
        destination,
        previous: Some(previous),
    }])
    .expect_err("restoration blocked");
    assert!(
        error
            .to_string()
            .contains(&format!("prior evidence retained at {}", backup.display()))
    );
    assert_eq!(
        std::fs::read_to_string(backup).expect("retained backup"),
        "accepted evidence"
    );
}
