//! Preserve the original configuration diagnostic before cleanup can replace its error.
use super::{Context, state::LifecycleStage};
use crate::independent::controls;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

/// Write the message of an explicit `pytest.exit` to stderr, as stock pytest does.
pub(super) fn write_exit_message(py: Python<'_>, error: &PyErr) -> PyResult<()> {
    let stderr = py.import("sys")?.getattr("stderr")?;
    let message = format!("Exit: {}\n", error.value(py).getattr("msg")?.str()?);
    stderr.call_method1("write", (message,))?;
    Ok(())
}

impl Context {
    pub(in crate::independent) fn report_error<T>(
        &self,
        py: Python<'_>,
        result: &PyResult<T>,
    ) -> PyResult<()> {
        let Err(error) = result else { return Ok(()) };
        if !self.config.bind(py).getattr("_configured")?.is_truthy()?
            || error.is_instance_of::<crate::IndependentUsageError>(py)
            || error.is_instance_of::<crate::UnsupportedFeatureError>(py)
        {
            return Ok(());
        }
        if let Some(failure) = crate::parallel::independent::validated_failure(py, error) {
            let super::state::Disposition::Return(status) = failure.disposition else {
                return Err(PyRuntimeError::new_err(
                    "invalid validated worker disposition",
                ));
            };
            self.session.bind(py).setattr("exitstatus", status)?;
            if matches!(
                failure.origin,
                super::state::FailureOrigin::Internal | super::state::FailureOrigin::FixtureCleanup
            ) {
                py.import("sys")?
                    .getattr("stderr")?
                    .call_method1("write", (format!("{}\n", error.value(py).str()?),))?;
            }
            self.invocation.reported(
                py,
                error,
                failure.stage,
                failure.disposition,
                failure.origin,
            );
            return Ok(());
        }
        if let Some(control) = controls::kind(py, error)? {
            let explicit = matches!(control, controls::ControlKind::Exit);
            if explicit
                && matches!(
                    self.stage.get(),
                    LifecycleStage::Configuring | LifecycleStage::SessionStarting
                )
            {
                write_exit_message(py, error)?;
            }
            let code = if explicit {
                Some(error.value(py).getattr("returncode")?)
            } else {
                None
            };
            // A peer's interruption during this notification only repeats the stop in progress.
            self.invocation.begin_worker_shutdown(py)?;
            controls::notify(
                py,
                self.registry.bind(py),
                error,
                controls::Notification::Interrupt,
            )?;
            let code = match code {
                Some(code) if !code.is_none() => code,
                _ => py
                    .import("vfy.independent.facade")?
                    .getattr("ExitCode")?
                    .call1((2,))?,
            };
            self.session.bind(py).setattr("exitstatus", &code)?;
            self.invocation.reported(
                py,
                error,
                self.stage.get(),
                super::state::Disposition::Return(code.extract()?),
                super::state::FailureOrigin::Control(control),
            );
            return Ok(());
        }
        self.report_internal_error(py, error)
    }

    fn report_internal_error(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        let status = py
            .import("vfy.independent.facade")?
            .getattr("ExitCode")?
            .call1((3,))?;
        self.session.bind(py).setattr("exitstatus", status)?;
        let lines = py
            .import("traceback")?
            .call_method1("format_exception", (error.value(py),))?
            .extract::<Vec<String>>()?;
        let stderr = py.import("sys")?.getattr("stderr")?;
        if error.is_instance_of::<pyo3::exceptions::PySystemExit>(py) {
            stderr.call_method1("write", ("mainloop: caught unexpected SystemExit!\n",))?;
        }
        let representation = lines.join("").into_pyobject(py)?.into_any();
        let handled = self.notify_internal_error(py, error, &representation, &stderr)?;
        if !handled {
            // No hook handled it, so write pytest's lines to stderr as its `main` does.
            for line in lines.join("").split_inclusive('\n') {
                stderr.call_method1("write", (format!("INTERNALERROR> {line}"),))?;
            }
        }
        self.invocation.reported(
            py,
            error,
            self.stage.get(),
            super::state::Disposition::Return(
                self.session.bind(py).getattr("exitstatus")?.extract()?,
            ),
            super::state::FailureOrigin::Internal,
        );
        Ok(())
    }

    fn notify_internal_error(
        &self,
        py: Python<'_>,
        error: &PyErr,
        representation: &Bound<'_, PyAny>,
        stderr: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let notified = controls::notify(
            py,
            self.registry.bind(py),
            error,
            controls::Notification::Internal(representation),
        );
        match notified {
            Ok(responses) => {
                let mut handled = false;
                for response in responses.bind(py).try_iter()? {
                    if response?.is_truthy()? {
                        handled = true;
                        break;
                    }
                }
                Ok(handled)
            }
            Err(secondary)
                if matches!(
                    controls::kind(py, &secondary)?,
                    Some(controls::ControlKind::Exit)
                ) =>
            {
                let code = secondary.value(py).getattr("returncode")?;
                if !code.is_none() {
                    self.session.bind(py).setattr("exitstatus", code)?;
                }
                stderr.call_method1(
                    "write",
                    (format!(
                        "{}: {}\n",
                        secondary.get_type(py).name()?,
                        secondary.value(py).str()?
                    ),),
                )?;
                Ok(true)
            }
            Err(secondary) => Err(secondary),
        }
    }
}
