//! Install and apply pytest's warning scopes across the invocation.
use super::Context;
use crate::independent::Collector;
use crate::independent::capture::{WarningScope, WarningSettings, warnings};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

const PROVIDER: &str = "warnings";

#[pyfunction]
fn initial_warning_scope(config: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    warnings::scope(config)
}

impl Collector<'_> {
    pub(in crate::independent) fn install_warning_provider(
        &mut self,
        raw_plugins: &[String],
    ) -> PyResult<()> {
        self.exclude_raw_provider(raw_plugins, PROVIDER)?;
        self.activate_warning_provider()
    }

    pub(in crate::independent) fn activate_warning_provider(&mut self) -> PyResult<()> {
        let context = self.context.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("the warning provider requires a configured collector")
        })?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", (PROVIDER,))?
            .is_truthy()?
        {
            return Ok(());
        }
        let plugin = self
            .py
            .import("vfy.independent.protocol")?
            .getattr("BuiltinWarnings")?
            .call1((wrap_pyfunction!(initial_warning_scope, self.py)?,))?;
        let registered = manager.call_method1("register", (&plugin, PROVIDER))?;
        if !registered.is_none() {
            self.plugins.insert(PROVIDER.to_owned(), plugin.unbind());
        }
        Ok(())
    }
}

impl Context {
    pub(in crate::independent) fn capture_warnings<T>(
        &self,
        py: Python<'_>,
        when: &str,
        operation: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let enabled = self.registry.bind(py).try_borrow()?.has_plugin("warnings");
        if !enabled {
            return operation();
        }
        let settings = WarningSettings::from_config(self.config.bind(py))?;
        let scope = WarningScope::start(py, &settings, None)?;
        let result = operation();
        scope.dispatch(py, "", |message, nodeid| {
            let nodeid = nodeid.into_pyobject(py)?.into_any();
            let when = when.into_pyobject(py)?.into_any();
            self.call(
                py,
                "pytest_warning_recorded",
                &[
                    ("warning_message", message),
                    ("when", &when),
                    ("nodeid", &nodeid),
                    ("location", py.None().bind(py)),
                ],
            )?;
            Ok(())
        })?;
        result
    }
}

impl Context {
    pub(super) fn cleanup_warning_filters<T>(
        &self,
        py: Python<'_>,
        operation: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let enabled = self.registry.bind(py).try_borrow()?.has_plugin("warnings");
        if !enabled {
            return operation();
        }
        // On a rejected filter, run the cleanups the invocation still owes, as pytest's
        // `_ensure_unconfigure` does. The rejection is already recorded; returning it
        // again would replace the recorded usage disposition.
        let Ok(scope) = WarningSettings::from_config(self.config.bind(py))
            .and_then(|settings| WarningScope::publish(py, &settings))
        else {
            return operation();
        };
        let result = operation();
        scope.dispatch(py, "", |_, _| Ok(()))?;
        result
    }
}
