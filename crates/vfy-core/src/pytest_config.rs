//! Pytest configuration discovery and option precedence.

use std::collections::BTreeSet;
use std::env;
use std::fmt;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;

mod argument_files;
mod arguments;
mod config_files;
mod paths;
mod requirements;
mod shell_words;
mod validation;

use argument_files::{ArgumentFiles, expand_arguments};
use arguments::{ArgumentPhase, filtered_scope, parse_arguments, parse_arguments_from};
pub use arguments::{
    MutationBaselineCoveragePolicy, PytestParserBehavior, coverage_report_sources,
    mutation_baseline_coverage_policy, mutation_worker_arguments,
};
use config_files::{ConfigAddopts, config_error, select_config};
use paths::{absolute_path, expand_home, expand_vars};
pub use shell_words::split_shell_words;
pub use validation::{
    ConfigurationDiagnostic, ConfigurationPolicy, StrictnessSettings, minimum_version_diagnostic,
    required_plugins_diagnostic,
};

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PytestConfigPolicy {
    /// pytest 8.0/8.1, before argument files were supported.
    LegacyWithoutArgumentFiles,
    /// pytest 8.2+ discovery and `pyproject.toml` INI-compatible settings.
    Legacy,
    /// pytest 9.0 discovery, including `pytest.toml` and native TOML settings.
    Toml,
    /// pytest 9.1+ also applies `-o` from config `addopts` to INI values.
    TomlWithAddoptsOverrides,
    /// pytest 9.2 resolves an explicit non-file config from the invocation directory.
    TomlWithNonFileConfig,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PytestConfigProjection {
    pub config_file: Option<PathBuf>,
    /// Identity of the exact selected configuration contents used for discovery.
    pub config_contents_fingerprint: String,
    /// SHA-256 of the exact selected file bytes consumed during resolution.
    pub config_file_sha256: Option<String>,
    pub rootdir: PathBuf,
    /// Config `addopts`, then `PYTEST_ADDOPTS`; CLI arguments remain caller-owned.
    pub configured_args: Vec<String>,
    /// Config, environment, and CLI arguments after argument-file expansion.
    /// Plugin bootstrap and pytest launch still consume their original tokens.
    pub effective_args: Vec<String>,
    /// Expanded CLI tokens, excluding config and environment addopts.
    pub cli_args: Vec<String>,
    /// Invocation selectors narrow execution within the requested directory suite.
    /// Static selectors in config `addopts` define the suite instead.
    pub filtered_scope: bool,
    /// Collection-only invocations produce no execution observations for history.
    pub collect_only: bool,
    /// One actual invocation positional with a positive ASCII-decimal line suffix.
    /// Keep the raw spelling; this classifies scope without resolving a selection plan.
    pub invocation_location: Option<String>,
    pub cache_dir: PathBuf,
    pub basetemp: Option<PathBuf>,
    /// Full selected settings, including per-value override precedence and format.
    pub ini_values: IndexMap<String, PytestIniSetting>,
}

impl PytestConfigProjection {
    /// Return undeclared setting names, including command-line-only overrides.
    #[must_use]
    pub fn unknown_ini(&self, known_names: &[String]) -> Vec<String> {
        let known: BTreeSet<_> = known_names.iter().collect();
        self.ini_values
            .keys()
            .filter(|name| !known.contains(name))
            .cloned()
            .collect()
    }

    /// Resolve canonical and alias settings using pytest 9.1 override precedence.
    #[must_use]
    pub fn ini_setting(&self, name: &str, aliases: &[String]) -> Option<&PytestIniSetting> {
        let mut selected = self.ini_values.get(name).map(|value| (value, true));
        for alias in aliases {
            let Some(value) = self.ini_values.get(alias) else {
                continue;
            };
            if selected.is_none_or(|(current, canonical)| {
                (value.overridden, false) > (current.overridden, canonical)
            }) {
                selected = Some((value, false));
            }
        }
        selected.map(|(value, _)| value)
    }
}

/// Failure category before a frontend maps configuration errors to its protocol.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PytestConfigErrorKind {
    /// Invalid configuration or command-line syntax.
    Usage,
    /// Invalid shell quoting, preserving the shell parser's original message.
    Value,
    /// An operating-system failure reading the selected file.
    Read,
    /// The selected file contains invalid UTF-8 bytes.
    Decode,
}

#[derive(Debug)]
enum ConfigFailure {
    Usage,
    Value(String),
    Read(PathBuf, std::io::Error),
    Decode(PathBuf, std::string::FromUtf8Error),
}

#[derive(Debug)]
pub struct PytestConfigError {
    message: String,
    failure: ConfigFailure,
}

impl PytestConfigError {
    fn message(message: String) -> Self {
        Self {
            message,
            failure: ConfigFailure::Usage,
        }
    }

    fn value(message: String, reason: String) -> Self {
        Self {
            message,
            failure: ConfigFailure::Value(reason),
        }
    }

    /// Return the configuration loading failure category.
    #[must_use]
    pub const fn kind(&self) -> PytestConfigErrorKind {
        match self.failure {
            ConfigFailure::Usage => PytestConfigErrorKind::Usage,
            ConfigFailure::Value(_) => PytestConfigErrorKind::Value,
            ConfigFailure::Read(..) => PytestConfigErrorKind::Read,
            ConfigFailure::Decode(..) => PytestConfigErrorKind::Decode,
        }
    }

    /// Return the selected path and original operating-system read error.
    #[must_use]
    pub fn read_error(&self) -> Option<(&Path, &std::io::Error)> {
        match &self.failure {
            ConfigFailure::Read(path, error) => Some((path, error)),
            _ => None,
        }
    }

    /// Return the original shell parser message without frontend context.
    #[must_use]
    pub fn value_error_message(&self) -> Option<&str> {
        match &self.failure {
            ConfigFailure::Value(message) => Some(message),
            _ => None,
        }
    }

    /// Return the exact selected bytes rejected by UTF-8 decoding.
    #[must_use]
    pub fn invalid_utf8(&self) -> Option<(&Path, &[u8])> {
        match &self.failure {
            ConfigFailure::Decode(path, error) => Some((path, error.as_bytes())),
            _ => None,
        }
    }
}

impl fmt::Display for PytestConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for PytestConfigError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match &self.failure {
            ConfigFailure::Read(_, error) => Some(error),
            ConfigFailure::Decode(_, error) => Some(error),
            ConfigFailure::Usage | ConfigFailure::Value(_) => None,
        }
    }
}

type ConfigResult<T> = Result<T, PytestConfigError>;
/// Format used to interpret a pytest configuration setting.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PytestIniMode {
    Ini,
    Toml,
}

/// A file value or command-line override before declared-type conversion.
#[derive(Clone, Debug, PartialEq)]
pub struct PytestIniSetting {
    pub value: toml::Value,
    pub mode: PytestIniMode,
    pub overridden: bool,
}
/// Resolve pytest configuration without modifying files.
///
/// # Errors
///
/// Returns a config error for invalid selected files, malformed option values,
/// invalid `PYTEST_ADDOPTS`, or a nonexistent `--rootdir`/`-c` path.
pub fn resolve_pytest_configuration(
    arguments: &[String],
    invocation_dir: &Path,
    env_addopts: Option<&str>,
    policy: PytestConfigPolicy,
) -> ConfigResult<PytestConfigProjection> {
    resolve_pytest_configuration_with_parser(
        arguments,
        invocation_dir,
        env_addopts,
        policy,
        PytestParserBehavior::Current,
    )
}

fn resolve_rootdir(
    override_path: Option<&str>,
    invocation_dir: &Path,
    discovered: PathBuf,
) -> ConfigResult<PathBuf> {
    let Some(rootdir) = override_path else {
        return Ok(discovered);
    };
    let expanded = expand_vars(rootdir);
    let path = absolute_path(invocation_dir, Path::new(&expanded));
    if !path.is_dir() {
        return Err(PytestConfigError::message(format!(
            "Directory '{}' not found. Check your '--rootdir' option.",
            path.display()
        )));
    }
    Ok(path)
}

/// Resolve configuration using the target interpreter's argument parsers.
///
/// # Errors
/// Returns the same configuration errors as [`resolve_pytest_configuration`].
pub fn resolve_pytest_configuration_with_parser(
    arguments: &[String],
    invocation_dir: &Path,
    env_addopts: Option<&str>,
    policy: PytestConfigPolicy,
    parser_behavior: PytestParserBehavior,
) -> ConfigResult<PytestConfigProjection> {
    let mut argument_files = ArgumentFiles::new(invocation_dir);
    let arguments = expand_arguments(arguments, policy, &mut argument_files)?;
    let env_arguments = match env_addopts {
        Some(value) => split_shell_words(value).map_err(|message| {
            PytestConfigError::value(format!("invalid PYTEST_ADDOPTS: {message}"), message)
        })?,
        None => Vec::new(),
    };
    let expanded_env_arguments = expand_arguments(&env_arguments, policy, &mut argument_files)?;
    let initial_args: Vec<_> = expanded_env_arguments
        .iter()
        .chain(&arguments)
        .cloned()
        .collect();
    let initial = parse_arguments(
        &initial_args,
        ArgumentPhase::Discovery,
        policy,
        parser_behavior,
    );
    let selected = select_config(&initial, invocation_dir, policy)?;
    let rootdir = resolve_rootdir(initial.rootdir.as_deref(), invocation_dir, selected.rootdir)?;

    let addopts_override = initial.overrides.iter().rev().find_map(|override_value| {
        override_value
            .split_once('=')
            .and_then(|(key, value)| (key == "addopts").then_some(value))
    });
    let mut configured_args = configured_addopts(
        addopts_override,
        selected.values.addopts,
        selected.file.as_deref().unwrap_or(invocation_dir),
    )?;
    let mut expanded_configured_args =
        expand_arguments(&configured_args, policy, &mut argument_files)?;
    let configured_len = expanded_configured_args.len();
    expanded_configured_args.extend(expanded_env_arguments);
    configured_args.extend(env_arguments);

    let default_cache_dir = selected.values.cache_dir.unwrap_or_else(cache_dir_default);
    let mut basetemp = None;
    let final_args = [expanded_configured_args, arguments.clone()].concat();
    let final_options = parse_arguments(
        &final_args,
        ArgumentPhase::Effective,
        policy,
        parser_behavior,
    );
    let invocation_options = parse_arguments_from(
        &final_args,
        ArgumentPhase::Effective,
        policy,
        parser_behavior,
        configured_len,
    );
    let filtered_scope = filtered_scope(
        &final_args[..configured_len],
        &invocation_options,
        addopts_override,
        policy,
        parser_behavior,
    );
    let all_overrides = last_cache_dir_override(&final_options.overrides)?;
    let include_addopts_overrides = matches!(
        policy,
        PytestConfigPolicy::TomlWithAddoptsOverrides | PytestConfigPolicy::TomlWithNonFileConfig
    );
    let cache_dir_override = if include_addopts_overrides {
        all_overrides
    } else {
        // Before pytest 9.1, config addopts were parsed after INI overrides
        // were captured. Environment and explicit CLI overrides still apply.
        last_cache_dir_override(&initial.overrides)?
    };
    let cache_dir = cache_dir_override.unwrap_or(&default_cache_dir);
    if let Some(value) = final_options.basetemp {
        basetemp = Some(absolute_path(invocation_dir, Path::new(&value)));
    }

    let mut ini_values = selected.values.ini_values;
    let overrides = if include_addopts_overrides {
        &final_options.overrides
    } else {
        &initial.overrides
    };
    apply_ini_overrides(&mut ini_values, overrides)?;

    let cache_dir = expand_home(cache_dir)?;
    let cache_dir = expand_vars(&cache_dir);
    let cache_dir = absolute_path(&rootdir, Path::new(&cache_dir));
    Ok(PytestConfigProjection {
        config_file: selected.file,
        config_contents_fingerprint: selected.contents_fingerprint,
        config_file_sha256: selected.contents_sha256,
        rootdir,
        configured_args,
        effective_args: final_args,
        cli_args: arguments,
        filtered_scope,
        collect_only: final_options.collect_only,
        invocation_location: invocation_options.invocation_location().map(str::to_owned),
        cache_dir,
        basetemp,
        ini_values,
    })
}

/// Return the raw builtin cache directory default before path expansion.
#[must_use]
pub fn cache_dir_default() -> String {
    env::var("TOX_ENV_DIR").map_or_else(
        |_| ".pytest_cache".to_owned(),
        |directory| {
            Path::new(&directory)
                .join(".pytest_cache")
                .to_string_lossy()
                .into_owned()
        },
    )
}

fn apply_ini_overrides(
    values: &mut IndexMap<String, PytestIniSetting>,
    overrides: &[String],
) -> ConfigResult<()> {
    for override_value in overrides {
        let Some((name, value)) = override_value.split_once('=') else {
            return Err(PytestConfigError::message(format!(
                "-o/--override-ini expects option=value style (got: {override_value:?})."
            )));
        };
        values.insert(
            name.to_owned(),
            PytestIniSetting {
                value: toml::Value::String(value.to_owned()),
                mode: PytestIniMode::Ini,
                overridden: true,
            },
        );
    }

    Ok(())
}
fn configured_addopts(
    addopts_override: Option<&str>,
    selected_addopts: ConfigAddopts,
    selected_path: &Path,
) -> ConfigResult<Vec<String>> {
    if let Some(value) = addopts_override {
        return split_shell_words(value).map_err(|message| {
            PytestConfigError::value(format!("invalid addopts override: {message}"), message)
        });
    }
    match selected_addopts {
        ConfigAddopts::Empty => Ok(Vec::new()),
        ConfigAddopts::Words(words) => Ok(words),
        ConfigAddopts::Shell(value) => split_shell_words(&value).map_err(|message| {
            PytestConfigError::value(
                format!("invalid addopts: {message} ({})", selected_path.display()),
                message,
            )
        }),
        ConfigAddopts::Invalid(message) => Err(config_error(selected_path, message)),
    }
}

fn last_cache_dir_override(overrides: &[String]) -> ConfigResult<Option<&str>> {
    let mut cache_dir = None;
    for override_value in overrides {
        let (key, value) = override_value.split_once('=').ok_or_else(|| {
            PytestConfigError::message(format!(
                "pytest: -o/--override-ini expects option=value, got: {override_value}"
            ))
        })?;
        if key == "cache_dir" {
            cache_dir = Some(value);
        }
    }
    Ok(cache_dir)
}

#[cfg(test)]
mod tests;
