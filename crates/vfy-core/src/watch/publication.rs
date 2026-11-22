//! Preflight all artifacts, publish atomically per file, and restore on failure.
use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use crate::output::{PreparedFile, PublishedFile, prepare, restore};

use super::{RunCurrency, RunOutcome, WatchSession};

fn expand(
    stage: &Path,
    source: &Path,
    destination: &Path,
    files: &mut BTreeMap<PathBuf, PathBuf>,
) -> io::Result<()> {
    let metadata = std::fs::symlink_metadata(source)?;
    if metadata.file_type().is_symlink() || !source.canonicalize()?.starts_with(stage) {
        return Err(io::Error::other(format!(
            "staged artifact escapes run directory: {}",
            source.display()
        )));
    }
    if metadata.is_dir() {
        let mut children = std::fs::read_dir(source)?.collect::<io::Result<Vec<_>>>()?;
        children.sort_by_key(std::fs::DirEntry::file_name);
        for child in children {
            expand(
                stage,
                &child.path(),
                &destination.join(child.file_name()),
                files,
            )?;
        }
    } else if metadata.is_file() {
        let destination = crate::absolute_path(destination);
        if files
            .insert(destination.clone(), source.to_path_buf())
            .is_some()
        {
            return Err(io::Error::other(format!(
                "conflicting publication destination: {}",
                destination.display()
            )));
        }
    } else {
        return Err(io::Error::other("staged artifact is not a regular file"));
    }
    Ok(())
}

impl WatchSession {
    /// Publish staged artifacts and complete the active run.
    /// Edits after that point belong to the next run.
    ///
    /// # Errors
    /// Publication errors retain the active run so the caller can submit failure.
    pub fn finalize(
        &mut self,
        stage: &Path,
        artifacts: &[(PathBuf, PathBuf)],
        outcome: RunOutcome,
    ) -> io::Result<RunCurrency> {
        let currency = if self.publish(stage, artifacts)? {
            RunCurrency::Current
        } else {
            RunCurrency::Obsolete
        };
        self.complete(outcome, currency)
    }

    /// Publish artifacts for the sole active run, retaining prior files on disk.
    ///
    /// # Errors
    /// Validation and preparation finish before any destination is replaced.
    /// After a commit or reconciliation failure, every rollback is attempted;
    /// failures include both the original error and unsuccessful restorations.
    pub(super) fn publish(
        &mut self,
        stage: &Path,
        artifacts: &[(PathBuf, PathBuf)],
    ) -> io::Result<bool> {
        if !self.is_current()? {
            return Ok(false);
        }
        let stage = stage.canonicalize()?;
        let mut files = BTreeMap::new();
        for (source, destination) in artifacts {
            if !destination.is_absolute() {
                return Err(io::Error::other("publication destination must be absolute"));
            }
            expand(&stage, source, destination, &mut files)?;
        }
        for destination in files.keys() {
            if destination.starts_with(&stage)
                || destination
                    .ancestors()
                    .skip(1)
                    .any(|parent| files.contains_key(parent))
            {
                return Err(io::Error::other(format!(
                    "overlapping publication destination: {}",
                    destination.display()
                )));
            }
        }
        let mut prepared = Vec::with_capacity(files.len());
        for (destination, source) in files {
            let parent = destination
                .parent()
                .ok_or_else(|| io::Error::other("output requires a parent directory"))?;
            std::fs::create_dir_all(parent)?;
            let previous = match prepare(&destination, parent) {
                Ok(previous) => Some(previous),
                Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                Err(error) => return Err(error),
            };
            prepared.push(PreparedFile {
                replacement: prepare(&source, parent)?,
                previous,
                destination,
            });
        }
        if !self.is_current()? {
            return Ok(false);
        }
        let mut published = Vec::with_capacity(prepared.len());
        for file in prepared {
            if let Err(error) = file.replacement.persist(&file.destination) {
                let failure = format!(
                    "failed to publish {}: {}",
                    file.destination.display(),
                    error.error
                );
                return match restore(published) {
                    Ok(()) => Err(io::Error::other(failure)),
                    Err(rollback) => Err(io::Error::other(format!("{failure}; {rollback}"))),
                };
            }
            published.push(PublishedFile {
                destination: file.destination,
                previous: file.previous,
            });
        }
        match self.is_current() {
            Ok(true) => Ok(true),
            Ok(false) => {
                restore(published)?;
                Ok(false)
            }
            Err(error) => match restore(published) {
                Ok(()) => Err(error),
                Err(rollback) => Err(io::Error::other(format!("{error}; {rollback}"))),
            },
        }
    }
}
