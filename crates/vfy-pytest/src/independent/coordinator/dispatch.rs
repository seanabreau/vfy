//! Invoke the supervisor only through the native runtestloop default.
use super::super::lifecycle::Context;
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyAssertionError, PyBaseException, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::RefCell;

pub(super) struct Completed {
    pub(super) result: Py<NativeRunResult>,
    pub(super) interrupted: bool,
    // Only this callback's synthetic worker exception grants suppression authority.
    synthetic_interruption: Option<Py<PyBaseException>>,
}

enum State {
    Pending {
        callback: Py<PyAny>,
        session: Py<PyAny>,
        invocation: std::rc::Weak<super::super::lifecycle::state::InvocationState>,
    },
    Running,
    Complete(Completed),
    Failed,
    Closed,
}

#[pyclass(frozen, unsendable)]
struct SupervisorCallback {
    state: RefCell<State>,
}

impl SupervisorCallback {
    fn complete(&self, py: Python<'_>, completed: Completed) -> PyResult<bool> {
        let result = completed.result.clone_ref(py);
        let interrupted = completed.interrupted;
        self.state.replace(State::Complete(completed));
        let parent = result.borrow_mut(py).take_parent_interruption();
        if let Some(parent) = parent {
            return Err(parent.into_error(py));
        }
        if !interrupted {
            return Ok(true);
        }
        let exception = py
            .import("vfy.independent.facade")?
            .getattr("Interrupted")?
            .call1(("worker execution interrupted",))?
            .cast_into::<PyBaseException>()?
            .unbind();
        let error = PyErr::from_value(exception.bind(py).clone().into_any());
        let previous = {
            let mut state = self.state.borrow_mut();
            match &mut *state {
                State::Complete(completed) => completed.synthetic_interruption.replace(exception),
                State::Pending { .. } | State::Running | State::Failed | State::Closed => None,
            }
        };
        drop(previous);
        Err(error)
    }
}

#[pymethods]
impl SupervisorCallback {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "the native callable exposes its bound signature"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<bool> {
        let (invocation, session, callback) = match self.state.replace(State::Running) {
            State::Pending {
                callback,
                session,
                invocation,
            } => (invocation, session, callback),
            previous => {
                self.state.replace(previous);
                return Err(PyAssertionError::new_err(
                    "coordinator supervisor callback is consumed",
                ));
            }
        };
        let result = (|| {
            let result = callback
                .bind(py)
                .call0()?
                .extract::<Py<NativeRunResult>>()?;
            let (collected, failed, interrupted) = {
                let result = result.borrow(py);
                let record = result.record();
                if record.engine != "vfy-independent-v1"
                    || record.exitstatus.is_none()
                    || record.observations.is_none()
                {
                    return Err(PyValueError::new_err(
                        "coordinator requires a measured independent worker result",
                    ));
                }
                vfy_core::test_execution::validate_run_record(record)
                    .map_err(|error| PyValueError::new_err(error.to_string()))?;
                crate::results::validate_worker_observations(record.observations.as_ref())?;
                (
                    record.collected,
                    record.counts.failed + record.counts.errors,
                    record.exitstatus == Some(2),
                )
            };
            session.bind(py).setattr("testscollected", collected)?;
            session.bind(py).setattr("testsfailed", failed)?;
            let invocation = invocation.upgrade().ok_or_else(|| {
                PyAssertionError::new_err("terminal collection invocation is no longer available")
            })?;
            super::super::reporting::terminal::collection::result(
                &invocation,
                session.bind(py),
                result.bind(py),
            )?;
            Ok(Completed {
                result,
                interrupted,
                synthetic_interruption: None,
            })
        })();
        match result {
            Ok(completed) => self.complete(py, completed),
            Err(error) => {
                self.state.replace(State::Failed);
                Err(error)
            }
        }
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(state) = self.state.try_borrow() {
            match &*state {
                State::Pending {
                    callback, session, ..
                } => {
                    visit.call(callback)?;
                    visit.call(session)?;
                }
                State::Complete(completed) => {
                    visit.call(&completed.result)?;
                    visit.call(&completed.synthetic_interruption)?;
                }
                State::Running | State::Failed | State::Closed => (),
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let previous = self.state.replace(State::Closed);
        drop(previous);
    }
}

pub(super) struct Outcome {
    pub(super) completed: Completed,
    pub(super) error: Option<PyErr>,
    pub(super) interrupted: bool,
}

pub(super) fn run(py: Python<'_>, context: &Context, callback: Py<PyAny>) -> PyResult<Outcome> {
    context
        .stage
        .set(super::super::lifecycle::state::LifecycleStage::Running);
    let callback = Py::new(
        py,
        SupervisorCallback {
            state: RefCell::new(State::Pending {
                callback,
                session: context.session.clone_ref(py),
                invocation: std::rc::Rc::downgrade(&context.invocation),
            }),
        },
    )?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("session", &context.session)?;
    let dispatched = context.registry.bind(py).call_method1(
        "call_with_default",
        ("pytest_runtestloop", kwargs, &callback),
    );
    let state = callback.borrow(py).state.replace(State::Closed);
    let State::Complete(completed) = state else {
        drop(state);
        return match dispatched {
            Err(error) => Err(error),
            Ok(_) => Err(crate::UnsupportedFeatureError::new_err(
                "coordinator runtestloop must complete its supervisor callback",
            )),
        };
    };
    let interrupted = dispatched.as_ref().err().is_some_and(|error| {
        completed
            .synthetic_interruption
            .as_ref()
            .is_some_and(|synthetic| error.value(py).is(synthetic.bind(py)))
    });
    let error = if interrupted { None } else { dispatched.err() };
    Ok(Outcome {
        completed,
        error,
        interrupted,
    })
}
