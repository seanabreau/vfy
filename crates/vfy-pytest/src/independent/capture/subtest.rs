//! A standalone subtest logging handler without resetting phase or caplog buffers.

use super::{NativeLogState, NativeLoggingHooks};
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};

pub(in crate::independent) struct SubtestLogs {
    root: Option<Py<PyAny>>,
    handler: Option<Py<PyAny>>,
    original_level: Option<Py<PyAny>>,
}

impl SubtestLogs {
    pub(in crate::independent) fn start(config: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = config.py();
        let mut lease = Self {
            root: None,
            handler: None,
            original_level: None,
        };
        let plugin = config
            .getattr("pluginmanager")?
            .call_method1("get_plugin", ("logging-plugin",))?;
        if plugin.is_none() {
            return Ok(lease);
        }
        let hooks = plugin
            .getattr("_capture")?
            .extract::<Py<NativeLoggingHooks>>()?;
        let Some((formatter, level)) = hooks.borrow(py).subtest_settings(py)? else {
            return Ok(lease);
        };
        let state = Py::new(py, NativeLogState::new())?;
        let handler = py
            .import("vfy.independent.capture")?
            .call_method1("LogHandler", (state, formatter))?;
        if let Some(level) = level {
            handler.call_method1("setLevel", (level,))?;
        }
        let root = py.import("logging")?.call_method0("getLogger")?;
        root.call_method1("addHandler", (&handler,))?;
        lease.handler = Some(handler.unbind());
        lease.root = Some(root.clone().unbind());
        if let Some(level) = level {
            let original = root.getattr("level")?;
            lease.original_level = Some(original.clone().unbind());
            let selected = if original.lt(level)? {
                original
            } else {
                level.into_pyobject(py)?.into_any()
            };
            root.call_method1("setLevel", (selected,))?;
        }
        Ok(lease)
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        let root = self.root.take();
        let original = self.original_level.take();
        let Some(root) = root else {
            return Ok(());
        };
        if let Some(level) = original {
            root.bind(py).call_method1("setLevel", (level,))?;
        }
        if let Some(handler) = &self.handler {
            root.bind(py).call_method1("removeHandler", (handler,))?;
        }
        Ok(())
    }

    pub(in crate::independent) fn complete(mut self, py: Python<'_>) -> PyResult<String> {
        self.close(py)?;
        match self.handler.take() {
            Some(handler) => handler
                .bind(py)
                .getattr("stream")?
                .call_method0("getvalue")?
                .extract(),
            None => Ok(String::new()),
        }
    }

    pub(in crate::independent) fn traverse(
        &self,
        visit: &PyVisit<'_>,
    ) -> Result<(), PyTraverseError> {
        visit.call(&self.root)?;
        visit.call(&self.handler)?;
        visit.call(&self.original_level)?;
        Ok(())
    }
}

impl Drop for SubtestLogs {
    fn drop(&mut self) {
        super::drop_capture_scope(|py| {
            let _ = self.close(py);
        });
    }
}
