//! Register `JUnit` fixture declarations through plugin history.

use super::Collector;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

const PROVIDER: &str = "junitxml";

impl Collector<'_> {
    pub(super) fn install_property_provider(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_property_provider()
    }

    pub(super) fn activate_property_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("JUnit provider requires a configured collector")
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
            .import("vfy.independent.junit")?
            .getattr("JUnitProvider")?
            .call0()?;
        let registered = manager.call_method1("register", (&provider, PROVIDER))?;
        if !registered.is_none() {
            self.plugins.insert(PROVIDER.to_owned(), provider.unbind());
        }
        Ok(())
    }
}
