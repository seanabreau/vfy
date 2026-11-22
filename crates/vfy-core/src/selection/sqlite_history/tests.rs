use std::collections::BTreeSet;
use std::path::Path;
use std::sync::{Arc, Barrier};

use rusqlite::Connection;
use serde_json::{Value, json};

use crate::selection::{
    FailedPhase, TestAttempt, TestOutcome, TestReport, TestRun, read_test_history, record_test_run,
    write_test_history,
};

fn report(nodeid: &str, duration: f64) -> TestReport {
    TestReport {
        nodeid: nodeid.to_owned(),
        outcome: TestOutcome::Passed,
        failed_phase: None,
        duration_ms: duration,
        call_duration_ms: Some(duration),
    }
}

fn run(reports: Vec<TestReport>) -> TestRun {
    TestRun {
        infrastructure_failed: false,
        tests: reports,
    }
}

fn document(path: &Path) -> Value {
    super::read_document(path, u64::MAX).expect("storage-neutral history document")
}

#[test]
fn sqlite_history_matches_json_for_duplicates_and_attempts() {
    let directory = tempfile::tempdir().expect("history directory");
    let json_path = directory.path().join("history.json");
    let sqlite_path = directory.path().join("history.sqlite3");
    for attempt in [TestAttempt::Primary, TestAttempt::Confirmation] {
        let mut reports: Vec<_> = (0..23)
            .map(|index| {
                let mut report = report("test_é", f64::from(index));
                report.call_duration_ms = (index == 1).then_some(7.0);
                report
            })
            .collect();
        reports[22].outcome = TestOutcome::Failed;
        reports[22].failed_phase = Some(FailedPhase::Teardown);
        reports.push(report("new", 0.25));
        let run = run(reports);
        let expected = record_test_run(&json_path, &run, "é\n", attempt).expect("JSON merge");
        let actual = record_test_run(&sqlite_path, &run, "é\n", attempt).expect("SQLite merge");
        assert_eq!(actual, expected);
        assert_eq!(
            document(&sqlite_path),
            serde_json::from_slice::<Value>(&std::fs::read(&json_path).expect("JSON history"))
                .expect("JSON value")
        );
        assert_eq!(
            read_test_history(&sqlite_path).durations,
            read_test_history(&json_path).durations
        );
    }
    let connection = Connection::open(&sqlite_path).expect("database");
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM runs", [], |row| row.get::<_, i64>(0))
            .expect("run count"),
        1
    );
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM observations", [], |row| row
                .get::<_, i64>(0))
            .expect("observation count"),
        22
    );
}

#[test]
fn bounded_history_discards_unreferenced_runs_and_duration_updates_reset_only_affected_tests() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    for index in 0..23 {
        record_test_run(
            &path,
            &run(vec![report("a", f64::from(index)), report("b", 2.0)]),
            &format!("run-{index}"),
            TestAttempt::Primary,
        )
        .expect("record run");
    }
    let value = document(&path);
    assert_eq!(
        value["tests"]["a"]["observations"]
            .as_array()
            .expect("observations")
            .len(),
        20
    );
    assert_eq!(value["tests"]["a"]["observations"][0]["run_id"], "run-3");
    let connection = Connection::open(&path).expect("database");
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM runs", [], |row| row.get::<_, i64>(0))
            .expect("bounded run count"),
        20
    );
    write_test_history(&path, [("a", 4.0), ("ignored", f64::NAN)]).expect("duration-only update");
    assert_eq!(document(&path)["tests"]["a"], json!({"duration_ms": 4.0}));
    assert_eq!(document(&path)["tests"]["b"], value["tests"]["b"]);
    write_test_history(&path, [("b", 5.0)]).expect("release remaining observations");
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM runs", [], |row| row.get::<_, i64>(0))
            .expect("orphan run count"),
        0
    );
}

#[test]
fn invalid_run_does_not_create_database_and_sql_failure_rolls_back_every_row() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    assert!(
        record_test_run(
            &path,
            &run(vec![report("a", 1.0), report("z", f64::NAN)]),
            "invalid",
            TestAttempt::Primary
        )
        .is_err()
    );
    assert!(!path.exists());
    record_test_run(
        &path,
        &run(vec![report("a", 1.0), report("z", 2.0)]),
        "before",
        TestAttempt::Primary,
    )
    .expect("initial history");
    let connection = Connection::open(&path).expect("database");
    connection.execute_batch("CREATE TRIGGER reject_second BEFORE INSERT ON observations WHEN NEW.test_id=(SELECT id FROM tests WHERE nodeid='z') BEGIN SELECT RAISE(ABORT,'injected failure'); END;").expect("inject storage failure");
    let before = std::fs::read(&path).expect("initial database bytes");
    assert!(
        record_test_run(
            &path,
            &run(vec![report("a", 9.0), report("z", 8.0)]),
            "rejected",
            TestAttempt::Primary
        )
        .is_err()
    );
    assert_eq!(
        std::fs::read(&path).expect("rolled back database bytes"),
        before
    );
    assert_eq!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM runs WHERE run_id='rejected'",
                [],
                |row| row.get::<_, i64>(0)
            )
            .expect("rolled back identity"),
        0
    );
}

#[test]
fn concurrent_first_writers_keep_every_observation() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    let barrier = Arc::new(Barrier::new(8));
    std::thread::scope(|scope| {
        for index in 0..8 {
            let barrier = Arc::clone(&barrier);
            let path = &path;
            scope.spawn(move || {
                barrier.wait();
                record_test_run(
                    path,
                    &run(vec![report("a", f64::from(index))]),
                    &format!("run-{index}"),
                    TestAttempt::Primary,
                )
                .expect("concurrent history update");
            });
        }
    });
    let value = document(&path);
    let identities: BTreeSet<_> = value["tests"]["a"]["observations"]
        .as_array()
        .expect("all observations")
        .iter()
        .map(|observation| {
            observation["run_id"]
                .as_str()
                .expect("run identity")
                .to_owned()
        })
        .collect();
    assert_eq!(
        identities,
        (0..8).map(|index| format!("run-{index}")).collect()
    );
}

#[test]
fn corrupt_or_foreign_databases_are_not_replaced() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    for corrupt in [false, true] {
        if corrupt {
            std::fs::write(&path, b"not a database").expect("corrupt database");
        } else {
            Connection::open(&path)
                .expect("foreign database")
                .execute_batch("CREATE TABLE unrelated(value);")
                .expect("foreign schema");
        }
        let before = std::fs::read(&path).expect("original database");
        assert!(
            record_test_run(
                &path,
                &run(vec![report("a", 1.0)]),
                "rejected",
                TestAttempt::Primary
            )
            .is_err()
        );
        assert_eq!(std::fs::read(&path).expect("preserved database"), before);
        assert!(read_test_history(&path).durations.is_empty());
        std::fs::remove_file(&path).expect("reset fixture");
    }
}

#[test]
fn signed_zero_preserves_duration_bits_observations_and_selection_order() {
    let directory = tempfile::tempdir().expect("history directory");
    let json_path = directory.path().join("history.json");
    let sqlite_path = directory.path().join("history.sqlite3");
    write_test_history(&json_path, [("z", -0.0), ("a", 0.0)]).expect("signed-zero JSON");
    write_test_history(&sqlite_path, [("z", -0.0), ("a", 0.0)]).expect("signed-zero SQLite");
    for update in [
        None,
        Some(TestAttempt::Confirmation),
        Some(TestAttempt::Primary),
    ] {
        if let Some(attempt) = update {
            let run = run(vec![report("z", -0.0), report("a", 0.0)]);
            record_test_run(&json_path, &run, "zero", attempt).expect("JSON signed zero");
            record_test_run(&sqlite_path, &run, "zero", attempt).expect("SQLite signed zero");
        }
        let expected = read_test_history(&json_path).durations;
        let actual = read_test_history(&sqlite_path).durations;
        assert_eq!(actual["z"].to_bits(), expected["z"].to_bits());
        assert_eq!(actual["a"].to_bits(), expected["a"].to_bits());
        let mut selected = vec!["a".to_owned(), "z".to_owned()];
        crate::selection::sort_tests(&mut selected, &actual);
        assert_eq!(selected, ["z", "a"]);
        if update.is_some() {
            assert_eq!(
                document(&sqlite_path)["tests"]["z"]["observations"][0]["duration_ms"]
                    .as_f64()
                    .expect("signed-zero observation")
                    .to_bits(),
                (-0.0_f64).to_bits()
            );
        }
    }
    write_test_history(&sqlite_path, [("z", -0.0)]).expect("duration-only signed zero");
    assert_eq!(
        read_test_history(&sqlite_path).durations["z"].to_bits(),
        (-0.0_f64).to_bits()
    );
}

#[test]
fn snapshot_includes_committed_wal_rows_while_a_writer_has_uncommitted_changes() {
    let directory = tempfile::tempdir().expect("history directory");
    let source = directory.path().join("history.sqlite3");
    let destination = directory.path().join("snapshot.sqlite3");
    write_test_history(&source, [("a", 1.0)]).expect("initial history");
    let writer = Connection::open(&source).expect("history writer");
    let mode: String = writer
        .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
        .expect("enable WAL");
    assert_eq!(mode, "wal");
    writer
        .pragma_update(None, "wal_autocheckpoint", 0)
        .expect("retain WAL frames");
    let main_database = std::fs::read(&source).expect("checkpointed database");
    writer
        .execute("UPDATE tests SET duration_ms = 2.0", [])
        .expect("commit to WAL");
    assert_eq!(
        std::fs::read(&source).expect("database after WAL commit"),
        main_database
    );
    assert!(
        std::fs::metadata(directory.path().join("history.sqlite3-wal"))
            .expect("uncheckpointed WAL")
            .len()
            > 32
    );
    let committed = document(&source);
    writer
        .execute_batch("BEGIN IMMEDIATE; UPDATE tests SET duration_ms = 3.0;")
        .expect("uncommitted writer");

    super::snapshot_test_history(&source, &destination).expect("snapshot with an active writer");
    assert_eq!(document(&destination), committed);
    assert_eq!(
        read_test_history(&destination).durations["a"].to_bits(),
        2.0_f64.to_bits()
    );

    writer.execute_batch("COMMIT").expect("finish writer");
    assert_eq!(
        read_test_history(&source).durations["a"].to_bits(),
        3.0_f64.to_bits()
    );
    assert_eq!(document(&destination), committed);
}

#[test]
fn failed_snapshot_preserves_the_destination_and_allows_writes_and_retry() {
    let directory = tempfile::tempdir().expect("history directory");
    let source = directory.path().join("history.sqlite3");
    let destination = directory.path().join("snapshot.sqlite3");
    write_test_history(&source, [("a", 1.0)]).expect("initial history");
    std::fs::create_dir(&destination).expect("blocked destination");
    let sentinel = destination.join("keep");
    std::fs::write(&sentinel, b"existing destination").expect("destination contents");
    let original = document(&source);

    let error = super::snapshot_test_history(&source, &destination)
        .expect_err("a snapshot cannot replace a directory");
    assert!(matches!(error, crate::CoreError::ArtifactIo { path, .. } if path == destination));
    assert_eq!(
        std::fs::read(&sentinel).expect("preserved destination"),
        b"existing destination"
    );
    assert_eq!(document(&source), original);

    let writer = Connection::open(&source).expect("history writer");
    writer
        .busy_timeout(std::time::Duration::ZERO)
        .expect("fail immediately on a retained read lock");
    writer
        .execute("UPDATE tests SET duration_ms = 4.0", [])
        .expect("failed snapshot releases its read transaction");
    std::fs::remove_file(sentinel).expect("remove destination fixture");
    std::fs::remove_dir(&destination).expect("unblock destination");
    super::snapshot_test_history(&source, &destination).expect("retry snapshot");
    assert_eq!(document(&destination), document(&source));
    assert_eq!(
        read_test_history(&destination).durations["a"].to_bits(),
        4.0_f64.to_bits()
    );
}

#[test]
fn watch_lease_blocks_writer_until_snapshot_publication_and_writer_opens_new_inode() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    let stage = directory.path().join("stage.sqlite3");
    write_test_history(&path, [("a", 1.0)]).expect("live history");
    let lease = super::TestHistoryLease::acquire(&path).expect("watch lease");
    let (started, ready) = std::sync::mpsc::channel();
    let (finished, completion) = std::sync::mpsc::channel();
    std::thread::scope(|scope| {
        let path = &path;
        scope.spawn(move || {
            started.send(()).expect("writer started");
            record_test_run(
                path,
                &run(vec![report("b", 2.0)]),
                "concurrent",
                TestAttempt::Primary,
            )
            .expect("writer after publication");
            finished.send(()).expect("writer completed");
        });
        ready.recv().expect("writer is running");
        assert!(
            completion
                .recv_timeout(std::time::Duration::from_millis(50))
                .is_err()
        );
        super::snapshot_test_history(path, &stage).expect("consistent stage snapshot");
        write_test_history(&stage, [("a", 9.0)]).expect("staged update");
        std::fs::rename(&stage, path).expect("publish stage under watch lease");
        drop(lease);
        completion
            .recv_timeout(std::time::Duration::from_secs(5))
            .expect("writer resumes after lease");
    });
    assert_eq!(
        read_test_history(&path).durations,
        [("a".to_owned(), 9.0), ("b".to_owned(), 2.0)].into()
    );
}

#[test]
fn canonical_document_limit_rejects_oversized_history() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.sqlite3");
    record_test_run(
        &path,
        &run(vec![report("a", 1.0), report("b", -0.0), report("c", 3.0)]),
        "old",
        TestAttempt::Primary,
    )
    .expect("initial history");
    record_test_run(
        &path,
        &run(vec![report("a", 1.0), report("b", -0.0)]),
        "new",
        TestAttempt::Primary,
    )
    .expect("typed observations");
    let expected = document(&path);
    let bytes = serde_json::to_vec(&expected).expect("canonical JSON");
    let fitting_limit = u64::try_from(bytes.len()).expect("document size") + 64;
    assert_eq!(
        super::read_document(&path, fitting_limit).expect("fitting document"),
        expected
    );
    let small_limit = u64::try_from(bytes.len() / 2).expect("small document limit");
    assert!(
        super::read_document(&path, small_limit)
            .expect_err("oversized document")
            .to_string()
            .contains("artifact limit")
    );
}

#[test]
fn new_duplicate_test_observations_remain_bounded_in_one_run() {
    let directory = tempfile::tempdir().expect("history directory");
    let json_path = directory.path().join("history.json");
    let sqlite_path = directory.path().join("history.sqlite3");
    let reports = (0..23)
        .map(|index| report("case", f64::from(index)))
        .collect();
    let run = run(reports);
    let expected =
        record_test_run(&json_path, &run, "shared", TestAttempt::Primary).expect("JSON history");
    let actual = record_test_run(&sqlite_path, &run, "shared", TestAttempt::Primary)
        .expect("SQLite history");
    assert_eq!(actual, expected);
    let history = document(&sqlite_path);
    assert_eq!(
        history,
        serde_json::from_slice::<Value>(&std::fs::read(&json_path).expect("JSON history"))
            .expect("JSON value")
    );
    let observations = history["tests"]["case"]["observations"]
        .as_array()
        .expect("observations");
    assert_eq!(observations.len(), 20);
    assert_eq!(observations[0]["duration_ms"], 3.0);
    let connection = Connection::open(&sqlite_path).expect("database");
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM runs", [], |row| row.get::<_, i64>(0))
            .expect("run count"),
        1
    );
}

#[test]
fn empty_history_with_duplicate_cases_matches_later_existing_updates() {
    let directory = tempfile::tempdir().expect("history directory");
    let json_path = directory.path().join("history.json");
    let sqlite_path = directory.path().join("history.sqlite3");
    for (run_id, attempt, reports) in [
        (
            "first",
            TestAttempt::Primary,
            vec![report("b", 1.0), report("a", 2.0), report("b", 3.0)],
        ),
        (
            "second",
            TestAttempt::Confirmation,
            vec![report("a", 4.0), report("c", 5.0)],
        ),
    ] {
        let run = run(reports);
        let expected = record_test_run(&json_path, &run, run_id, attempt).expect("JSON history");
        let actual = record_test_run(&sqlite_path, &run, run_id, attempt).expect("SQLite history");
        assert_eq!(actual, expected);
        assert_eq!(
            document(&sqlite_path),
            serde_json::from_slice::<Value>(&std::fs::read(&json_path).expect("JSON history"))
                .expect("JSON value")
        );
    }
}

#[test]
fn first_observation_durations_match_json_for_new_duplicate_and_existing_cases() {
    let directory = tempfile::tempdir().expect("history directory");
    let json_path = directory.path().join("history.json");
    let sqlite_path = directory.path().join("history.sqlite3");
    let first = run(vec![
        report("zero", -0.0),
        TestReport {
            call_duration_ms: None,
            ..report("later", 1.0)
        },
        report("later", 2.0),
        TestReport {
            call_duration_ms: None,
            ..report("zero", 3.0)
        },
        report("positive", 0.0),
    ]);
    for (id, attempt, batch) in [
        ("initial", TestAttempt::Primary, first),
        (
            "confirm",
            TestAttempt::Confirmation,
            run(vec![report("zero", 8.0), report("new-confirmation", -0.0)]),
        ),
        (
            "update",
            TestAttempt::Primary,
            run(vec![report("zero", 5.0), report("new-confirmation", -0.0)]),
        ),
    ] {
        record_test_run(&json_path, &batch, id, attempt).expect("JSON history");
        record_test_run(&sqlite_path, &batch, id, attempt).expect("SQLite history");
        let json = read_test_history(&json_path);
        let sqlite = read_test_history(&sqlite_path);
        assert_eq!(json.durations.len(), sqlite.durations.len());
        for (nodeid, duration) in &json.durations {
            assert_eq!(
                sqlite.durations[nodeid].to_bits(),
                duration.to_bits(),
                "{nodeid}"
            );
        }
        let expected: Value =
            serde_json::from_slice(&std::fs::read(&json_path).expect("JSON file"))
                .expect("JSON document");
        assert_eq!(document(&sqlite_path), expected);
    }
}
