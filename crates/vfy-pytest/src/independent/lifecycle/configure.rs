//! Builtin registration runs at the core plugin's normal configure priority.
use super::Context;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::RefCell;

#[pyclass(frozen, unsendable)]
struct NativeConfigureCallback {
    context: RefCell<Option<Context>>,
}

#[pymethods]
impl NativeConfigureCallback {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let context = self.context.borrow_mut().take().ok_or_else(|| {
            pyo3::exceptions::PyAssertionError::new_err(
                "core configure callback is already complete",
            )
        })?;
        let protocol = py.import("vfy.independent.protocol")?;
        for marker in protocol.getattr("_BUILTIN_MARKERS")?.try_iter()? {
            context
                .config
                .bind(py)
                .call_method1("addinivalue_line", ("markers", marker?))?;
        }
        context.install_junit_writer(py)?;
        let registry = context.registry.bind(py);
        if !registry
            .call_method1("is_blocked", ("terminal",))?
            .is_truthy()?
        {
            let callbacks = PyDict::new(py);
            callbacks.set_item(
                "status_index",
                context.session.bind(py).getattr("_vfy_report")?,
            )?;
            callbacks.set_item(
                "collection_finish",
                crate::independent::reporting::terminal::collection::callback(
                    py,
                    &context.invocation,
                )?,
            )?;
            let reporter = py
                .import("vfy.independent.terminal")?
                .getattr("TerminalReporter")?
                .call((&context.config,), Some(&callbacks))?;
            let reporter = reporter.unbind();
            context.register(py, reporter.clone_ref(py), "terminalreporter")?;
            if registry
                .call_method1("has_plugin", ("terminalreporter",))?
                .is_truthy()?
            {
                let internal_error = Py::new(
                    py,
                    super::internalerror::NativeInternalErrorHooks {
                        reporter: pyo3::types::PyWeakrefReference::new(reporter.bind(py))?.unbind(),
                    },
                )?;
                context.register(py, internal_error.into_any(), "vfy_builtin_internal_error")?;
                let recorder = Py::new(
                    py,
                    crate::independent::capture::NativeWarningRecorder::new(std::rc::Rc::clone(
                        &context.warnings,
                    )),
                )?;
                let history = context
                    .session
                    .bind(py)
                    .getattr("_vfy_report")?
                    .cast_into::<crate::independent::reporting::NativeSessionReports>()?;
                history
                    .borrow_mut()
                    .summary
                    .attach_warnings(py, &recorder)?;
                let hooks = protocol.getattr("BuiltinTerminalHooks")?.call1((
                    crate::independent::session_hooks::summary_callback(py, &context)?,
                    recorder.bind(py).getattr("record")?,
                    super::header::callback(py, &context)?,
                ))?;
                context.register(py, hooks.unbind(), "vfy_builtin_terminal")?;
            }
        }
        Ok(())
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(context) = self.context.try_borrow()
            && let Some(context) = context.as_ref()
        {
            context.traverse(&visit)?;
        }
        Ok(())
    }
    fn __clear__(&self) {
        let previous = self.context.borrow_mut().take();
        drop(previous);
    }
}

impl Context {
    pub(super) fn install_configure(&self, py: Python<'_>) -> PyResult<()> {
        let callback = Py::new(
            py,
            NativeConfigureCallback {
                context: RefCell::new(Some(self.clone_ref(py))),
            },
        )?;
        let plugin = py
            .import("vfy.independent.protocol")?
            .getattr("BuiltinConfigureHooks")?
            .call1((callback,))?;
        self.register(py, plugin.unbind(), "vfy_builtin_configure")
    }
}
