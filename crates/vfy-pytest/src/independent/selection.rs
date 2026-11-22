//! Bind controller plans to the final project collection before execution.

use super::Collector;
use super::lifecycle::Context;
use crate::hooks::NativeHookRegistry;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::BTreeSet;
use std::path::Path;
use vfy_core::selection::ci;

fn selection_mask(nodeids: &[String], selected: &[String]) -> Result<Vec<bool>, String> {
    let inventory = nodeids.iter().map(String::as_str).collect::<BTreeSet<_>>();
    let selected_set = selected.iter().map(String::as_str).collect::<BTreeSet<_>>();
    if selected_set.len() != selected.len() {
        return Err("selection repeats a test identity".to_owned());
    }
    if let Some(missing) = selected_set.difference(&inventory).next() {
        return Err(format!(
            "selection references an uncollected test: {missing}"
        ));
    }
    Ok(nodeids
        .iter()
        .map(|nodeid| selected_set.contains(nodeid.as_str()))
        .collect())
}

fn environment_value(py: Python<'_>, name: &str) -> PyResult<Option<String>> {
    Ok(py
        .import("os")?
        .getattr("environ")?
        .call_method1("get", (name,))?
        .extract::<Option<String>>()?
        .filter(|value| !value.is_empty()))
}

fn ci_failure(error: &vfy_core::CoreError) -> PyErr {
    crate::IndependentUsageError::new_err(format!("Failed to finalize vfy CI selection: {error}"))
}

/// The baseline identities a selected run keeps instead of collecting their files.
///
/// Audits and collect-only runs report the complete collection, so they never scope it.
fn collection_scope(py: Python<'_>, context: &Context) -> PyResult<Option<ci::CollectionScope>> {
    if environment_value(py, "VFY_CI_COLLECTION")?.is_none()
        || environment_value(py, "VFY_CI_AUDIT")?.as_deref() == Some("1")
        || context
            .config
            .bind(py)
            .call_method1("getoption", ("collect_only", false))?
            .is_truthy()?
    {
        return Ok(None);
    }
    let Some(request_path) = environment_value(py, "VFY_CI_REQUEST")? else {
        return Ok(None);
    };
    ci::collection_scope(Path::new(&request_path)).map_err(|error| ci_failure(&error))
}

/// Whether a collector has read its selected run's collection scope yet.
pub(super) enum CollectionScopeState {
    Unread,
    Read(Option<ci::CollectionScope>),
}

impl Collector<'_> {
    /// Whether the selected run keeps this file's baseline identities without importing it.
    pub(super) fn outside_collection_scope(&mut self, path: &Path) -> PyResult<bool> {
        if matches!(self.collection_scope, CollectionScopeState::Unread) {
            let context = self
                .context
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
            self.collection_scope = CollectionScopeState::Read(collection_scope(self.py, context)?);
        }
        let CollectionScopeState::Read(Some(scope)) = &self.collection_scope else {
            return Ok(false);
        };
        let Some(file) = path
            .strip_prefix(&self.root)
            .ok()
            .and_then(Path::to_str)
            .filter(|file| !file.is_empty())
        else {
            return Ok(false);
        };
        Ok(scope.skips(file))
    }
}

pub(super) fn bind(py: Python<'_>, context: &Context, items: &Bound<'_, PyList>) -> PyResult<()> {
    let value = |name: &str| environment_value(py, name);
    let Some(collection_path) = value("VFY_CI_COLLECTION")? else {
        return Ok(());
    };
    let nodeids = items
        .iter()
        .map(|item| item.getattr("nodeid")?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?;
    let failure = |error: vfy_core::CoreError| ci_failure(&error);
    let Some(request_path) = value("VFY_CI_REQUEST")? else {
        return ci::write_collection(Path::new(&collection_path), &nodeids).map_err(failure);
    };
    let final_path = value("VFY_CI_FINAL")?.ok_or_else(|| {
        crate::IndependentUsageError::new_err("vfy CI selection requires a final plan destination")
    })?;
    let ignored = serde_json::from_str::<Vec<String>>(
        &value("VFY_CI_IGNORED")?.unwrap_or_else(|| "[]".to_owned()),
    )
    .map_err(|error| {
        crate::IndependentUsageError::new_err(format!("Invalid vfy CI ignored paths: {error}"))
    })?;
    if !ci::plan_source_current(Path::new(&request_path), &ignored).map_err(failure)? {
        return Err(crate::IndependentUsageError::new_err(
            "source changed after the CI plan was prepared; rerun the command",
        ));
    }
    let scope = collection_scope(py, context)?;
    let plan = match ci::finalize_independent_collection(
        Path::new(&request_path),
        nodeids.clone(),
        scope.as_ref(),
        Path::new(&final_path),
        Path::new(&collection_path),
    )
    .map_err(failure)?
    {
        ci::IndependentCollection::Finalized(plan) => plan,
        ci::IndependentCollection::ScopeChanged => {
            return Err(crate::IndependentUsageError::new_err(
                "the selected test files collected different tests than the passing baseline; \
                 vfy repeats the run with complete collection",
            ));
        }
    };
    if plan.selection.fallback_full
        || value("VFY_CI_AUDIT")?.as_deref() == Some("1")
        || context
            .config
            .bind(py)
            .call_method1("getoption", ("collect_only", false))?
            .is_truthy()?
    {
        return Ok(());
    }
    let mask = selection_mask(&nodeids, &plan.selection.selected)
        .map_err(crate::IndependentUsageError::new_err)?;
    let retained = PyList::empty(py);
    let removed = PyList::empty(py);
    for (item, selected) in items.iter().zip(mask) {
        if selected {
            retained.append(item)?;
        } else {
            removed.append(item)?;
        }
    }
    if !removed.is_empty() {
        let kwargs = PyDict::new(py);
        kwargs.set_item("items", removed)?;
        NativeHookRegistry::call(
            context.registry.bind(py),
            py,
            "pytest_deselected",
            &kwargs,
            false,
        )?;
        items.set_slice(0, items.len(), &retained)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
