//! Defer worker SIGINT callbacks until shutdown has published its owned outcome.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyModule;
use std::sync::Mutex;

#[pyclass(frozen)]
struct DeferredInterrupt {
    pending: Mutex<Vec<(i32, Py<PyAny>)>>,
}

#[pymethods]
impl DeferredInterrupt {
    fn __call__(&self, signum: i32, frame: Py<PyAny>) -> PyResult<()> {
        self.pending
            .lock()
            .map_err(|_| PyRuntimeError::new_err("worker interrupt lock is poisoned"))?
            .push((signum, frame));
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(pending) = self.pending.try_lock() {
            for (_, frame) in pending.iter() {
                visit.call(frame)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .pending
            .lock()
            .ok()
            .map(|mut pending| std::mem::take(&mut *pending));
        drop(removed);
    }
}

pub(in crate::independent) struct WorkerShutdown {
    signal: Py<PyModule>,
    signum: Py<PyAny>,
    previous: Py<PyAny>,
    callback: Py<DeferredInterrupt>,
}

/// Whether the shutdown already carries a failure that outranks deferred cancellation.
#[derive(Clone, Copy, PartialEq, Eq)]
enum ShutdownFailure {
    None,
    Recorded,
}

pub(super) enum ShutdownOutcome {
    Completed,
    Failed(Option<Py<PyAny>>),
}

impl WorkerShutdown {
    pub(in crate::independent) fn begin(py: Python<'_>) -> PyResult<Option<Self>> {
        // The C module keeps the swap free of Python bytecode, where a pending SIGINT would
        // run the previous handler between reading and replacing it.
        let signal = py.import("_signal")?;
        let signum = signal.getattr("SIGINT")?;
        let previous = signal.call_method1("getsignal", (&signum,))?;
        if previous.is_none() || previous.eq(&signal.getattr("SIG_IGN")?)? {
            return Ok(None);
        }
        let callback = Py::new(
            py,
            DeferredInterrupt {
                pending: Mutex::default(),
            },
        )?;
        signal.call_method1("signal", (&signum, callback.bind(py)))?;
        Ok(Some(Self {
            signal: signal.unbind(),
            signum: signum.unbind(),
            previous: previous.unbind(),
            callback,
        }))
    }

    pub(in crate::independent) fn traverse(
        &self,
        visit: &pyo3::PyVisit<'_>,
    ) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.signal)?;
        visit.call(&self.signum)?;
        visit.call(&self.previous)?;
        visit.call(&self.callback)
    }

    pub(super) fn restore(self, py: Python<'_>, outcome: ShutdownOutcome) -> PyResult<()> {
        let active = py.import("sys")?.call_method0("exception")?;
        let failure = if !active.is_none() || matches!(&outcome, ShutdownOutcome::Failed(_)) {
            ShutdownFailure::Recorded
        } else {
            ShutdownFailure::None
        };
        let primary = if active.is_none() {
            match outcome {
                ShutdownOutcome::Failed(error) => error,
                ShutdownOutcome::Completed => None,
            }
        } else {
            Some(active.unbind())
        };
        let restored = self.restore_and_replay(py, failure);
        if let Err(error) = restored {
            if let Some(primary) = primary {
                crate::parallel::retain_parent_context(
                    py,
                    &PyErr::from_value(primary.into_bound(py)),
                    error,
                );
            } else {
                return Err(error);
            }
        }
        Ok(())
    }

    fn restore_and_replay(self, py: Python<'_>, failure: ShutdownFailure) -> PyResult<()> {
        let signal = self.signal.bind(py);
        // A cleanup callback can install a different handler and keeps that ownership.
        if !signal
            .call_method1("getsignal", (self.signum.bind(py),))?
            .is(self.callback.bind(py))
        {
            return Ok(());
        }
        signal.call_method1("signal", (self.signum.bind(py), self.previous.bind(py)))?;
        let pending = std::mem::take(
            &mut *self
                .callback
                .borrow(py)
                .pending
                .lock()
                .map_err(|_| PyRuntimeError::new_err("worker interrupt lock is poisoned"))?,
        );
        // A notified internal cleanup error may already have released its Python
        // exception. Its recorded failure still outranks deferred cancellation.
        if failure == ShutdownFailure::Recorded {
            return Ok(());
        }
        for (signum, frame) in pending {
            if self.previous.bind(py).is_callable() {
                self.previous.bind(py).call1((signum, frame))?;
            } else {
                // SIG_DFL is an integer disposition, not a Python callback.
                signal.call_method1("raise_signal", (signum,))?;
            }
        }
        Ok(())
    }
}
