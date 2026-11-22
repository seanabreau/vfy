//! Adapt capture scopes to executor phase records.

use super::super::CollectedSuite;
use super::super::capture::{NativeLoggingHooks, TestCapture, WarningScope, WarningSettings};
use super::super::reporting::call::{PhaseClock, PhaseCompletion};
use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::rc::Rc;
use vfy_core::test_execution::ExecutionPhase;

pub(super) struct ExecutionCapture {
    pub(super) logs: Option<Rc<TestCapture>>,
    warnings: Option<(WarningScope, Py<NativeHookRegistry>)>,
    hooks: Option<Py<NativeLoggingHooks>>,
}

impl ExecutionCapture {
    pub(super) fn start(py: Python<'_>, suite: &CollectedSuite, index: usize) -> PyResult<Self> {
        let context = &suite.context;
        let config = context.config.bind(py);
        let plugin = context
            .registry
            .bind(py)
            .try_borrow()?
            .get_plugin(py, "logging-plugin");
        let hooks = plugin
            .map(|plugin| -> PyResult<_> {
                Ok(plugin
                    .bind(py)
                    .getattr("_capture")?
                    .extract::<Py<NativeLoggingHooks>>()?)
            })
            .transpose()?;
        let logs = hooks
            .as_ref()
            .map(|hooks| hooks.borrow(py).new_capture(py))
            .transpose()?;
        let warnings_enabled = context
            .registry
            .bind(py)
            .try_borrow()?
            .has_plugin("warnings");
        let warnings = if warnings_enabled {
            let settings = WarningSettings::from_config(config)?;
            Some((
                WarningScope::start(
                    py,
                    &settings,
                    context.items.get(index).map(|item| item.bind(py)),
                )?,
                context.registry.clone_ref(py),
            ))
        } else {
            None
        };
        if let Some(hooks) = &hooks {
            hooks.borrow(py).set_capture(logs.clone());
        }
        Ok(Self {
            logs,
            warnings,
            hooks,
        })
    }

    pub(super) fn phase(
        &self,
        py: Python<'_>,
        which: ExecutionPhase,
        operation: impl FnOnce() -> PyResult<()>,
    ) -> PhaseCompletion {
        let clock = PhaseClock::start();
        let result = operation();
        let mut completion = clock.complete(which, result);
        completion.capture.log = self
            .hooks
            .as_ref()
            .map_or_else(String::new, |hooks| hooks.borrow(py).take_log(which));
        completion
    }

    pub(super) fn finish(mut self, py: Python<'_>, nodeid: &str) -> PyResult<()> {
        let Some((scope, registry)) = self.warnings.take() else {
            return Ok(());
        };
        scope.dispatch(py, nodeid, |message, nodeid| {
            let kwargs = PyDict::new(py);
            kwargs.set_item("warning_message", message)?;
            kwargs.set_item("when", "runtest")?;
            kwargs.set_item("nodeid", nodeid)?;
            kwargs.set_item("location", py.None())?;
            registry
                .bind(py)
                .call_method1("call_historic", ("pytest_warning_recorded", kwargs))?;
            Ok(())
        })
    }
}

impl Drop for ExecutionCapture {
    fn drop(&mut self) {
        if let Some(hooks) = self.hooks.take() {
            Python::attach(|py| hooks.borrow(py).set_capture(None));
        }
    }
}
