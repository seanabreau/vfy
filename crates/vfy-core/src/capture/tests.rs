use super::CaptureReader;
use std::io::{Seek, SeekFrom, Write};

#[test]
fn reads_preserve_writer_position_and_offsets_across_streams() {
    let mut stdout = tempfile::tempfile().expect("create stdout capture file");
    let mut stderr = tempfile::tempfile().expect("create stderr capture file");
    stdout.write_all(b"first").expect("write stdout");
    stderr.write_all(b"error").expect("write stderr");
    let mut reader = CaptureReader::default();
    let mut stdout_offset = 0;
    let mut stderr_offset = 0;

    assert_eq!(
        reader
            .read(&stdout, &mut stdout_offset)
            .expect("read stdout"),
        "first"
    );
    assert_eq!(stdout.stream_position().expect("read writer position"), 5);
    assert_eq!(
        reader
            .read(&stderr, &mut stderr_offset)
            .expect("read stderr"),
        "error"
    );
    stdout.write_all(b" second").expect("append stdout");
    assert_eq!(
        reader
            .read(&stdout, &mut stdout_offset)
            .expect("read new stdout"),
        " second"
    );
    assert!(
        reader
            .read(&stdout, &mut stdout_offset)
            .expect("read empty stdout")
            .is_empty()
    );
    assert!(
        reader
            .read(&stderr, &mut stderr_offset)
            .expect("read empty stderr")
            .is_empty()
    );
}

#[test]
fn reads_preserve_a_writer_cursor_inside_the_file() {
    let mut file = tempfile::tempfile().expect("create capture file");
    file.write_all(b"captured output")
        .expect("write capture output");
    file.seek(SeekFrom::Start(3))
        .expect("position writer inside capture file");
    let mut reader = CaptureReader::default();
    let mut offset = 0;

    assert_eq!(
        reader
            .read(&file, &mut offset)
            .expect("read capture output"),
        "captured output"
    );
    assert_eq!(file.stream_position().expect("read writer position"), 3);
}

#[test]
fn snapshots_decode_complete_unicode_and_reset_shared_writer_position() {
    let mut file = tempfile::tempfile().expect("create capture file");
    let mut writer = file.try_clone().expect("duplicate capture writer");
    let mut payload = vec![b'a'; 65_535];
    payload.extend_from_slice("é".as_bytes());
    payload.extend_from_slice(&[0xff, b'\n']);
    file.write_all(&payload).expect("write captured bytes");
    let mut reader = CaptureReader::default();
    let mut offset = 0;
    let output = reader
        .snapshot(&file, &mut offset)
        .expect("snapshot captured bytes");

    assert_eq!(output, String::from_utf8_lossy(&payload));
    assert_eq!(offset, 0);
    assert_eq!(file.stream_position().expect("read writer position"), 0);
    assert_eq!(
        writer
            .stream_position()
            .expect("read duplicated writer position"),
        0
    );
    assert_eq!(file.metadata().expect("read capture metadata").len(), 0);
    writer.write_all(b"next phase").expect("write next phase");
    assert_eq!(
        reader
            .snapshot(&file, &mut offset)
            .expect("snapshot next phase"),
        "next phase"
    );
}

#[test]
fn a_failed_read_leaves_the_offset_so_a_retry_returns_every_byte() {
    let mut reader = CaptureReader::default();
    let mut offset = 4;
    let mut calls = 0;
    let error = reader
        .read_with(&mut offset, |buffer, _| {
            calls += 1;
            if calls == 1 {
                buffer[..3].copy_from_slice(b"abc");
                Ok(3)
            } else {
                Err(std::io::Error::other("injected"))
            }
        })
        .expect_err("second chunk fails");

    assert_eq!(error.to_string(), "injected");
    assert_eq!(offset, 4);

    let mut file = tempfile::tempfile().expect("create capture file");
    file.write_all(b"skipabc").expect("write capture file");
    assert_eq!(reader.read(&file, &mut offset).expect("retry read"), "abc");
}

#[test]
fn a_failed_snapshot_truncate_preserves_unread_output() {
    let capture = tempfile::NamedTempFile::new().expect("create capture file");
    std::fs::write(capture.path(), b"first second").expect("write capture file");
    let read_only = std::fs::File::open(capture.path()).expect("open read-only capture");
    let mut reader = CaptureReader::default();
    let mut offset = 6;

    assert!(reader.snapshot(&read_only, &mut offset).is_err());
    assert_eq!(offset, 6);
    assert_eq!(
        reader.read(&read_only, &mut offset).expect("retry"),
        "second"
    );
}
