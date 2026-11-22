//! Registration identities and invocation evidence contain no Python references.

use super::NativeHookRegistry;
use pyo3::exceptions::{PyAttributeError, PyOSError, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use vfy_core::test_execution::PluginObservation;

struct Identity {
    module: String,
    origin: Option<String>,
    sha256: Option<String>,
}

/// Python reflection result; the origin file is digested separately.
struct Reflection {
    module: String,
    origin: Option<String>,
}

/// Digests of origin files, shared by every registration in one registry.
///
/// Native plug-ins report the extension module (`vfy/_native*.so`) as their
/// origin, so many registrations name one large file. Each file is read once.
#[derive(Default)]
pub(super) struct FileDigests {
    by_path: BTreeMap<PathBuf, Result<String, String>>,
}

impl FileDigests {
    fn digest(&mut self, path: &Path) -> Result<String, String> {
        if let Some(digest) = self.by_path.get(path) {
            return digest.clone();
        }
        let digest =
            vfy_core::file_digest(path).map_err(|error| PyOSError::new_err(error).to_string());
        self.by_path.insert(path.to_path_buf(), digest.clone());
        digest
    }

    fn identity(&mut self, reflection: Reflection) -> Result<Identity, String> {
        let sha256 = match reflection
            .origin
            .as_deref()
            .filter(|path| Path::new(path).is_file())
        {
            Some(path) => Some(self.digest(Path::new(path))?),
            None => None,
        };
        Ok(Identity {
            module: reflection.module,
            origin: reflection.origin,
            sha256,
        })
    }
}

/// One counter is owned by its registration and shared by every alias that
/// implements the same hook. Detached dispatch snapshots retain that owner.
#[derive(Default)]
pub(super) struct InvocationCount(AtomicU64);

impl InvocationCount {
    pub(super) fn increment(&self) -> PyResult<()> {
        self.0
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |count| {
                count.checked_add(1)
            })
            .map(|_| ())
            .map_err(|_| PyOverflowError::new_err("hook call count exceeds u64"))
    }

    fn get(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}

struct Registration {
    name: String,
    identity: Result<Identity, String>,
    hooks: Vec<String>,
    counts: BTreeMap<String, Arc<InvocationCount>>,
    published: bool,
}

#[derive(Default)]
pub(super) struct Observations {
    next_generation: u64,
    registrations: BTreeMap<u64, Registration>,
}

fn optional_string(value: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<String>> {
    match value.getattr(name) {
        Ok(value) if value.is_none() => Ok(None),
        Ok(value) => value.extract().map(Some),
        Err(error) if error.is_instance_of::<PyAttributeError>(value.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

fn reflect(plugin: &Bound<'_, PyAny>) -> PyResult<Reflection> {
    let py = plugin.py();
    let inspect = py.import("inspect")?;
    let module_object = inspect.call_method1("getmodule", (plugin,))?;
    let module = optional_string(&module_object, "__name__")?
        .or(optional_string(plugin, "__module__")?)
        .unwrap_or_else(|| {
            plugin.get_type().module().map_or_else(
                |_| String::new(),
                |value| value.to_string_lossy().into_owned(),
            )
        });
    let source_object = if inspect.call_method1("ismodule", (plugin,))?.is_truthy()?
        || inspect.call_method1("isclass", (plugin,))?.is_truthy()?
    {
        plugin.clone()
    } else {
        plugin.get_type().into_any()
    };
    let origin = match inspect.call_method1("getsourcefile", (&source_object,)) {
        Ok(source) if !source.is_none() => Some(source.extract::<String>()?),
        Ok(_) => optional_string(&module_object, "__file__")?,
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyTypeError>(py) => {
            optional_string(&module_object, "__file__")?
        }
        Err(error) => return Err(error),
    };
    Ok(Reflection { module, origin })
}

impl Observations {
    pub(super) fn begin(
        registry: &Bound<'_, NativeHookRegistry>,
        plugin: &Bound<'_, PyAny>,
        name: &str,
    ) -> PyResult<u64> {
        // Reflection can execute Python. Do it before taking a native borrow,
        // and report evidence failures separately from public registration.
        let reflection = reflect(plugin).map_err(|error| error.to_string());
        let mut registry = registry.borrow_mut();
        let registry = &mut *registry;
        let identity = reflection.and_then(|reflection| registry.file_digests.identity(reflection));
        let state = &mut registry.observations;
        let generation = state.next_generation.checked_add(1).ok_or_else(|| {
            PyOverflowError::new_err("plugin registration generation exceeds u64")
        })?;
        state.next_generation = generation;
        state.registrations.insert(
            generation,
            Registration {
                name: name.to_owned(),
                identity,
                hooks: Vec::new(),
                counts: BTreeMap::new(),
                published: false,
            },
        );
        Ok(generation)
    }

    pub(super) fn declare(&mut self, generation: u64, name: &str) -> Option<Arc<InvocationCount>> {
        let record = self.registrations.get_mut(&generation)?;
        if !record.hooks.iter().any(|hook| hook == name) {
            record.hooks.push(name.to_owned());
        }
        Some(Arc::clone(
            record.counts.entry(name.to_owned()).or_default(),
        ))
    }

    pub(super) fn publish(&mut self, generation: u64) {
        if let Some(record) = self.registrations.get_mut(&generation) {
            record.published = true;
        }
    }

    pub(super) fn snapshot(&self) -> PyResult<Vec<PluginObservation>> {
        self.registrations
            .iter()
            .filter(|(_, record)| {
                record.published || record.counts.values().any(|count| count.get() != 0)
            })
            .map(|(generation, record)| {
                let identity = record.identity.as_ref().map_err(|error| {
                    PyValueError::new_err(format!(
                        "failed to observe plugin {:?} registration {generation}: {error}",
                        record.name
                    ))
                })?;
                let mut hook_calls: BTreeMap<_, _> = record
                    .counts
                    .iter()
                    .filter_map(|(name, count)| {
                        let value = count.get();
                        (value != 0).then(|| (name.clone(), value))
                    })
                    .collect();
                for hook in &record.hooks {
                    hook_calls.entry(hook.clone()).or_default();
                }
                Ok(PluginObservation {
                    generation: *generation,
                    name: record.name.clone(),
                    module: identity.module.clone(),
                    origin: identity.origin.clone(),
                    sha256: identity.sha256.clone(),
                    hooks: record.hooks.clone(),
                    hook_calls,
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests;
