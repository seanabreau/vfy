//! Resolved builtin ignore decisions, including conftest path values.

use super::{Pattern, absolute, patterns};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// Values supplied by the frontend's authoritative option and INI declarations.
pub struct CollectionIgnoreSettings {
    pub invocation_dir: PathBuf,
    pub ignore: Vec<String>,
    pub ignore_glob: Vec<String>,
    pub norecursedirs: Vec<String>,
    pub collect_in_virtualenv: bool,
}

/// Pytest resolves strings against their conftest; path-like values retain their spelling.
pub enum ConftestPathValue {
    String(String),
    PathLike(PathBuf),
}

/// Validated policy used at the builtin ignore hook's actual dispatch priority.
#[derive(Clone, Debug)]
pub struct CollectionExclusions {
    paths: BTreeSet<PathBuf>,
    globs: Vec<Pattern>,
    directories: Vec<Pattern>,
    collect_in_virtualenv: bool,
}

impl CollectionExclusions {
    /// Resolve configured CLI paths and compile pytest fnmatch patterns.
    ///
    /// # Errors
    /// Returns an error when the invocation path or a pattern cannot be resolved.
    pub fn from_resolved(settings: CollectionIgnoreSettings) -> Result<Self, String> {
        let base = absolute(&settings.invocation_dir, Path::new(""))?;
        Ok(Self {
            paths: settings
                .ignore
                .iter()
                .map(|value| absolute(&base, Path::new(value)))
                .collect::<Result<_, _>>()?,
            globs: settings
                .ignore_glob
                .into_iter()
                .map(|value| {
                    Pattern::new(
                        absolute(&base, Path::new(&value))?
                            .to_string_lossy()
                            .into_owned(),
                    )
                })
                .collect::<Result<_, String>>()?,
            directories: patterns(settings.norecursedirs)?,
            collect_in_virtualenv: settings.collect_in_virtualenv,
        })
    }

    /// Add the nearest conftest's `collect_ignore` declaration.
    ///
    /// # Errors
    /// Returns an error if a string path cannot be resolved against its conftest.
    pub fn add_paths(&mut self, base: &Path, values: &[ConftestPathValue]) -> Result<(), String> {
        for value in values {
            self.paths.insert(conftest_path(base, value)?);
        }
        Ok(())
    }

    /// Add the nearest conftest's `collect_ignore_glob` declaration.
    ///
    /// # Errors
    /// Returns an error for an unresolved string path or invalid pattern.
    pub fn add_globs(&mut self, base: &Path, values: &[ConftestPathValue]) -> Result<(), String> {
        for value in values {
            self.globs.push(Pattern::new(
                conftest_path(base, value)?.to_string_lossy().into_owned(),
            )?);
        }
        Ok(())
    }

    #[must_use]
    pub fn is_ignored(&self, path: &Path) -> bool {
        Self::is_bytecode_cache(path)
            || self.paths.contains(path)
            || self
                .globs
                .iter()
                .any(|pattern| pattern.matcher.is_match(&path.to_string_lossy()))
            || (path.is_dir()
                && (self
                    .directories
                    .iter()
                    .any(|pattern| pattern.matches_path(path))
                    || (!self.collect_in_virtualenv && Self::is_virtual_environment(path))))
    }

    /// Bytecode cache directories are the builtin hook's first unconditional exclusion.
    #[must_use]
    pub fn is_bytecode_cache(path: &Path) -> bool {
        path.file_name().is_some_and(|name| name == "__pycache__")
    }

    /// Recognize the environment markers consulted by pytest's builtin hook.
    #[must_use]
    pub fn is_virtual_environment(path: &Path) -> bool {
        path.join("pyvenv.cfg").is_file() || path.join("conda-meta/history").is_file()
    }

    /// Match live recursion patterns after earlier builtin exclusions have declined.
    ///
    /// # Errors
    /// Returns an error for an invalid fnmatch pattern.
    pub fn matches_norecursedirs(path: &Path, values: Vec<String>) -> Result<bool, String> {
        Ok(patterns(values)?
            .iter()
            .any(|pattern| pattern.matches_path(path)))
    }
}

fn conftest_path(base: &Path, value: &ConftestPathValue) -> Result<PathBuf, String> {
    match value {
        ConftestPathValue::String(value) => absolute(
            base,
            Path::new(&value.replace('/', std::path::MAIN_SEPARATOR_STR)),
        ),
        ConftestPathValue::PathLike(value) => Ok(value.clone()),
    }
}
