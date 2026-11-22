//! A phase retains the original exception until the report hook consumes it.

use crate::module_cache::Module;
use pyo3::exceptions::{PyAttributeError, PyBaseException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyString, PyType};
use pyo3::{PyTraverseError, PyVisit};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use vfy_core::test_execution::{ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult};

/// Collection is a call context, but never an executable test phase.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(in crate::independent) enum CallStage {
    Execution(ExecutionPhase),
    Collect,
}

impl CallStage {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "setup" => Ok(Self::Execution(ExecutionPhase::Setup)),
            "call" => Ok(Self::Execution(ExecutionPhase::Call)),
            "teardown" => Ok(Self::Execution(ExecutionPhase::Teardown)),
            "collect" => Ok(Self::Collect),
            _ => Err(PyValueError::new_err(
                "call context must be collect, setup, call, or teardown",
            )),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Execution(phase) => phase.as_str(),
            Self::Collect => "collect",
        }
    }

    fn execution(self) -> PyResult<ExecutionPhase> {
        match self {
            Self::Execution(phase) => Ok(phase),
            Self::Collect => Err(PyValueError::new_err(
                "a collection call has no execution phase",
            )),
        }
    }
}

fn exception_info(py: Python<'_>, error: PyErr) -> PyResult<Py<PyAny>> {
    let info = Module::Facade.get(py)?.getattr("ExceptionInfo")?.call0()?;
    let traceback = error.traceback(py);
    info.call_method1("_fill", (error.into_value(py), traceback))?;
    super::diagnostics::capture_exception_name(py, &info)?;
    Ok(info.unbind())
}

fn system_seconds() -> f64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => duration.as_secs_f64(),
        Err(error) => -error.duration().as_secs_f64(),
    }
}

pub(in crate::independent) struct PhaseClock {
    started: Instant,
    wall: f64,
}

/// The same completed clock values back both `CallInfo` and its default report.
#[derive(Clone, Copy)]
pub(in crate::independent) struct PhaseTiming {
    pub(in crate::independent) start: f64,
    pub(in crate::independent) stop: f64,
    pub(in crate::independent) duration: f64,
}

impl PhaseClock {
    pub(in crate::independent) fn start() -> Self {
        Self {
            started: Instant::now(),
            wall: system_seconds(),
        }
    }

    pub(in crate::independent) fn complete_collection(
        self,
        py: Python<'_>,
        result: PyResult<Py<PyAny>>,
    ) -> PyResult<Py<NativeIndependentCallInfo>> {
        let (result, excinfo) = match result {
            Ok(result) => (result, None),
            Err(error) => {
                if crate::independent::controls::is_control_error(py, &error)? {
                    return Err(error);
                }
                (py.None(), Some(exception_info(py, error)?))
            }
        };
        Py::new(
            py,
            NativeIndependentCallInfo {
                when: CallStage::Collect,
                timing: PhaseTiming {
                    start: self.wall,
                    stop: system_seconds(),
                    duration: self.started.elapsed().as_secs_f64(),
                },
                excinfo,
                result: Some(result),
                pending_report: None,
            },
        )
    }

    pub(in crate::independent) fn complete(
        self,
        when: ExecutionPhase,
        result: PyResult<()>,
    ) -> PhaseCompletion {
        PhaseCompletion {
            when,
            timing: PhaseTiming {
                start: self.wall,
                stop: system_seconds(),
                duration: self.started.elapsed().as_secs_f64(),
            },
            result,
            capture: PhasePayload::default(),
        }
    }
}

/// This transient state contains the original `PyErr`, rather than formatted text.
pub(in crate::independent) struct PhaseCompletion {
    when: ExecutionPhase,
    timing: PhaseTiming,
    result: PyResult<()>,
    pub(in crate::independent) capture: PhasePayload,
}

#[derive(Clone, Copy)]
enum ControlReportPolicy {
    Propagate,
    Report,
}

impl PhaseCompletion {
    pub(in crate::independent) fn unittest_callback(result: PyResult<()>) -> Self {
        Self {
            when: ExecutionPhase::Call,
            timing: PhaseTiming {
                start: 0.0,
                stop: 0.0,
                duration: 0.0,
            },
            result,
            capture: PhasePayload::default(),
        }
    }

    pub(in crate::independent) fn replace_exception(&mut self, error: PyErr) {
        self.result = Err(error);
    }

    pub(in crate::independent) fn map_error(
        mut self,
        normalize: impl FnOnce(PyErr) -> PyResult<PyErr>,
    ) -> PyResult<Self> {
        self.result = match self.result {
            Ok(()) => Ok(()),
            Err(error) => Err(normalize(error)?),
        };
        Ok(self)
    }

    pub(in crate::independent) fn phase(&self) -> ExecutionPhase {
        self.when
    }

    #[cfg(test)]
    fn into_call(self, py: Python<'_>) -> PyResult<(Py<NativeIndependentCallInfo>, PhasePayload)> {
        self.into_call_policy(py, ControlReportPolicy::Propagate)
    }

    /// Keep the call as a Rust value until a hook needs the Python object.
    pub(in crate::independent) fn into_call_state(
        self,
        py: Python<'_>,
    ) -> PyResult<(NativeIndependentCallInfo, PhasePayload)> {
        self.into_call_state_policy(py, ControlReportPolicy::Propagate)
    }

    pub(in crate::independent) fn into_subtest_call(
        self,
        py: Python<'_>,
    ) -> PyResult<(Py<NativeIndependentCallInfo>, PhasePayload)> {
        self.into_call_policy(py, ControlReportPolicy::Report)
    }

    fn into_call_policy(
        self,
        py: Python<'_>,
        policy: ControlReportPolicy,
    ) -> PyResult<(Py<NativeIndependentCallInfo>, PhasePayload)> {
        let (call, capture) = self.into_call_state_policy(py, policy)?;
        Ok((Py::new(py, call)?, capture))
    }

    fn into_call_state_policy(
        self,
        py: Python<'_>,
        policy: ControlReportPolicy,
    ) -> PyResult<(NativeIndependentCallInfo, PhasePayload)> {
        let excinfo = match self.result {
            Ok(()) => None,
            Err(error) => {
                if matches!(policy, ControlReportPolicy::Propagate)
                    && crate::independent::controls::is_control_error(py, &error)?
                {
                    return Err(error);
                }
                Some(exception_info(py, error)?)
            }
        };
        Ok((
            NativeIndependentCallInfo {
                when: CallStage::Execution(self.when),
                timing: self.timing,
                excinfo,
                result: Some(py.None()),
                pending_report: None,
            },
            self.capture,
        ))
    }
}

/// The report-hook boundary owns exception information and phase timing.
#[pyclass(module = "vfy._native._pytest", dict, weakref, generic)]
pub(crate) struct NativeIndependentCallInfo {
    when: CallStage,
    timing: PhaseTiming,
    excinfo: Option<Py<PyAny>>,
    // A successful collection call and its report share the actual child list.
    result: Option<Py<PyAny>>,
    pub(super) pending_report: Option<super::make::PendingReport>,
}

impl NativeIndependentCallInfo {
    pub(crate) fn take_pending_report(&mut self) -> Option<super::make::PendingReport> {
        self.pending_report.take()
    }

    pub(crate) fn has_exception(&self) -> bool {
        self.excinfo.is_some()
    }

    pub(crate) fn phase(&self) -> PyResult<ExecutionPhase> {
        self.when.execution()
    }

    pub(super) fn timing(&self) -> PhaseTiming {
        self.timing
    }

    pub(super) fn projection(
        &self,
        py: Python<'_>,
    ) -> PyResult<(ExecutionPhase, PhaseTiming, Option<Py<PyAny>>)> {
        Ok((self.phase()?, self.timing, self.excinfo(py)))
    }

    pub(in crate::independent) fn error(slf: &Bound<'_, Self>) -> PyResult<Option<PyErr>> {
        let info = slf.borrow().excinfo(slf.py());
        Self::error_from_optional_info(slf.py(), info)
    }

    pub(crate) fn error_state(&self, py: Python<'_>) -> PyResult<Option<PyErr>> {
        Self::error_from_optional_info(py, self.excinfo(py))
    }

    fn error_from_optional_info(
        py: Python<'_>,
        info: Option<Py<PyAny>>,
    ) -> PyResult<Option<PyErr>> {
        let Some(info) = info else {
            return Ok(None);
        };
        let info = info.bind(py);
        if !info.is_truthy()? {
            return Ok(None);
        }
        Self::error_from_info(info).map(Some)
    }

    fn error_from_info(info: &Bound<'_, PyAny>) -> PyResult<PyErr> {
        let value = info.getattr("value")?.cast_into::<PyBaseException>()?;
        Ok(PyErr::from_value(value.into_any()))
    }

    #[cfg(test)]
    fn phase_result<'py>(
        slf: &Bound<'py, Self>,
        payload: PhasePayload,
        format: impl FnOnce(&PyErr, &Bound<'py, PyAny>, &mut PhasePayload) -> PyResult<()>,
    ) -> PyResult<PhaseResult> {
        let py = slf.py();
        let (when, timing, info) = {
            let call = slf.borrow();
            call.projection(py)?
        };
        Self::phase_result_parts(py, when, timing, info, payload, format)
    }

    #[cfg(test)]
    fn phase_result_state<'py>(
        &self,
        py: Python<'py>,
        payload: PhasePayload,
        format: impl FnOnce(&PyErr, &Bound<'py, PyAny>, &mut PhasePayload) -> PyResult<()>,
    ) -> PyResult<PhaseResult> {
        let (when, timing, info) = self.projection(py)?;
        Self::phase_result_parts(py, when, timing, info, payload, format)
    }

    pub(super) fn phase_result_parts<'py>(
        py: Python<'py>,
        when: ExecutionPhase,
        timing: PhaseTiming,
        info: Option<Py<PyAny>>,
        mut payload: PhasePayload,
        format: impl FnOnce(&PyErr, &Bound<'py, PyAny>, &mut PhasePayload) -> PyResult<()>,
    ) -> PyResult<PhaseResult> {
        let outcome = if let Some(info) = info
            && info.bind(py).is_truthy()?
        {
            let info = info.bind(py);
            let error = Self::error_from_info(info)?;
            let skipped = error.is_instance(py, &Module::Facade.get(py)?.getattr("Skipped")?)
                || error.is_instance(py, &py.import("unittest")?.getattr("SkipTest")?);
            let outcome = if skipped {
                ExecutionOutcome::Skipped
            } else if when == ExecutionPhase::Call {
                ExecutionOutcome::Failed
            } else {
                ExecutionOutcome::Error
            };
            format(&error, info, &mut payload)?;
            outcome
        } else {
            ExecutionOutcome::Passed
        };
        Ok(PhaseResult::new(
            outcome,
            when,
            timing.duration * 1000.0,
            payload,
        ))
    }
}

#[cfg(test)]
mod tests;

#[pymethods]
impl NativeIndependentCallInfo {
    #[new]
    #[pyo3(signature = (result, excinfo, start, stop, duration, when, *, _ispytest=false))]
    #[expect(
        clippy::too_many_arguments,
        reason = "CallInfo preserves pytest's public constructor signature"
    )]
    #[expect(
        clippy::used_underscore_binding,
        reason = "pytest names the constructor's private-API guard _ispytest"
    )]
    fn new(
        py: Python<'_>,
        result: Py<PyAny>,
        excinfo: Py<PyAny>,
        start: f64,
        stop: f64,
        duration: f64,
        when: &str,
        _ispytest: bool,
    ) -> PyResult<Self> {
        py.import("vfy.independent.fixtures")?
            .getattr("_private_api")?
            .call1((_ispytest,))?;
        Ok(Self {
            when: CallStage::parse(when)?,
            timing: PhaseTiming {
                start,
                stop,
                duration,
            },
            excinfo: (!excinfo.bind(py).is_none()).then_some(excinfo),
            result: Some(result),
            pending_report: None,
        })
    }

    #[classmethod]
    #[pyo3(signature = (func, when, reraise=None))]
    fn from_call(
        cls: &Bound<'_, PyType>,
        func: &Bound<'_, PyAny>,
        when: &str,
        reraise: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let py = cls.py();
        let clock = PhaseClock::start();
        let (result, excinfo) = match func.call0() {
            Ok(result) => (result.unbind(), py.None()),
            Err(error) => {
                let info = exception_info(py, error)?;
                if let Some(reraise) = reraise
                    && info.bind(py).getattr("value")?.is_instance(reraise)?
                {
                    return Err(Self::error_from_info(info.bind(py))?);
                }
                (py.None(), info)
            }
        };
        let stop = system_seconds();
        let duration = clock.started.elapsed().as_secs_f64();
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("_ispytest", true)?;
        Ok(cls
            .call(
                (result, excinfo, clock.wall, stop, duration, when),
                Some(&kwargs),
            )?
            .unbind())
    }

    #[getter]
    fn when<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        match self.when {
            CallStage::Execution(phase) => super::super::phase_text(py, phase),
            CallStage::Collect => super::super::collect_text(py),
        }
    }

    #[setter]
    fn set_when(&mut self, value: &str) -> PyResult<()> {
        self.when = CallStage::parse(value)?;
        Ok(())
    }

    #[getter]
    pub(super) fn start(&self) -> f64 {
        self.timing.start
    }

    #[setter]
    fn set_start(&mut self, value: f64) {
        self.timing.start = value;
    }

    #[getter]
    pub(super) fn stop(&self) -> f64 {
        self.timing.stop
    }

    #[setter]
    fn set_stop(&mut self, value: f64) {
        self.timing.stop = value;
    }

    #[getter]
    pub(super) fn duration(&self) -> f64 {
        self.timing.duration
    }

    #[setter]
    fn set_duration(&mut self, value: f64) {
        self.timing.duration = value;
    }

    #[getter(_result)]
    fn retained_result(&self, py: Python<'_>) -> Py<PyAny> {
        self.result
            .as_ref()
            .map_or_else(|| py.None(), |value| value.clone_ref(py))
    }

    #[setter(_result)]
    fn set_retained_result(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = slf.borrow_mut().result.replace(value);
        drop(previous);
    }

    #[getter]
    fn excinfo(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.excinfo.as_ref().map(|info| info.clone_ref(py))
    }

    #[setter]
    fn set_excinfo(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let value = (!value.bind(slf.py()).is_none()).then_some(value);
        let previous = std::mem::replace(&mut slf.borrow_mut().excinfo, value);
        drop(previous);
    }

    #[getter]
    fn result(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let failed = slf.borrow().excinfo.is_some();
        if failed {
            return Err(PyAttributeError::new_err(format!(
                "{} has no valid result",
                Self::__repr__(slf)?
            )));
        }
        Ok(slf.borrow().retained_result(slf.py()))
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        let (when, info, result) = {
            let call = slf.borrow();
            (
                call.when,
                call.excinfo(slf.py()),
                call.retained_result(slf.py()),
            )
        };
        match info {
            Some(info) => Ok(format!(
                "<CallInfo when='{}' excinfo={}>",
                when.as_str(),
                info.bind(slf.py()).repr()?.to_str()?
            )),
            None => Ok(format!(
                "<CallInfo when='{}' result: {}>",
                when.as_str(),
                result.bind(slf.py()).repr()?.to_str()?
            )),
        }
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.excinfo)?;
        visit.call(&self.result)
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut call = slf.borrow_mut();
            (
                call.excinfo.take(),
                call.result.take(),
                call.pending_report.take(),
            )
        };
        drop(removed);
    }
}
