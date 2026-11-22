//! Register the doctest namespace through session fixture bindings.

use super::Collector;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

const PROVIDER: &str = "doctest";

impl Collector<'_> {
    pub(super) fn install_doctest_provider(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_doctest_provider()
    }

    pub(super) fn activate_doctest_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("doctest provider requires a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", (PROVIDER,))?
            .is_truthy()?
        {
            return Ok(());
        }
        let provider = self
            .py
            .import("vfy.independent.doctest_provider")?
            .getattr("DoctestProvider")?
            .call0()?;
        let registered = manager.call_method1("register", (&provider, PROVIDER))?;
        if !registered.is_none() {
            // Retain the actual provider for later positive plugin activation.
            self.plugins.insert(PROVIDER.to_owned(), provider.unbind());
        }
        Ok(())
    }
}
