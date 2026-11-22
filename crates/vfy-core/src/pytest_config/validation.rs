use std::collections::BTreeMap;
use std::path::Path;

use super::python_whitespace;
use super::requirements::PluginRequirement;
use pep440_rs::Version;

/// Typed settings after configuration and argument override precedence.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StrictnessSettings {
    pub strict: bool,
    pub strict_config: Option<bool>,
    pub strict_markers: Option<bool>,
    pub strict_xfail: Option<bool>,
}

impl StrictnessSettings {
    #[must_use]
    pub fn resolve(self) -> ConfigurationPolicy {
        ConfigurationPolicy {
            strict_config: self.strict_config.unwrap_or(self.strict),
            strict_markers: self.strict_markers.unwrap_or(self.strict),
            strict_xfail: self.strict_xfail.unwrap_or(self.strict),
        }
    }
}

/// Effective policy shared by diagnostics, execution, and run observations.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConfigurationPolicy {
    pub strict_config: bool,
    pub strict_markers: bool,
    pub strict_xfail: bool,
}

/// Frontend action for a validated configuration or marker diagnostic.
#[derive(Debug, Eq, PartialEq)]
pub enum ConfigurationDiagnostic {
    ConfigWarning(String),
    UnknownMarkWarning(String),
    UsageError(String),
    MarkerError(String),
}

impl ConfigurationPolicy {
    /// Evaluate unknown settings after late plug-ins have registered declarations.
    #[must_use]
    pub fn unknown_ini_diagnostics(&self, names: &[String]) -> Vec<ConfigurationDiagnostic> {
        let mut names: Vec<_> = names.iter().collect();
        names.sort();
        names.dedup();
        let mut diagnostics = Vec::new();
        for name in names {
            let message = format!("Unknown config option: {name}\n");
            if self.strict_config {
                diagnostics.push(ConfigurationDiagnostic::UsageError(message));
                break;
            }
            diagnostics.push(ConfigurationDiagnostic::ConfigWarning(message));
        }
        diagnostics
    }

    /// Validate a marker against the current, possibly extended declarations.
    #[must_use]
    pub fn marker_diagnostic(
        &self,
        name: &str,
        declarations: &[String],
    ) -> Option<ConfigurationDiagnostic> {
        let known = declarations.iter().any(|declaration| {
            declaration
                .split([':', '('])
                .next()
                .is_some_and(|declared| declared.trim_matches(python_whitespace) == name)
        });
        if known {
            return None;
        }
        if matches!(name, "parameterize" | "parametrise" | "parameterise") {
            return Some(ConfigurationDiagnostic::MarkerError(format!(
                "Unknown '{name}' mark, did you mean 'parametrize'?"
            )));
        }
        if self.strict_markers {
            return Some(ConfigurationDiagnostic::MarkerError(format!(
                "{} not found in `markers` configuration option",
                crate::path::quote_python_string(name)
            )));
        }
        Some(ConfigurationDiagnostic::UnknownMarkWarning(format!(
            "Unknown pytest.mark.{name} - is this a typo?  You can register custom marks to avoid this warning - for details, see https://docs.pytest.org/en/stable/how-to/mark.html"
        )))
    }
}

/// Validate pytest's minimum version without importing Python packaging modules.
///
/// # Errors
/// Returns an error for an invalid minimum or actual PEP 440 version.
pub fn minimum_version_diagnostic(
    required: &str,
    actual: &str,
    config_file: Option<&Path>,
) -> Result<Option<ConfigurationDiagnostic>, String> {
    if required.is_empty() {
        return Ok(None);
    }
    let minimum = parse_version(required)?;
    let actual_version = parse_version(actual)?;
    Ok((minimum > actual_version).then(|| {
        ConfigurationDiagnostic::UsageError(format!(
            "{}: 'minversion' requires pytest-{required}, actual pytest-{actual}'",
            config_file.map_or_else(|| "None".to_owned(), |path| path.display().to_string())
        ))
    }))
}

/// Validate requirements against distributions of actually loaded plug-ins.
///
/// Environment markers and extras are parsed but do not filter requirements,
/// matching pytest's required plug-in check. Distribution names preserve case.
///
/// # Errors
/// Returns an error for an invalid version reported by a matched distribution.
pub fn required_plugins_diagnostic(
    required: &[String],
    plugins: &[(String, String)],
) -> Result<Option<ConfigurationDiagnostic>, String> {
    let plugins: BTreeMap<_, _> = plugins
        .iter()
        .map(|(name, version)| (name.as_str(), version.as_str()))
        .collect();
    let mut required: Vec<_> = required.iter().collect();
    required.sort();
    let mut missing = Vec::new();
    for source in required {
        let Some(requirement) = PluginRequirement::parse(source) else {
            missing.push(source.as_str());
            continue;
        };
        let Some(version) = plugins.get(requirement.name) else {
            missing.push(source.as_str());
            continue;
        };
        let version = parse_version(version)?;
        if !requirement.contains(&version) {
            missing.push(source.as_str());
        }
    }
    Ok((!missing.is_empty()).then(|| {
        ConfigurationDiagnostic::UsageError(format!(
            "Missing required plugins: {}",
            missing.join(", ")
        ))
    }))
}

fn parse_version(source: &str) -> Result<Version, String> {
    source
        .trim_matches(python_whitespace)
        .parse::<Version>()
        .map_err(|error| format!("invalid plugin or pytest version {source:?}: {error}"))
}

#[cfg(test)]
mod tests;
