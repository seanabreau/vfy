use super::*;
use notify::event::{CreateKind, DataChange, Flag, RemoveKind};

fn isolate_events(session: &mut WatchSession) -> mpsc::Sender<notify::Result<Event>> {
    // The test supplies its own event ordering and keeps the channel open.
    let (events, receiver) = mpsc::channel();
    session.receiver = receiver;
    events
}

fn session(root: &Path) -> (WatchSession, mpsc::Sender<notify::Result<Event>>) {
    let mut session =
        WatchSession::new(&[root.to_path_buf()], &[], None).expect("register temporary watch root");
    let events = isolate_events(&mut session);
    (session, events)
}

fn modified(path: PathBuf) -> Event {
    Event::new(EventKind::Modify(ModifyKind::Data(DataChange::Content))).add_path(path)
}

#[test]
fn generated_paths_do_not_hide_configuration_or_prefix_neighbors() {
    assert!(!generated_path(Path::new(".coveragerc")));
    assert!(generated_path(Path::new(".coverage.worker.123")));
    assert!(!is_ignored(
        Path::new("/project/database.py"),
        &[PathBuf::from("/project/data.")],
        &[]
    ));
    assert!(is_ignored(
        Path::new("/project/data.worker"),
        &[PathBuf::from("/project/data.")],
        &[]
    ));
    assert!(!generated_path(Path::new("fixtures/input.json")));
}

#[test]
fn trailing_debounce_coalesces_paths_and_ignores_generated_events() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    let now = Instant::now();
    session.accept_at(
        Ok(Event::new(EventKind::Create(CreateKind::File)).add_path(root.path().join("b.py"))),
        now,
    );
    session.accept_at(
        Ok(modified(root.path().join("a.py"))),
        now + Duration::from_millis(50),
    );
    session.accept_at(
        Ok(modified(root.path().join(".coverage.worker"))),
        now + Duration::from_millis(240),
    );
    assert!(
        session
            .take_batch(now + Duration::from_millis(249))
            .is_none()
    );
    let batch = session
        .take_batch(now + Duration::from_millis(250))
        .expect("quiet batch");
    assert_eq!(batch.generation, 2);
    assert!(batch.full_run);
    assert_eq!(
        batch.paths,
        [root.path().join("a.py"), root.path().join("b.py")]
            .map(|path| normalize(&path).to_string_lossy().into_owned())
    );
}

#[test]
fn running_changes_stay_queued_and_subset_cannot_clear_recovery() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin run");
    let now = Instant::now();
    session.accept_at(Ok(modified(root.path().join("app.py"))), now);
    assert!(session.take_batch(now + Duration::from_secs(1)).is_none());
    assert_eq!(
        session.finish_run(RunOutcome::Failed).expect("finish run"),
        RunCurrency::Obsolete
    );
    assert!(session.take_batch(now + Duration::from_secs(1)).is_some());
    session.begin_run().expect("begin subset");
    session
        .finish_run(RunOutcome::PassedSubset)
        .expect("finish subset");
    assert!(session.requires_full_run());
    session.begin_run().expect("begin recovery");
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("finish full");
    assert!(!session.requires_full_run());
}

#[test]
fn invalidated_runs_queue_recovery_without_pending_events() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin run");
    session.active.as_mut().expect("active run").obsolete = true;
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("obsolete completion");
    assert!(
        session
            .take_batch(Instant::now() + Duration::from_secs(1))
            .expect("recovery batch")
            .full_run
    );
}

#[test]
fn overflow_requires_rescan_but_backend_failure_stops_observation() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    let now = Instant::now();
    session.accept_at(Ok(Event::new(EventKind::Other).set_flag(Flag::Rescan)), now);
    assert!(
        session
            .take_batch(now + Duration::from_secs(1))
            .expect("rescan batch")
            .full_run
    );
    session.begin_run().expect("begin run");
    session.accept_at(Err(notify::Error::generic("backend unavailable")), now);
    assert_eq!(session.state(), WatchState::Stopping);
    assert!(
        session
            .failure()
            .expect("backend error")
            .contains("backend unavailable")
    );
    assert!(session.take_batch(now + Duration::from_secs(2)).is_none());
}

#[test]
fn edit_revert_invalidates_generation_even_with_identical_content() {
    let root = tempfile::tempdir().expect("temporary root");
    std::fs::write(root.path().join("app.py"), "value = 1").expect("source");
    let (mut session, _events) = session(root.path());
    let identity = session.input_identity().expect("read inputs");
    session.begin_run().expect("begin run");
    let now = Instant::now();
    session.accept_at(Ok(modified(root.path().join("app.py"))), now);
    session.accept_at(
        Ok(modified(root.path().join("app.py"))),
        now + Duration::from_millis(1),
    );
    assert_eq!(identity, session.input_identity().expect("read inputs"));
    assert!(!session.is_current().expect("reconcile edit/revert"));
}

#[test]
fn idle_parent_events_ignore_generated_assets_and_detect_source_edits() {
    let root = tempfile::tempdir().expect("temporary root");
    let reports = root.path().join("reports");
    let assets = reports.join("assets");
    std::fs::create_dir_all(&assets).expect("report assets");
    let source = reports.join("app.py");
    std::fs::write(&source, "value = 1\n").expect("source");
    std::fs::write(assets.join("style.css"), "old assets").expect("assets");
    let mut session = WatchSession::new(
        &[root.path().to_path_buf()],
        std::slice::from_ref(&assets),
        None,
    )
    .expect("watch");
    let _events = isolate_events(&mut session);
    session.begin_run().expect("begin baseline run");
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("accept baseline run");

    let now = Instant::now();
    session.accept_at(
        Ok(Event::new(EventKind::Create(CreateKind::File)).add_path(source.clone())),
        now,
    );
    let identity = session
        .input_identity()
        .expect("inputs after delayed create");
    session.reconcile_pending_identity(&identity, now);
    assert!(session.pending.is_empty());
    assert!(!session.requires_full_run());

    std::fs::write(assets.join("style.css"), "new assets").expect("updated assets");
    session.last_idle_reconcile = now
        .checked_sub(Duration::from_millis(DEFAULT_IDLE_RECONCILE_MS))
        .expect("instant supports the reconciliation interval");
    session
        .reconcile_idle_inputs(now)
        .expect("ignore metadata changes inside generated assets");
    assert!(session.pending.is_empty());
    assert!(!session.requires_full_run());

    session.accept_at(
        Ok(modified(reports.clone())),
        now + Duration::from_millis(1),
    );
    let identity = session.input_identity().expect("inputs after assets");
    session.reconcile_pending_identity(&identity, now + Duration::from_millis(1));
    assert!(session.pending.is_empty());
    assert!(!session.requires_full_run());

    std::fs::write(&source, "value = 2\n").expect("updated source");
    session.accept_at(
        Ok(modified(reports.clone())),
        now + Duration::from_millis(2),
    );
    let identity = session.input_identity().expect("inputs after source edit");
    session.reconcile_pending_identity(&identity, now + Duration::from_millis(2));
    let batch = session
        .take_batch(now + Duration::from_secs(1))
        .expect("source change batch");
    assert!(batch.full_run);
    assert_eq!(
        batch.paths,
        [normalize(&reports).to_string_lossy().into_owned()]
    );
}

#[test]
fn idle_empty_directory_creation_requires_a_full_run() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin baseline run");
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("accept baseline run");

    let package = root.path().join("namespace_package");
    std::fs::create_dir(&package).expect("create importable directory");
    let now = Instant::now();
    session.accept_at(
        Ok(Event::new(EventKind::Create(CreateKind::Folder)).add_path(package.clone())),
        now,
    );
    let identity = session
        .input_identity()
        .expect("inputs after directory creation");
    session.reconcile_pending_identity(&identity, now);

    let batch = session
        .take_batch(now + Duration::from_secs(1))
        .expect("directory creation batch");
    assert!(batch.full_run);
    assert_eq!(
        batch.paths,
        [normalize(&package).to_string_lossy().into_owned()]
    );
}

#[test]
fn create_remove_during_active_run_still_invalidates_generation() {
    let root = tempfile::tempdir().expect("temporary root");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin run");
    let transient = root.path().join("transient.py");
    std::fs::write(&transient, "value = 1\n").expect("create transient source");
    session.accept_at(
        Ok(Event::new(EventKind::Create(CreateKind::File)).add_path(transient.clone())),
        Instant::now(),
    );
    std::fs::remove_file(&transient).expect("remove transient source");
    session.accept_at(
        Ok(Event::new(EventKind::Remove(RemoveKind::File)).add_path(transient)),
        Instant::now() + Duration::from_millis(1),
    );

    assert!(!session.is_current().expect("reconcile create/remove"));
    assert_eq!(
        session
            .finish_run(RunOutcome::PassedFull)
            .expect("finish run"),
        RunCurrency::Obsolete
    );
    assert!(session.requires_full_run());
    let identity = session.input_identity().expect("reverted inputs");
    session.reconcile_pending_identity(&identity, Instant::now());
    assert!(session.requires_full_run());
    assert!(
        session
            .take_batch(Instant::now() + Duration::from_secs(1))
            .expect("recovery batch")
            .full_run
    );
}

#[test]
fn idle_metadata_reconciliation_recovers_a_missed_filesystem_event() {
    let root = tempfile::tempdir().expect("temporary root");
    let source = root.path().join("app.py");
    std::fs::write(&source, "value = 1\n").expect("source");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin baseline run");
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("accept baseline run");

    std::fs::write(&source, "value = 2\n").expect("edit without notifying session");
    let now = Instant::now();
    session.last_idle_reconcile = now
        .checked_sub(Duration::from_millis(DEFAULT_IDLE_RECONCILE_MS))
        .expect("instant supports the reconciliation interval");
    session
        .reconcile_idle_inputs(now)
        .expect("reconcile metadata and content");

    let batch = session
        .take_batch(now + Duration::from_secs(1))
        .expect("missed event recovery batch");
    assert!(batch.full_run);
    assert!(batch.paths.is_empty());
    assert!(
        batch
            .reasons
            .iter()
            .any(|reason| reason == "watched inputs changed without a filesystem event")
    );
}

#[test]
fn publication_preflights_all_destinations_before_replacing_evidence() {
    let root = tempfile::tempdir().expect("root");
    let stage = tempfile::tempdir().expect("stage");
    let evidence = root.path().join(".coverage");
    std::fs::write(&evidence, "previous").expect("old evidence");
    std::fs::write(root.path().join("blocked"), "file").expect("blocked directory");
    std::fs::write(stage.path().join("data"), "new").expect("new data");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin run");
    let artifacts = [
        (stage.path().join("data"), evidence.clone()),
        (
            stage.path().join("data"),
            root.path().join("blocked/report.json"),
        ),
    ];
    assert!(session.publish(stage.path(), &artifacts).is_err());
    assert_eq!(
        std::fs::read_to_string(evidence).expect("retained evidence"),
        "previous"
    );
}

#[test]
fn finalization_reconciles_content_and_rolls_back_replaced_files() {
    let root = tempfile::tempdir().expect("root");
    let stage = tempfile::tempdir().expect("stage");
    let source = root.path().join("source.py");
    let evidence = root.path().join(".coverage");
    std::fs::write(&source, "old source").expect("source");
    std::fs::write(&evidence, "old evidence").expect("evidence");
    std::fs::write(stage.path().join("new"), "new").expect("staged");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin run");
    // An output overlapping an input changes the filesystem during finalization,
    // independent of whether notify has delivered that write yet.
    let artifacts = [
        (stage.path().join("new"), evidence.clone()),
        (stage.path().join("new"), source.clone()),
    ];
    assert!(!session.publish(stage.path(), &artifacts).expect("rollback"));
    assert_eq!(
        std::fs::read_to_string(evidence).expect("evidence"),
        "old evidence"
    );
    assert_eq!(
        std::fs::read_to_string(source).expect("source"),
        "old source"
    );
}

#[test]
fn current_publication_preserves_unrelated_report_files() {
    let root = tempfile::tempdir().expect("root");
    let stage = tempfile::tempdir().expect("stage");
    let reports = root.path().join("reports");
    std::fs::create_dir(&reports).expect("reports");
    std::fs::write(reports.join("keep.txt"), "user file").expect("unrelated file");
    std::fs::write(stage.path().join("index.html"), "new report").expect("report");
    let mut session = WatchSession::new(
        &[root.path().to_path_buf()],
        std::slice::from_ref(&reports),
        None,
    )
    .expect("watch");
    let _events = isolate_events(&mut session);
    session.begin_run().expect("begin run");
    assert!(
        session
            .publish(
                stage.path(),
                &[(stage.path().to_path_buf(), reports.clone())],
            )
            .expect("publish")
    );
    assert_eq!(
        std::fs::read_to_string(reports.join("keep.txt")).expect("unrelated"),
        "user file"
    );
    assert_eq!(
        std::fs::read_to_string(reports.join("index.html")).expect("report"),
        "new report"
    );
}

#[test]
fn events_in_the_initial_snapshot_do_not_schedule_duplicate_runs() {
    let root = tempfile::tempdir().expect("root");
    let (mut session, _events) = session(root.path());
    let now = Instant::now();
    session.accept_at(
        Ok(Event::new(EventKind::Create(CreateKind::File)).add_path(root.path().join("app.py"))),
        now,
    );
    session.begin_run().expect("begin run");
    assert!(session.requires_full_run());
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("finish full");
    assert!(!session.requires_full_run());
    assert!(session.take_batch(now + Duration::from_secs(1)).is_none());
}

#[test]
fn active_runs_cannot_overlap_or_complete_twice() {
    let root = tempfile::tempdir().expect("root");
    let (mut session, _events) = session(root.path());
    let request = session.begin_run().expect("begin");
    assert!(session.begin_run().is_err());
    assert_eq!(
        session.active_run_identity().expect("active identity"),
        request.input_identity
    );
    assert_eq!(
        session
            .finish_run(RunOutcome::PassedFull)
            .expect("complete"),
        RunCurrency::Current
    );
    assert!(session.finish_run(RunOutcome::PassedFull).is_err());
    assert!(!session.requires_full_run());
    session.stop();
    assert!(session.begin_run().is_err());
}

#[test]
fn a_reconciled_edit_cannot_be_erased_by_reverting_content() {
    let root = tempfile::tempdir().expect("root");
    let source = root.path().join("app.py");
    std::fs::write(&source, "original").expect("source");
    let (mut session, _events) = session(root.path());
    let request = session.begin_run().expect("begin");
    std::fs::write(&source, "edited").expect("edit");
    assert!(!session.is_current().expect("reconcile"));
    std::fs::write(&source, "original").expect("revert");
    assert_eq!(
        request.input_identity,
        session.input_identity().expect("identity")
    );
    assert_eq!(
        session
            .finish_run(RunOutcome::PassedFull)
            .expect("complete"),
        RunCurrency::Obsolete
    );
    assert!(session.begin_run().expect("recovery").full_run);
}

#[test]
fn finalization_consumes_the_execution_before_another_save() {
    let root = tempfile::tempdir().expect("root");
    let stage = tempfile::tempdir().expect("stage");
    let source = root.path().join("app.py");
    let evidence = root.path().join(".coverage");
    std::fs::write(&source, "original").expect("source");
    std::fs::write(stage.path().join("data"), "accepted evidence").expect("data");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin");
    assert_eq!(
        session
            .finalize(
                stage.path(),
                &[(stage.path().join("data"), evidence.clone())],
                RunOutcome::PassedFull
            )
            .expect("finalize"),
        RunCurrency::Current
    );
    std::fs::write(&source, "next input").expect("edit");
    assert!(session.finish_run(RunOutcome::PassedFull).is_err());
    assert_eq!(
        std::fs::read_to_string(evidence).expect("accepted evidence"),
        "accepted evidence"
    );
}

#[test]
fn destination_aliases_are_rejected_before_publication() {
    let root = tempfile::tempdir().expect("root");
    let stage = tempfile::tempdir().expect("stage");
    let evidence = root.path().join(".coverage");
    std::fs::write(&evidence, "accepted").expect("evidence");
    std::fs::write(stage.path().join("data"), "new").expect("staged");
    let (mut session, _events) = session(root.path());
    session.begin_run().expect("begin");
    let artifacts = [
        (stage.path().join("data"), evidence.clone()),
        (stage.path().join("data"), root.path().join("./.coverage")),
    ];
    assert!(
        session
            .finalize(stage.path(), &artifacts, RunOutcome::PassedFull)
            .is_err()
    );
    assert_eq!(
        std::fs::read_to_string(evidence).expect("evidence"),
        "accepted"
    );
}

#[test]
fn python_inputs_outside_the_git_worktree_require_full_execution() {
    let root = tempfile::tempdir().expect("root");
    let external = tempfile::tempdir().expect("external root");
    std::fs::create_dir(root.path().join(".git")).expect("Git marker");
    let mut session = WatchSession::new(
        &[root.path().to_path_buf(), external.path().to_path_buf()],
        &[],
        None,
    )
    .expect("watch");
    let _events = isolate_events(&mut session);
    let now = Instant::now();
    session.accept_at(Ok(modified(external.path().join("runtime.py"))), now);
    let batch = session
        .take_batch(now + Duration::from_secs(1))
        .expect("batch");
    assert!(batch.full_run);
    assert_eq!(batch.reasons, ["inputs outside the Git worktree changed"]);
    assert!(session.begin_run().expect("begin").full_run);
}

#[test]
fn configuration_reload_does_not_permanently_ignore_former_outputs() {
    let root = tempfile::tempdir().expect("root");
    let former_output = root.path().join("runtime.json");
    std::fs::write(&former_output, "runtime data").expect("input");
    let (mut session, _events) = session(root.path());
    session.set_resolved_ignored(std::slice::from_ref(&former_output));
    let request = session.begin_run().expect("begin");
    session.set_ignored(&[]);
    assert_eq!(
        request.input_identity,
        session.input_identity().expect("retained until resolution")
    );
    session.set_resolved_ignored(&[]);
    assert!(!session.is_current().expect("newly observed input"));
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("obsolete completion");
    session.begin_run().expect("recovery");
    let now = Instant::now();
    session.accept_at(Ok(modified(former_output)), now);
    assert_eq!(
        session
            .finish_run(RunOutcome::PassedFull)
            .expect("changed runtime"),
        RunCurrency::Obsolete
    );
}

#[test]
fn removed_source_roots_no_longer_participate_in_input_identity() {
    let root = tempfile::tempdir().expect("root");
    let external = tempfile::tempdir().expect("external");
    let external_path = external.path().to_path_buf();
    let mut session = WatchSession::new(
        &[root.path().to_path_buf(), external_path.clone()],
        &[],
        None,
    )
    .expect("watch");
    external.close().expect("remove obsolete source root");
    session
        .begin_run()
        .expect("capture a deleted input before configuration reload");
    session
        .set_roots(&[root.path().to_path_buf()])
        .expect("remove obsolete root");
    session
        .finish_run(RunOutcome::PassedFull)
        .expect("finish changed scope");
    let identity = session.input_identity().expect("current roots");
    std::fs::create_dir(&external_path).expect("recreate former root");
    std::fs::write(external_path.join("input.py"), "ignored former root").expect("external input");
    assert_eq!(
        identity,
        session.input_identity().expect("current identity")
    );
    std::fs::remove_dir_all(external_path).expect("clean former root");
}

#[cfg(target_os = "linux")]
#[test]
fn overlapping_root_replacements_keep_edit_revert_events() {
    for expand in [false, true] {
        let root = tempfile::tempdir().expect("external root");
        // Expect resolved paths, as on macOS where temporary paths are symlinks.
        let root_path = crate::path::absolute_path(root.path());
        let package = root_path.join("pkg");
        std::fs::create_dir(&package).expect("package");
        let source = package.join("app.py");
        std::fs::write(&source, "VALUE = 1\n").expect("original source");
        let (old, new) = if expand {
            (package.as_path(), root_path.as_path())
        } else {
            (root_path.as_path(), package.as_path())
        };
        let mut session = WatchSession::new(&[old.to_path_buf()], &[], None)
            .expect("register temporary watch root");
        session
            .set_roots(&[new.to_path_buf()])
            .expect("replace root");
        session.begin_run().expect("begin run");
        std::fs::write(&source, "VALUE = 2\n").expect("edit source");
        std::fs::write(&source, "VALUE = 1\n").expect("revert source");
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let event = session
                .receiver
                .recv_timeout(deadline.saturating_duration_since(Instant::now()))
                .expect("replacement root must deliver source events");
            // Capturing inputs can enqueue access events before the edit notification.
            let is_source = event.as_ref().is_ok_and(|event| {
                event.paths.contains(&source)
                    && matches!(event.kind, EventKind::Modify(ModifyKind::Data(_)))
            });
            session.accept_at(event, Instant::now());
            if is_source {
                break;
            }
        }
        assert_eq!(
            session
                .finish_run(RunOutcome::PassedFull)
                .expect("finish run"),
            RunCurrency::Obsolete
        );
    }
}

#[cfg(target_os = "linux")]
#[test]
fn only_missing_inotify_descriptors_are_accepted_as_unwatched() {
    let root = tempfile::tempdir().expect("root");
    let removed = root.path().join("removed");
    let invalid_descriptor = |path: &Path| {
        notify::Error::io(io::Error::from_raw_os_error(22)).add_path(path.to_path_buf())
    };
    assert!(already_unwatched(&invalid_descriptor(&removed)));
    assert!(!already_unwatched(&invalid_descriptor(root.path())));
    assert!(!already_unwatched(&notify::Error::io(
        io::Error::from_raw_os_error(22)
    )));
    assert!(!already_unwatched(
        &notify::Error::io(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid watcher"
        ))
        .add_path(removed.clone())
    ));
    assert!(!already_unwatched(
        &notify::Error::io(io::Error::from(io::ErrorKind::PermissionDenied)).add_path(removed)
    ));
}

#[test]
fn generated_names_above_a_watch_root_do_not_hide_inputs() {
    let parent = tempfile::tempdir().expect("temporary parent");
    let root = parent.path().join("target").join(".cache").join("project");
    std::fs::create_dir_all(root.join("__pycache__")).expect("project layout");
    std::fs::write(root.join("module.py"), "value = 1\n").expect("source");
    std::fs::write(root.join("__pycache__").join("module.pyc"), "one").expect("bytecode");
    let (mut session, _events) = session(&root);
    let before = session.input_identity().expect("initial identity");
    std::fs::write(root.join("module.py"), "value = 2\n").expect("edit source");
    assert_ne!(session.input_identity().expect("edited identity"), before);
    std::fs::write(root.join("__pycache__").join("module.pyc"), "two").expect("edit bytecode");
    let edited = session.input_identity().expect("bytecode identity");
    std::fs::write(root.join("__pycache__").join("module.pyc"), "three").expect("edit bytecode");
    assert_eq!(session.input_identity().expect("final identity"), edited);

    let now = Instant::now();
    session.accept_at(Ok(modified(normalize(&root.join("module.py")))), now);
    assert_eq!(session.pending.len(), 1);
    session.accept_at(
        Ok(modified(normalize(
            &root.join("__pycache__").join("module.pyc"),
        ))),
        now,
    );
    assert_eq!(session.pending.len(), 1);
}
