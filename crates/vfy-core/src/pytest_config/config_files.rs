//! Selection and parsing of pytest INI and TOML configuration files.

use std::borrow::Cow;
use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use indexmap::IndexMap;

use super::arguments::Arguments;
use super::paths::{absolute_path, common_ancestor, target_directories};
use super::{
    ConfigFailure, ConfigResult, PytestConfigError, PytestConfigPolicy, PytestIniMode,
    PytestIniSetting,
};

type IniSections = BTreeMap<String, IndexMap<String, String>>;
type TomlTable<'a> = Cow<'a, toml::map::Map<String, toml::Value>>;

pub(super) struct SelectedConfig {
    pub(super) file: Option<PathBuf>,
    pub(super) contents_fingerprint: String,
    pub(super) contents_sha256: Option<String>,
    pub(super) rootdir: PathBuf,
    pub(super) values: ConfigValues,
}

struct LoadedConfig {
    values: Option<ConfigValues>,
    contents_fingerprint: String,
    contents_sha256: Option<String>,
}

#[derive(Default)]
pub(super) struct ConfigValues {
    pub(super) addopts: ConfigAddopts,
    pub(super) cache_dir: Option<String>,
    pub(super) ini_values: IndexMap<String, PytestIniSetting>,
}

#[derive(Default)]
pub(super) enum ConfigAddopts {
    #[default]
    Empty,
    Shell(String),
    Words(Vec<String>),
    Invalid(&'static str),
}

#[derive(Clone, Copy)]
enum ConfigValueMode {
    Ini,
    Toml,
}

const TOML_CONFIG_NAMES: &[&str] = &[
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
];
const LEGACY_CONFIG_NAMES: &[&str] = &[
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
];

pub(super) fn select_config(
    arguments: &Arguments,
    invocation_dir: &Path,
    policy: PytestConfigPolicy,
) -> ConfigResult<SelectedConfig> {
    if let Some(config_file) = arguments.config_file.as_deref() {
        let path = absolute_path(invocation_dir, Path::new(config_file));
        let config = load_config(&path, policy)?;
        return Ok(SelectedConfig {
            rootdir: if policy == PytestConfigPolicy::TomlWithNonFileConfig && !path.is_file() {
                invocation_dir
            } else {
                path.parent().unwrap_or(invocation_dir)
            }
            .to_path_buf(),
            file: Some(path),
            contents_fingerprint: config.contents_fingerprint,
            contents_sha256: config.contents_sha256,
            values: config.values.unwrap_or_default(),
        });
    }

    let target_dirs = target_directories(&arguments.targets, invocation_dir);
    let ancestor = common_ancestor(target_dirs.iter().cloned(), invocation_dir);
    if let Some(config) = locate_config(std::slice::from_ref(&ancestor), invocation_dir, policy)? {
        return Ok(config);
    }

    if arguments.rootdir.is_none() {
        for possible_root in std::iter::once(ancestor.as_path()).chain(ancestor.ancestors().skip(1))
        {
            if possible_root.join("setup.py").is_file() {
                return Ok(SelectedConfig {
                    file: None,
                    contents_fingerprint: config_contents_fingerprint(""),
                    contents_sha256: None,
                    rootdir: possible_root.to_path_buf(),
                    values: ConfigValues::default(),
                });
            }
        }
    }

    if arguments.rootdir.is_none()
        && !(target_dirs.len() == 1 && target_dirs[0] == ancestor)
        && let Some(config) = locate_config(&target_dirs, invocation_dir, policy)?
    {
        return Ok(config);
    }
    let rootdir = common_ancestor(
        [invocation_dir.to_path_buf(), ancestor.clone()],
        invocation_dir,
    );
    let rootdir = if rootdir.parent().is_none() {
        ancestor
    } else {
        rootdir
    };
    Ok(SelectedConfig {
        file: None,
        contents_fingerprint: config_contents_fingerprint(""),
        contents_sha256: None,
        rootdir,
        values: ConfigValues::default(),
    })
}

fn locate_config(
    starts: &[PathBuf],
    invocation_dir: &Path,
    policy: PytestConfigPolicy,
) -> ConfigResult<Option<SelectedConfig>> {
    let names = match policy {
        PytestConfigPolicy::LegacyWithoutArgumentFiles | PytestConfigPolicy::Legacy => {
            LEGACY_CONFIG_NAMES
        }
        PytestConfigPolicy::Toml
        | PytestConfigPolicy::TomlWithAddoptsOverrides
        | PytestConfigPolicy::TomlWithNonFileConfig => TOML_CONFIG_NAMES,
    };
    let starts = if starts.is_empty() {
        vec![invocation_dir.to_path_buf()]
    } else {
        starts.to_vec()
    };
    let mut fallback_pyproject = None;
    for start in starts {
        for base in std::iter::once(start.as_path()).chain(start.ancestors().skip(1)) {
            for name in names {
                let path = base.join(name);
                if !path.is_file() {
                    continue;
                }
                let config = load_config(&path, policy)?;
                if *name == "pyproject.toml" && fallback_pyproject.is_none() {
                    fallback_pyproject = Some((
                        path.clone(),
                        config.contents_fingerprint.clone(),
                        config.contents_sha256.clone(),
                    ));
                }
                if let Some(values) = config.values {
                    return Ok(Some(SelectedConfig {
                        file: Some(path),
                        contents_fingerprint: config.contents_fingerprint,
                        contents_sha256: config.contents_sha256,
                        rootdir: base.to_path_buf(),
                        values,
                    }));
                }
            }
        }
    }
    if let Some((path, contents_fingerprint, contents_sha256)) = fallback_pyproject {
        let rootdir = path.parent().unwrap_or(invocation_dir).to_path_buf();
        return Ok(Some(SelectedConfig {
            file: Some(path),
            contents_fingerprint,
            contents_sha256,
            rootdir,
            values: ConfigValues::default(),
        }));
    }
    Ok(None)
}

fn load_config(path: &Path, policy: PytestConfigPolicy) -> ConfigResult<LoadedConfig> {
    let filename = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    if !matches!(
        path.extension().and_then(OsStr::to_str),
        Some("ini" | "cfg" | "toml")
    ) {
        return Ok(LoadedConfig {
            values: None,
            contents_fingerprint: config_contents_fingerprint(""),
            contents_sha256: None,
        });
    }
    let content = read_config(path)?;
    let contents_fingerprint = config_contents_fingerprint(&content);
    let contents_sha256 = Some(crate::output::sha256_hex(content.as_bytes()));
    let values = match path.extension() {
        Some(extension) if extension == OsStr::new("ini") || extension == OsStr::new("cfg") => {
            load_ini_config(path, filename, &content)
        }
        Some(extension) if extension == OsStr::new("toml") => {
            load_toml_config(path, filename, policy, &content)
        }
        _ => Ok(None),
    }?;
    Ok(LoadedConfig {
        values,
        contents_fingerprint,
        contents_sha256,
    })
}

pub(super) fn config_contents_fingerprint(content: &str) -> String {
    crate::selection::replay_fingerprint([content])
}

fn load_ini_config(
    path: &Path,
    filename: &str,
    content: &str,
) -> ConfigResult<Option<ConfigValues>> {
    let sections = parse_ini(content, path)?;
    let is_cfg_file = path.extension() == Some(OsStr::new("cfg"));
    let section_name = if is_cfg_file { "tool:pytest" } else { "pytest" };
    if is_cfg_file && sections.contains_key("pytest") && !sections.contains_key(section_name) {
        return Err(config_error(
            path,
            "[pytest] section in setup.cfg is no longer supported; use [tool:pytest] instead",
        ));
    }
    if let Some(values) = sections.get(section_name) {
        return Ok(Some(ConfigValues {
            addopts: values.get("addopts").map_or(ConfigAddopts::Empty, |value| {
                ConfigAddopts::Shell(value.clone())
            }),
            cache_dir: values.get("cache_dir").cloned(),
            ini_values: values
                .iter()
                .map(|(name, value)| {
                    (
                        name.clone(),
                        PytestIniSetting {
                            value: toml::Value::String(value.clone()),
                            mode: PytestIniMode::Ini,
                            overridden: false,
                        },
                    )
                })
                .collect(),
        }));
    }
    if matches!(filename, "pytest.ini" | ".pytest.ini") {
        return Ok(Some(ConfigValues::default()));
    }
    Ok(None)
}

fn parse_ini(content: &str, path: &Path) -> ConfigResult<IniSections> {
    let mut sections: IniSections = BTreeMap::new();
    let mut current: Option<String> = None;
    let mut last_key: Option<String> = None;
    for (line_index, raw_line) in content.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if raw_line.starts_with('[') {
            let header = line.split(['#', ';']).next().unwrap_or(line).trim_end();
            let Some(name) = header
                .strip_prefix('[')
                .and_then(|line| line.strip_suffix(']'))
            else {
                return Err(config_error(
                    path,
                    format!("invalid section header at line {}", line_index + 1),
                ));
            };
            let name = name.to_owned();
            if name.is_empty() || sections.contains_key(&name) {
                return Err(config_error(
                    path,
                    format!("invalid or duplicate section at line {}", line_index + 1),
                ));
            }
            sections.insert(name.clone(), IndexMap::new());
            current = Some(name);
            last_key = None;
            continue;
        }
        if raw_line.starts_with(char::is_whitespace) {
            let Some(value) = current
                .as_ref()
                .zip(last_key.as_ref())
                .and_then(|(section, key)| {
                    sections
                        .get_mut(section)
                        .and_then(|values| values.get_mut(key))
                })
            else {
                return Err(config_error(
                    path,
                    format!("unexpected value continuation at line {}", line_index + 1),
                ));
            };
            if !value.is_empty() {
                value.push('\n');
            }
            value.push_str(line);
            continue;
        }
        let Some(section) = current.as_ref() else {
            return Err(config_error(
                path,
                format!("option outside a section at line {}", line_index + 1),
            ));
        };
        let Some((key, value)) = line.split_once(['=', ':']) else {
            return Err(config_error(
                path,
                format!("invalid option at line {}", line_index + 1),
            ));
        };
        let key = key.trim().to_owned();
        if key.is_empty() {
            return Err(config_error(
                path,
                format!("empty option at line {}", line_index + 1),
            ));
        }
        let Some(values) = sections.get_mut(section) else {
            return Err(config_error(
                path,
                format!("invalid section state at line {}", line_index + 1),
            ));
        };
        if values.contains_key(&key) {
            return Err(config_error(
                path,
                format!("duplicate option '{key}' at line {}", line_index + 1),
            ));
        }
        values.insert(key.clone(), value.trim().to_owned());
        last_key = Some(key);
    }
    Ok(sections)
}

fn load_toml_config(
    path: &Path,
    filename: &str,
    policy: PytestConfigPolicy,
    content: &str,
) -> ConfigResult<Option<ConfigValues>> {
    let document: toml::Value =
        toml::from_str(content).map_err(|error| config_error(path, error))?;
    let Some((table, mode)) = select_toml_table(&document, path, filename, policy)? else {
        return Ok(None);
    };
    let addopts = parse_toml_addopts(&table, mode);
    let cache_dir = parse_toml_cache_dir(&table, path, mode)?;
    let ini_values = table
        .iter()
        .map(|(name, value)| {
            (
                name.clone(),
                PytestIniSetting {
                    value: value.clone(),
                    mode: match mode {
                        ConfigValueMode::Ini => PytestIniMode::Ini,
                        ConfigValueMode::Toml => PytestIniMode::Toml,
                    },
                    overridden: false,
                },
            )
        })
        .collect();
    Ok(Some(ConfigValues {
        addopts,
        cache_dir,
        ini_values,
    }))
}

fn select_toml_table<'a>(
    document: &'a toml::Value,
    path: &Path,
    filename: &str,
    policy: PytestConfigPolicy,
) -> ConfigResult<Option<(TomlTable<'a>, ConfigValueMode)>> {
    if matches!(filename, "pytest.toml" | ".pytest.toml") {
        if !matches!(
            policy,
            PytestConfigPolicy::LegacyWithoutArgumentFiles | PytestConfigPolicy::Legacy
        ) {
            let table = document
                .get("pytest")
                .map(|value| {
                    value.as_table().map(Cow::Borrowed).ok_or_else(|| {
                        config_error(path, "[pytest] in pytest.toml must be a table")
                    })
                })
                .transpose()?
                .unwrap_or_default();
            return Ok(Some((table, ConfigValueMode::Toml)));
        }
        return Ok(None);
    }

    let Some(tool) = document.get("tool") else {
        return Ok(None);
    };
    let Some(pytest_value) = tool.get("pytest") else {
        return Ok(None);
    };
    let pytest = pytest_value
        .as_table()
        .ok_or_else(|| config_error(path, "[tool.pytest] must be a table"))?;
    match policy {
        PytestConfigPolicy::LegacyWithoutArgumentFiles | PytestConfigPolicy::Legacy => {
            let Some(ini_value) = pytest.get("ini_options") else {
                return Ok(None);
            };
            let table = ini_value
                .as_table()
                .map(Cow::Borrowed)
                .ok_or_else(|| config_error(path, "[tool.pytest.ini_options] must be a table"))?;
            Ok(Some((table, ConfigValueMode::Ini)))
        }
        PytestConfigPolicy::Toml
        | PytestConfigPolicy::TomlWithAddoptsOverrides
        | PytestConfigPolicy::TomlWithNonFileConfig => select_toml_or_ini_table(pytest, path),
    }
}

fn select_toml_or_ini_table<'a>(
    pytest: &'a toml::map::Map<String, toml::Value>,
    path: &Path,
) -> ConfigResult<Option<(TomlTable<'a>, ConfigValueMode)>> {
    let has_toml_values = pytest.keys().any(|key| key != "ini_options");
    let ini_table = pytest
        .get("ini_options")
        .map(|value| {
            value
                .as_table()
                .ok_or_else(|| config_error(path, "[tool.pytest.ini_options] must be a table"))
        })
        .transpose()?;
    if has_toml_values && ini_table.is_some_and(|table| !table.is_empty()) {
        return Err(config_error(
            path,
            "cannot use both [tool.pytest] and [tool.pytest.ini_options]",
        ));
    }
    if has_toml_values {
        return Ok(Some((Cow::Borrowed(pytest), ConfigValueMode::Toml)));
    }
    let Some(table) = ini_table else {
        return Ok(None);
    };
    Ok(Some((Cow::Borrowed(table), ConfigValueMode::Ini)))
}

fn parse_toml_addopts(
    table: &toml::map::Map<String, toml::Value>,
    mode: ConfigValueMode,
) -> ConfigAddopts {
    let Some(value) = table.get("addopts") else {
        return ConfigAddopts::Empty;
    };
    match (mode, value) {
        (ConfigValueMode::Toml, toml::Value::Array(values)) => {
            let words: Option<Vec<_>> = values
                .iter()
                .map(|value| value.as_str().map(str::to_owned))
                .collect();
            words.map_or(
                ConfigAddopts::Invalid("pytest TOML addopts must contain only strings"),
                ConfigAddopts::Words,
            )
        }
        (ConfigValueMode::Toml, _) => {
            ConfigAddopts::Invalid("pytest TOML addopts must be an array of strings")
        }
        (ConfigValueMode::Ini, toml::Value::String(value)) => ConfigAddopts::Shell(value.clone()),
        (ConfigValueMode::Ini, toml::Value::Array(values)) => {
            let words: Option<Vec<_>> = values
                .iter()
                .map(|value| value.as_str().map(str::to_owned))
                .collect();
            words.map_or(
                ConfigAddopts::Invalid("pytest addopts must contain only strings"),
                ConfigAddopts::Words,
            )
        }
        (ConfigValueMode::Ini, _) => {
            ConfigAddopts::Invalid("pytest addopts must be a string or an array of strings")
        }
    }
}

fn parse_toml_cache_dir(
    table: &toml::map::Map<String, toml::Value>,
    path: &Path,
    mode: ConfigValueMode,
) -> ConfigResult<Option<String>> {
    match table.get("cache_dir") {
        None => Ok(None),
        Some(toml::Value::String(value)) => Ok(Some(value.clone())),
        Some(toml::Value::Integer(value)) if matches!(mode, ConfigValueMode::Ini) => {
            Ok(Some(value.to_string()))
        }
        Some(toml::Value::Boolean(value)) if matches!(mode, ConfigValueMode::Ini) => {
            Ok(Some(if *value { "True" } else { "False" }.to_owned()))
        }
        Some(toml::Value::Float(value)) if matches!(mode, ConfigValueMode::Ini) => {
            Ok(Some(python_float_string(*value)))
        }
        Some(_) => Err(config_error(path, "pytest cache_dir must be a string")),
    }
}

fn python_float_string(value: f64) -> String {
    // JSON's shortest round-trip formatter shares Python's ties-to-even rounding.
    // Rust Debug rounds some equally short decimal representations differently.
    let value = serde_json::Number::from_f64(value).map_or_else(
        || format!("{value:?}").to_ascii_lowercase(),
        |number| number.to_string(),
    );
    let (sign, unsigned) = value
        .strip_prefix('-')
        .map_or(("", value.as_str()), |unsigned| ("-", unsigned));
    // Python switches to scientific notation below 1e-4; JSON retains 1e-5.
    if let Some(fraction) = unsigned.strip_prefix("0.")
        && let Some(zeros) = fraction.find(|digit| digit != '0')
        && zeros >= 4
    {
        let (first, remaining) = fraction[zeros..].split_at(1);
        let mantissa = if remaining.is_empty() {
            first.to_owned()
        } else {
            format!("{first}.{remaining}")
        };
        return format!("{sign}{mantissa}e-{:02}", zeros + 1);
    }
    let Some((mantissa, exponent)) = value.split_once('e') else {
        return value;
    };
    let exponent = exponent.strip_prefix('+').unwrap_or(exponent);
    let (sign, digits) = exponent
        .strip_prefix('-')
        .map_or(("+", exponent), |digits| ("-", digits));
    format!("{mantissa}e{sign}{digits:0>2}")
}

fn read_config(path: &Path) -> ConfigResult<String> {
    let bytes = fs::read(path).map_err(|error| PytestConfigError {
        message: format!("failed to read configuration: {error} ({})", path.display()),
        failure: ConfigFailure::Read(path.to_path_buf(), error),
    })?;
    String::from_utf8(bytes).map_err(|error| PytestConfigError {
        message: format!(
            "failed to read configuration: stream did not contain valid UTF-8 ({})",
            path.display()
        ),
        failure: ConfigFailure::Decode(path.to_path_buf(), error),
    })
}

pub(super) fn config_error(path: &Path, message: impl fmt::Display) -> PytestConfigError {
    PytestConfigError::message(format!("{} ({})", message, path.display()))
}
