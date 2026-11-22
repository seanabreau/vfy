//! Register the cache fixture through the retained plugin bindings.

use super::{Collector, lifecycle::Context};
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::collections::BTreeSet;
use std::sync::Arc;

const PROVIDER: &str = "cacheprovider";

/// The controller does not collect Items; use the validated merged selection.
pub(super) fn collect_worker_result(
    py: Python<'_>,
    context: &Context,
    result: &Bound<'_, NativeRunResult>,
) -> PyResult<()> {
    let plugin = context
        .config
        .bind(py)
        .getattr("pluginmanager")?
        .call_method1("getplugin", ("nfplugin",))?;
    if plugin.is_none()
        || !plugin.is_instance(&py.import("vfy.independent.cache")?.getattr("NFPlugin")?)?
    {
        return Ok(());
    }
    let state = plugin.getattr("_state")?;
    let Ok(state) = state.cast::<NodeidCache>() else {
        return Ok(());
    };
    // Collection observations include selected tests not executed after failfast.
    // Read the existing native projection before taking the mutable cache borrow.
    let nodeids = {
        let result = result.borrow();
        if result.record().engine != "vfy-independent-v1" || result.record().observations.is_none()
        {
            return Err(PyValueError::new_err(
                "controller node-ID cache requires independent collection observations",
            ));
        }
        result.collection_nodeids()
    };
    Arc::make_mut(&mut state.borrow_mut().nodeids).extend(nodeids);
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (config, publisher_pid))]
fn configure_cache(config: &Bound<'_, PyAny>, publisher_pid: Option<u32>) -> PyResult<()> {
    let cache = config
        .py()
        .import("vfy.independent.cache")?
        .getattr("Cache")?
        .call_method1("for_config", (config,))?;
    config.setattr("cache", &cache)?;
    let nodeids = cache
        .call_method1("get", ("cache/nodeids", Vec::<String>::new()))?
        .try_iter()?
        .map(|nodeid| nodeid?.extract())
        .collect::<PyResult<BTreeSet<String>>>()?;
    let state = Py::new(
        config.py(),
        NodeidCache {
            nodeids: Arc::new(nodeids),
            publisher_pid,
        },
    )?;
    let plugin = config
        .py()
        .import("vfy.independent.cache")?
        .getattr("NFPlugin")?
        .call1((config, state))?;
    config
        .getattr("pluginmanager")?
        .call_method1("register", (plugin, "nfplugin"))?;
    Ok(())
}

/// Only node IDs cross hook boundaries. No plugin, session, or cache handle is retained.
#[pyclass]
struct NodeidCache {
    nodeids: Arc<BTreeSet<String>>,
    publisher_pid: Option<u32>,
}

#[pymethods]
impl NodeidCache {
    fn collect(slf: &Bound<'_, Self>, items: &Bound<'_, PyAny>) -> PyResult<()> {
        let nodeids = items
            .try_iter()?
            .map(|item| item?.getattr("nodeid")?.extract())
            .collect::<PyResult<Vec<String>>>()?;
        Arc::make_mut(&mut slf.borrow_mut().nodeids).extend(nodeids);
        Ok(())
    }

    fn finish(slf: &Bound<'_, Self>, config: &Bound<'_, PyAny>) -> PyResult<()> {
        // Forked workers inherit this object, while fresh workers never receive a
        // publishing PID. Neither may replace the controller's complete inventory.
        let nodeids = {
            let state = slf.borrow();
            if state.publisher_pid != Some(std::process::id()) {
                return Ok(());
            }
            Arc::clone(&state.nodeids)
        };
        if config
            .call_method1("getoption", ("cacheshow", false))?
            .is_truthy()?
            || config.hasattr("workerinput")?
            || config
                .call_method1("getoption", ("collectonly", false))?
                .is_truthy()?
        {
            return Ok(());
        }
        let cache = config.getattr("cache")?;
        let nodeids = PyList::new(slf.py(), nodeids.iter().map(String::as_str))?;
        cache.call_method1("set", ("cache/nodeids", nodeids))?;
        Ok(())
    }
}

impl Collector<'_> {
    pub(super) fn install_cache_provider(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_cache_provider()
    }

    pub(super) fn activate_cache_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("cache provider requires a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", (PROVIDER,))?
            .is_truthy()?
        {
            return Ok(());
        }
        let callback = wrap_pyfunction!(configure_cache, self.py)?;
        let provider = self
            .py
            .import("vfy.independent.cache")?
            .getattr("CacheProvider")?
            .call1((
                callback,
                vfy_core::pytest_config::cache_dir_default(),
                if context.invocation.is_worker() && !context.invocation.is_fresh_serial() {
                    None
                } else {
                    Some(std::process::id())
                },
            ))?;
        let registered = manager.call_method1("register", (&provider, PROVIDER))?;
        if !registered.is_none() {
            // Retain the actual builtin for later positive plugin activation.
            // The registry's historic event owns fixture registration order.
            self.plugins.insert(PROVIDER.to_owned(), provider.unbind());
        }
        Ok(())
    }
}
