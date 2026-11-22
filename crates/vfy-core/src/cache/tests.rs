use std::fs;

use super::{CacheCleanup, clean_cache};

#[test]
fn cleanup_preserves_sibling_artifacts_and_rejects_files() {
    let directory = tempfile::tempdir().expect("temporary project");
    let cache = directory.path().join(".vfy");
    let coverage = directory.path().join(".coverage");
    fs::write(&coverage, b"coverage").expect("coverage sentinel");
    assert_eq!(
        clean_cache(&cache).expect("missing cache"),
        CacheCleanup::Missing
    );
    fs::write(&cache, b"not a directory").expect("file at cache path");
    assert!(clean_cache(&cache).is_err());
    assert_eq!(
        fs::read(&cache).expect("unchanged file"),
        b"not a directory"
    );
    fs::remove_file(&cache).expect("remove file fixture");
    fs::create_dir_all(cache.join("nested")).expect("cache directory");
    fs::write(cache.join("nested/history.json"), b"{}").expect("cached history");
    assert_eq!(
        clean_cache(&cache).expect("clean directory"),
        CacheCleanup::Removed
    );
    assert!(!cache.exists());
    assert_eq!(fs::read(coverage).expect("coverage remains"), b"coverage");
}

#[cfg(unix)]
#[test]
fn cleanup_rejects_root_links_and_does_not_follow_nested_links() {
    use std::os::unix::fs::symlink;

    let directory = tempfile::tempdir().expect("temporary project");
    let cache = directory.path().join(".vfy");
    let target = directory.path().join("outside");
    fs::create_dir(&target).expect("external directory");
    fs::write(target.join("sentinel"), b"keep").expect("external sentinel");
    symlink(&target, &cache).expect("root cache symlink");
    assert!(clean_cache(&cache).is_err());
    assert!(
        fs::symlink_metadata(&cache)
            .expect("link remains")
            .file_type()
            .is_symlink()
    );
    fs::remove_file(&cache).expect("remove fixture link");
    fs::create_dir(&cache).expect("cache directory");
    symlink(&target, cache.join("nested")).expect("nested link");
    clean_cache(&cache).expect("remove cache containing link");
    assert_eq!(
        fs::read(target.join("sentinel")).expect("external sentinel remains"),
        b"keep"
    );
    symlink(directory.path().join("missing"), &cache).expect("dangling root link");
    assert!(clean_cache(&cache).is_err());
}
