//! Separate fixture invocation outcomes from failures in the outcome protocol.

use super::super::asyncio;
use super::asynchronous::{FixtureCall, FixtureDispatch};
use super::callbacks::FinishGenerator;
use super::executor::Executor;
use super::fixture_outcomes;
use super::request::RequestContext;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cell::RefCell;
use std::rc::Rc;
use vfy_core::FixtureCacheKey;
use vfy_core::test_plan::{FixtureExecution, FixturePlan};

pub(super) type InvocationResult = PyResult<Py<PyAny>>;

impl Executor {
    pub(super) fn invoke_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        key: &FixtureCacheKey,
        fixture: &FixturePlan,
        call: FixtureCall<'_, '_>,
    ) -> PyResult<InvocationResult> {
        let execution = match self.synchronized_fixture(py, context, key, fixture, call)? {
            FixtureDispatch::Handled(result) => return Ok(result),
            FixtureDispatch::Ready(execution) => execution,
            FixtureDispatch::Reclassify => asyncio::classify(call.callable)?,
        };
        call_fixture(py, call, execution)
    }
}

pub(super) fn call_fixture(
    py: Python<'_>,
    call: FixtureCall<'_, '_>,
    execution: FixtureExecution,
) -> PyResult<InvocationResult> {
    let result = (|| {
        let value = call.callable.call((), Some(call.kwargs))?;
        match execution {
            FixtureExecution::Generator => {
                let yielded = py
                    .import("builtins")?
                    .call_method1("next", (&value,))?
                    .unbind();
                call.finalizers.append(Py::new(
                    py,
                    FinishGenerator {
                        generator: RefCell::new(Some(value.unbind())),
                    },
                )?)?;
                Ok(yielded)
            }
            FixtureExecution::AsyncCoroutine
            | FixtureExecution::AsyncGenerator
            | FixtureExecution::UnsupportedAsync => Err(PyValueError::new_err(
                "async generator fixtures are unsupported by the independent frontend",
            )),
            FixtureExecution::Callable | FixtureExecution::DirectParameter => Ok(value.unbind()),
        }
    })();
    fixture_outcomes::complete(py, result)
}
