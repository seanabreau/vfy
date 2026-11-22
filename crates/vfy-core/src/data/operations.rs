//! Coverage data serialization, remapping, and shard transactions.

use std::collections::BTreeSet;
use std::fmt;
use std::path::{Component, Path, PathBuf};

use rusqlite::Connection;

use crate::path::{
    CaseSensitivity, CoverageGlobSet, absolute_path, canonical_path, glob_path, glob_set_with_case,
};

use super::{CoverageData, DataError};

/// Options controlling an atomic shard-combination transaction.
#[derive(Clone, Debug, Default)]
pub struct CombineOptions {
    /// Retain successfully combined input shards.
    pub keep: bool,
    /// Reject a transaction that discovers no input shards.
    pub strict: bool,
    /// Include an existing output database in the merge.
    pub include_output: bool,
    /// Coverage.py path-alias groups, with the canonical path first.
    pub aliases: Vec<Vec<String>>,
    /// Filename policy from the owning Coverage configuration.
    pub path_alias_mode: PathAliasMode,
    /// Interpreter search paths for relative absolute-mode alias destinations.
    pub path_alias_search_paths: Vec<PathBuf>,
}

/// Failure from a coverage shard combination transaction.
#[derive(Debug)]
pub enum CombineError {
    /// A path-alias group or pattern is invalid.
    InvalidAliases(String),
    /// The output path has no file name.
    MissingOutputName,
    /// A shard directory could not be inspected.
    Discover {
        path: PathBuf,
        source: std::io::Error,
    },
    /// An explicitly requested input does not exist.
    MissingInput(PathBuf),
    /// Strict combination found no inputs.
    NoData,
    /// A schema-7 database could not be read or validated.
    Read {
        path: PathBuf,
        source: rusqlite::Error,
    },
    /// Valid inputs cannot be merged.
    Merge(&'static str),
    /// The combined database could not be committed.
    Write { path: PathBuf, source: DataError },
    /// Path remapping failed before data could be merged.
    Remap(DataError),
    /// A shard could not be removed after the output committed.
    Cleanup {
        path: PathBuf,
        source: std::io::Error,
    },
}

impl fmt::Display for CombineError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidAliases(message) => formatter.write_str(message),
            Self::MissingOutputName => formatter.write_str("coverage output must have a file name"),
            Self::Discover { path, source } => write!(
                formatter,
                "failed to discover coverage shards in {}: {source}",
                path.display()
            ),
            Self::MissingInput(path) => {
                write!(
                    formatter,
                    "failed to combine from non-existent path {}",
                    path.display()
                )
            }
            Self::NoData => formatter.write_str("no coverage data files found"),
            Self::Read { path, source } => write!(
                formatter,
                "failed to read Coverage.py schema-7 data from {}: {source}",
                path.display()
            ),
            Self::Merge(message) => formatter.write_str(message),
            Self::Write { path, source } => write!(
                formatter,
                "failed to write combined coverage data to {}: {source}",
                path.display()
            ),
            Self::Remap(source) => source.fmt(formatter),
            Self::Cleanup { path, source } => write!(
                formatter,
                "combined coverage data but failed to remove shard {}: {source}",
                path.display()
            ),
        }
    }
}

impl std::error::Error for CombineError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Discover { source, .. } | Self::Cleanup { source, .. } => Some(source),
            Self::Read { source, .. } => Some(source),
            Self::Write { source, .. } | Self::Remap(source) => Some(source),
            _ => None,
        }
    }
}

/// Filename policy when applying configured path aliases.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum PathAliasMode {
    /// Keep unmapped filenames and resolve mapped filenames absolutely.
    #[default]
    Absolute,
    /// Preserve relative mapped filenames and infer relative source roots.
    Relative,
}

#[derive(Clone)]
struct PathAliases {
    groups: Vec<PathAliasGroup>,
    mode: PathAliasMode,
    implicit_roots: Vec<String>,
    relative_directory: Option<PathBuf>,
    search_paths: Vec<PathBuf>,
}

#[derive(Clone)]
struct PathAliasGroup {
    canonical: String,
    matchers: Vec<CoverageGlobSet>,
}

impl PathAliases {
    #[cfg(test)]
    fn new(groups: Vec<Vec<String>>) -> Result<Self, String> {
        Self::with_mode(groups, PathAliasMode::Absolute, Vec::new())
    }

    fn with_mode(
        groups: Vec<Vec<String>>,
        mode: PathAliasMode,
        search_paths: Vec<PathBuf>,
    ) -> Result<Self, String> {
        let mut compiled = Vec::with_capacity(groups.len());
        for group in groups {
            if group.is_empty() {
                return Err("path alias groups must contain a canonical path".to_owned());
            }
            let mut matchers = Vec::with_capacity(group.len().saturating_sub(1));
            for pattern in group.iter().skip(1) {
                // Coverage.py keeps a lone separator, which is the filesystem root.
                let trimmed = if pattern.chars().count() > 1 {
                    pattern.trim_end_matches(['/', '\\'])
                } else {
                    pattern.as_str()
                };
                if trimmed.ends_with('*') {
                    return Err("path alias patterns must not end with wildcards".to_owned());
                }
                let normalized = trimmed.replace('\\', "/");
                let absolute_anywhere = normalized.starts_with('/')
                    || normalized
                        .as_bytes()
                        .get(1)
                        .is_some_and(|byte| *byte == b':');
                let normalized = if mode == PathAliasMode::Relative
                    || normalized.starts_with('*')
                    || absolute_anywhere
                {
                    normalized
                } else {
                    glob_path(&absolute_path(Path::new(&normalized)))
                };
                // Alias roots include their separator, so bare relative roots stay anchored.
                let rooted = if normalized.ends_with('/') {
                    normalized
                } else {
                    format!("{normalized}/")
                };
                if let Some(matcher) = glob_set_with_case(&[rooted], CaseSensitivity::Insensitive)
                    .map_err(|error| {
                    format!("invalid path alias pattern {pattern:?}: {error}")
                })? {
                    matchers.push(matcher);
                }
            }
            compiled.push(PathAliasGroup {
                // An empty result maps to the root, as in Coverage.py.
                canonical: if group[0].is_empty() {
                    "/".to_owned()
                } else {
                    group[0].replace('\\', "/")
                },
                matchers,
            });
        }
        Ok(Self {
            groups: compiled,
            mode,
            implicit_roots: Vec::new(),
            relative_directory: (mode == PathAliasMode::Relative)
                .then(|| std::env::current_dir().ok())
                .flatten(),
            search_paths,
        })
    }

    fn map(&mut self, path: &str) -> String {
        let normalized = path.replace('\\', "/");
        for group in &self.groups {
            for matcher in &group.matchers {
                // Coverage.py's wildcard roots match the last repeated directory name.
                let matched_end = normalized
                    .match_indices('/')
                    .map(|(index, _)| index + 1)
                    .rev()
                    .find(|end| matcher.is_match(&normalized[..*end]));
                let Some(end) = matched_end else {
                    continue;
                };
                let suffix = normalized[end..].trim_start_matches('/');
                let candidate = Path::new(&group.canonical).join(suffix);
                // Coverage.py canonicalizes absolute results before the existence check.
                let candidate = if self.mode == PathAliasMode::Absolute {
                    resolve_unwalkable_parents(&candidate)
                } else {
                    candidate
                };
                let candidate = if self.mode == PathAliasMode::Absolute
                    && candidate.is_relative()
                    && !candidate.exists()
                {
                    self.search_paths
                        .iter()
                        .map(|root| root.join(&candidate))
                        .find(|path| path.exists())
                        .unwrap_or(candidate)
                } else {
                    candidate
                };
                if candidate.exists() {
                    return if self.mode == PathAliasMode::Absolute {
                        glob_path(&absolute_path(&candidate))
                    } else {
                        let mapped = glob_path(&candidate);
                        if group.canonical.starts_with("./") && group.canonical.len() > 2 {
                            mapped
                        } else {
                            mapped.strip_prefix("./").unwrap_or(&mapped).to_owned()
                        }
                    };
                }
            }
        }
        if self.mode == PathAliasMode::Absolute {
            return path.to_owned();
        }
        for root in &self.implicit_roots {
            let prefix = format!("{root}/");
            let suffix = normalized
                .rsplit_once(&format!("/{prefix}"))
                .map(|(_, suffix)| suffix)
                .or_else(|| normalized.strip_prefix(&prefix));
            if let Some(suffix) = suffix {
                let candidate = format!("{prefix}{suffix}");
                if Path::new(&candidate).exists() {
                    return if root == "." {
                        candidate
                            .strip_prefix("./")
                            .unwrap_or(&candidate)
                            .to_owned()
                    } else {
                        candidate
                    };
                }
            }
        }
        let relative = self
            .relative_directory
            .as_ref()
            .and_then(|root| {
                Path::new(&normalized)
                    .strip_prefix(root)
                    .ok()
                    .map(glob_path)
            })
            .unwrap_or(normalized);
        if !(Path::new(&relative).is_absolute()
            || relative.as_bytes().get(1) == Some(&b':')
                && relative.as_bytes().get(2) == Some(&b'/'))
            && let Some((root, _)) = relative.split_once('/')
            && !self.implicit_roots.iter().any(|known| known == root)
        {
            self.implicit_roots.push(root.to_owned());
            return self.map(&relative);
        }
        relative
    }
}

/// Resolve `..` after a missing component or a file, as Python's realpath does.
fn resolve_unwalkable_parents(path: &Path) -> PathBuf {
    let mut collapsed = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir
                if matches!(
                    collapsed.components().next_back(),
                    Some(Component::Normal(_))
                ) && !collapsed.is_dir() =>
            {
                if let Ok(resolved) = canonical_path(&collapsed) {
                    collapsed = resolved;
                }
                collapsed.pop();
            }
            other => collapsed.push(other),
        }
    }
    collapsed
}

fn same_file(left: &Path, right: &Path) -> bool {
    match (canonical_path(left), canonical_path(right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => left == right,
    }
}

fn discover_shards(directory: &Path, basename: &str) -> Result<Vec<PathBuf>, std::io::Error> {
    let prefix = format!("{basename}.");
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if !entry
            .file_name()
            .to_str()
            .is_some_and(|name| name.starts_with(&prefix))
        {
            continue;
        }
        let is_file = if file_type.is_symlink() {
            match std::fs::metadata(entry.path()) {
                Ok(metadata) => metadata.is_file(),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
                Err(error) => return Err(error),
            }
        } else {
            file_type.is_file()
        };
        if is_file {
            paths.push(entry.path());
        }
    }
    paths.sort();
    Ok(paths)
}

impl CoverageData {
    fn remapped(self, aliases: &mut PathAliases) -> Result<Self, &'static str> {
        let mapped = self
            .file_order
            .iter()
            .map(|path| (path.clone(), aliases.map(path)))
            .collect();
        self.remapped_files(&mapped)
    }

    /// Serialize the complete store into vfy's schema-7 transport payload.
    ///
    /// # Errors
    ///
    /// Returns an error when an in-memory database cannot be created or written.
    pub fn serialize(&self) -> Result<Vec<u8>, DataError> {
        let mut data = self.clone();
        if data.mode_is_known() {
            data.set_metadata("has_arcs", if data.branch_mode() { "1" } else { "0" });
        }
        let mut connection = Connection::open_in_memory().map_err(DataError::Serialize)?;
        data.write_connection(&mut connection)?;
        let mut payload = b"vfy-data\0".to_vec();
        payload.extend_from_slice(&connection.serialize("main").map_err(DataError::Serialize)?);
        Ok(payload)
    }

    /// Deserialize and validate vfy's schema-7 transport payload.
    ///
    /// # Errors
    ///
    /// Returns an error when the payload marker or embedded database is invalid.
    pub fn deserialize(payload: &[u8]) -> Result<Self, DataError> {
        let Some(database) = payload.strip_prefix(b"vfy-data\0") else {
            return Err(DataError::InvalidTransport);
        };
        let mut connection = Connection::open_in_memory().map_err(DataError::Deserialize)?;
        connection
            .deserialize_read_exact("main", database, database.len(), true)
            .map_err(DataError::Deserialize)?;
        Self::read_connection(&connection).map_err(DataError::CorruptTransport)
    }

    /// Apply Coverage.py path aliases to every file and related record.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid aliases or incompatible mapped collisions.
    pub fn remap_paths(self, aliases: Vec<Vec<String>>) -> Result<Self, DataError> {
        self.remap_paths_with_mode(aliases, PathAliasMode::Absolute, Vec::new())
    }

    /// Apply path aliases using the reporting filename policy.
    ///
    /// # Errors
    /// Returns an error for invalid aliases or incompatible mapped collisions.
    pub fn remap_paths_with_mode(
        self,
        aliases: Vec<Vec<String>>,
        mode: PathAliasMode,
        search_paths: Vec<PathBuf>,
    ) -> Result<Self, DataError> {
        let mut aliases = PathAliases::with_mode(aliases, mode, search_paths)
            .map_err(|message| DataError::invalid("compile path aliases", message))?;
        let mapped = self
            .file_order
            .iter()
            .map(|path| (path.clone(), aliases.map(path)))
            .collect();
        self.remapped_files(&mapped)
            .map_err(|message| DataError::invalid("remap coverage paths", message))
    }

    /// Remap report data, choosing classic branch mode from stored arc evidence.
    ///
    /// # Errors
    /// Returns an error for invalid aliases or incompatible mapped collisions.
    pub fn remap_paths_for_reporting(
        self,
        aliases: Vec<Vec<String>>,
        mode: PathAliasMode,
        search_paths: Vec<PathBuf>,
    ) -> Result<Self, DataError> {
        let mut mapped = self.remap_paths_with_mode(aliases, mode, search_paths)?;
        if mapped.has_arcs && !mapped.has_semantic_data() && !mapped.has_arc_evidence() {
            mapped.has_arcs = false;
            mapped.set_metadata("has_arcs", "0");
        }
        Ok(mapped)
    }
}

/// Atomically combine schema-7 shards and clean them up only after commit.
///
/// # Errors
///
/// Returns a typed error when discovery, validation, merge, commit, or cleanup fails.
pub fn combine_transaction(
    paths: Vec<String>,
    output: &Path,
    base: Option<CoverageData>,
    options: CombineOptions,
) -> Result<(Option<CoverageData>, usize), CombineError> {
    combine_transaction_with_cleanup(paths, output, base, None, options, |path| {
        std::fs::remove_file(path)
    })
}

/// Atomically publish an in-memory shard together with discovered shards.
///
/// The incoming shard is remapped with the same aliases as the discovered
/// shards, is not required to exist on disk, and a transaction with no
/// discovered shard still commits it. The existing output is read inside the
/// transaction when `options.include_output` is set.
///
/// # Errors
///
/// Returns a typed error when discovery, validation, merge, commit, or cleanup fails.
pub fn publish_transaction(
    incoming: CoverageData,
    paths: Vec<String>,
    output: &Path,
    options: CombineOptions,
) -> Result<(CoverageData, usize), CombineError> {
    let (combined, count) =
        combine_transaction_with_cleanup(paths, output, None, Some(incoming), options, |path| {
            std::fs::remove_file(path)
        })?;
    // An incoming shard always yields committed data; the error covers a broken invariant.
    Ok((combined.ok_or(CombineError::NoData)?, count))
}

/// Resolve requested files and directories to distinct input shards, excluding `output`.
fn discover_candidates(
    paths: Vec<String>,
    output: &Path,
) -> Result<BTreeSet<PathBuf>, CombineError> {
    let basename = output
        .file_name()
        .and_then(std::ffi::OsStr::to_str)
        .ok_or(CombineError::MissingOutputName)?;
    let requested = if paths.is_empty() {
        vec![
            output
                .parent()
                .filter(|parent| !parent.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new("."))
                .to_path_buf(),
        ]
    } else {
        paths.into_iter().map(PathBuf::from).collect()
    };
    let mut candidates = BTreeSet::new();
    for path in requested {
        if path.is_dir() {
            candidates.extend(
                discover_shards(&path, basename)
                    .map_err(|source| CombineError::Discover { path, source })?
                    .into_iter()
                    .filter(|candidate| !same_file(candidate, output)),
            );
        } else if !path.exists() {
            return Err(CombineError::MissingInput(path));
        } else if !same_file(&path, output) {
            candidates.insert(path);
        }
    }
    // Resolve directories, but retain the final name: distinct symlink entries
    // must each be removed even when they refer to the same shard.
    let mut identities = BTreeSet::new();
    candidates.retain(|path| {
        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let identity = canonical_path(parent)
            .ok()
            .zip(path.file_name())
            .map_or_else(|| path.clone(), |(parent, name)| parent.join(name));
        identities.insert(identity)
    });
    Ok(candidates)
}

fn combine_transaction_with_cleanup<F>(
    paths: Vec<String>,
    output: &Path,
    base: Option<CoverageData>,
    incoming: Option<CoverageData>,
    options: CombineOptions,
    cleanup: F,
) -> Result<(Option<CoverageData>, usize), CombineError>
where
    F: Fn(&Path) -> Result<(), std::io::Error>,
{
    let mut aliases = PathAliases::with_mode(
        options.aliases,
        options.path_alias_mode,
        options.path_alias_search_paths,
    )
    .map_err(CombineError::InvalidAliases)?;
    let candidates = discover_candidates(paths, output)?;
    if candidates.is_empty() && incoming.is_none() {
        if options.strict {
            return Err(CombineError::NoData);
        }
        if options.include_output && output.exists() {
            return CoverageData::read_path(output)
                .map(|data| (Some(data), 0))
                .map_err(|source| CombineError::Read {
                    path: output.to_path_buf(),
                    source,
                });
        }
        return Ok((None, 0));
    }

    let mut combined = if let Some(base) = base {
        base
    } else if options.include_output && output.exists() {
        CoverageData::read_path(output).map_err(|source| CombineError::Read {
            path: output.to_path_buf(),
            source,
        })?
    } else {
        CoverageData::empty()
    };
    if let Some(incoming) = incoming {
        let incoming = incoming.remapped(&mut aliases).map_err(|message| {
            CombineError::Remap(DataError::invalid("remap coverage paths", message))
        })?;
        combined
            .merge_from(incoming, false)
            .map_err(CombineError::Merge)?;
    }
    for path in &candidates {
        let current = CoverageData::read_path(path)
            .map_err(|source| CombineError::Read {
                path: path.clone(),
                source,
            })?
            .remapped(&mut aliases)
            .map_err(|message| {
                CombineError::Remap(DataError::invalid("remap coverage paths", message))
            })?;
        combined
            .merge_from(current, false)
            .map_err(CombineError::Merge)?;
    }
    combined
        .write_path(output)
        .map_err(|source| CombineError::Write {
            path: output.to_path_buf(),
            source,
        })?;
    if !options.keep {
        for path in &candidates {
            cleanup(path).map_err(|source| CombineError::Cleanup {
                path: path.clone(),
                source,
            })?;
        }
    }
    Ok((Some(combined), candidates.len()))
}

#[cfg(test)]
mod tests;
