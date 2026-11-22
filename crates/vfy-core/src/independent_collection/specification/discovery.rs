//! Filesystem traversal with typed frontend collection callbacks.

use super::{CollectionArgumentPolicy, CollectionSpecification, absolute, selected_paths};
use crate::independent_collection::CollectionFile;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

/// A directory or regular file encountered before naming and recursion filters.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CollectionEntryKind {
    Directory,
    File,
}

/// Canonical ancestors of one directory traversal, preserving symlink spelling.
#[derive(Clone, Debug, Default)]
pub struct DirectoryLineage {
    identities: Vec<PathBuf>,
}

/// An accepted immediate child before any recursive collection takes place.
#[derive(Debug)]
pub struct DirectoryEntry {
    pub path: PathBuf,
    pub kind: CollectionEntryKind,
}

/// The immediate children and ancestry shared by eager and live collection.
#[derive(Debug)]
pub struct DirectoryBatch {
    pub lineage: DirectoryLineage,
    pub entries: Vec<DirectoryEntry>,
}

/// The builtin policy is provided separately so firstresult dispatch can override it.
pub struct CollectionCandidate<'a> {
    pub path: &'a Path,
    pub kind: CollectionEntryKind,
    pub default_ignored: bool,
}

/// Callbacks required to decide one directory's immediate children.
pub trait DirectoryPolicy {
    type Error;
    /// Load the active directory context before enumerating entries.
    ///
    /// # Errors
    /// Returns the original context-loading error.
    fn enter_directory(&mut self, path: &Path) -> Result<(), Self::Error>;
    /// Decide a non-explicit candidate's ignore policy.
    ///
    /// # Errors
    /// Returns the original callback error.
    fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> Result<bool, Self::Error>;
    /// Apply live naming policy to an accepted non-explicit file.
    ///
    /// # Errors
    /// Returns the original callback error.
    fn accept_file(&mut self, path: &Path, default_matches: bool) -> Result<bool, Self::Error>;
}

/// Frontend operations preserve collection order and their original error values.
pub trait CollectionVisitor {
    type Error;
    /// Load the directory's active collection context before enumerating entries.
    ///
    /// # Errors
    /// Returns the frontend's original context-loading error.
    fn enter_directory(&mut self, path: &Path) -> Result<(), Self::Error>;
    /// Release successful directory-local state after consuming its children.
    fn leave_directory(&mut self, _path: &Path) {}
    /// Dispatch the frontend's ignore policy for a non-explicit candidate.
    ///
    /// # Errors
    /// Returns the frontend's original hook error.
    fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> Result<bool, Self::Error>;
    /// Collect one selected file with all of its normalized selectors.
    ///
    /// # Errors
    /// Returns the frontend's original collection error.
    fn collect_file(&mut self, file: &CollectionFile) -> Result<(), Self::Error>;
    /// Record a directory failure, or propagate it when collection must stop.
    ///
    /// # Errors
    /// Returns an unhandled failure without converting the visitor error.
    fn collection_error(
        &mut self,
        _path: &Path,
        error: DiscoveryError<Self::Error>,
    ) -> Result<(), DiscoveryError<Self::Error>> {
        Err(error)
    }
    /// Apply live naming policy after ignore hooks have declined a file.
    ///
    /// # Errors
    /// Returns the frontend's original naming-policy error.
    fn accept_file(&mut self, _path: &Path, default_matches: bool) -> Result<bool, Self::Error> {
        Ok(default_matches)
    }
}

impl<V: CollectionVisitor> DirectoryPolicy for V {
    type Error = V::Error;

    fn enter_directory(&mut self, path: &Path) -> Result<(), Self::Error> {
        CollectionVisitor::enter_directory(self, path)
    }

    fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> Result<bool, Self::Error> {
        CollectionVisitor::ignore(self, candidate)
    }

    fn accept_file(&mut self, path: &Path, default_matches: bool) -> Result<bool, Self::Error> {
        CollectionVisitor::accept_file(self, path, default_matches)
    }
}

/// Filesystem failures and callback errors remain distinct at the frontend boundary.
#[derive(Debug)]
pub enum DiscoveryError<E> {
    CollectionArenaRequired,
    Filesystem {
        path: PathBuf,
        source: std::io::Error,
    },
    RecursiveDirectory(PathBuf),
    Visitor(E),
}

impl<E: fmt::Display> fmt::Display for DiscoveryError<E> {
    fn fmt(&self, output: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CollectionArenaRequired => {
                output.write_str("keep-duplicates requires live collector identities")
            }
            Self::Filesystem { path, source } => write!(output, "{}: {source}", path.display()),
            Self::RecursiveDirectory(path) => write!(
                output,
                "recursive symlink directory during test discovery: {}",
                path.display()
            ),
            Self::Visitor(error) => error.fmt(output),
        }
    }
}

impl CollectionSpecification {
    /// Visit candidates and collect files after their parent directory's ignore calls.
    ///
    /// Each directory first decides all immediate entries, then consumes those entries
    /// in sorted order. Explicit normalized targets bypass ignore calls on themselves.
    ///
    /// # Errors
    /// Returns an unreadable path, directory cycle, or the original visitor error.
    pub fn walk<V: CollectionVisitor>(
        &self,
        visitor: &mut V,
    ) -> Result<Vec<CollectionFile>, DiscoveryError<V::Error>> {
        if self.argument_policy == CollectionArgumentPolicy::Keep {
            return Err(DiscoveryError::CollectionArenaRequired);
        }
        let mut files = Vec::new();
        let mut indexes = BTreeMap::new();
        for target in &self.targets {
            if target.path.is_dir() {
                self.walk_directory(
                    &target.path,
                    &mut Vec::new(),
                    &mut files,
                    &mut indexes,
                    visitor,
                )?;
            } else {
                self.visit_file(&target.path, &mut files, &mut indexes, visitor)?;
            }
        }
        Ok(files)
    }

    fn visit_file<V: CollectionVisitor>(
        &self,
        path: &Path,
        files: &mut Vec<CollectionFile>,
        indexes: &mut BTreeMap<PathBuf, usize>,
        visitor: &mut V,
    ) -> Result<(), DiscoveryError<V::Error>> {
        if indexes.contains_key(path) {
            return Ok(());
        }
        let mut selectors = Vec::new();
        for target in self.targets.iter().filter(|target| target.path == path) {
            if target.selector.is_empty() {
                selectors.clear();
                break;
            }
            if !selectors.contains(&target.selector) {
                selectors.push(target.selector.clone());
            }
        }
        indexes.insert(path.to_path_buf(), files.len());
        files.push(CollectionFile {
            path: path.to_path_buf(),
            nodeid: self.nodeid(path),
            selectors,
        });
        visitor
            .collect_file(
                files
                    .last()
                    .expect("collection file pushed above is the last element"),
            )
            .map_err(DiscoveryError::Visitor)
    }

    fn walk_directory<V: CollectionVisitor>(
        &self,
        directory: &Path,
        ancestors: &mut Vec<PathBuf>,
        files: &mut Vec<CollectionFile>,
        indexes: &mut BTreeMap<PathBuf, usize>,
        visitor: &mut V,
    ) -> Result<(), DiscoveryError<V::Error>> {
        let depth = ancestors.len();
        let result = self.walk_directory_inner(directory, ancestors, files, indexes, visitor);
        ancestors.truncate(depth);
        match result {
            Ok(()) => {
                visitor.leave_directory(directory);
                Ok(())
            }
            Err(error) => visitor.collection_error(directory, error),
        }
    }

    fn walk_directory_inner<V: CollectionVisitor>(
        &self,
        directory: &Path,
        ancestors: &mut Vec<PathBuf>,
        files: &mut Vec<CollectionFile>,
        indexes: &mut BTreeMap<PathBuf, usize>,
        visitor: &mut V,
    ) -> Result<(), DiscoveryError<V::Error>> {
        let batch = self.directory_children(
            directory,
            &DirectoryLineage {
                identities: ancestors.clone(),
            },
            visitor,
        )?;
        *ancestors = batch.lineage.identities;
        for DirectoryEntry { path, kind } in batch.entries {
            match kind {
                CollectionEntryKind::Directory => {
                    self.walk_directory(&path, ancestors, files, indexes, visitor)?;
                }
                CollectionEntryKind::File => self.visit_file(&path, files, indexes, visitor)?,
            }
        }
        ancestors.pop();
        Ok(())
    }

    /// Decide immediate children using the same policy as recursive discovery.
    ///
    /// All immediate ignore and naming callbacks precede any child collection.
    /// This operation does not recurse or invoke `collect_file`/`leave_directory`.
    ///
    /// # Errors
    /// Returns the original filesystem, cycle, or callback failure.
    pub fn directory_children<V: DirectoryPolicy>(
        &self,
        directory: &Path,
        parent: &DirectoryLineage,
        visitor: &mut V,
    ) -> Result<DirectoryBatch, DiscoveryError<V::Error>> {
        let failure = |source| DiscoveryError::Filesystem {
            path: directory.to_path_buf(),
            source,
        };
        let identity = fs::canonicalize(directory).map_err(failure)?;
        if parent.identities.contains(&identity) {
            return Err(DiscoveryError::RecursiveDirectory(directory.to_path_buf()));
        }
        let mut lineage = parent.clone();
        lineage.identities.push(identity);
        visitor
            .enter_directory(directory)
            .map_err(DiscoveryError::Visitor)?;
        let mut entries = fs::read_dir(directory)
            .map_err(failure)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(failure)?;
        entries.sort_by_key(fs::DirEntry::file_name);
        let mut accepted = Vec::new();
        let initial: BTreeSet<_> = self
            .targets
            .iter()
            .map(|target| target.path.clone())
            .collect();
        for entry in entries {
            let path = entry.path();
            let kind = if path.is_dir() {
                CollectionEntryKind::Directory
            } else if path.is_file() {
                CollectionEntryKind::File
            } else {
                continue;
            };
            let explicit = match kind {
                CollectionEntryKind::Directory => {
                    initial.iter().any(|target| target.starts_with(&path))
                }
                CollectionEntryKind::File => initial.contains(&path),
            };
            if !explicit
                && visitor
                    .ignore(&CollectionCandidate {
                        path: &path,
                        kind,
                        default_ignored: self.exclusions.is_ignored(&path),
                    })
                    .map_err(DiscoveryError::Visitor)?
            {
                continue;
            }
            let accepted_file = kind == CollectionEntryKind::File
                && (explicit
                    || visitor
                        .accept_file(
                            &path,
                            path.extension().is_some_and(|extension| extension == "py")
                                && self
                                    .python_files
                                    .iter()
                                    .any(|pattern| pattern.matches_path(&path)),
                        )
                        .map_err(DiscoveryError::Visitor)?);
            if kind == CollectionEntryKind::Directory || accepted_file {
                accepted.push(DirectoryEntry { path, kind });
            }
        }
        Ok(DirectoryBatch {
            lineage,
            entries: accepted,
        })
    }
}

/// Plan initial anchors and their immediate test* directories before imports.
///
/// Preserve argument order and redundant anchors. The loader checks conftest
/// file existence separately as each directory's imports run.
///
/// # Errors
/// Returns an unresolved invocation path or an unreadable immediate directory.
pub fn initial_conftest_anchors(
    root: &Path,
    invocation_dir: &Path,
    arguments: &[String],
    testpaths: &[String],
) -> Result<Vec<PathBuf>, String> {
    let invocation_dir = absolute(invocation_dir, Path::new(""))?;
    let root = absolute(&invocation_dir, root)?;
    let (arguments, _) = selected_paths(&root, &invocation_dir, arguments, testpaths)?;
    let mut anchors = arguments
        .iter()
        .map(|argument| {
            absolute(
                &invocation_dir,
                Path::new(
                    argument
                        .split_once("::")
                        .map_or(argument.as_str(), |(path, _)| path),
                ),
            )
        })
        .collect::<Result<Vec<_>, _>>()?;
    anchors.retain(|anchor| anchor.exists());
    if anchors.is_empty() {
        anchors.push(invocation_dir);
    }
    let mut initial = Vec::new();
    for anchor in anchors {
        initial.push(anchor.clone());
        if anchor.is_dir() {
            for entry in
                fs::read_dir(&anchor).map_err(|error| format!("{}: {error}", anchor.display()))?
            {
                let entry = entry.map_err(|error| format!("{}: {error}", anchor.display()))?;
                if entry.file_name().to_string_lossy().starts_with("test") && entry.path().is_dir()
                {
                    initial.push(entry.path());
                }
            }
        }
    }
    Ok(initial)
}

#[cfg(test)]
mod tests;
