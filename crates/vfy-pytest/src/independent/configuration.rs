//! Consume native configuration policy after protocol metadata registration.
use super::Collector;
use super::lifecycle::Context;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::{Path, PathBuf};
use vfy_core::pytest_config::{
    ConfigurationDiagnostic, ConfigurationPolicy, StrictnessSettings, minimum_version_diagnostic,
    required_plugins_diagnostic,
};

impl Collector<'_> {
    pub(super) fn conftest_cutoff(&self) -> PyResult<PathBuf> {
        let Some(context) = &self.context else {
            return Ok(self.root.clone());
        };
        conftest_cutoff(context.config.bind(self.py), &self.root)
    }
}

pub(super) fn conftest_cutoff(config: &Bound<'_, PyAny>, root: &Path) -> PyResult<PathBuf> {
    let path = config
        .getattr("_configuration")?
        .call_method0("projection")?
        .get_item("config_file")?;
    if path.is_none() {
        return Ok(root.to_path_buf());
    }
    let path = PathBuf::from(path.str()?.to_str()?);
    Ok(path.parent().unwrap_or(root).to_path_buf())
}

pub(super) fn policy(py: Python<'_>, context: &Context) -> PyResult<ConfigurationPolicy> {
    let config = context.config.bind(py);
    let optional = |name| {
        config
            .call_method1("getini", (name,))?
            .extract::<Option<bool>>()
    };
    Ok(StrictnessSettings {
        strict: config.call_method1("getini", ("strict",))?.extract()?,
        strict_config: optional("strict_config")?,
        strict_markers: optional("strict_markers")?,
        strict_xfail: optional("strict_xfail")?,
    }
    .resolve())
}

pub(super) fn diagnostic(py: Python<'_>, diagnostic: ConfigurationDiagnostic) -> PyResult<()> {
    let facade = py.import("vfy.independent.facade")?;
    match diagnostic {
        ConfigurationDiagnostic::ConfigWarning(message) => {
            py.import("warnings")?
                .call_method1("warn", (message, facade.getattr("PytestConfigWarning")?))?;
        }
        ConfigurationDiagnostic::UnknownMarkWarning(message) => {
            py.import("warnings")?.call_method1(
                "warn",
                (message, facade.getattr("PytestUnknownMarkWarning")?),
            )?;
        }
        ConfigurationDiagnostic::UsageError(message) => {
            return Err(crate::IndependentUsageError::new_err(message));
        }
        ConfigurationDiagnostic::MarkerError(message) => {
            return Err(PyErr::from_value(
                facade.getattr("Failed")?.call1((message,))?,
            ));
        }
    }
    Ok(())
}

pub(super) fn unknown_ini(py: Python<'_>, context: &Context) -> PyResult<()> {
    let declarations = context.parser.bind(py).getattr("_ini")?;
    let declarations = declarations.cast::<PyDict>()?;
    let mut known = Vec::new();
    for (name, declaration) in declarations.iter() {
        known.push(name.extract::<String>()?);
        known.extend(declaration.get_item(2)?.extract::<Vec<String>>()?);
    }
    let names = context
        .config
        .bind(py)
        .getattr("_configuration")?
        .call_method1("unknown_ini", (known,))?
        .extract::<Vec<String>>()?;
    for warning in policy(py, context)?.unknown_ini_diagnostics(&names) {
        diagnostic(py, warning)?;
    }
    Ok(())
}

pub(super) fn minimum_version(py: Python<'_>, context: &Context) -> PyResult<()> {
    let config = context.config.bind(py);
    let minimum = config
        .call_method1("getini", ("minversion",))?
        .extract::<String>()?;
    let path = config.getattr("inipath")?;
    let path = if path.is_none() {
        None
    } else {
        Some(PathBuf::from(path.str()?.to_str()?))
    };
    if let Some(issue) = minimum_version_diagnostic(&minimum, "9.1.1", path.as_deref())
        .map_err(crate::IndependentUsageError::new_err)?
    {
        diagnostic(py, issue)?;
    }
    Ok(())
}

pub(super) fn required_plugins(py: Python<'_>, context: &Context) -> PyResult<()> {
    let config = context.config.bind(py);
    let required = config
        .call_method1("getini", ("required_plugins",))?
        .extract::<Vec<String>>()?;
    if required.is_empty() {
        return Ok(());
    }
    let mut loaded = Vec::new();
    for entry in config
        .getattr("pluginmanager")?
        .call_method0("list_plugin_distinfo")?
        .try_iter()?
    {
        let distribution = entry?.get_item(1)?;
        loaded.push((
            distribution
                .getattr("metadata")?
                .get_item("Name")?
                .extract::<String>()?,
            distribution.getattr("version")?.extract::<String>()?,
        ));
    }
    if let Some(issue) = required_plugins_diagnostic(&required, &loaded)
        .map_err(crate::IndependentUsageError::new_err)?
    {
        diagnostic(py, issue)?;
    }
    Ok(())
}

pub(super) fn markers(
    py: Python<'_>,
    context: &Context,
    marks: &Bound<'_, PyList>,
) -> PyResult<()> {
    let declarations = context
        .config
        .bind(py)
        .call_method1("getini", ("markers",))?
        .extract::<Vec<String>>()?;
    let policy = policy(py, context)?;
    for mark in marks.iter() {
        let name = mark.getattr("name")?.extract::<String>()?;
        if let Some(issue) = policy.marker_diagnostic(&name, &declarations) {
            diagnostic(py, issue)?;
        }
    }
    Ok(())
}
