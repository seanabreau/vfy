//! Lexical nodeid paths and existing canonical diagnostics share one policy.

#[cfg(unix)]
#[test]
fn symlink_nodeid_spelling_and_canonical_diagnostic_remain_distinct() {
    let directory = tempfile::tempdir().expect("create path contract directory");
    let suite = directory.path().join("suite");
    std::fs::create_dir(&suite).expect("create suite directory");
    let source = suite.join("test_source.py");
    std::fs::write(&source, "def test_value(): pass\n").expect("write source file");
    let alias = suite.join("test_alias.py");
    std::os::unix::fs::symlink("test_source.py", &alias).expect("create lexical alias");

    assert_eq!(
        super::relative_path(directory.path(), &alias),
        std::path::Path::new("suite/test_alias.py")
    );
    assert_eq!(
        super::relative_location(directory.path(), &alias),
        "suite/test_source.py"
    );
}
