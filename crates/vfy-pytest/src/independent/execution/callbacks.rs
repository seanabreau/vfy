//! Python-callable fixture finishers and phase callbacks.

use super::executor::Executor;
use super::request::{NativeIndependentRequest, RequestContext};
use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyRuntimeError, PyStopIteration, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyWeakrefMethods, PyWeakrefReference};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;
use std::rc::Rc;
use vfy_core::FixtureCacheKey;
use vfy_core::test_execution::ExecutionPhase;

#[pyclass(frozen, unsendable)]
pub(super) struct FinishFixture {
    pub(super) owner: Rc<Executor>,
    pub(super) key: FixtureCacheKey,
    pub(super) request: RefCell<Option<Py<NativeIndependentRequest>>>,
}

#[derive(Clone, Copy)]
pub(super) enum PhaseOperation {
    Setup,
    Call,
    Teardown { next: Option<usize> },
}

#[derive(Clone, Copy)]
pub(super) enum FixtureCaptureAction {
    Start,
    Close,
}

impl PhaseOperation {
    pub(super) fn hook_name(self) -> &'static str {
        match self {
            Self::Setup => "pytest_runtest_setup",
            Self::Call => "pytest_runtest_call",
            Self::Teardown { .. } => "pytest_runtest_teardown",
        }
    }

    pub(super) fn phase(self) -> ExecutionPhase {
        match self {
            Self::Setup => ExecutionPhase::Setup,
            Self::Call => ExecutionPhase::Call,
            Self::Teardown { .. } => ExecutionPhase::Teardown,
        }
    }
}

pub(super) struct PhaseInvocation {
    pub(super) owner: Rc<Executor>,
    pub(super) context: RequestContext,
    pub(super) operation: PhaseOperation,
}

// The Rust dispatch scope is the only persistent strong owner. The callback's
// exact no-callback weakref cannot retain this thread-affine state through cycles.
#[pyclass(frozen, unsendable, weakref)]
pub(super) struct PhaseCallbackOwner {
    pub(super) pending: RefCell<Option<PhaseInvocation>>,
}

#[pyclass(frozen)]
pub(super) struct PhaseCallback {
    pub(super) owner: Py<PyWeakrefReference>,
    pub(super) thread: std::thread::ThreadId,
}

#[pymethods]
impl PhaseCallback {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        if self.thread != std::thread::current().id() {
            return Err(PyRuntimeError::new_err(
                "test phase callback belongs to another thread",
            ));
        }
        let owner = self.owner.bind(py).upgrade().ok_or_else(|| {
            PyAssertionError::new_err("test phase callback is no longer available")
        })?;
        let owner = owner.cast_into::<PhaseCallbackOwner>()?;
        let pending = owner.borrow().pending.borrow_mut().take().ok_or_else(|| {
            PyAssertionError::new_err("test phase callback is no longer available")
        })?;
        pending
            .owner
            .perform_phase(py, &pending.context, pending.operation)
    }
}

#[pymethods]
impl FinishFixture {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.owner.finalize_fixture(py, &self.key)
    }

    pub(super) fn _register(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<()> {
        let request = {
            let this = slf.borrow();
            let request = this.request.borrow();
            request.as_ref().map(|request| request.clone_ref(py))
        }
        .ok_or_else(|| PyAssertionError::new_err("fixture finalizer has been cleared"))?;
        request
            .bind(py)
            .getattr(pyo3::intern!(py, "node"))?
            .call_method1(pyo3::intern!(py, "addfinalizer"), (slf,))?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(request) = self.request.try_borrow() {
            visit.call(&*request)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let request = self.request.borrow_mut().take();
        drop(request);
    }
}

#[pyclass(frozen, unsendable)]
pub(super) struct FinishGenerator {
    pub(super) generator: RefCell<Option<Py<PyAny>>>,
}

#[pymethods]
impl FinishGenerator {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let generator = self.generator.borrow_mut().take();
        let Some(generator) = generator else {
            return Ok(());
        };
        match Module::Builtins
            .get(py)?
            .call_method1("next", (generator.bind(py),))
        {
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(()),
            Err(error) => Err(error),
            Ok(_) => {
                generator.bind(py).call_method0("close")?;
                Err(PyValueError::new_err("fixture yielded more than once"))
            }
        }
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(generator) = self.generator.try_borrow() {
            visit.call(&*generator)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let generator = self.generator.borrow_mut().take();
        drop(generator);
    }
}
