//! Coverage.py and vfy configuration parsing exposed to Python.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use indexmap::IndexMap;
use pyo3::IntoPyObjectExt;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use serde_json::{Map, Value};

use vfy_core::canonical_path;

#[cfg(test)]
mod tests;

const BARE_SECTIONS: [&str; 7] = ["run", "report", "html", "xml", "json", "lcov", "paths"];
const DISCOVERY_NAMES: [&str; 4] = [".coveragerc", ".coveragerc.toml", "setup.cfg", "tox.ini"];

fn is_null_device(path: &Path) -> bool {
    #[cfg(windows)]
    {
        path.to_string_lossy().eq_ignore_ascii_case("nul")
    }
    #[cfg(not(windows))]
    {
        path == Path::new("/dev/null")
    }
}

#[derive(Clone, Copy)]
enum Kind {
    Bool,
    Integer,
    Number,
    List,
    Text,
    Workers,
}

fn supported_options(section: &str) -> Option<&'static [(&'static str, Kind)]> {
    match section {
        "run" => Some(&[
            ("branch", Kind::Bool),
            ("concurrency", Kind::List),
            ("context", Kind::Text),
            ("core", Kind::Text),
            ("cover_pylib", Kind::Bool),
            ("data_file", Kind::Text),
            ("disable_warnings", Kind::List),
            ("dynamic_context", Kind::Text),
            ("include", Kind::List),
            ("omit", Kind::List),
            ("parallel", Kind::Bool),
            ("patch", Kind::List),
            ("plugins", Kind::List),
            ("relative_files", Kind::Bool),
            ("sigterm", Kind::Bool),
            ("source", Kind::List),
            ("source_pkgs", Kind::List),
        ]),
        "report" => Some(&[
            ("contexts", Kind::List),
            ("exclude_also", Kind::List),
            ("exclude_lines", Kind::List),
            ("fail_under", Kind::Number),
            ("include", Kind::List),
            ("omit", Kind::List),
            ("partial_branches", Kind::List),
            ("partial_also", Kind::List),
            ("precision", Kind::Integer),
            ("format", Kind::Text),
            ("ignore_errors", Kind::Bool),
            ("show_missing", Kind::Bool),
            ("skip_covered", Kind::Bool),
            ("skip_empty", Kind::Bool),
            ("sort", Kind::Text),
        ]),
        "html" => Some(&[
            ("directory", Kind::Text),
            ("show_contexts", Kind::Bool),
            ("skip_covered", Kind::Bool),
            ("skip_empty", Kind::Bool),
            ("title", Kind::Text),
        ]),
        "xml" => Some(&[("output", Kind::Text), ("package_depth", Kind::Integer)]),
        "lcov" => Some(&[("output", Kind::Text)]),
        "json" => Some(&[
            ("output", Kind::Text),
            ("pretty_print", Kind::Bool),
            ("show_contexts", Kind::Bool),
        ]),
        "annotate" => Some(&[("directory", Kind::Text)]),
        _ => None,
    }
}

fn vfy_options(section: &str) -> Option<&'static [(&'static str, Kind)]> {
    match section {
        "run" => Some(&[("decisions", Kind::Bool)]),
        "test" => Some(&[
            ("history", Kind::Text),
            ("selection", Kind::Text),
            ("source", Kind::List),
            ("workers", Kind::Workers),
        ]),
        "mutate" => Some(&[
            ("history", Kind::Text),
            ("source", Kind::List),
            ("workers", Kind::Workers),
            ("timeout", Kind::Number),
            ("timeout_policy", Kind::Text),
        ]),
        "check" => Some(&[
            ("fail_under_decisions", Kind::Number),
            ("fail_under_conditions", Kind::Number),
            ("fail_under_mcdc", Kind::Number),
            ("format", Kind::Text),
            ("output", Kind::Text),
            ("include", Kind::List),
            ("omit", Kind::List),
            ("contexts", Kind::List),
            ("exclude_decisions", Kind::List),
        ]),
        _ => None,
    }
}

fn normalize_paths(raw_values: Value, path: &Path) -> PyResult<Value> {
    let Value::Object(values) = raw_values else {
        return Err(config_error(
            path,
            "configuration section [paths] must be a table",
        ));
    };
    let mut normalized = Map::new();
    for (name, value) in values {
        // Coverage.py uses the TOML array entries verbatim, including spaces and empty results.
        let aliases = match &value {
            Value::Array(items) => items
                .iter()
                .map(|item| item.as_str().map(|text| Value::String(text.to_owned())))
                .collect::<Option<Vec<Value>>>(),
            _ => normalized_list(&value),
        }
        .filter(|aliases| !aliases.is_empty())
        .ok_or_else(|| config_error(path, format!("invalid value for [paths] {name}")))?;
        for alias in &aliases {
            let alias = alias.as_str().expect("normalized lists contain strings");
            if alias.trim_end_matches(['/', '\\']).ends_with('*') {
                return Err(config_error(
                    path,
                    format!("[paths] {name} pattern must not end with wildcards"),
                ));
            }
        }
        normalized.insert(name, Value::Array(aliases));
    }
    Ok(Value::Object(normalized))
}

fn config_error(path: &Path, message: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(format!("{message} ({})", path.display()))
}

fn parse_toml(path: &Path) -> PyResult<Value> {
    let source = std::fs::read_to_string(path).map_err(|error| {
        config_error(path, format!("failed to read TOML configuration ({error})"))
    })?;
    let value: toml::Value = toml::from_str(&source).map_err(|error| {
        config_error(path, format!("failed to read TOML configuration ({error})"))
    })?;
    serde_json::to_value(value).map_err(|error| {
        config_error(
            path,
            format!("failed to decode TOML configuration ({error})"),
        )
    })
}

/// Whether Coverage.py treats a TOML file as its own configuration file.
#[derive(Clone, Copy, PartialEq, Eq)]
enum TomlOwnership {
    /// An explicit file, `COVERAGE_RCFILE`, or `.coveragerc.toml`: bare sections are read.
    Own,
    /// A discovered `pyproject.toml`: only `[tool.coverage.*]` is read.
    Shared,
}

fn toml_ownership(path: &Path, explicit: bool) -> TomlOwnership {
    let rcfile = std::env::var_os("COVERAGE_RCFILE").is_some_and(|value| !value.is_empty());
    if explicit || rcfile || path.file_name().is_none_or(|name| name != "pyproject.toml") {
        TomlOwnership::Own
    } else {
        TomlOwnership::Shared
    }
}

/// Coverage.py reads each section from `tool.coverage.<section>` first and, in its own
/// files, from the bare top-level `<section>` otherwise; sections are not merged.
fn select_toml_config(
    value: &Value,
    path: &Path,
    ownership: TomlOwnership,
) -> PyResult<Map<String, Value>> {
    let tool_coverage = match value.get("tool").and_then(|tool| tool.get("coverage")) {
        None => Map::new(),
        Some(Value::Object(table)) => table.clone(),
        Some(_) => {
            return Err(config_error(
                path,
                "coverage configuration root must be a table",
            ));
        }
    };
    let mut selected = Map::new();
    if ownership == TomlOwnership::Own {
        let plugins = tool_coverage
            .get("run")
            .or_else(|| value.get("run"))
            .and_then(|run| run.get("plugins"))
            .and_then(normalized_list)
            .unwrap_or_default();
        let plugins = plugins.iter().filter_map(Value::as_str);
        for name in BARE_SECTIONS.into_iter().chain(plugins) {
            if let Some(section) = value.get(name) {
                selected.insert(name.to_owned(), section.clone());
            }
        }
    }
    selected.extend(tool_coverage);
    if let Some(vfy) = value.get("tool").and_then(|tool| tool.get("vfy")).cloned() {
        selected.insert("vfy".to_owned(), vfy);
    }
    Ok(selected)
}

fn selected_toml_config(path: &Path, ownership: TomlOwnership) -> PyResult<Map<String, Value>> {
    select_toml_config(&parse_toml(path)?, path, ownership)
}

fn parse_ini(path: &Path) -> PyResult<Map<String, Value>> {
    let source = std::fs::read_to_string(path)
        .map_err(|error| config_error(path, format!("failed to read configuration ({error})")))?;
    let mut sections: IndexMap<String, IndexMap<String, String>> = IndexMap::new();
    let mut current: Option<String> = None;
    let mut previous_key: Option<String> = None;
    let mut previous_indent = 0;
    for (index, raw_line) in source.lines().enumerate() {
        let line_number = index + 1;
        let trimmed = raw_line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with(';') {
            continue;
        }
        if let Some(section) = trimmed
            .strip_prefix('[')
            .and_then(|line| line.strip_suffix(']'))
        {
            let section = section.trim().to_owned();
            if section.is_empty() || sections.contains_key(&section) {
                return Err(config_error(
                    path,
                    format!("invalid or duplicate section at line {line_number}"),
                ));
            }
            sections.insert(section.clone(), IndexMap::new());
            current = Some(section);
            previous_key = None;
            previous_indent = 0;
            continue;
        }
        let indent = raw_line.len() - raw_line.trim_start().len();
        if let Some(key) = previous_key.as_ref().filter(|_| indent > previous_indent) {
            let section = current.as_ref().expect("an option has a section");
            let value = sections
                .get_mut(section)
                .and_then(|values| values.get_mut(key))
                .expect("current INI option exists");
            value.push('\n');
            value.push_str(trimmed);
            continue;
        }
        let section = current.as_ref().ok_or_else(|| {
            config_error(
                path,
                format!("option outside a section at line {line_number}"),
            )
        })?;
        let delimiter = raw_line
            .char_indices()
            .find(|(_, character)| matches!(character, '=' | ':'))
            .map(|(offset, _)| offset)
            .ok_or_else(|| {
                config_error(path, format!("option has no value at line {line_number}"))
            })?;
        let key = raw_line[..delimiter].trim().to_ascii_lowercase();
        let value = raw_line[delimiter + 1..].trim().to_owned();
        let values = sections
            .get_mut(section)
            .expect("current INI section exists");
        if key.is_empty() || values.insert(key.clone(), value).is_some() {
            return Err(config_error(
                path,
                format!("invalid or duplicate option at line {line_number}"),
            ));
        }
        previous_key = Some(key);
        previous_indent = indent;
    }

    resolve_ini_sections(sections, path)
}

fn resolve_ini_sections(
    mut sections: IndexMap<String, IndexMap<String, String>>,
    path: &Path,
) -> PyResult<Map<String, Value>> {
    let defaults = sections.shift_remove("DEFAULT").unwrap_or_default();
    let namespaced = path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| matches!(name, "setup.cfg" | "tox.ini"));
    let mut result = Map::new();
    for (raw_section, mut values) in sections {
        let section = if namespaced {
            let Some(section) = raw_section.strip_prefix("coverage:") else {
                continue;
            };
            section.to_owned()
        } else {
            raw_section
                .strip_prefix("coverage:")
                .unwrap_or(&raw_section)
                .to_owned()
        };
        if let Some(supported) = supported_options(&section) {
            for (key, _) in supported {
                if let Some(value) = defaults.get(*key) {
                    values
                        .entry((*key).to_owned())
                        .or_insert_with(|| value.clone());
                }
            }
        }
        if result.contains_key(&section) {
            return Err(config_error(
                path,
                format!("duplicate normalized configuration section [{section}]"),
            ));
        }
        result.insert(
            section,
            Value::Object(
                values
                    .into_iter()
                    .map(|(key, value)| (key, Value::String(value)))
                    .collect(),
            ),
        );
    }
    Ok(result)
}

fn substitute_environment(text: &str, path: &Path, section: &str, key: &str) -> PyResult<String> {
    let bytes = text.as_bytes();
    let mut output = String::with_capacity(text.len());
    let mut offset = 0;
    while offset < bytes.len() {
        if bytes[offset] != b'$' {
            let character = text[offset..].chars().next().expect("offset is in bounds");
            output.push(character);
            offset += character.len_utf8();
            continue;
        }
        if bytes.get(offset + 1) == Some(&b'$') {
            output.push('$');
            offset += 2;
            continue;
        }

        let (name, strict, default, next) = if bytes.get(offset + 1) == Some(&b'{') {
            let Some(relative_end) = text[offset + 2..].find('}') else {
                output.push('$');
                offset += 1;
                continue;
            };
            let end = offset + 2 + relative_end;
            let expression = &text[offset + 2..end];
            let (name, strict, default) = if let Some(name) = expression.strip_suffix('?') {
                (name, true, None)
            } else if let Some((name, default)) = expression.split_once('-') {
                (name, false, Some(default))
            } else {
                (expression, false, None)
            };
            (name, strict, default, end + 1)
        } else {
            let start = offset + 1;
            let mut end = start;
            while end < bytes.len() && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'_') {
                end += 1;
            }
            if end == start {
                output.push('$');
                offset += 1;
                continue;
            }
            (&text[start..end], false, None, end)
        };
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
        {
            output.push_str(&text[offset..next]);
        } else if let Ok(value) = std::env::var(name) {
            output.push_str(&value);
        } else if strict {
            return Err(config_error(
                path,
                format!("environment variable {name} is undefined for [{section}] {key}"),
            ));
        } else if let Some(default) = default {
            output.push_str(default);
        }
        offset = next;
    }
    Ok(output)
}

fn substitute_config_environment(config: &mut Map<String, Value>, path: &Path) -> PyResult<()> {
    for (section, values) in config {
        let Some(values) = values.as_object_mut() else {
            continue;
        };
        if section == "vfy" {
            for (name, options) in values {
                if let Some(options) = options.as_object_mut() {
                    substitute_section_environment(options, path, &format!("tool.vfy.{name}"))?;
                }
            }
        } else {
            substitute_section_environment(values, path, section)?;
        }
    }
    Ok(())
}

fn substitute_section_environment(
    values: &mut Map<String, Value>,
    path: &Path,
    section: &str,
) -> PyResult<()> {
    for (key, value) in values {
        match value {
            Value::String(text) => {
                *text = substitute_environment(text, path, section, key)?;
            }
            Value::Array(items) => {
                for item in items {
                    if let Value::String(text) = item {
                        *text = substitute_environment(text, path, section, key)?;
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

#[expect(
    clippy::too_many_lines,
    reason = "Option coercion and cross-option validation share the normalized result."
)]
fn normalize(config: Map<String, Value>, path: &Path) -> PyResult<Map<String, Value>> {
    let plugin_names: BTreeSet<String> = config
        .get("run")
        .and_then(Value::as_object)
        .and_then(|run| run.get("plugins"))
        .and_then(normalized_list)
        .into_iter()
        .flatten()
        .filter_map(|name| name.as_str().map(str::to_owned))
        .collect();
    let mut result = Map::new();
    for (section, raw_values) in config {
        if section == "vfy" {
            let Value::Object(sections) = raw_values else {
                return Err(config_error(
                    path,
                    "configuration section [vfy] must be a table",
                ));
            };
            let mut normalized_vfy = Map::new();
            for (name, values) in sections {
                let supported = vfy_options(&name).ok_or_else(|| {
                    config_error(
                        path,
                        format!("unsupported configuration section [tool.vfy.{name}]"),
                    )
                })?;
                let Value::Object(mut values) = values else {
                    return Err(config_error(
                        path,
                        format!("configuration section [tool.vfy.{name}] must be a table"),
                    ));
                };
                let mut normalized = Map::new();
                for (key, kind) in supported {
                    if let Some(value) = values.remove(*key) {
                        let value = normalize_vfy_value(&name, key, *kind, &value, path)?;
                        normalized.insert((*key).to_owned(), value);
                    }
                }
                if !values.is_empty() {
                    let unknown: Vec<&str> = values.keys().map(String::as_str).collect();
                    return Err(config_error(
                        path,
                        format!(
                            "unsupported option(s) in [tool.vfy.{name}]: {}",
                            unknown.join(", ")
                        ),
                    ));
                }
                normalized_vfy.insert(name, Value::Object(normalized));
            }
            result.insert(section, Value::Object(normalized_vfy));
            continue;
        }
        if section == "paths" {
            result.insert(section, normalize_paths(raw_values, path)?);
            continue;
        }
        let plugin_section = if section == "plugin" || section.starts_with("plugin:") {
            Some(section.clone())
        } else if plugin_names.contains(&section) {
            Some(format!("plugin:{section}"))
        } else {
            None
        };
        if let Some(plugin_section) = plugin_section {
            if result.insert(plugin_section.clone(), raw_values).is_some() {
                return Err(config_error(
                    path,
                    format!("duplicate normalized configuration section [{plugin_section}]"),
                ));
            }
            continue;
        }
        let supported = supported_options(&section).ok_or_else(|| {
            config_error(
                path,
                format!("unsupported configuration section [{section}]"),
            )
        })?;
        let Value::Object(mut values) = raw_values else {
            return Err(config_error(
                path,
                format!("configuration section [{section}] must be a table"),
            ));
        };
        let declared: Vec<(&str, Kind, Value)> = supported
            .iter()
            .filter_map(|(name, kind)| values.remove(*name).map(|value| (*name, *kind, value)))
            .collect();
        if !values.is_empty() {
            let unknown: Vec<&str> = values.keys().map(String::as_str).collect();
            return Err(config_error(
                path,
                format!(
                    "unsupported option(s) in [{section}]: {}",
                    unknown.join(", ")
                ),
            ));
        }
        let mut normalized = Map::new();
        for (key, kind, value) in declared {
            let value = normalize_value(kind, &value).ok_or_else(|| {
                config_error(path, format!("invalid value for [{section}] {key}"))
            })?;
            normalized.insert(key.to_owned(), value);
        }
        result.insert(section, Value::Object(normalized));
    }
    validate_patch(&result, path)?;
    validate_concurrency(&result, path)?;
    validate_core(&result, path)?;
    validate_report_choices(&result, path)?;
    Ok(result)
}

fn validate_report_choices(config: &Map<String, Value>, path: &Path) -> PyResult<()> {
    let Some(report) = config.get("report").and_then(Value::as_object) else {
        return Ok(());
    };
    for (name, allowed) in [
        ("format", &["text", "markdown", "total"][..]),
        (
            "sort",
            &["name", "stmts", "miss", "branch", "brpart", "cover"][..],
        ),
    ] {
        if let Some(value) = report.get(name).and_then(Value::as_str) {
            let normalized = if name == "sort" {
                value.strip_prefix('-').unwrap_or(value)
            } else {
                value
            }
            .to_ascii_lowercase();
            if !allowed.contains(&normalized.as_str()) {
                return Err(config_error(
                    path,
                    format!("invalid value for [report] {name}: {value:?}"),
                ));
            }
        }
    }
    Ok(())
}

fn normalize_vfy_value(
    section: &str,
    key: &str,
    kind: Kind,
    raw: &Value,
    path: &Path,
) -> PyResult<Value> {
    let normalized = normalize_value(kind, raw);
    let value = normalized.as_ref().unwrap_or(&Value::Null);
    let (valid, expected) = match key {
        "selection" => (
            value
                .as_str()
                .is_some_and(|mode| matches!(mode, "safe" | "balanced" | "aggressive")),
            "safe, balanced, or aggressive",
        ),
        "workers" => (
            match value {
                Value::String(workers) => matches!(workers.as_str(), "auto" | "logical"),
                Value::Number(count) => count
                    .as_u64()
                    .is_some_and(|count| (1..=u64::from(vfy_core::MAX_WORKERS)).contains(&count)),
                _ => false,
            },
            vfy_core::WORKERS_EXPECTED,
        ),
        "format" => (
            value
                .as_str()
                .is_some_and(|format| matches!(format, "text" | "json")),
            "text or json",
        ),
        "timeout_policy" => (
            value
                .as_str()
                .is_some_and(|policy| matches!(policy, "adaptive" | "fixed")),
            "adaptive or fixed",
        ),
        "timeout" => (
            value.as_f64().is_some_and(|timeout| timeout > 0.0),
            "a finite number greater than zero",
        ),
        "fail_under_decisions" | "fail_under_conditions" | "fail_under_mcdc" => (
            value
                .as_f64()
                .is_some_and(|threshold| (0.0..=100.0).contains(&threshold)),
            "a percentage from 0 to 100",
        ),
        _ => (
            true,
            match kind {
                Kind::Bool => "a boolean",
                Kind::Integer => "an integer",
                Kind::Number => "a finite number",
                Kind::List => "a list of strings",
                Kind::Text => "a string",
                Kind::Workers => vfy_core::WORKERS_EXPECTED,
            },
        ),
    };
    normalized.filter(|_| valid).ok_or_else(|| {
        config_error(
            path,
            format!("invalid value for [tool.vfy.{section}] {key}: {raw}; use {expected}"),
        )
    })
}

/// Validate command-line vfy settings with the same rules as configuration files.
#[pyfunction]
pub(crate) fn validate_vfy_options_json(section: &str, options: &str) -> PyResult<()> {
    let path = Path::new("command line");
    let options: Value = serde_json::from_str(options)
        .map_err(|error| config_error(path, format!("invalid command options: {error}")))?;
    let sections = Map::from_iter([(section.to_owned(), options)]);
    normalize(
        Map::from_iter([("vfy".to_owned(), Value::Object(sections))]),
        path,
    )?;
    Ok(())
}

fn normalize_value(kind: Kind, value: &Value) -> Option<Value> {
    match kind {
        Kind::Bool => normalized_bool(value).map(Value::Bool),
        Kind::Integer => normalized_integer(value).map(Value::from),
        Kind::Number => normalized_number(value).map(Value::from),
        Kind::List => normalized_list(value).map(Value::Array),
        Kind::Text => value.as_str().map(|value| Value::String(value.to_owned())),
        // A count is a number whether the file spelled it as a number or as text.
        Kind::Workers => match value {
            Value::Number(count) => count.as_u64().map(Value::from),
            Value::String(text) => Some(
                text.parse::<u64>()
                    .map_or_else(|_| Value::String(text.clone()), Value::from),
            ),
            _ => None,
        },
    }
}

fn normalized_bool(value: &Value) -> Option<bool> {
    if let Some(value) = value.as_bool() {
        return Some(value);
    }
    match value.as_str()?.trim().to_ascii_lowercase().as_str() {
        "1" | "yes" | "true" | "on" => Some(true),
        "0" | "no" | "false" | "off" => Some(false),
        _ => None,
    }
}

fn normalized_integer(value: &Value) -> Option<i64> {
    if let Some(value) = value.as_i64() {
        return Some(value);
    }
    value.as_str()?.trim().parse().ok()
}

fn normalized_number(value: &Value) -> Option<f64> {
    let number = match value {
        Value::Number(_) => value.as_f64()?,
        Value::String(text) => text.trim().parse().ok()?,
        _ => return None,
    };
    number.is_finite().then_some(number)
}

fn normalized_list(value: &Value) -> Option<Vec<Value>> {
    match value {
        Value::Array(values) => values
            .iter()
            .map(|value| Some(value.as_str()?.trim()))
            .collect::<Option<Vec<&str>>>()
            .map(|values| {
                values
                    .into_iter()
                    .filter(|value| !value.is_empty())
                    .map(|value| Value::String(value.to_owned()))
                    .collect()
            }),
        Value::String(text) => Some(
            text.replace('\n', ",")
                .split(',')
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(|value| Value::String(value.to_owned()))
                .collect(),
        ),
        _ => None,
    }
}

fn validate_patch(config: &Map<String, Value>, path: &Path) -> PyResult<()> {
    let Some(values) = config
        .get("run")
        .and_then(Value::as_object)
        .and_then(|run| run.get("patch"))
        .and_then(Value::as_array)
    else {
        return Ok(());
    };
    validate_patch_choices(values.iter().filter_map(Value::as_str))
        .map_err(|message| config_error(path, message))
}

fn validate_patch_choices<'a>(values: impl Iterator<Item = &'a str>) -> Result<(), String> {
    let unsupported: Vec<&str> = values
        .filter(|value| !matches!(*value, "subprocess" | "_exit"))
        .collect();
    if unsupported.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "unsupported [run] patch value(s): {}",
            unsupported.join(", ")
        ))
    }
}

/// Validate Python API patch choices before process hooks or collection start.
#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts an owned patch sequence; validation borrows its choices"
)]
pub(crate) fn validate_run_patch(patches: Vec<String>) -> PyResult<()> {
    validate_patch_choices(patches.iter().map(String::as_str)).map_err(PyValueError::new_err)
}

/// Coverage.py's documented `[run] concurrency` choices.
const CONCURRENCY_CHOICES: [&str; 5] = [
    "eventlet",
    "gevent",
    "greenlet",
    "multiprocessing",
    "thread",
];
/// Choices that sys.monitoring collection measures without a greenlet-aware tracer.
const SUPPORTED_CONCURRENCY: [&str; 2] = ["multiprocessing", "thread"];

fn validate_concurrency(config: &Map<String, Value>, path: &Path) -> PyResult<()> {
    let Some(values) = config
        .get("run")
        .and_then(Value::as_object)
        .and_then(|run| run.get("concurrency"))
        .and_then(Value::as_array)
    else {
        return Ok(());
    };
    let choices: BTreeSet<&str> = values.iter().filter_map(Value::as_str).collect();
    let unknown: Vec<&str> = choices
        .iter()
        .copied()
        .filter(|choice| !CONCURRENCY_CHOICES.contains(choice))
        .collect();
    if !unknown.is_empty() {
        return Err(config_error(
            path,
            format!(
                "unknown [run] concurrency choice(s): {}",
                unknown.join(", ")
            ),
        ));
    }
    let unsupported: Vec<&str> = choices
        .iter()
        .copied()
        .filter(|choice| !SUPPORTED_CONCURRENCY.contains(choice))
        .collect();
    if unsupported.is_empty() {
        Ok(())
    } else {
        Err(config_error(
            path,
            format!(
                "unsupported [run] concurrency value(s): {}; vfy measures thread and \
                 multiprocessing concurrency",
                unsupported.join(", ")
            ),
        ))
    }
}

/// Coverage.py's measurement cores. vfy always collects with sys.monitoring.
const CORES: [&str; 3] = ["ctrace", "pytrace", "sysmon"];

pub(crate) fn validate_core_name(core: &str) -> PyResult<()> {
    if CORES.contains(&core) {
        Ok(())
    } else {
        Err(PyValueError::new_err(format!(
            "Unknown core value: {core:?}"
        )))
    }
}

fn validate_core(config: &Map<String, Value>, path: &Path) -> PyResult<()> {
    let Some(core) = config
        .get("run")
        .and_then(Value::as_object)
        .and_then(|run| run.get("core"))
        .and_then(Value::as_str)
    else {
        return Ok(());
    };
    // vfy collects with sys.monitoring while selecting the branch-arc
    // conventions Coverage.py uses for the requested core.
    if validate_core_name(core).is_ok() {
        Ok(())
    } else {
        Err(config_error(
            path,
            format!("unknown [run] core value: {core}"),
        ))
    }
}

#[derive(Default)]
struct TomlProbes(BTreeMap<PathBuf, Option<Value>>);

impl TomlProbes {
    fn has_section(&mut self, path: &Path, section: &str) -> bool {
        self.0
            .entry(path.to_path_buf())
            // Invalid TOML does not participate in implicit discovery.
            .or_insert_with(|| parse_toml(path).ok())
            .as_ref()
            .and_then(|value| value.get("tool"))
            .and_then(|tool| tool.get(section))
            .is_some()
    }

    fn parsed(&self, path: &Path) -> Option<&Value> {
        self.0.get(path).and_then(Option::as_ref)
    }
}

fn discover_with_probes(start: Option<&str>, probes: &mut TomlProbes) -> PyResult<Option<PathBuf>> {
    if let Ok(configured) = std::env::var("COVERAGE_RCFILE")
        && !configured.is_empty()
    {
        let path = PathBuf::from(configured);
        if is_null_device(&path) {
            return Ok(None);
        }
        if !path.exists() {
            return Err(config_error(&path, "configuration file does not exist"));
        }
        return Ok(Some(path));
    }
    let current = canonical_path(Path::new(start.unwrap_or("."))).map_err(|error| {
        PyValueError::new_err(format!("failed to resolve config start: {error}"))
    })?;
    for candidate in DISCOVERY_NAMES {
        let path = current.join(candidate);
        if !path.exists() {
            continue;
        }
        if matches!(candidate, "setup.cfg" | "tox.ini") && parse_ini(&path)?.is_empty() {
            continue;
        }
        return Ok(Some(path));
    }
    let pyproject = current.join("pyproject.toml");
    if pyproject.exists() && probes.has_section(&pyproject, "coverage") {
        return Ok(Some(pyproject));
    }
    Ok(None)
}

fn discover(start: Option<&str>) -> PyResult<Option<PathBuf>> {
    discover_with_probes(start, &mut TomlProbes::default())
}

fn discover_vfy_with_probes(
    start: Option<&str>,
    probes: &mut TomlProbes,
) -> PyResult<Option<PathBuf>> {
    let current = canonical_path(Path::new(start.unwrap_or("."))).map_err(|error| {
        PyValueError::new_err(format!("failed to resolve config start: {error}"))
    })?;
    let mut directory = current;
    loop {
        let candidate = directory.join("pyproject.toml");
        if candidate.exists() && probes.has_section(&candidate, "vfy") {
            return Ok(Some(candidate));
        }
        if !directory.pop() {
            break;
        }
    }
    Ok(None)
}

fn discover_vfy(start: Option<&str>) -> PyResult<Option<PathBuf>> {
    discover_vfy_with_probes(start, &mut TomlProbes::default())
}

fn selected_vfy_config(path: &Path) -> PyResult<Map<String, Value>> {
    let value = parse_toml(path)?;
    let vfy = value
        .get("tool")
        .and_then(|tool| tool.get("vfy"))
        .cloned()
        .or_else(|| value.get("vfy").cloned())
        .ok_or_else(|| config_error(path, "configuration does not contain [tool.vfy]"))?;
    let mut selected = Map::new();
    selected.insert("vfy".to_owned(), vfy);
    Ok(selected)
}

#[pyfunction]
#[pyo3(signature = (path=None, start=None))]
pub(crate) fn discover_config(path: Option<&str>, start: Option<&str>) -> PyResult<Option<String>> {
    if let Some(path) = path {
        let path = Path::new(path);
        if is_null_device(path) {
            return Ok(None);
        }
        if !path.exists() {
            return Err(config_error(path, "configuration file does not exist"));
        }
        return Ok(Some(path.to_string_lossy().into_owned()));
    }
    discover(start).map(|path| path.map(|path| path.to_string_lossy().into_owned()))
}

#[pyfunction]
#[pyo3(signature = (path=None, start=None))]
pub(crate) fn discover_vfy_config(
    path: Option<&str>,
    start: Option<&str>,
) -> PyResult<Option<String>> {
    if let Some(path) = path {
        let path = Path::new(path);
        if !path.exists() {
            return Err(config_error(path, "configuration file does not exist"));
        }
        return Ok(Some(path.to_string_lossy().into_owned()));
    }
    discover_vfy(start).map(|path| path.map(|path| path.to_string_lossy().into_owned()))
}

#[pyfunction]
#[pyo3(signature = (path=None, start=None))]
pub(crate) fn load_config_json(path: Option<&str>, start: Option<&str>) -> PyResult<String> {
    let path_given = path.is_some();
    let Some(path) = discover_config(path, start)?.map(PathBuf::from) else {
        return Ok("{}".to_owned());
    };
    let config = load_selected_config(&path, path_given)?;
    serde_json::to_string(&config)
        .map_err(|error| config_error(&path, format!("failed to encode configuration ({error})")))
}

fn load_selected_config(path: &Path, explicit: bool) -> PyResult<Map<String, Value>> {
    load_selected_config_with_value(path, None, explicit)
}

fn load_selected_config_with_value(
    path: &Path,
    probed: Option<&Value>,
    explicit: bool,
) -> PyResult<Map<String, Value>> {
    let mut config = if path
        .extension()
        .is_some_and(|extension| extension == "toml")
    {
        let ownership = toml_ownership(path, explicit);
        if let Some(value) = probed {
            select_toml_config(value, path, ownership)?
        } else {
            selected_toml_config(path, ownership)?
        }
    } else {
        parse_ini(path)?
    };
    substitute_config_environment(&mut config, path)?;
    normalize(config, path)
}

struct ResolvedConfig {
    source: Option<String>,
    discovery: &'static str,
    settings: Map<String, Value>,
}

impl ResolvedConfig {
    fn into_value(self) -> Value {
        Value::Object(Map::from_iter([
            (
                "source".to_owned(),
                self.source.map_or(Value::Null, Value::String),
            ),
            (
                "discovery".to_owned(),
                Value::String(self.discovery.to_owned()),
            ),
            ("settings".to_owned(), Value::Object(self.settings)),
        ]))
    }
}

fn resolve_config(
    scope: &str,
    path: Option<&str>,
    start: Option<&str>,
) -> PyResult<ResolvedConfig> {
    let explicit = path.is_some();
    let mut probes = TomlProbes::default();
    let (selected, discovery) = match scope {
        "project" if path.is_none() => {
            if let Some(path) = discover_vfy_with_probes(start, &mut probes)? {
                (Some(path), "nearest [tool.vfy]")
            } else {
                (
                    discover_with_probes(start, &mut probes)?,
                    "Coverage.py fallback",
                )
            }
        }
        "project" | "coverage" => (
            if path.is_some() {
                discover_config(path, start)?.map(PathBuf::from)
            } else {
                discover_with_probes(start, &mut probes)?
            },
            if path.is_some() {
                "explicit"
            } else {
                "Coverage.py discovery"
            },
        ),
        other => {
            return Err(PyValueError::new_err(format!(
                "invalid configuration scope {other:?}; expected 'project' or 'coverage'"
            )));
        }
    };
    let settings = selected
        .as_deref()
        .map(|path| load_selected_config_with_value(path, probes.parsed(path), explicit))
        .transpose()?
        .unwrap_or_default();
    Ok(ResolvedConfig {
        discovery: if selected.is_none() {
            "defaults"
        } else {
            discovery
        },
        source: selected.map(|path| path.to_string_lossy().into_owned()),
        settings,
    })
}

#[pyfunction]
#[pyo3(signature = (scope, path=None, start=None))]
pub(crate) fn resolve_config_json(
    scope: &str,
    path: Option<&str>,
    start: Option<&str>,
) -> PyResult<String> {
    serde_json::to_string(&resolve_config(scope, path, start)?.into_value())
        .map_err(|error| PyValueError::new_err(format!("failed to encode configuration: {error}")))
}

#[pyfunction]
#[pyo3(signature = (path=None, start=None))]
pub(crate) fn load_vfy_config_json(path: Option<&str>, start: Option<&str>) -> PyResult<String> {
    let Some(path) = discover_vfy_config(path, start)?.map(PathBuf::from) else {
        return Ok("{}".to_owned());
    };
    let mut config = selected_vfy_config(&path)?;
    substitute_config_environment(&mut config, &path)?;
    let config = normalize(config, &path)?;
    serde_json::to_string(&config)
        .map_err(|error| config_error(&path, format!("failed to encode configuration ({error})")))
}

fn resolve_cli_record(arguments: &str, coverage_file: Option<&str>) -> PyResult<Value> {
    let mut args: Map<String, Value> = serde_json::from_str(arguments)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let command = args
        .get("command")
        .and_then(Value::as_str)
        .ok_or_else(|| PyValueError::new_err("missing CLI command"))?
        .to_owned();
    let project_command = matches!(
        command.as_str(),
        "test" | "explain" | "mutate" | "check" | "config" | "diagnostics" | "cache"
    );
    let compatibility = matches!(
        command.as_str(),
        "run"
            | "report"
            | "xml"
            | "json"
            | "lcov"
            | "html"
            | "annotate"
            | "combine"
            | "erase"
            | "debug"
    );
    let mut path = args
        .get("rcfile")
        .and_then(Value::as_str)
        .map(str::to_owned);
    if path.is_none() && compatibility {
        path = args
            .get("config_file")
            .and_then(Value::as_str)
            .map(str::to_owned);
        if let Some(path) = &path {
            args.insert("rcfile".to_owned(), Value::String(path.clone()));
        }
    }
    let resolved = if args.get("isolated").and_then(Value::as_bool) == Some(true) {
        if args.contains_key("rcfile") {
            args.insert("rcfile".to_owned(), Value::Null);
        }
        ResolvedConfig {
            settings: Map::new(),
            source: None,
            discovery: "isolated",
        }
    } else if project_command {
        resolve_config(
            "project",
            args.get("config_file").and_then(Value::as_str),
            None,
        )?
    } else {
        resolve_config("coverage", path.as_deref(), None)?
    };
    let config = Value::Object(resolved.settings);
    args.insert(
        "config_source".to_owned(),
        resolved.source.map_or(Value::Null, Value::String),
    );
    args.insert(
        "config_discovery".to_owned(),
        Value::String(resolved.discovery.to_owned()),
    );
    let args = vfy_core::resolve_cli_options(args, &config, coverage_file)
        .map_err(PyValueError::new_err)?;
    Ok(Value::Object(Map::from_iter([
        ("arguments".to_owned(), Value::Object(args)),
        ("config".to_owned(), config),
    ])))
}

#[pyfunction]
#[pyo3(signature = (arguments, coverage_file=None))]
fn resolve_cli_config(arguments: &str, coverage_file: Option<&str>) -> PyResult<String> {
    serde_json::to_string(&resolve_cli_record(arguments, coverage_file)?)
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

pub(crate) fn config_value_to_python(py: Python<'_>, value: Value) -> PyResult<Py<PyAny>> {
    match value {
        Value::Null => Ok(py.None()),
        Value::Bool(value) => value.into_py_any(py),
        Value::String(value) => value.into_py_any(py),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                value.into_py_any(py)
            } else if let Some(value) = value.as_u64() {
                value.into_py_any(py)
            } else {
                value
                    .as_f64()
                    .ok_or_else(|| PyValueError::new_err("invalid configuration number"))?
                    .into_py_any(py)
            }
        }
        Value::Array(values) => {
            let result = PyList::empty(py);
            for value in values {
                result.append(config_value_to_python(py, value)?)?;
            }
            Ok(result.into_any().unbind())
        }
        Value::Object(values) => {
            let result = PyDict::new(py);
            for (key, value) in values {
                result.set_item(key, config_value_to_python(py, value)?)?;
            }
            Ok(result.into_any().unbind())
        }
    }
}

#[pyfunction]
#[pyo3(signature = (arguments, coverage_file=None))]
fn resolve_cli_config_record(
    py: Python<'_>,
    arguments: &str,
    coverage_file: Option<&str>,
) -> PyResult<Py<PyAny>> {
    config_value_to_python(py, resolve_cli_record(arguments, coverage_file)?)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(discover_config, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_cli_config, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_cli_config_record, module)?)?;
    module.add_function(wrap_pyfunction!(discover_vfy_config, module)?)?;
    module.add_function(wrap_pyfunction!(load_config_json, module)?)?;
    module.add_function(wrap_pyfunction!(load_vfy_config_json, module)?)?;
    module.add_function(wrap_pyfunction!(resolve_config_json, module)?)?;
    module.add_function(wrap_pyfunction!(validate_vfy_options_json, module)?)?;
    module.add_function(wrap_pyfunction!(validate_run_patch, module)?)?;
    Ok(())
}
