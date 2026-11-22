//! Retain the capture plugin only after startup exclusions are applied.

use super::Collector;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

const PROVIDER: &str = "capture";

impl Collector<'_> {
    pub(super) fn install_capture_provider(&mut self, raw_plugins: &[String]) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_capture_provider()
    }

    pub(super) fn activate_capture_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("capture provider requires a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", (PROVIDER,))?
            .is_truthy()?
        {
            return Ok(());
        }
        if let Some(plugin) = super::global_capture::install(self.py, context)? {
            // Positive plugin requests reuse these hooks and their one native capture owner.
            self.plugins.insert(PROVIDER.to_owned(), plugin);
        }
        Ok(())
    }
}
