//! Capture session warnings through the same native scope as test protocols.
use super::Context;
use crate::independent::capture::{WarningScope, WarningSettings};
use pyo3::prelude::*;

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
