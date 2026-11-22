//! Plugin registration, unregistration, and hookspec publication.

use super::discovery::implementation;
use super::record::Implementation;
use super::registry::NativeHookRegistry;
use super::selection::{
    HookSelection, RegisteredImplementation, registration_order, release_hook_rows,
};
use super::specification::verify;
use super::{admission, monitoring, observations, specification};
use pyo3::exceptions::{PyAssertionError, PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::path::{Path, PathBuf};

fn duplicate_name(
    registry: &Bound<'_, NativeHookRegistry>,
    plugin: &Bound<'_, PyAny>,
    name: &str,
) -> PyResult<bool> {
    let py = registry.py();
    let (blocked, same_name, same_plugin, mappings) = {
        let registry = registry.borrow();
        let matching = registry.plugins.iter().find(|entry| entry.name == name);
        (
            matching.is_some_and(|entry| entry.object.is_none()),
            matching.is_some(),
            registry.plugins.iter().any(|entry| {
                entry
                    .object
                    .as_ref()
                    .is_some_and(|object| object.is(plugin))
            }),
            registry.mappings(py)?,
        )
    };
    if blocked {
        return Ok(true);
    }
    if same_name {
        return Err(PyValueError::new_err(format!(
            "Plugin name already registered: {name}={}\n{}",
            plugin.str()?,
            mappings.repr()?
        )));
    }
    if same_plugin {
        return Err(PyValueError::new_err(format!(
            "Plugin already registered under a different name: {name}={}\n{}",
            plugin.str()?,
            mappings.repr()?
        )));
    }
    Ok(false)
}

fn replay_history(
    registry: &Bound<'_, NativeHookRegistry>,
    implementation: &Py<Implementation>,
) -> PyResult<()> {
    let py = registry.py();
    let hook_name = implementation.borrow(py).name.clone();
    let mut history_index = 0;
    loop {
        let call = registry
            .borrow()
            .history
            .get(&hook_name)
            .and_then(|history| history.get(history_index))
            .map(|call| call.clone_ref(py));
        let Some(call) = call else {
            break;
        };
        history_index += 1;
        let result = monitoring::execute(
            registry,
            &hook_name,
            HookSelection::Owned(vec![implementation.clone_ref(py)]),
            call.kwargs.bind(py),
            false,
        )?;
        if let Some(callback) = call.callback
            && let Ok(result) = result.bind(py).cast::<PyList>()
            && let Some(value) = result.try_iter()?.next()
        {
            callback.bind(py).call1((value?,))?;
        }
    }
    Ok(())
}

/// When a registration's plugin mapping becomes visible.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Publication {
    /// `PluginManager.register`: the mapping is published before hook validation, as pluggy does.
    Incremental,
    /// Primitive `NativeHookRegistry.register`: nothing is published unless every hook validates.
    Atomic,
}

/// Admission runs before publication in both modes, so an unsupported hook leaves no mapping.
pub(super) fn register_plugin(
    registry: &Bound<'_, NativeHookRegistry>,
    plugin: &Bound<'_, PyAny>,
    name: Option<String>,
    publication: Publication,
) -> PyResult<Option<String>> {
    let py = registry.py();
    let name = match name.filter(|name| !name.is_empty()) {
        Some(name) => name,
        None => canonical_name(plugin)?,
    };
    if duplicate_name(registry, plugin, &name)? {
        return Ok(None);
    }
    let conftest_directory = conftest_directory_of(plugin)?;
    let generation = observations::Observations::begin(registry, plugin, &name)?;
    let (project_name, discover_prefix) = {
        let state = registry.borrow();
        (state.project_name.clone(), state.discover_prefix)
    };
    let admitted = admission::prepare(registry, plugin, &name, &project_name, discover_prefix)?;
    let incremental = publication == Publication::Incremental;
    if incremental {
        let mut state = registry.borrow_mut();
        state.plugins.insert(
            name.clone(),
            Some(plugin.clone().unbind()),
            conftest_directory.clone(),
        )?;
        state.observations.publish(generation);
    }
    let mut staged = Vec::new();
    for candidate in
        registration_candidates(plugin, &name, &project_name, discover_prefix, admitted)?
    {
        let Some(candidate) = candidate? else {
            continue;
        };
        let mut implementation = candidate.into_verified(registry)?;
        implementation.invocation = registry
            .borrow_mut()
            .observations
            .declare(generation, &implementation.name);
        let implementation = Py::new(py, implementation)?;
        replay_history(registry, &implementation)?;
        if incremental {
            publish_rows(registry, &[implementation], None)?;
        } else {
            staged.push(implementation);
        }
    }
    if !incremental {
        publish_rows(
            registry,
            &staged,
            Some(&NewPlugin {
                name: &name,
                plugin,
                directory: conftest_directory,
                generation,
            }),
        )?;
    }
    Ok(Some(name))
}

struct NewPlugin<'a> {
    pub(super) name: &'a str,
    pub(super) plugin: &'a Bound<'a, PyAny>,
    pub(super) directory: Option<PathBuf>,
    generation: u64,
}

/// Publish Rust-owned immutable rows in one mutation. Cloning Python handles
/// does not invoke callbacks; replaced rows are released after the borrow.
fn publish_rows(
    registry: &Bound<'_, NativeHookRegistry>,
    staged: &[Py<Implementation>],
    plugin: Option<&NewPlugin<'_>>,
) -> PyResult<()> {
    let py = registry.py();
    let removed = {
        let mut state = registry.borrow_mut();
        let count = u64::try_from(staged.len()).map_err(|_| {
            pyo3::exceptions::PyOverflowError::new_err("hook registration count exceeds u64")
        })?;
        state
            .next_implementation_sequence
            .checked_add(count)
            .ok_or_else(|| {
                pyo3::exceptions::PyOverflowError::new_err("hook registration order exceeds u64")
            })?;
        if let Some(plugin) = plugin {
            state.plugins.insert(
                plugin.name.to_owned(),
                Some(plugin.plugin.clone().unbind()),
                plugin.directory.clone(),
            )?;
            state.observations.publish(plugin.generation);
        }
        let mut removed = Vec::new();
        for implementation in staged {
            let name = implementation.borrow(py).name.clone();
            if !state.hook_names.contains(&name) {
                state.hook_names.push(name.clone());
            }
            let sequence = state.next_implementation_sequence;
            state.next_implementation_sequence += 1;
            let mut entries: Vec<_> = state
                .implementations
                .get(&name)
                .map_or_else(Vec::new, |rows| {
                    rows.iter().map(|entry| entry.clone_ref(py)).collect()
                });
            let entry = RegisteredImplementation {
                sequence,
                implementation: implementation.clone_ref(py),
            };
            let position = entries
                .partition_point(|previous| registration_order(py, previous, &entry).is_lt());
            entries.insert(position, entry);
            if let Some(previous) = state.implementations.insert(name, entries.into()) {
                removed.push(previous);
            }
        }
        removed
    };
    release_hook_rows(py, removed);
    Ok(())
}

fn registration_candidates<'a>(
    plugin: &'a Bound<'_, PyAny>,
    name: &'a str,
    project_name: &'a str,
    discover_prefix: bool,
    admitted: Option<Vec<admission::Candidate>>,
) -> PyResult<impl Iterator<Item = PyResult<Option<admission::Candidate>>> + 'a> {
    let prevalidated = admitted.is_some();
    let attributes = if prevalidated {
        Vec::new()
    } else {
        plugin.dir()?.extract::<Vec<String>>()?
    };
    let mut attributes = attributes.into_iter();
    let mut admitted = admitted.unwrap_or_default().into_iter();
    Ok(std::iter::from_fn(move || {
        if prevalidated {
            admitted
                .next()
                .map(|implementation| Ok(Some(implementation)))
        } else {
            attributes.next().map(|attribute| {
                implementation(plugin, name, &attribute, project_name, discover_prefix)
                    .map(|implementation| implementation.map(admission::Candidate::unvalidated))
            })
        }
    }))
}

/// Directory of a `conftest.py` plugin. Other modules have no conftest directory.
fn conftest_directory_of(plugin: &Bound<'_, PyAny>) -> PyResult<Option<PathBuf>> {
    if !plugin.hasattr("__file__")? {
        return Ok(None);
    }
    let source = PathBuf::from(plugin.getattr("__file__")?.extract::<String>()?);
    if source.file_name().is_some_and(|name| name == "conftest.py") {
        Ok(source.parent().map(Path::to_path_buf))
    } else {
        Ok(None)
    }
}

pub(super) fn canonical_name(plugin: &Bound<'_, PyAny>) -> PyResult<String> {
    match plugin.getattr("__name__") {
        Ok(name) if !name.is_none() && name.is_truthy()? => name.extract(),
        Ok(_) => Ok(format!("{}", plugin.as_ptr() as usize)),
        Err(error) if error.is_instance_of::<PyAttributeError>(plugin.py()) => {
            Ok(format!("{}", plugin.as_ptr() as usize))
        }
        Err(error) => Err(error),
    }
}

/// Publishes each hook specification in the namespace, then verifies the implementations
/// already registered for it. A later failure leaves earlier specifications in place.
pub(super) fn add_specifications(
    registry: &Bound<'_, NativeHookRegistry>,
    namespace: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = registry.py();
    let (project_name, discover_prefix) = {
        let state = registry.borrow();
        (state.project_name.clone(), state.discover_prefix)
    };
    let mut found = false;
    for name in namespace.dir()?.extract::<Vec<String>>()? {
        let Some(specification) =
            specification::read_specification(namespace, &name, &project_name, discover_prefix)?
        else {
            continue;
        };
        found = true;
        let name = specification.name.clone();
        let previous = registry
            .borrow()
            .specifications
            .get(&name)
            .map(|spec| spec.clone_ref(py));
        if let Some(previous) = previous {
            let namespace = previous.borrow(py).namespace.clone_ref(py);
            return Err(PyValueError::new_err(format!(
                "Hook '{name}' is already registered within namespace {}",
                namespace.bind(py).str()?
            )));
        }
        let historic = specification.historic;
        let specification = Py::new(py, specification)?;
        {
            let mut registry = registry.borrow_mut();
            if !registry.hook_names.contains(&name) {
                registry.hook_names.push(name.clone());
            }
            registry
                .specifications
                .insert(name.clone(), specification.clone_ref(py));
            if historic {
                registry.history.insert(name.clone(), Vec::new());
            }
        }
        let implementations = registry.borrow().selected_owned(py, &name);
        for implementation in implementations.iter().rev() {
            let snapshot = implementation.borrow(py).clone_ref(py);
            verify(py, &specification, &snapshot)?;
        }
    }
    if !found {
        return Err(PyValueError::new_err(format!(
            "did not find any '{project_name}' hooks in {}",
            namespace.repr()?
        )));
    }
    Ok(())
}

/// Update all affected hooks while retaining detached rows until the registry
/// borrow ends; running calls keep their own row snapshot and registry pin.
fn remove_plugin_rows(registry: &Bound<'_, NativeHookRegistry>, plugin: &Py<PyAny>, name: &str) {
    let py = registry.py();
    let (removed, mapping) = {
        let mut state = registry.borrow_mut();
        let mut removed = Vec::new();
        for rows in state.implementations.values_mut() {
            if rows
                .iter()
                .any(|entry| entry.implementation.borrow(py).plugin.is(plugin))
            {
                let retained: Vec<_> = rows
                    .iter()
                    .filter(|entry| !entry.implementation.borrow(py).plugin.is(plugin))
                    .map(|entry| entry.clone_ref(py))
                    .collect();
                removed.push(std::mem::replace(rows, retained.into()));
            }
        }
        state.implementations.retain(|_, rows| !rows.is_empty());
        let mapping = state.plugins.remove(name, false);
        (removed, mapping)
    };
    release_hook_rows(py, removed);
    drop(mapping);
}

pub(super) fn unregister_plugin(
    registry: &Bound<'_, NativeHookRegistry>,
    plugin: Option<&Bound<'_, PyAny>>,
    name: Option<&str>,
) -> PyResult<Option<Py<PyAny>>> {
    let py = registry.py();
    if plugin.is_none() && name.is_none() {
        return Err(PyAssertionError::new_err(
            "one of name or plugin needs to be specified",
        ));
    }
    let name = name
        .map(str::to_owned)
        .or_else(|| plugin.and_then(|plugin| registry.borrow().get_name(plugin)));
    let Some(name) = name else {
        return Err(PyAssertionError::new_err("plugin is not registered"));
    };
    let plugin = plugin
        .map(|plugin| plugin.clone().unbind())
        .or_else(|| registry.borrow().get_plugin(py, &name));
    let Some(plugin) = plugin else {
        return Ok(None);
    };
    remove_plugin_rows(registry, &plugin, &name);
    Ok(Some(plugin))
}
