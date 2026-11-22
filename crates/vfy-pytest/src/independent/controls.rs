//! Recognize owned pytest exit controls without inspecting arbitrary exceptions.
use crate::module_cache::Module;
use pyo3::exceptions::PyKeyboardInterrupt;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ControlKind {
    KeyboardInterrupt,
    Exit,
}

pub(super) fn kind(py: Python<'_>, error: &PyErr) -> PyResult<Option<ControlKind>> {
    if error.is_instance_of::<PyKeyboardInterrupt>(py) {
        return Ok(Some(ControlKind::KeyboardInterrupt));
    }
    let exit = Module::Facade.get(py)?.getattr("Exit")?;
    Ok(error.is_instance(py, &exit).then_some(ControlKind::Exit))
}

pub(crate) fn is_control_error(py: Python<'_>, error: &PyErr) -> PyResult<bool> {
    Ok(kind(py, error)?.is_some())
}

pub(super) fn exitstatus(py: Python<'_>, error: &PyErr) -> PyResult<Option<i32>> {
    match kind(py, error)? {
        None => Ok(None),
        Some(ControlKind::KeyboardInterrupt) => Ok(Some(2)),
        Some(ControlKind::Exit) => {
            let code = error.value(py).getattr("returncode")?;
            Ok(Some(if code.is_none() { 2 } else { code.extract()? }))
        }
    }
}

pub(super) fn exception_info<'py>(py: Python<'py>, error: &PyErr) -> PyResult<Bound<'py, PyAny>> {
    let info = Module::Facade.get(py)?.getattr("ExceptionInfo")?.call0()?;
    info.setattr("_value", error.value(py))?;
    Ok(info)
}

#[derive(Clone, Copy)]
pub(super) enum Notification<'a, 'py> {
    Interrupt,
    Internal(&'a Bound<'py, PyAny>),
}

#[derive(Clone, Copy)]
pub(super) enum LoopCompletion {
    Completed,
    SessionFailed,
    Interrupted,
    Control,
}

impl super::lifecycle::Context {
    pub(super) fn finish_execution(
        &self,
        py: Python<'_>,
        has_internal_errors: bool,
        collected: usize,
        completion: LoopCompletion,
    ) -> PyResult<i32> {
        if matches!(completion, LoopCompletion::Control) || has_internal_errors {
            return self.finish_current(py);
        }
        let failed = self
            .session
            .bind(py)
            .getattr("testsfailed")?
            .extract::<usize>()?;
        self.finish(
            py,
            if has_internal_errors {
                3
            } else if matches!(completion, LoopCompletion::SessionFailed) {
                1
            } else if matches!(completion, LoopCompletion::Interrupted) {
                2
            } else if failed > 0 {
                1
            } else if collected == 0 && !self.invocation.surplus.get() {
                5
            } else {
                0
            },
        )
    }
}

pub(super) fn notify<'py>(
    py: Python<'py>,
    registry: &Bound<'py, crate::hooks::NativeHookRegistry>,
    error: &PyErr,
    notification: Notification<'_, 'py>,
) -> PyResult<Py<PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("excinfo", exception_info(py, error)?)?;
    let name = match notification {
        Notification::Interrupt => "pytest_keyboard_interrupt",
        Notification::Internal(representation) => {
            kwargs.set_item("excrepr", representation)?;
            "pytest_internalerror"
        }
    };
    let callback = Module::Functools.get(py)?.getattr("partial")?.call1((
        registry.getattr("call")?,
        name,
        kwargs,
    ))?;
    Ok(py
        .import("vfy._hook_callbacks")?
        .getattr("call_while_handling")?
        .call1((callback, error.value(py)))?
        .unbind())
}
