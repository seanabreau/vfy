//! Independent-engine admission precedes plugin publication and historic replay.

use super::{Implementation, NativeHookRegistry, Specification, implementation, verify};
use pyo3::prelude::*;
use std::collections::BTreeSet;

pub(super) struct Admission {
    denied: BTreeSet<String>,
}

pub(super) fn install(
    registry: &Bound<'_, NativeHookRegistry>,
    namespace: &Bound<'_, PyAny>,
    supported: &[&str],
) -> PyResult<()> {
    let py = registry.py();
    let denied = registry
        .borrow()
        .specifications
        .iter()
        .filter(|(name, specification)| {
            specification.borrow(py).namespace.bind(py).is(namespace)
                && !supported.contains(&name.as_str())
        })
        .map(|(name, _)| name.clone())
        .collect::<BTreeSet<_>>();
    let invalid = registry
        .borrow()
        .registrations(py)
        .into_iter()
        .find_map(|entry| {
            let record = entry.implementation.borrow(py);
            denied
                .contains(&record.name)
                .then(|| (record.plugin_name.clone(), record.name.clone()))
        });
    if let Some((plugin, hook)) = invalid {
        return Err(unsupported(&plugin, &hook));
    }
    registry.borrow_mut().admission = Some(Admission { denied });
    Ok(())
}

pub(super) fn unsupported(plugin: &str, hook: &str) -> PyErr {
    crate::UnsupportedFeatureError::new_err(format!(
        "independent pytest does not support hook '{hook}' from plugin '{plugin}'"
    ))
}

/// An implementation read from a plugin, with the specification it was validated against.
pub(super) struct Candidate {
    implementation: Implementation,
    specification: Option<Py<Specification>>,
}

impl Candidate {
    pub(super) fn unvalidated(implementation: Implementation) -> Self {
        Self {
            implementation,
            specification: None,
        }
    }

    /// Verifies against the specification current at publication, unless this candidate was
    /// already verified against it.
    pub(super) fn into_verified(
        self,
        registry: &Bound<'_, NativeHookRegistry>,
    ) -> PyResult<Implementation> {
        let py = registry.py();
        let current = registry
            .borrow()
            .specifications
            .get(&self.implementation.name)
            .map(|specification| specification.clone_ref(py));
        if let Some(current) = current
            && !self
                .specification
                .as_ref()
                .is_some_and(|previous| previous.is(&current))
        {
            verify(py, &current, &self.implementation)?;
        }
        Ok(self.implementation)
    }
}

/// Reads and validates the plugin's hooks before publication. Returns `None` when admission
/// is not installed, so the caller reads the hooks after publication.
pub(super) fn prepare(
    registry: &Bound<'_, NativeHookRegistry>,
    plugin: &Bound<'_, PyAny>,
    name: &str,
    project_name: &str,
    discover_prefix: bool,
) -> PyResult<Option<Vec<Candidate>>> {
    if registry.borrow().admission.is_none() {
        return Ok(None);
    }
    let mut candidates = Vec::new();
    for attribute in plugin.dir()?.extract::<Vec<String>>()? {
        let Some(implementation) =
            implementation(plugin, name, &attribute, project_name, discover_prefix)?
        else {
            continue;
        };
        let specification = validate(registry, &implementation)?;
        let denied = registry
            .borrow()
            .admission
            .as_ref()
            .is_some_and(|policy| policy.denied.contains(&implementation.name));
        if denied {
            return Err(unsupported(name, &implementation.name));
        }
        candidates.push(Candidate {
            implementation,
            specification,
        });
    }
    Ok(Some(candidates))
}

fn validate(
    registry: &Bound<'_, NativeHookRegistry>,
    implementation: &Implementation,
) -> PyResult<Option<Py<Specification>>> {
    let py = registry.py();
    let specification = registry
        .borrow()
        .specifications
        .get(&implementation.name)
        .map(|specification| specification.clone_ref(py));
    if let Some(specification) = &specification {
        verify(py, specification, implementation)?;
    }
    Ok(specification)
}
