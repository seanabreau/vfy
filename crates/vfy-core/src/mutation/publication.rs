//! Stage mutation artifacts until every issued interpreter result is validated.

use std::collections::BTreeSet;
use std::io;
use std::path::{Path, PathBuf};

use crate::output::{PublishedFile, file_digest, prepare_publication, publication_target, restore};

/// The artifact written by a mutation run.
#[derive(Clone, Copy, Ord, PartialEq, PartialOrd, Eq)]
pub enum MutationOutput {
    Coverage,
    Cache,
    Baseline,
    Report,
}

struct OutputFile {
    kind: MutationOutput,
    destination: PathBuf,
    staged: PathBuf,
    previous_digest: String,
}

struct PreparedBaseline {
    request: String,
    coverage_digest: String,
}

/// Owns private staging files; public destinations remain unchanged until commit.
pub struct MutationPublication {
    _directory: tempfile::TempDir,
    files: Vec<OutputFile>,
    prepared_baseline: Option<PreparedBaseline>,
}

impl MutationPublication {
    /// Capture prior artifact bytes before baseline or mutant execution starts.
    ///
    /// # Errors
    /// Rejects overlapping destinations or unreadable prior artifacts.
    pub fn new(outputs: &[(MutationOutput, PathBuf)]) -> io::Result<Self> {
        let directory = tempfile::Builder::new()
            .prefix("vfy-mutation-stage-")
            .tempdir()?;
        let mut destinations = BTreeSet::new();
        let mut kinds = BTreeSet::new();
        for (kind, path) in outputs {
            if !kinds.insert(*kind) {
                return Err(io::Error::other("mutation artifact roles overlap"));
            }
            let (path, _) = publication_target(path)?;
            if !destinations.insert(path) {
                return Err(io::Error::other("mutation artifact destinations overlap"));
            }
        }
        if destinations.iter().any(|path| {
            path.ancestors()
                .skip(1)
                .any(|parent| destinations.contains(parent))
        }) {
            return Err(io::Error::other("mutation artifact destinations overlap"));
        }
        let mut files = Vec::with_capacity(outputs.len());
        for (index, (kind, destination)) in outputs.iter().enumerate() {
            let destination = crate::absolute_path(destination);
            let staged = directory.path().join(format!("artifact-{index}"));
            let previous_digest = file_digest(&destination)?;
            if previous_digest != "missing" {
                std::fs::copy(&destination, &staged)?;
                if file_digest(&staged)? != previous_digest {
                    return Err(io::Error::other("mutation artifact changed during staging"));
                }
            }
            files.push(OutputFile {
                kind: *kind,
                destination,
                staged,
                previous_digest,
            });
        }
        Ok(Self {
            _directory: directory,
            files,
            prepared_baseline: None,
        })
    }

    /// Return the private path where the artifact writer stages this output.
    #[must_use]
    pub fn staged(&self, kind: MutationOutput) -> Option<&Path> {
        self.output(kind).map(|file| file.staged.as_path())
    }

    fn output(&self, kind: MutationOutput) -> Option<&OutputFile> {
        self.files.iter().find(|file| file.kind == kind)
    }

    fn baseline_request(&self, request: &str) -> Result<PreparedBaseline, String> {
        let coverage = self
            .output(MutationOutput::Coverage)
            .ok_or("mutation coverage resource is unavailable")?;
        self.output(MutationOutput::Baseline)
            .ok_or("mutation baseline resource is unavailable")?;
        let mut value: serde_json::Value =
            serde_json::from_str(request).map_err(|error| error.to_string())?;
        let logical = value
            .get("data_file")
            .and_then(serde_json::Value::as_str)
            .ok_or("mutation baseline requires its coverage destination")?;
        if crate::absolute_path(Path::new(logical)) != coverage.destination {
            return Err("mutation baseline belongs to another coverage destination".to_owned());
        }
        value["data_file"] =
            serde_json::Value::String(coverage.destination.to_string_lossy().into_owned());
        let coverage_digest = file_digest(&coverage.staged).map_err(|error| error.to_string())?;
        if coverage_digest == "missing" {
            return Err("staged mutation coverage is missing".to_owned());
        }
        value["data_hash"] = serde_json::Value::String(coverage_digest.clone());
        Ok(PreparedBaseline {
            request: serde_json::to_string(&value).map_err(|error| error.to_string())?,
            coverage_digest,
        })
    }

    /// Validate the staged baseline and retain its owned binding through commit.
    ///
    /// # Errors
    /// Rejects missing resources, foreign destinations or malformed requests.
    pub fn baseline_valid(&mut self, request: &str) -> Result<bool, String> {
        let bound = self.baseline_request(request)?;
        let baseline = self
            .staged(MutationOutput::Baseline)
            .ok_or("mutation baseline resource is unavailable")?;
        let valid = super::baseline::mutation_baseline_valid(baseline, &bound.request)?;
        if valid {
            self.prepared_baseline = Some(bound);
        }
        Ok(valid)
    }

    /// Prepare baseline metadata with the digest of the staged coverage.
    ///
    /// # Errors
    /// Invalid inputs or writer failures leave the prior staged baseline intact.
    pub fn write_baseline(&mut self, request: &str) -> Result<(), String> {
        let bound = self.baseline_request(request)?;
        let baseline = self
            .staged(MutationOutput::Baseline)
            .ok_or("mutation baseline resource is unavailable")?;
        super::baseline::write_mutation_baseline(baseline, &bound.request)?;
        self.prepared_baseline = Some(bound);
        Ok(())
    }

    fn validate_prepared_baseline(&self) -> io::Result<()> {
        let Some(bound) = &self.prepared_baseline else {
            return Ok(());
        };
        let coverage = self
            .staged(MutationOutput::Coverage)
            .ok_or_else(|| io::Error::other("mutation coverage resource is unavailable"))?;
        let baseline = self
            .staged(MutationOutput::Baseline)
            .ok_or_else(|| io::Error::other("mutation baseline resource is unavailable"))?;
        if file_digest(coverage)? != bound.coverage_digest
            || !super::baseline::mutation_baseline_valid(baseline, &bound.request)
                .map_err(io::Error::other)?
        {
            return Err(io::Error::other(
                "staged mutation baseline binding changed before publication",
            ));
        }
        Ok(())
    }

    /// Publish changed artifacts atomically, restoring prior bytes on failure.
    ///
    /// # Errors
    /// A changed prior destination, invalid staged resource or preparation failure
    /// leaves all public artifact bytes untouched. A commit failure attempts every
    /// rollback and retains the shared backup diagnostics.
    pub fn commit(&self) -> io::Result<()> {
        self.validate_prepared_baseline()?;
        let mut changed = Vec::new();
        for file in &self.files {
            publication_target(&file.destination)?;
            if file_digest(&file.destination)? != file.previous_digest {
                return Err(io::Error::other(
                    "mutation artifact changed before publication",
                ));
            }
            let staged_digest = file_digest(&file.staged)?;
            if staged_digest == "missing" || staged_digest == file.previous_digest {
                continue;
            }
            if !std::fs::symlink_metadata(&file.staged)?
                .file_type()
                .is_file()
            {
                return Err(io::Error::other(
                    "staged mutation artifact is not a regular file",
                ));
            }
            changed.push(file);
        }
        let mut prepared = Vec::with_capacity(changed.len());
        for file in changed {
            let parent = file
                .destination
                .parent()
                .ok_or_else(|| io::Error::other("mutation artifact requires a parent"))?;
            std::fs::create_dir_all(parent)?;
            let (destination, mode) = publication_target(&file.destination)?;
            prepared.push(prepare_publication(destination, mode, |output| {
                io::copy(&mut std::fs::File::open(&file.staged)?, output)?;
                Ok(())
            })?);
        }
        let mut published = Vec::with_capacity(prepared.len());
        for file in prepared {
            if let Err(error) = file.replacement.persist(&file.destination) {
                let failure = format!(
                    "failed to publish mutation artifact {}: {}",
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
        Ok(())
    }
}

#[cfg(test)]
mod tests;
