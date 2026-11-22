//! Retained native collection for isolated execution providers.

use super::{CollectedSuite, assignment, execute_prepared, lifecycle, prepare_suite};
use crate::results::NativeRunResult;
use pyo3::exceptions::PyRuntimeError;
use pyo3::gc::{PyTraverseError, PyVisit};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::time::Instant;

mod code_observation;
mod mutation;
pub(crate) use mutation::{
    fresh_mutation_test_plans, has_collectable_mutations, install_mutation_code,
};

enum State {
    Pending(Box<CollectedSuite>),
    Running,
    Finished,
}

/// A collected suite is consumed once in each executing interpreter.
#[pyclass(module = "vfy._native._pytest", weakref, unsendable)]
pub(crate) struct NativeIndependentSession {
    state: State,
    root: String,
    options: Option<Py<PyDict>>,
    finalizer: Option<Py<PyAny>>,
    started: Instant,
    mutation_evidence: Option<crate::parallel::FreshEvidence>,
    mutation_inputs: Option<crate::parallel::mutation::cache::FreshInputs>,
    mutation_observed_code: Option<Vec<Py<PyAny>>>,
}

impl NativeIndependentSession {
    fn take_pending(&mut self) -> PyResult<CollectedSuite> {
        match self.state {
            State::Pending(_) => (),
            State::Running => {
                return Err(PyRuntimeError::new_err("independent session is running"));
            }
            State::Finished => {
                return Err(PyRuntimeError::new_err("independent session is finished"));
            }
        }
        let State::Pending(suite) = std::mem::replace(&mut self.state, State::Running) else {
            return Err(PyRuntimeError::new_err(
                "independent session has no pending suite",
            ));
        };
        Ok(*suite)
    }

    fn finish_pending(py: Python<'_>, suite: &CollectedSuite, exitstatus: i32) -> PyResult<()> {
        let context = &suite.context;
        let cleanup = lifecycle::cleanup::CleanupGuard::new(py, context);
        let result = Py::new(py, assignment::collection_result(py, suite)?)?;
        context.bind_junit_result(py, &result)?;
        cleanup.complete(context.finish(py, exitstatus))?;
        Ok(())
    }
}

#[pymethods]
impl NativeIndependentSession {
    #[new]
    #[pyo3(signature = (paths, root, keyword=None, basetemp=None, options=None, *, mutation_arguments=None, mutation_timeout=30.0, mutation_guard=false))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts Python sequences into an owned Vec at this boundary"
    )]
    #[expect(
        clippy::too_many_arguments,
        reason = "PyO3 preserves the session constructor and keyword-only mutation evidence inputs"
    )]
    fn new(
        py: Python<'_>,
        paths: Vec<String>,
        root: &str,
        keyword: Option<&str>,
        basetemp: Option<&str>,
        options: Option<&Bound<'_, PyDict>>,
        mutation_arguments: Option<Vec<String>>,
        mutation_timeout: f64,
        mutation_guard: bool,
    ) -> PyResult<Py<Self>> {
        let started = Instant::now();
        let mutation_inputs = mutation_arguments
            .map(|arguments| {
                let environment = py
                    .import("os")?
                    .getattr("environ")?
                    .call_method0("copy")?
                    .cast::<PyDict>()?
                    .extract()?;
                let executable = py
                    .import("sys")?
                    .getattr("executable")?
                    .extract::<String>()?;
                let timeout = std::time::Duration::try_from_secs_f64(mutation_timeout)
                    .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
                if timeout.is_zero() {
                    return Err(pyo3::exceptions::PyValueError::new_err(
                        "mutation timeout must be positive",
                    ));
                }
                crate::parallel::mutation::cache::FreshInputs::capture(
                    &executable,
                    std::path::Path::new(root),
                    &arguments,
                    &environment,
                    crate::parallel::independent::InvocationPurpose::FreshSerial,
                    timeout,
                    Some(vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                        py.version_info().major,
                        py.version_info().minor,
                        py.version_info().patch,
                    )),
                )
                .map(crate::parallel::mutation::cache::FreshInputs::retained)
            })
            .transpose()?;
        let guard = if mutation_guard {
            code_observation::CollectionGuard::begin(py)?
        } else {
            None
        };
        let suite = prepare_suite(py, &paths, root, keyword, basetemp, options)?;
        let mutation_observed_code = guard.map(code_observation::CollectionGuard::finish);
        let session = Py::new(
            py,
            Self {
                state: State::Pending(Box::new(suite)),
                root: root.to_owned(),
                options: options.map(|options| options.clone().unbind()),
                finalizer: None,
                started,
                mutation_evidence: None,
                mutation_inputs,
                mutation_observed_code,
            },
        )?;
        // Python finalizers run before cyclic GC clears callback globals. PyO3
        // Drop/tp_clear alone cannot safely call user cleanup in that phase.
        let finalizer = py
            .import("vfy.independent.session")?
            .getattr("_NativeSessionFinalizer")?
            .call1((&session,))?
            .unbind();
        session.borrow_mut(py).finalizer = Some(finalizer);
        Ok(session)
    }

    #[getter]
    fn state(&self) -> &'static str {
        match self.state {
            State::Pending(_) => "pending",
            State::Running => "running",
            State::Finished => "finished",
        }
    }

    #[getter]
    fn nodeids(&self) -> PyResult<Vec<String>> {
        match &self.state {
            State::Pending(suite) => Ok(suite
                .plan
                .tests()
                .iter()
                .map(|test| test.nodeid.clone())
                .collect()),
            State::Running => Err(PyRuntimeError::new_err("independent session is running")),
            State::Finished => Err(PyRuntimeError::new_err("independent session is finished")),
        }
    }

    #[getter]
    fn pytest_session(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match &self.state {
            State::Pending(suite) => Ok(suite.context.session.clone_ref(py)),
            State::Running => Err(PyRuntimeError::new_err("independent session is running")),
            State::Finished => Err(PyRuntimeError::new_err("independent session is finished")),
        }
    }

    fn collection(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<NativeRunResult> {
        let suite = slf.borrow_mut().take_pending()?;
        let result = assignment::collection_result(py, &suite);
        slf.borrow_mut().state = State::Pending(Box::new(suite));
        result
    }

    #[expect(
        clippy::too_many_arguments,
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts one owned mutation batch and its two lifecycle callbacks"
    )]
    fn run_mutations(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        requests: &str,
        arguments: Vec<String>,
        timeout: f64,
        context_data: &str,
        fresh: &Bound<'_, PyAny>,
        record: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        Self::run_mutations_owned(
            slf,
            py,
            mutation::MutationBatch {
                requests,
                arguments: &arguments,
                timeout,
                context_data,
                fresh,
                record,
            },
        )
    }

    fn prepare_mutation_cache(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        request: &str,
        plans: &str,
        context_data: &str,
    ) -> PyResult<Option<(String, std::collections::BTreeMap<String, String>)>> {
        Self::prepare_mutation_cache_owned(slf, py, request, plans, context_data)
    }

    fn read_fresh_mutation_cache(&self, path: &str, request: &str) -> PyResult<String> {
        self.pending_mutation_evidence()?.read(path, request)
    }

    fn write_fresh_mutation_cache(
        slf: &Bound<'_, Self>,
        path: &str,
        request: &str,
        results: Vec<Py<NativeRunResult>>,
    ) -> PyResult<()> {
        let inputs = slf
            .borrow()
            .pending_mutation_evidence()?
            .write_inputs(request)?;
        if let Some(inputs) = inputs {
            inputs.commit(slf.py(), path, results)?;
        }
        Ok(())
    }

    #[pyo3(signature = (selected_nodeids=None))]
    fn run(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        selected_nodeids: Option<Vec<String>>,
    ) -> PyResult<Py<NativeRunResult>> {
        let (mut suite, root, options, started) = {
            let mut session = slf.borrow_mut();
            let suite = session.take_pending()?;
            (
                suite,
                session.root.clone(),
                session.options.take(),
                session.started,
            )
        };
        let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &suite.context);
        let result = (|| {
            if let Some(selected) = selected_nodeids {
                assignment::select(py, &mut suite, &selected)?;
            }
            execute_prepared(
                py,
                suite,
                &root,
                false,
                options.as_ref().map(|o| o.bind(py)),
                started,
            )
        })();
        let result = cleanup.complete(result);
        let finalizer = {
            let mut session = slf.borrow_mut();
            session.state = State::Finished;
            session.finalizer.take()
        };
        drop(finalizer);
        result
    }

    #[pyo3(signature = (exitstatus=0))]
    fn close(slf: &Bound<'_, Self>, py: Python<'_>, exitstatus: i32) -> PyResult<()> {
        let (suite, options, finalizer) = {
            let mut session = slf.borrow_mut();
            if matches!(session.state, State::Finished) {
                return Ok(());
            }
            let suite = session.take_pending()?;
            session.state = State::Finished;
            (suite, session.options.take(), session.finalizer.take())
        };
        let result = Self::finish_pending(py, &suite, exitstatus);
        drop(suite);
        drop(options);
        drop(finalizer);
        result
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.options)?;
        visit.call(&self.finalizer)?;
        if let Some(functions) = &self.mutation_observed_code {
            for function in functions {
                visit.call(function)?;
            }
        }
        if let State::Pending(suite) = &self.state {
            let context = &suite.context;
            visit.call(&context.config)?;
            visit.call(&context.session)?;
            visit.call(&context.registry)?;
            visit.call(&context.parser)?;
            for item in &context.items {
                visit.call(item)?;
            }
            for fixture in &suite.fixtures {
                visit.call(&fixture.runtime)?;
                visit.call(&fixture.definition)?;
                visit.call(&fixture.ids)?;
                if let Some(parameters) = &fixture.parameters {
                    for parameter in parameters {
                        visit.call(parameter)?;
                    }
                }
            }
            for test in &suite.tests {
                visit.call(&test.callable)?;
                visit.call(&test.class)?;
                visit.call(&test.argument_keys)?;
            }
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut session = slf.borrow_mut();
            (
                std::mem::replace(&mut session.state, State::Finished),
                session.options.take(),
                session.finalizer.take(),
                session.mutation_observed_code.take(),
            )
        };
        // PEP 442 already closed the suite. Clearing only releases references;
        // callbacks are unsafe after GC starts breaking the surrounding graph.
        drop(removed);
    }
}

impl Drop for NativeIndependentSession {
    fn drop(&mut self) {
        if let State::Pending(suite) = std::mem::replace(&mut self.state, State::Finished) {
            let _ = Python::try_attach(|py| Self::finish_pending(py, &suite, 0));
        }
    }
}
