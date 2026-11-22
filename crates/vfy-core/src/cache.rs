//! Cache directory removal that refuses symbolic links and junctions.

use std::fs;
use std::io;
use std::path::Path;

/// The outcome of removing a cache directory.
#[derive(Debug, PartialEq, Eq)]
pub enum CacheCleanup {
    /// The directory did not exist.
    Missing,
    /// The directory and its contents were removed.
    Removed,
}

/// Remove a cache directory without following a link at its root or in its contents.
///
/// # Errors
/// Returns an error if the root is a link or non-directory, or deletion fails.
/// Links inside the directory are removed without following them. A failed
/// deletion may leave the directory partially removed.
pub fn clean_cache(path: &Path) -> io::Result<CacheCleanup> {
    // Inspect the original entry: canonicalization would hide a root symlink.
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(CacheCleanup::Missing),
        Err(error) => return Err(error),
    };
    if is_link(&metadata) || !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "cache path must be a directory, not a symlink or junction",
        ));
    }
    fs::remove_dir_all(path)?;
    Ok(CacheCleanup::Removed)
}

fn is_link(metadata: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // FILE_ATTRIBUTE_REPARSE_POINT includes directory junctions.
        metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

#[cfg(test)]
mod tests;
