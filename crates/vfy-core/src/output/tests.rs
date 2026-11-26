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

#[cfg(unix)]
fn mode(path: &Path) -> u32 {
    use std::os::unix::fs::PermissionsExt as _;
    std::fs::metadata(path)
        .expect("metadata")
        .permissions()
        .mode()
        & 0o7777
}

#[cfg(unix)]
#[test]
fn write_through_a_symlink_chain_replaces_the_final_target_and_keeps_its_mode() {
    use std::os::unix::fs::{PermissionsExt as _, symlink};

    let root = tempfile::tempdir().expect("root");
    let target = root.path().join("target.json");
    std::fs::write(&target, "old").expect("target");
    std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o604)).expect("mode");
    symlink("target.json", root.path().join("middle")).expect("middle link");
    let output = root.path().join("output.json");
    symlink("middle", &output).expect("output link");

    atomic_write(&output, b"new").expect("write through links");

    for link in [&output, &root.path().join("middle")] {
        assert!(
            std::fs::symlink_metadata(link)
                .expect("link")
                .file_type()
                .is_symlink()
        );
    }
    assert_eq!(std::fs::read(&target).expect("target"), b"new");
    assert_eq!(mode(&target), 0o604);
}

#[cfg(unix)]
#[test]
fn dangling_symlink_creates_its_target_only_in_an_existing_directory() {
    use std::os::unix::fs::symlink;

    let root = tempfile::tempdir().expect("root");
    let output = root.path().join("output.json");
    symlink("created.json", &output).expect("dangling link");
    atomic_write(&output, b"new").expect("create link target");
    assert_eq!(
        std::fs::read(root.path().join("created.json")).expect("target"),
        b"new"
    );
    assert!(
        std::fs::symlink_metadata(&output)
            .expect("link")
            .file_type()
            .is_symlink()
    );

    // Like `open(path, "w")`, writing does not create a link target's directory.
    let missing = root.path().join("missing.json");
    symlink("absent/target.json", &missing).expect("link into a missing directory");
    let error = atomic_write(&missing, b"new").expect_err("target directory is missing");
    assert_eq!(error.kind(), io::ErrorKind::NotFound);
    assert!(!root.path().join("absent").exists());
}

#[cfg(unix)]
#[test]
fn replacing_a_regular_file_keeps_its_mode() {
    use std::os::unix::fs::PermissionsExt as _;

    let root = tempfile::tempdir().expect("root");
    let output = root.path().join("report.xml");
    std::fs::write(&output, "old").expect("existing report");
    std::fs::set_permissions(&output, std::fs::Permissions::from_mode(0o640)).expect("mode");

    atomic_write(&output, b"new").expect("replace report");

    assert_eq!(std::fs::read(&output).expect("report"), b"new");
    assert_eq!(mode(&output), 0o640);
}

#[cfg(unix)]
#[test]
fn a_fifo_is_written_in_place() {
    use std::os::unix::fs::{FileTypeExt as _, OpenOptionsExt as _};

    #[cfg(target_os = "linux")]
    const NONBLOCK: i32 = 0o4000;
    #[cfg(not(target_os = "linux"))]
    const NONBLOCK: i32 = 0x0004;

    let root = tempfile::tempdir().expect("root");
    let fifo = root.path().join("report.json");
    let status = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .expect("run mkfifo");
    assert!(status.success());
    // A nonblocking reader lets the writer open the FIFO from this thread.
    let mut reader = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(NONBLOCK)
        .open(&fifo)
        .expect("open reader");

    atomic_write(&fifo, b"streamed").expect("write FIFO");

    let mut content = Vec::new();
    reader.read_to_end(&mut content).expect("read FIFO");
    assert_eq!(content, b"streamed");
    assert!(
        std::fs::symlink_metadata(&fifo)
            .expect("FIFO")
            .file_type()
            .is_fifo()
    );
}

#[test]
fn a_directory_destination_is_rejected_unchanged() {
    let root = tempfile::tempdir().expect("root");
    let output = root.path().join("report");
    std::fs::create_dir(&output).expect("directory");
    std::fs::write(output.join("kept"), "kept").expect("directory content");

    assert!(atomic_write(&output, b"new").is_err());

    assert_eq!(
        std::fs::read(output.join("kept")).expect("directory kept"),
        b"kept"
    );
    assert_eq!(
        std::fs::read_dir(root.path()).expect("root").count(),
        1,
        "no temporary file remains"
    );
}
