//! The builtin logging wrappers enter native guards at their hook priority.

use super::{
    LogCaptureSettings, LogPhase, LoopCapture, NativeLiveStream, NativeLogState, TestCapture,
    phase_index,
};
use crate::module_cache::Module;
use pyo3::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use vfy_core::test_execution::ExecutionPhase;

#[derive(Clone, Copy, Eq, PartialEq)]
enum LoopState {
    Idle,
    Starting,
    Active,
}

#[pyclass(frozen, unsendable)]
pub(in crate::independent) struct NativeLoggingHooks {
    current: RefCell<Option<Rc<TestCapture>>>,
    completed: RefCell<[String; 3]>,
    loop_state: Cell<LoopState>,
    live: RefCell<Option<Py<NativeLiveStream>>>,
    handlers: RefCell<Option<(Py<PyAny>, Py<PyAny>)>>,
    level: Cell<Option<i32>>,
}

impl NativeLoggingHooks {
    pub(in crate::independent) fn configure(
        &self,
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let stream = Module::Sys.get(py)?.getattr("stdout")?;
        let settings = LogCaptureSettings::from_config(py, config, &stream)?;
        let capture = TestCapture::new(py, &settings)?;
        self.level.set(capture.level);
        let previous = self
            .handlers
            .replace(Some((capture.handler, capture.report_handler)));
        drop(previous);
        Ok(())
    }

    pub(in crate::independent) fn new_capture(&self, py: Python<'_>) -> PyResult<Rc<TestCapture>> {
        // Release the RefCell borrow before Python setters can invoke user code.
        let (handler, report_handler) = self
            .handlers
            .borrow()
            .as_ref()
            .map(|(handler, report)| (handler.clone_ref(py), report.clone_ref(py)))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("logging is not configured")
            })?;
        let live = match self.loop_capture(py)? {
            LoopCapture::Outside => None,
            LoopCapture::Active(live) => live,
        };
        Ok(Rc::new(TestCapture::reuse(
            py,
            handler,
            report_handler,
            self.level.get(),
            live,
        )?))
    }

    pub(in crate::independent) fn set_capture(&self, capture: Option<Rc<TestCapture>>) {
        let previous = self.current.replace(capture);
        if let Some(previous) = &previous {
            previous
                .caplog
                .get()
                .active
                .store(false, std::sync::atomic::Ordering::Relaxed);
        }
        for log in &mut *self.completed.borrow_mut() {
            log.clear();
        }
        drop(previous);
    }

    pub(in crate::independent) fn take_log(&self, phase: ExecutionPhase) -> String {
        std::mem::take(&mut self.completed.borrow_mut()[phase_index(phase)])
    }

    pub(super) fn loop_capture(&self, py: Python<'_>) -> PyResult<LoopCapture> {
        match self.loop_state.get() {
            LoopState::Idle => Ok(LoopCapture::Outside),
            LoopState::Starting => Err(pyo3::exceptions::PyRuntimeError::new_err(
                "logging loop is starting",
            )),
            LoopState::Active => Ok(LoopCapture::Active(
                self.live.borrow().as_ref().map(|live| live.clone_ref(py)),
            )),
        }
    }

    pub(in crate::independent) fn subtest_settings(
        &self,
        py: Python<'_>,
    ) -> PyResult<Option<(Py<PyAny>, Option<i32>)>> {
        let current = self.current.borrow().clone();
        current
            .map(|capture| {
                Ok((
                    capture
                        .report_handler
                        .bind(py)
                        .getattr("formatter")?
                        .unbind(),
                    capture.level,
                ))
            })
            .transpose()
    }

    fn take_loop(&self) -> Option<Py<NativeLiveStream>> {
        self.loop_state.set(LoopState::Idle);
        self.live.borrow_mut().take()
    }
}

#[pymethods]
impl NativeLoggingHooks {
    #[new]
    pub(in crate::independent) fn new() -> Self {
        Self {
            current: RefCell::new(None),
            completed: RefCell::new(std::array::from_fn(|_| String::new())),
            loop_state: Cell::new(LoopState::Idle),
            live: RefCell::new(None),
            handlers: RefCell::new(None),
            level: Cell::new(None),
        }
    }

    fn phase(slf: PyRef<'_, Self>, py: Python<'_>, when: &str) -> PyResult<NativeLoggingPhase> {
        let phase = match when {
            "setup" => ExecutionPhase::Setup,
            "call" => ExecutionPhase::Call,
            "teardown" => ExecutionPhase::Teardown,
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "unknown logging phase",
                ));
            }
        };
        let capture = slf.current.borrow().clone();
        let records = if phase == ExecutionPhase::Teardown {
            capture.as_ref().map(|capture| capture.caplog.clone_ref(py))
        } else {
            None
        };
        let owner: Py<NativeLoggingHooks> = slf.into();
        let scope = capture
            .map(|capture| capture.begin_phase(py, phase))
            .transpose()?;
        Ok(NativeLoggingPhase {
            scope: RefCell::new(scope),
            owner,
            phase: Some(phase),
            records,
        })
    }

    fn protocol(
        slf: PyRef<'_, Self>,
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<NativeLoggingPhase> {
        if slf.loop_state.get() != LoopState::Idle {
            return Err(pyo3::exceptions::PyRuntimeError::new_err(
                "logging loop is already active",
            ));
        }
        slf.loop_state.set(LoopState::Starting);
        let owner: Py<Self> = slf.into();
        let result = (|| -> PyResult<_> {
            let stream = Module::Sys.get(py)?.getattr("stdout")?;
            let settings = LogCaptureSettings::for_loop(py, config, &stream)?;
            let live = settings
                .live
                .as_ref()
                .map(|live| NativeLiveStream::create(py, live, settings.auto_indent))
                .transpose()?;
            let scope = LogPhase::protocol(py, live.as_ref())?;
            Ok((live, scope))
        })();
        let (live, scope) = match result {
            Ok(value) => value,
            Err(error) => {
                let live = owner.borrow(py).take_loop();
                drop(live);
                return Err(error);
            }
        };
        let previous = {
            let hooks = owner.borrow(py);
            let previous = hooks.live.replace(live);
            hooks.loop_state.set(LoopState::Active);
            previous
        };
        drop(previous);
        Ok(NativeLoggingPhase {
            scope: RefCell::new(Some(scope)),
            owner,
            phase: None,
            records: None,
        })
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(current) = self.current.try_borrow()
            && let Some(capture) = &*current
        {
            visit.call(&capture.caplog)?;
            visit.call(&capture.report)?;
            visit.call(&capture.handler)?;
            visit.call(&capture.report_handler)?;
            visit.call(&capture.live)?;
        }
        if let Ok(handlers) = self.handlers.try_borrow()
            && let Some((handler, report)) = &*handlers
        {
            visit.call(handler)?;
            visit.call(report)?;
        }
        if let Ok(live) = self.live.try_borrow() {
            visit.call(&*live)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let current = self.current.borrow_mut().take();
        drop(current);
        let live = self.take_loop();
        drop(live);
        let handlers = self.handlers.borrow_mut().take();
        drop(handlers);
    }
}

#[pyclass(frozen, unsendable)]
struct NativeLoggingPhase {
    scope: RefCell<Option<LogPhase>>,
    owner: Py<NativeLoggingHooks>,
    phase: Option<ExecutionPhase>,
    records: Option<Py<NativeLogState>>,
}

impl NativeLoggingPhase {
    fn close_test_records(&self) {
        if let Some(records) = &self.records {
            records
                .get()
                .active
                .store(false, std::sync::atomic::Ordering::Relaxed);
        }
    }
}

#[pymethods]
impl NativeLoggingPhase {
    #[expect(
        clippy::unused_self,
        reason = "CPython's context manager protocol requires a bound __enter__ method"
    )]
    fn __enter__(&self) {}

    fn __exit__(
        &self,
        py: Python<'_>,
        _exception_type: &Bound<'_, PyAny>,
        _exception: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let scope = self.scope.borrow_mut().take();
        let loop_closed = self.phase.is_none() && scope.is_some();
        let result = if let Some(scope) = scope {
            let log = scope.finish(py);
            if let Some(phase) = self.phase
                && let Ok(log) = &log
            {
                self.owner.borrow(py).completed.borrow_mut()[phase_index(phase)].clone_from(log);
            }
            log.map(|_| ())
        } else {
            Ok(())
        };
        if loop_closed {
            let live = self.owner.borrow(py).take_loop();
            drop(live);
        }
        self.close_test_records();
        result?;
        Ok(false)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.owner)?;
        visit.call(&self.records)?;
        if let Ok(scope) = self.scope.try_borrow()
            && let Some(scope) = &*scope
        {
            visit.call(&scope.scope)?;
            visit.call(&scope.handler)?;
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        let scope = self.scope.borrow_mut().take();
        let loop_closed = self.phase.is_none() && scope.is_some();
        drop(scope);
        self.close_test_records();
        if loop_closed {
            let live = self.owner.borrow(py).take_loop();
            drop(live);
        }
    }
}

impl Drop for NativeLoggingPhase {
    fn drop(&mut self) {
        let scope = self.scope.get_mut().take();
        let loop_closed = self.phase.is_none() && scope.is_some();
        drop(scope);
        self.close_test_records();
        if loop_closed {
            Python::attach(|py| {
                let live = self.owner.borrow(py).take_loop();
                drop(live);
            });
        }
    }
}
