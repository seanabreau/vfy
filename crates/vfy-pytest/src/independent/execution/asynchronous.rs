//! Bind async callables to fixture runners and finalizers.
use super::super::asyncio;
use super::executor::Executor;
use super::fixture_outcomes;
use super::invocation::{InvocationResult, call_fixture};
use super::request::{NativeIndependentRequest, RequestContext};
use super::resolved::NativeResolvedFixtureDefinitions;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::rc::Rc;
use vfy_core::test_plan::{FixtureExecution, FixturePlan};
use vfy_core::{AsyncioLoopScope, FixtureCacheKey};

#[derive(Clone, Copy)]
pub(super) struct FixtureCall<'a, 'py> {
    pub(super) callable: &'a Bound<'py, PyAny>,
    pub(super) kwargs: &'a Bound<'py, PyDict>,
    pub(super) finalizers: &'a Bound<'py, PyList>,
    pub(super) request: &'a Py<NativeIndependentRequest>,
}

pub(super) enum FixtureDispatch {
    Handled(InvocationResult),
    Ready(FixtureExecution),
    Reclassify,
}

fn is_async(kind: FixtureExecution) -> bool {
    matches!(
        kind,
        FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
    )
}

impl Executor {
    fn runner_dependency(
        self: &Rc<Self>,
        py: Python<'_>,
        key: &FixtureCacheKey,
        scope: AsyncioLoopScope,
        request: &Py<NativeIndependentRequest>,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<()> {
        let callbacks = self
            .dependency_callbacks(py, &asyncio::runner_name(scope), resolved)
            .ok_or_else(|| PyValueError::new_err("async runner has no cached fixture"))?;
        callbacks
            .bind(py)
            .append(self.finish_fixture(py, key, request)?)
    }

    pub(super) fn synchronized_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        key: &FixtureCacheKey,
        fixture: &FixturePlan,
        call: FixtureCall<'_, '_>,
    ) -> PyResult<FixtureDispatch> {
        let suite = self.suite()?;
        if !asyncio::enabled(Some(&suite.context), py)? {
            let kind = asyncio::classify(call.callable)?;
            let handled = self.unhandled_async(py, context, fixture, kind)?;
            return Ok(match handled {
                Some(result) => FixtureDispatch::Handled(result),
                None if !is_async(kind) => FixtureDispatch::Ready(kind),
                None => FixtureDispatch::Reclassify,
            });
        }
        let mut kind = asyncio::classify(call.callable)?;
        let Some(mut scope) =
            asyncio::fixture_scope(Some(&suite.context), call.callable, context.scope)?
        else {
            return Ok(match self.unhandled_async(py, context, fixture, kind)? {
                Some(result) => FixtureDispatch::Handled(result),
                None => FixtureDispatch::Reclassify,
            });
        };
        let resolved = call.request.bind(py).get().fixture_definitions(py)?;
        let mut runner = self.resolve(
            py,
            context,
            &asyncio::runner_name(scope),
            call.request.bind(py).as_any(),
            &resolved,
        )?;
        // A runner's dependencies may replace this fixture's code or loop metadata.
        let Some(current) =
            asyncio::fixture_scope(Some(&suite.context), call.callable, context.scope)?
        else {
            return Ok(
                match self.unhandled_async(
                    py,
                    context,
                    fixture,
                    asyncio::classify(call.callable)?,
                )? {
                    Some(result) => FixtureDispatch::Handled(result),
                    None => FixtureDispatch::Reclassify,
                },
            );
        };
        if current != scope {
            scope = current;
            runner = self.resolve(
                py,
                context,
                &asyncio::runner_name(scope),
                call.request.bind(py).as_any(),
                &resolved,
            )?;
        }
        kind = asyncio::classify(call.callable)?;
        self.runner_dependency(py, key, scope, call.request, &resolved)?;
        if is_async(kind) {
            let result = (|| {
                let (value, cleanup) = py
                    .import("vfy.asyncio_adapter")?
                    .call_method1(
                        "run_async_fixture",
                        (runner, call.callable, PyTuple::empty(py), call.kwargs),
                    )?
                    .extract::<(Py<PyAny>, Option<Py<PyAny>>)>()?;
                if let Some(cleanup) = cleanup {
                    call.finalizers.append(cleanup)?;
                }
                Ok(value)
            })();
            return Ok(FixtureDispatch::Handled(fixture_outcomes::complete(
                py, result,
            )?));
        }
        let (helper, execution) = if kind == FixtureExecution::Generator {
            ("_wrap_syncgen_fixture", FixtureExecution::Generator)
        } else {
            ("_wrap_sync_fixture", FixtureExecution::Callable)
        };
        let wrapped = py
            .import("vfy.asyncio_runtime")?
            .call_method1(helper, (call.callable, runner))?;
        Ok(FixtureDispatch::Handled(call_fixture(
            py,
            FixtureCall {
                callable: &wrapped,
                ..call
            },
            execution,
        )?))
    }

    /// Async fixtures without an async plugin fail the requesting item, as pytest does.
    fn unhandled_async(
        &self,
        py: Python<'_>,
        context: &RequestContext,
        fixture: &FixturePlan,
        kind: FixtureExecution,
    ) -> PyResult<Option<InvocationResult>> {
        if !is_async(kind) {
            return Ok(None);
        }
        let suite = self.suite()?;
        let test = &suite.plan.tests()[context.test_index];
        let item_name = test.nodeid.rsplit("::").next().unwrap_or(&test.nodeid);
        let autouse = if suite.fixtures[fixture.id.index()].autouse {
            " with autouse=True"
        } else {
            ""
        };
        let repr = py.import("builtins")?.getattr("repr")?;
        let item_name = repr.call1((item_name,))?.extract::<String>()?;
        let fixture_name = repr.call1((&fixture.name,))?.extract::<String>()?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("pytrace", false)?;
        py.import("vfy.independent.facade")?.getattr("fail")?.call((format!(
            "{item_name} requested an async fixture {fixture_name}{autouse}, with no plugin or hook that handled it. This is an error, as pytest does not natively support it.\nSee: https://docs.pytest.org/en/stable/deprecations.html#sync-test-depending-on-async-fixture",
        ),), Some(&kwargs))?;
        Ok(None)
    }
}
