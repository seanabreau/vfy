//! Filesystem discovery for the dependency-independent test frontend.

mod basetemp;
mod keyword;
mod specification;
pub use basetemp::{prepare_basetemp, validate_basetemp};
pub use keyword::{KeywordExpression, MarkerExpression};
pub use specification::{
    CollectionArgumentPolicy, CollectionCandidate, CollectionEntryKind, CollectionExclusions,
    CollectionIgnoreSettings, CollectionSettings, CollectionSpecification, CollectionTarget,
    CollectionVisitor, ConftestPathValue, DirectoryBatch, DirectoryEntry, DirectoryLineage,
    DirectoryPolicy, DiscoveryError, SelectorCursor, initial_conftest_anchors, matches_python_file,
    matches_python_name,
};

use std::path::{Path, PathBuf};

/// A test file and explicit node selectors attached to that file.
#[derive(Debug, Eq, PartialEq)]
pub struct CollectionFile {
    pub path: PathBuf,
    pub nodeid: String,
    pub selectors: Vec<String>,
}

/// Discover test files using pytest's default naming and recursion policy.
///
/// Explicit files need not match the directory discovery naming convention.
/// Explicit target order and symlink spellings are retained.
///
/// # Errors
/// Returns an error for missing targets or unreadable directories.
pub fn discover(root: &Path, targets: &[String]) -> Result<Vec<CollectionFile>, String> {
    CollectionSpecification::defaults(root, targets)?.discover()
}

/// Return relevant directory ancestors without crossing above the config cutoff.
#[must_use]
pub fn collection_directories(cutoff: &Path, directory: &Path) -> Vec<PathBuf> {
    let mut directories = Vec::new();
    for current in directory.ancestors() {
        if current != cutoff && cutoff.starts_with(current) {
            break;
        }
        directories.push(current.to_path_buf());
    }
    directories.reverse();
    directories
}

/// Resolve a filesystem collector ID before the public parent/name fallback.
#[must_use]
pub fn filesystem_nodeid(root: &Path, initial_paths: &[PathBuf], path: &Path) -> Option<String> {
    if let Ok(relative) = path.strip_prefix(root) {
        return Some(if relative.as_os_str().is_empty() {
            ".".to_owned()
        } else {
            relative.to_string_lossy().into_owned()
        });
    }
    if initial_paths.iter().any(|initial| initial == path) {
        return Some(String::new());
    }
    path.ancestors().skip(1).find_map(|parent| {
        initial_paths
            .iter()
            .any(|initial| initial == parent)
            .then(|| path.strip_prefix(parent).ok())
            .flatten()
            .map(|relative| relative.to_string_lossy().into_owned())
    })
}

/// Determine conftest imports while excluding ancestors of the configured cutoff.
#[must_use]
pub fn conftests(cutoff: &Path, file: &Path) -> Vec<PathBuf> {
    file.parent().map_or_else(Vec::new, |directory| {
        collection_directories(cutoff, directory)
            .into_iter()
            .map(|directory| directory.join("conftest.py"))
            .filter(|path| path.is_file())
            .collect()
    })
}

#[cfg(test)]
mod duplicates_tests;
#[cfg(test)]
mod tests;
