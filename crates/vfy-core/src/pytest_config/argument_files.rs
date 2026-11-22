//! Expansion of pytest `@file` argument files within fixed resource bounds.

use std::collections::BTreeSet;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use super::config_files::config_error;
use super::paths::absolute_path;
use super::{ConfigResult, PytestConfigError, PytestConfigPolicy};

pub(super) const MAX_ARGUMENT_FILE_DEPTH: usize = 64;
pub(super) const MAX_ARGUMENT_FILES: usize = 1_024;
pub(super) const MAX_ARGUMENT_FILE_BYTES: u64 = 8 * 1_024 * 1_024;
pub(super) const MAX_EXPANDED_ARGUMENTS: usize = 100_000;

pub(super) struct ArgumentFiles<'a> {
    invocation_dir: &'a Path,
    active: BTreeSet<PathBuf>,
    files: usize,
    bytes: u64,
    arguments: usize,
}

impl<'a> ArgumentFiles<'a> {
    pub(super) fn new(invocation_dir: &'a Path) -> Self {
        Self {
            invocation_dir,
            active: BTreeSet::new(),
            files: 0,
            bytes: 0,
            arguments: 0,
        }
    }

    fn expand(
        &mut self,
        argument: &str,
        depth: usize,
        result: &mut Vec<String>,
    ) -> ConfigResult<()> {
        let Some(filename) = argument.strip_prefix('@') else {
            self.arguments += 1;
            if self.arguments > MAX_EXPANDED_ARGUMENTS {
                return Err(PytestConfigError::message(
                    "pytest argument expansion exceeds 100000 arguments".to_owned(),
                ));
            }
            result.push(argument.to_owned());
            return Ok(());
        };
        let path = absolute_path(self.invocation_dir, Path::new(filename));
        if depth >= MAX_ARGUMENT_FILE_DEPTH || self.files >= MAX_ARGUMENT_FILES {
            return Err(config_error(
                &path,
                "pytest argument-file expansion exceeds its recursion or file limit",
            ));
        }
        let identity = path.canonicalize().map_err(|error| {
            config_error(
                &path,
                format!("failed to read pytest argument file: {error}"),
            )
        })?;
        if !self.active.insert(identity.clone()) {
            return Err(config_error(&path, "recursive pytest argument file"));
        }
        self.files += 1;
        let file = fs::File::open(&path).map_err(|error| {
            config_error(
                &path,
                format!("failed to read pytest argument file: {error}"),
            )
        })?;
        let mut bytes = Vec::new();
        file.take(MAX_ARGUMENT_FILE_BYTES - self.bytes + 1)
            .read_to_end(&mut bytes)
            .map_err(|error| {
                config_error(
                    &path,
                    format!("failed to read pytest argument file: {error}"),
                )
            })?;
        self.bytes += bytes.len() as u64;
        if self.bytes > MAX_ARGUMENT_FILE_BYTES {
            return Err(config_error(
                &path,
                "pytest argument-file expansion exceeds 8 MiB",
            ));
        }
        let content = String::from_utf8(bytes).map_err(|error| {
            config_error(&path, format!("pytest argument file is not UTF-8: {error}"))
        })?;
        // argparse uses str.splitlines(), including CRLF and Unicode separators.
        let content = content.replace("\r\n", "\n");
        for line in content.split_terminator([
            '\n', '\r', '\u{000b}', '\u{000c}', '\u{001c}', '\u{001d}', '\u{001e}', '\u{0085}',
            '\u{2028}', '\u{2029}',
        ]) {
            self.expand(line, depth + 1, result)?;
        }
        self.active.remove(&identity);
        Ok(())
    }
}

pub(super) fn expand_arguments(
    arguments: &[String],
    policy: PytestConfigPolicy,
    files: &mut ArgumentFiles<'_>,
) -> ConfigResult<Vec<String>> {
    if policy == PytestConfigPolicy::LegacyWithoutArgumentFiles {
        return Ok(arguments.to_vec());
    }
    let mut result = Vec::new();
    for argument in arguments {
        files.expand(argument, 0, &mut result)?;
    }
    Ok(result)
}
