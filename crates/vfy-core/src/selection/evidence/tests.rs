use super::{green_test_run_valid, write_green_test_run};

#[test]
fn record_rejects_changed_and_missing_coverage() {
    let directory = tempfile::tempdir().expect("temporary evidence directory");
    let coverage = directory.path().join("replay.coverage");
    let record = directory.path().join("test-run.json");
    std::fs::write(&coverage, b"first").expect("initial coverage");
    write_green_test_run(&record, &coverage).expect("write evidence");
    assert!(green_test_run_valid(&record, &coverage).expect("validate evidence"));

    std::fs::write(&coverage, b"second").expect("changed coverage");
    assert!(!green_test_run_valid(&record, &coverage).expect("reject changed coverage"));
    std::fs::remove_file(&coverage).expect("remove coverage");
    assert!(!green_test_run_valid(&record, &coverage).expect("reject missing coverage"));
}
