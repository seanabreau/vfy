//! Plugin manager state and its Python-facing registry class.

use super::monitoring::{Monitor, NativeHookUndo, NativeTagTracerSub};
use super::multicall::{
    CallbackSignature, Extension, Multicall, NativeHookDefault, insert_default, multicall,
    prepare_call, subset,
};
use super::plugins::PluginStore;
use super::record::Implementation;
use super::registration::{
    Publication, add_specifications, canonical_name, register_plugin, unregister_plugin,
};
use super::selection::{
    HookSelection, NativeDispatch, RegisteredImplementation, release_hook_rows,
};
use super::specification::{Specification, verify_call};
use super::{admission, invocation, monitoring, observations, specification};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Plugin mappings and hook records mirror pluggy's `PluginManager` state. The
/// `*_compatible` entry points are the names the Python plugin manager calls.
#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeHookRegistry {
    pub(super) project_name: String,
    pub(super) discover_prefix: bool,
    pub(super) admission: Option<admission::Admission>,
    pub(super) hook_names: Vec<String>,
    pub(super) plugins: PluginStore,
    pub(super) implementations: BTreeMap<String, std::sync::Arc<[RegisteredImplementation]>>,
    pub(super) next_implementation_sequence: u64,
    pub(super) specifications: BTreeMap<String, Py<Specification>>,
    pub(super) history: BTreeMap<String, Vec<HistoryCall>>,
    pub(super) monitors: Vec<Monitor>,
    pub(super) trace: Option<Py<NativeTagTracerSub>>,
    pub(super) observations: observations::Observations,
    /// Origin-file digests for plug-in registrations; see `observations::FileDigests`.
    pub(super) file_digests: observations::FileDigests,
}

pub(super) struct HistoryCall {
    pub(super) kwargs: Py<PyDict>,
    pub(super) callback: Option<Py<PyAny>>,
}
impl HistoryCall {
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            kwargs: self.kwargs.clone_ref(py),
            callback: self
                .callback
                .as_ref()
                .map(|callback| callback.clone_ref(py)),
        }
    }
}

impl Drop for NativeHookRegistry {
    fn drop(&mut self) {
        drop(std::mem::take(&mut self.plugins));
        let owners: Vec<_> = std::mem::take(&mut self.implementations)
            .into_values()
            .collect();
        let _ = Python::try_attach(|py| release_hook_rows(py, owners));
    }
}

impl Default for NativeHookRegistry {
    fn default() -> Self {
        Self {
            project_name: "pytest".into(),
            discover_prefix: true,
            admission: None,
            hook_names: Vec::new(),
            plugins: PluginStore::default(),
            implementations: BTreeMap::new(),
            next_implementation_sequence: 0,
            specifications: BTreeMap::new(),
            history: BTreeMap::new(),
            monitors: Vec::new(),
            trace: None,
            observations: observations::Observations::default(),
            file_digests: observations::FileDigests::default(),
        }
    }
}

impl NativeHookRegistry {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    pub(crate) fn invokes_callbacks(&self, py: Python<'_>, name: &str) -> bool {
        // Enabled tracing is a monitor; merely accessing the trace property
        // creates its object without installing a dispatch callback.
        !self.monitors.is_empty() || self.has_implementations(py, name)
    }

    pub(crate) fn has_implementations(&self, _py: Python<'_>, name: &str) -> bool {
        self.implementations.contains_key(name)
    }

    /// Compute exclusions from the current item path and collection publication.
    /// Read the current registry and collection publication after path callbacks.
    pub(crate) fn item_exclusions<'py>(
        registry: &Bound<'py, Self>,
        item: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if registry.borrow().plugins.conftests.is_empty() {
            return Ok(PyTuple::empty(registry.py()).into_any());
        }
        let py = registry.py();
        let session = item.getattr(pyo3::intern!(py, "session"))?;
        // Resolve the function before path: a custom path descriptor can change it.
        let fspath = crate::module_cache::Module::Os
            .get(py)?
            .getattr(pyo3::intern!(py, "fspath"))?;
        let path = {
            let path = item.getattr(pyo3::intern!(py, "path"))?;
            fspath.call1((path,))?
        };
        drop(fspath);
        let arena = session
            .getattr(pyo3::intern!(py, "_vfy_collection"))?
            .cast_into::<crate::independent::NativeCollectionArena>()?;
        Ok(Self::removed_conftests(registry, &path, &arena, Some(item))?.into_bound(py))
    }

    /// Registered `conftest.py` plugins whose directory does not contain `path`,
    /// or every registered `conftest.py` plugin when `path` is `None`, in
    /// registration order.
    pub(crate) fn conftest_plugins_outside(
        &self,
        py: Python<'_>,
        path: Option<&Path>,
    ) -> Vec<Py<PyAny>> {
        self.plugins
            .conftests
            .iter()
            .filter_map(|plugin| {
                let directory = plugin.conftest_directory.as_ref()?;
                if path.is_some_and(|path| path.starts_with(directory)) {
                    return None;
                }
                plugin.object.as_ref().map(|object| object.clone_ref(py))
            })
            .collect()
    }

    pub(crate) fn restrict_builtin_hooks(
        registry: &Bound<'_, Self>,
        namespace: &Bound<'_, PyAny>,
        supported: &[&str],
    ) -> PyResult<()> {
        admission::install(registry, namespace, supported)
    }

    /// Dispatch an internal parameterless default directly when no Python
    /// observer or hookspec warning can see its temporary `HookImpl`.
    pub(crate) fn call_subset_with_lazy_default<'py, F, G>(
        registry: &Bound<'py, Self>,
        name: &str,
        kwargs: &Bound<'py, PyDict>,
        remove_plugins: &Bound<'py, PyAny>,
        firstresult: bool,
        plugin_name: &str,
        default: NativeHookDefault<F, G>,
    ) -> PyResult<Py<PyAny>>
    where
        F: FnOnce() -> PyResult<Py<PyAny>>,
        G: FnOnce() -> PyResult<Py<PyAny>>,
    {
        let mut arguments = invocation::CallArguments::Borrowed(kwargs);
        Self::call_subset_with_lazy_default_arguments(
            registry,
            name,
            &mut arguments,
            remove_plugins,
            firstresult,
            plugin_name,
            default,
        )?
        .into_python(registry.py())
    }

    pub(crate) fn call_subset_with_lazy_default_arguments<'py, F, G>(
        registry: &Bound<'py, Self>,
        name: &str,
        arguments: &mut invocation::CallArguments<'_, 'py>,
        remove_plugins: &Bound<'py, PyAny>,
        firstresult: bool,
        plugin_name: &str,
        default: NativeHookDefault<F, G>,
    ) -> PyResult<invocation::HookReturn>
    where
        F: FnOnce() -> PyResult<Py<PyAny>>,
        G: FnOnce() -> PyResult<Py<PyAny>>,
    {
        let NativeHookDefault {
            execute: direct,
            materialize: callback,
        } = default;
        let py = registry.py();
        let (implementations, firstresult) =
            prepare_call(registry, py, name, arguments, firstresult)?;
        let implementations = subset(py, name, implementations, remove_plugins)?;
        // Selection and subset can execute Python. Check the live state only
        // after those operations, just before the normal verification point.
        let private = {
            let registry = registry.borrow();
            registry.monitors.is_empty()
                && registry
                    .specifications
                    .get(name)
                    .is_none_or(|spec| spec.borrow(py).permits_private_default(py))
        };
        if private {
            if implementations.len(py) == 0 {
                let returned = direct().map_err(|error| {
                    PyErr::from_value(error.into_value(py).into_bound(py).into_any())
                })?;
                if !firstresult {
                    if returned.is_none(py) {
                        return Ok(invocation::HookReturn::EmptyList);
                    }
                    let results = PyList::empty(py);
                    results.append(returned)?;
                    return Ok(invocation::HookReturn::Python(results.into_any().unbind()));
                }
                return Ok(invocation::HookReturn::Python(returned));
            }
            let position = implementations.default_position(py);
            let direct = std::cell::RefCell::new(Some(direct));
            let invoke = || {
                let default = direct.borrow_mut().take().ok_or_else(|| {
                    PyRuntimeError::new_err("internal hook default is no longer available")
                })?;
                default()
            };
            let result = implementations.run_arguments(
                registry,
                name,
                arguments,
                firstresult,
                Some(NativeDispatch {
                    position,
                    callback: &invoke,
                }),
            );
            implementations.release();
            return result;
        }
        let kwargs = arguments.python_kwargs(py)?;
        let mut implementations = implementations.into_owned(py);
        insert_default(
            registry,
            py,
            name,
            &mut implementations,
            callback()?,
            plugin_name,
            CallbackSignature::NoParameters,
        )?;
        monitoring::execute(
            registry,
            name,
            HookSelection::Owned(implementations),
            kwargs,
            firstresult,
        )
        .map(invocation::HookReturn::Python)
    }

    /// Run one hook against authoritative native arguments, publishing them only
    /// when a monitor or a Python implementation actually needs the dictionary.
    pub(crate) fn call_subset_arguments<'py>(
        registry: &Bound<'py, Self>,
        name: &str,
        arguments: &mut invocation::CallArguments<'_, 'py>,
        remove_plugins: &Bound<'py, PyAny>,
        firstresult: bool,
    ) -> PyResult<invocation::HookReturn> {
        let py = registry.py();
        let (implementations, firstresult) =
            prepare_call(registry, py, name, arguments, firstresult)?;
        let implementations = subset(py, name, implementations, remove_plugins)?;
        Self::dispatch_arguments(registry, name, arguments, implementations, firstresult)
    }

    fn dispatch_arguments<'py>(
        registry: &Bound<'py, Self>,
        name: &str,
        arguments: &mut invocation::CallArguments<'_, 'py>,
        implementations: HookSelection,
        firstresult: bool,
    ) -> PyResult<invocation::HookReturn> {
        let py = registry.py();
        if registry.borrow().monitors.is_empty() {
            let result =
                implementations.run_arguments(registry, name, arguments, firstresult, None);
            implementations.release();
            result
        } else {
            let kwargs = arguments.python_kwargs(py)?;
            monitoring::execute(registry, name, implementations, kwargs, firstresult)
                .map(invocation::HookReturn::Python)
        }
    }

    pub(crate) fn plugin_observations(
        &self,
    ) -> PyResult<Vec<vfy_core::test_execution::PluginObservation>> {
        self.observations.snapshot()
    }

    /// Clone existing historic arguments without invoking Python consumers.
    pub(crate) fn historic_arguments(&self, py: Python<'_>, name: &str) -> Vec<Py<PyDict>> {
        self.history.get(name).map_or_else(Vec::new, |calls| {
            calls.iter().map(|call| call.kwargs.clone_ref(py)).collect()
        })
    }

    pub(crate) fn clear_history(registry: &Bound<'_, Self>, name: &str) {
        let history = registry
            .borrow_mut()
            .history
            .get_mut(name)
            .map(std::mem::take);
        drop(history);
    }

    /// The registry pin makes the shared current rows visible to Python GC;
    /// detached rows own their Python references independently.
    pub(super) fn selected(registry: &Bound<'_, Self>, name: &str) -> HookSelection {
        let rows = registry.borrow().implementations.get(name).cloned();
        rows.map_or(HookSelection::Empty, |rows| HookSelection::Rows {
            rows,
            registry: registry.clone().unbind(),
        })
    }

    pub(super) fn selected_owned(&self, py: Python<'_>, name: &str) -> Vec<Py<Implementation>> {
        self.implementations
            .get(name)
            .map_or_else(Vec::new, |rows| {
                rows.iter()
                    .map(|entry| entry.implementation.clone_ref(py))
                    .collect()
            })
    }

    pub(super) fn registrations(&self, py: Python<'_>) -> Vec<RegisteredImplementation> {
        let mut entries: Vec<_> = self
            .implementations
            .values()
            .flat_map(|rows| rows.iter())
            .map(|entry| entry.clone_ref(py))
            .collect();
        entries.sort_by_key(|entry| entry.sequence);
        entries
    }

    pub(super) fn mappings<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let result = PyDict::new(py);
        for plugin in self.plugins.iter() {
            result.set_item(
                &plugin.name,
                plugin.object.as_ref().map(|object| object.bind(py)),
            )?;
        }
        Ok(result)
    }

    fn removed_conftests(
        slf: &Bound<'_, Self>,
        path: &Bound<'_, PyAny>,
        arena: &Bound<'_, crate::independent::NativeCollectionArena>,
        item: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let path = path.extract::<PathBuf>()?;
        let published = if let Some(item) = item {
            crate::independent::NativeCollectionArena::conftests_published_for_item(
                arena, item, &path,
            )
        } else {
            crate::independent::NativeCollectionArena::conftests_published(arena, &path)
        };
        let removed = slf
            .borrow()
            .conftest_plugins_outside(py, published.then_some(path.as_path()));
        if removed.is_empty() {
            Ok(PyTuple::empty(py).into_any().unbind())
        } else {
            Ok(PyList::new(py, removed)?.into_any().unbind())
        }
    }
}

#[pymethods]
impl NativeHookRegistry {
    #[new]
    #[pyo3(signature = (project_name="pytest", discover_prefix=true))]
    fn py_new(project_name: &str, discover_prefix: bool) -> Self {
        let mut registry = Self::new();
        project_name.clone_into(&mut registry.project_name);
        registry.discover_prefix = discover_prefix;
        registry
    }
    #[getter]
    fn project_name(&self) -> &str {
        &self.project_name
    }
    #[getter]
    fn trace(slf: &Bound<'_, Self>) -> PyResult<Py<NativeTagTracerSub>> {
        monitoring::trace(slf)
    }
    fn add_hookcall_monitoring(
        slf: &Bound<'_, Self>,
        before: Py<PyAny>,
        after: Py<PyAny>,
    ) -> NativeHookUndo {
        monitoring::add(slf, Monitor::Callbacks { before, after })
    }
    fn enable_tracing(slf: &Bound<'_, Self>) -> PyResult<NativeHookUndo> {
        let trace = monitoring::trace(slf)?;
        let root = trace.borrow(slf.py()).root.clone_ref(slf.py());
        Ok(monitoring::add(slf, Monitor::Trace(root)))
    }
    #[getter]
    fn hook(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Ok(slf
            .py()
            .import("vfy.independent.protocol")?
            .getattr("HookRelay")?
            .call1((slf,))?
            .unbind())
    }

    fn hook_proxy(
        slf: &Bound<'_, Self>,
        path: &Bound<'_, PyAny>,
        config: &Bound<'_, PyAny>,
        arena: &Bound<'_, crate::independent::NativeCollectionArena>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let path = path.extract::<PathBuf>()?;
        let published =
            crate::independent::NativeCollectionArena::conftests_published(arena, &path);
        let removed = slf
            .borrow()
            .conftest_plugins_outside(py, published.then_some(path.as_path()));
        if removed.is_empty() {
            return Ok(config.getattr(pyo3::intern!(py, "hook"))?.unbind());
        }
        Ok(py
            .import("vfy.independent.protocol")?
            .getattr("HookRelay")?
            .call1((slf, PyList::new(py, removed)?))?
            .unbind())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for plugin in self.plugins.iter() {
            if let Some(object) = &plugin.object {
                visit.call(object)?;
            }
        }
        for rows in self.implementations.values() {
            for entry in rows.iter() {
                visit.call(&entry.implementation)?;
            }
        }
        for specification in self.specifications.values() {
            visit.call(specification)?;
        }
        for monitor in &self.monitors {
            monitor.traverse(&visit)?;
        }
        if let Some(trace) = &self.trace {
            visit.call(trace)?;
        }
        for history in self.history.values() {
            for call in history {
                visit.call(&call.kwargs)?;
                if let Some(callback) = &call.callback {
                    visit.call(callback)?;
                }
            }
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = std::mem::take(&mut *slf.borrow_mut());
        drop(removed);
    }

    #[pyo3(signature = (plugin, name=None))]
    pub(crate) fn register(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        plugin: Py<PyAny>,
        name: Option<String>,
    ) -> PyResult<Option<String>> {
        register_plugin(slf, &plugin.into_bound(py), name, Publication::Atomic)
    }
    #[pyo3(signature = (plugin, name=None))]
    fn register_compatible(
        slf: &Bound<'_, Self>,
        plugin: &Bound<'_, PyAny>,
        name: Option<String>,
    ) -> PyResult<Option<String>> {
        register_plugin(slf, plugin, name, Publication::Incremental)
    }

    #[pyo3(signature = (plugin=None, name=None))]
    fn unregister(
        slf: &Bound<'_, Self>,
        plugin: Option<&Bound<'_, PyAny>>,
        name: Option<&str>,
    ) -> PyResult<Option<Py<PyAny>>> {
        unregister_plugin(slf, plugin, name)
    }
    #[pyo3(signature = (plugin=None, name=None))]
    fn unregister_compatible(
        slf: &Bound<'_, Self>,
        plugin: Option<&Bound<'_, PyAny>>,
        name: Option<&str>,
    ) -> PyResult<Option<Py<PyAny>>> {
        unregister_plugin(slf, plugin, name)
    }

    fn get_canonical_name(_slf: &Bound<'_, Self>, plugin: &Bound<'_, PyAny>) -> PyResult<String> {
        canonical_name(plugin)
    }
    pub(crate) fn get_plugin(&self, py: Python<'_>, name: &str) -> Option<Py<PyAny>> {
        self.plugins
            .iter()
            .find(|entry| entry.name == name)
            .and_then(|entry| entry.object.as_ref().map(|object| object.clone_ref(py)))
    }
    fn get_plugins(&self, py: Python<'_>) -> Vec<Py<PyAny>> {
        self.plugins
            .iter()
            .filter_map(|entry| entry.object.as_ref().map(|object| object.clone_ref(py)))
            .collect()
    }
    pub(crate) fn has_plugin(&self, name: &str) -> bool {
        self.plugins
            .iter()
            .any(|entry| entry.name == name && entry.object.is_some())
    }
    pub(super) fn get_name(&self, plugin: &Bound<'_, PyAny>) -> Option<String> {
        self.plugins
            .iter()
            .find(|entry| {
                entry
                    .object
                    .as_ref()
                    .is_some_and(|object| object.is(plugin))
            })
            .map(|entry| entry.name.clone())
    }
    fn is_registered(&self, plugin: &Bound<'_, PyAny>) -> bool {
        self.get_name(plugin).is_some()
    }
    fn list_name_plugin(&self, py: Python<'_>) -> Vec<(String, Option<Py<PyAny>>)> {
        self.plugins
            .iter()
            .map(|entry| {
                (
                    entry.name.clone(),
                    entry.object.as_ref().map(|object| object.clone_ref(py)),
                )
            })
            .collect()
    }
    fn is_blocked(&self, name: &str) -> bool {
        self.plugins
            .iter()
            .any(|entry| entry.name == name && entry.object.is_none())
    }
    fn unblock(slf: &Bound<'_, Self>, name: &str) -> bool {
        let mut registry = slf.borrow_mut();
        registry.plugins.remove(name, true).is_some()
    }
    fn set_blocked(slf: &Bound<'_, Self>, name: &str) -> PyResult<()> {
        unregister_plugin(slf, None, Some(name))?;
        if !slf.borrow().is_blocked(name) {
            let mut registry = slf.borrow_mut();
            registry.plugins.insert(name.to_owned(), None, None)?;
        }
        Ok(())
    }

    fn add_hookspecs(slf: &Bound<'_, Self>, namespace: &Bound<'_, PyAny>) -> PyResult<()> {
        add_specifications(slf, namespace)
    }
    fn add_hookspecs_compatible(
        slf: &Bound<'_, Self>,
        namespace: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        add_specifications(slf, namespace)
    }
    fn get_hook_names(&self) -> Vec<String> {
        self.hook_names.clone()
    }
    fn has_hook(&self, name: &str) -> bool {
        self.hook_names.iter().any(|hook| hook == name)
    }
    fn has_spec(&self, name: &str) -> bool {
        self.specifications.contains_key(name)
    }
    fn is_historic(&self, name: &str) -> bool {
        self.history.contains_key(name)
    }
    fn get_spec(&self, py: Python<'_>, name: &str) -> Option<Py<Specification>> {
        self.specifications.get(name).map(|spec| spec.clone_ref(py))
    }
    fn get_hookimpls(&self, py: Python<'_>, name: &str) -> Vec<Py<Implementation>> {
        let mut result = self.selected_owned(py, name);
        result.reverse();
        result
    }
    fn get_subset_hookimpls(
        slf: &Bound<'_, Self>,
        name: &str,
        remove_plugins: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<Py<Implementation>>> {
        let py = slf.py();
        let implementations = NativeHookRegistry::selected(slf, name);
        let mut implementations = subset(py, name, implementations, remove_plugins)?.into_owned(py);
        implementations.reverse();
        Ok(implementations)
    }
    fn get_hookcallers(&self, plugin: &Bound<'_, PyAny>) -> Option<Vec<String>> {
        if !self.is_registered(plugin) {
            return None;
        }
        let py = plugin.py();
        let names = self
            .hook_names
            .iter()
            .filter(|name| {
                self.implementations.get(name.as_str()).is_some_and(|rows| {
                    rows.iter()
                        .any(|entry| entry.implementation.borrow(py).plugin.is(plugin))
                })
            })
            .cloned()
            .collect();
        Some(names)
    }
    fn check_pending(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let pending: Vec<_> = {
            let registry = slf.borrow();
            registry
                .registrations(py)
                .into_iter()
                .filter(|entry| {
                    !registry
                        .specifications
                        .contains_key(&entry.implementation.borrow(py).name)
                })
                .map(|entry| entry.implementation.borrow(py).clone_ref(py))
                .collect()
        };
        for implementation in pending {
            if !implementation.optionalhook {
                return Err(specification::validation_error(
                    implementation.plugin.bind(py),
                    format!(
                        "unknown hook '{}' in plugin {}",
                        implementation.name,
                        implementation.plugin.bind(py).repr()?
                    ),
                )?);
            }
        }
        Ok(())
    }

    fn check_supported(slf: &Bound<'_, Self>, names: &Bound<'_, PyAny>) -> PyResult<()> {
        let names: Vec<String> = names.extract()?;
        let py = slf.py();
        let builtin = py.import("vfy.independent.hookspecs")?;
        let implementations: Vec<_> = {
            let registry = slf.borrow();
            registry
                .registrations(py)
                .into_iter()
                .filter_map(|entry| {
                    let implementation = entry.implementation.borrow(py);
                    registry
                        .specifications
                        .get(&implementation.name)
                        .map(|specification| {
                            (implementation.clone_ref(py), specification.clone_ref(py))
                        })
                })
                .collect()
        };
        for (implementation, specification) in implementations {
            let namespace = specification.borrow(py).namespace.clone_ref(py);
            if namespace.bind(py).is(&builtin) && !names.contains(&implementation.name) {
                return Err(admission::unsupported(
                    &implementation.plugin_name,
                    &implementation.name,
                ));
            }
        }
        Ok(())
    }

    #[pyo3(signature = (name, kwargs, firstresult=false))]
    pub(crate) fn call(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        name: &str,
        kwargs: &Bound<'_, PyDict>,
        firstresult: bool,
    ) -> PyResult<Py<PyAny>> {
        multicall(slf, py, Multicall::new(name, kwargs, firstresult))
    }

    #[pyo3(signature = (name, kwargs, callback, firstresult=false, plugin_name="runner"))]
    pub(crate) fn call_with_default(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        name: &str,
        kwargs: &Bound<'_, PyDict>,
        callback: Py<PyAny>,
        firstresult: bool,
        plugin_name: &str,
    ) -> PyResult<Py<PyAny>> {
        let call = Multicall {
            extension: Extension::Default {
                callback,
                plugin_name,
                signature: CallbackSignature::Inspect,
            },
            ..Multicall::new(name, kwargs, firstresult)
        };
        multicall(slf, py, call)
    }

    #[pyo3(signature = (name, kwargs, remove_plugins, firstresult=false))]
    pub(crate) fn call_subset(
        slf: &Bound<'_, Self>,
        name: &str,
        kwargs: &Bound<'_, PyDict>,
        remove_plugins: &Bound<'_, PyAny>,
        firstresult: bool,
    ) -> PyResult<Py<PyAny>> {
        let call = Multicall {
            remove_plugins: Some(remove_plugins),
            ..Multicall::new(name, kwargs, firstresult)
        };
        multicall(slf, slf.py(), call)
    }

    #[pyo3(signature = (name, kwargs, remove_plugins, callback, firstresult=false, plugin_name="runner"))]
    fn call_subset_with_default(
        slf: &Bound<'_, Self>,
        name: &str,
        kwargs: &Bound<'_, PyDict>,
        remove_plugins: &Bound<'_, PyAny>,
        callback: Py<PyAny>,
        firstresult: bool,
        plugin_name: &str,
    ) -> PyResult<Py<PyAny>> {
        let call = Multicall {
            remove_plugins: Some(remove_plugins),
            extension: Extension::Default {
                callback,
                plugin_name,
                signature: CallbackSignature::Inspect,
            },
            ..Multicall::new(name, kwargs, firstresult)
        };
        multicall(slf, slf.py(), call)
    }

    #[pyo3(signature = (name, methods, kwargs, remove_plugins=None))]
    fn call_extra(
        slf: &Bound<'_, Self>,
        name: &str,
        methods: &Bound<'_, PyAny>,
        kwargs: &Bound<'_, PyDict>,
        remove_plugins: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let call = Multicall {
            remove_plugins,
            extension: Extension::Extra(methods),
            ..Multicall::new(name, kwargs, false)
        };
        multicall(slf, slf.py(), call)
    }

    #[pyo3(signature = (name, kwargs=None, result_callback=None))]
    fn call_historic(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        name: &str,
        kwargs: Option<&Bound<'_, PyDict>>,
        result_callback: Option<Py<PyAny>>,
    ) -> PyResult<()> {
        let kwargs = kwargs
            .filter(|kwargs| !kwargs.is_empty())
            .cloned()
            .unwrap_or_else(|| PyDict::new(py));
        let specification = slf
            .borrow()
            .specifications
            .get(name)
            .map(|spec| spec.clone_ref(py))
            .ok_or_else(|| PyAssertionError::new_err(""))?;
        if !specification.borrow(py).historic {
            return Err(PyAssertionError::new_err(""));
        }
        verify_call(
            py,
            &specification,
            &invocation::CallArguments::Borrowed(&kwargs),
        )?;
        {
            slf.borrow_mut()
                .history
                .entry(name.to_owned())
                .or_default()
                .push(HistoryCall {
                    kwargs: kwargs.clone().unbind(),
                    callback: result_callback
                        .as_ref()
                        .map(|callback| callback.clone_ref(py)),
                });
        }
        let implementations = NativeHookRegistry::selected(slf, name);
        let result = monitoring::execute(slf, name, implementations, &kwargs, false)?;
        if let Some(callback) = result_callback
            && let Ok(values) = result.bind(py).cast::<PyList>()
        {
            for value in values.try_iter()? {
                callback.bind(py).call1((value?,))?;
            }
        }
        Ok(())
    }
}
