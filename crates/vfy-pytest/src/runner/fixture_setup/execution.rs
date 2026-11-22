//! Runs fixture setup callables and registers dependency finalizers.

use super::{FixtureResolution, FixtureSetupPlan, resolve_named_fixture};
use crate::runner::compile::case_item;
use crate::runner::fixture_lookup::{
    bound_fixture_callable, unhandled_async_fixture_error_for_item,
    warn_unhandled_async_fixture_for_item,
};
use crate::runner::model::{FixtureCallable, FixtureStack, UnhandledAsyncFixture, asyncio_mode};
use crate::runner::runtime::{FixtureFinalizer, SharedRuntime};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyDictMethods, PyResult, Python};
use pyo3::types::{PyAny, PyDict, PyTuple};
use vfy_core::test_plan::FixtureExecution;
use vfy_core::{AsyncioLoopScope, FixtureId, FixtureScope};

fn execute_async_fixture(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
    callable: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
    runner: &Py<PyAny>,
) -> PyResult<(Py<PyAny>, Option<FixtureFinalizer>)> {
    let callable = bound_fixture_callable(py, runtime, test_index, plan, callable, kwargs)?;
    let result = py
        .import("vfy.asyncio_adapter")?
        .getattr("run_async_fixture")?
        .call1((
            runner.bind(py),
            callable.bind(py),
            PyTuple::empty(py),
            kwargs,
        ))?;
    let (value, finalizer) = result.extract::<(Py<PyAny>, Option<Py<PyAny>>)>()?;
    Ok((value, finalizer.map(FixtureFinalizer::Callable)))
}

fn current_fixture_callable(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    fixture: FixtureId,
    definition: &Bound<'_, PyAny>,
) -> PyResult<FixtureCallable> {
    // Dependencies can replace the function or its code before this setup.
    let current = {
        let state = runtime.lock();
        state.suite.fixture_bindings[fixture.index()]
            .callable
            .clone_ref(py)
    };
    let live = definition.getattr(intern!(py, "func"))?;
    if current.is_current(&live)? {
        return Ok(current);
    }
    let item = case_item(py, runtime, test_index)?;
    let config = item.bind(py).getattr(intern!(py, "config"))?;
    let inspect = py.import("inspect")?;
    let direct_callable = py
        .import("_pytest.python")?
        .getattr(intern!(py, "get_direct_param_fixture_func"))?;
    let dependencies = definition
        .getattr(intern!(py, "argnames"))?
        .extract::<Vec<String>>()?;
    let refreshed = FixtureCallable::from_definition(
        &config,
        &inspect,
        &direct_callable,
        asyncio_mode(&config)?,
        definition,
        &dependencies,
    )?;
    let previous = {
        let mut state = runtime.lock();
        std::mem::replace(
            &mut state.suite.fixture_bindings[fixture.index()].callable,
            refreshed.clone_ref(py),
        )
    };
    drop(previous);
    Ok(refreshed)
}

pub(super) fn fixture_callable_for_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    fixture: FixtureId,
    definition: &Bound<'_, PyAny>,
) -> PyResult<FixtureCallable> {
    let cached = {
        let state = runtime.lock();
        state.active_by_fixture.contains_key(&fixture).then(|| {
            state.suite.fixture_bindings[fixture.index()]
                .callable
                .clone_ref(py)
        })
    };
    // A cached fixture keeps its setup metadata until invalidation. Resolve its
    // implicit runner before comparing parameters, without validating mutations
    // that pytest ignores on cache hits. Cache misses refresh before invocation.
    match cached {
        Some(callable) => Ok(callable),
        None => current_fixture_callable(py, runtime, test_index, fixture, definition),
    }
}

fn fixture_runner_for_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
    current: &FixtureCallable,
) -> PyResult<Option<Py<PyAny>>> {
    let runner = if current.async_loop_scope == plan.callable.async_loop_scope {
        plan.async_runner
            .as_ref()
            .map(|runner| runner.clone_ref(py))
    } else {
        resolve_fixture_runner(
            py,
            runtime,
            test_index,
            current.async_loop_scope,
            plan.request_scope,
            &plan.fixture_stack,
            FixtureResolution::DECLARED,
        )?
    };
    if let Some(scope) = current.async_loop_scope {
        register_async_fixture_dependency_finalizer(
            runtime,
            test_index,
            plan.fixture_use.fixture,
            scope,
        )?;
    }
    Ok(runner)
}

pub(super) fn resolve_fixture_runner(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    loop_scope: Option<AsyncioLoopScope>,
    request_scope: FixtureScope,
    fixture_stack: &FixtureStack,
    resolution: FixtureResolution,
) -> PyResult<Option<Py<PyAny>>> {
    loop_scope
        .map(|scope| {
            resolve_named_fixture(
                py,
                runtime,
                test_index,
                &format!("_{}_scoped_runner", scope.as_str()),
                Some(request_scope),
                Some(fixture_stack.clone()),
                resolution,
            )
        })
        .transpose()
}

fn handle_unhandled_async_fixture(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
    execution: FixtureExecution,
) -> PyResult<()> {
    if execution != FixtureExecution::UnsupportedAsync {
        return Ok(());
    }
    let policy = runtime.lock().compatibility.unhandled_async_fixture;
    match policy {
        UnhandledAsyncFixture::Error => Err(unhandled_async_fixture_error_for_item(
            py,
            runtime,
            test_index,
            &plan.name,
            &plan.definition,
        )?),
        // Pytest 8.4–9.0 warns while returning the unhandled coroutine or generator.
        UnhandledAsyncFixture::Warn => warn_unhandled_async_fixture_for_item(
            py,
            runtime,
            test_index,
            &plan.name,
            &plan.definition,
        ),
        UnhandledAsyncFixture::Allow => Ok(()),
    }
}

pub(super) fn execute_fixture_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
) -> PyResult<(Py<PyAny>, Option<FixtureFinalizer>)> {
    let cached = runtime.cached_fixture_result(py, &plan.key);
    if cached.is_none() && !runtime.lock().compatibility.legacy_fixture_order {
        register_fixture_dependency_finalizers(
            runtime,
            plan.fixture_use.fixture,
            plan.declared_dependency_count,
        );
    }
    if let Some(outcome) = cached {
        return outcome.map(|value| (value, None));
    }
    let refreshed;
    let live = plan.definition.bind(py).getattr(intern!(py, "func"))?;
    let current = if plan.callable.is_current(&live)? {
        &plan.callable
    } else {
        refreshed = current_fixture_callable(
            py,
            runtime,
            test_index,
            plan.fixture_use.fixture,
            plan.definition.bind(py),
        )?;
        &refreshed
    };
    handle_unhandled_async_fixture(py, runtime, test_index, plan, current.execution)?;

    if plan.name == "event_loop_policy" {
        // Share pytest's asyncio warning while this item's warning filters are active.
        py.import("vfy.asyncio_plugin")?
            .getattr(intern!(py, "_warn_event_loop_policy_override"))?
            .call1((plan.name.as_str(), current.callable.bind(py)))?;
    }

    if current.execution == FixtureExecution::DirectParameter {
        return plan
            .fixture_use
            .param
            .as_ref()
            .map(|value| (value.clone_ref(py), None))
            .ok_or_else(|| PyAttributeError::new_err("param"));
    }

    let async_runner = fixture_runner_for_setup(py, runtime, test_index, plan, current)?;
    let kwargs = PyDict::new(py);
    for (dependency, value) in &plan.arguments {
        kwargs.set_item(dependency, value.bind(py))?;
    }
    if matches!(
        current.execution,
        FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
    ) {
        let runner = async_runner
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("async fixture plan has no scoped runner"))?;
        return execute_async_fixture(
            py,
            runtime,
            test_index,
            plan,
            &current.callable,
            &kwargs,
            runner,
        );
    }
    let mut callable =
        bound_fixture_callable(py, runtime, test_index, plan, &current.callable, &kwargs)?;
    if let Some(runner) = &async_runner {
        let wrapper = if current.execution == FixtureExecution::Generator {
            "_wrap_syncgen_fixture"
        } else {
            "_wrap_sync_fixture"
        };
        callable = py
            .import("vfy.asyncio_plugin")?
            .getattr(wrapper)?
            .call1((callable.bind(py), runner.bind(py)))?
            .unbind();
    }
    let result = callable.bind(py).call((), Some(&kwargs))?;
    if current.execution == FixtureExecution::Generator {
        match result.call_method0(intern!(py, "__next__")) {
            Ok(value) => Ok((
                value.unbind(),
                Some(FixtureFinalizer::Generator {
                    fixture: callable,
                    generator: result.unbind(),
                }),
            )),
            Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => Err(
                PyValueError::new_err(format!("{} did not yield a value", plan.name)),
            ),
            Err(error) => Err(error),
        }
    } else {
        Ok((result.unbind(), None))
    }
}

fn register_async_fixture_dependency_finalizer(
    runtime: &SharedRuntime,
    test_index: usize,
    fixture: FixtureId,
    scope: AsyncioLoopScope,
) -> PyResult<()> {
    let name = format!("_{}_scoped_runner", scope.as_str());
    let mut state = runtime.lock();
    let key = state
        .resolved_fixture_keys
        .get(&test_index)
        .and_then(|fixtures| fixtures.get(&name))
        .ok_or_else(|| PyRuntimeError::new_err("async runner has no resolved fixture key"))?;
    // The asyncio setup hook registers only the effective runner on cache
    // misses, after a callable refresh can change its loop scope.
    state
        .pending_finalizers
        .entry(key)
        .or_default()
        .push(FixtureFinalizer::Dependent(fixture));
    Ok(())
}

fn register_fixture_dependency_finalizers(
    runtime: &SharedRuntime,
    fixture: FixtureId,
    declared_dependency_count: usize,
) {
    let mut state = runtime.lock();
    let dependencies = state.resolving.last().map_or_else(Vec::new, |fixture| {
        fixture
            .dependencies
            .iter()
            .take(declared_dependency_count)
            .cloned()
            .collect::<Vec<_>>()
    });
    // Finish callbacks bind the FixtureDef identity: after a parameter switch,
    // an earlier callback finishes its current generation, as pytest does.
    for dependency in dependencies {
        state
            .pending_finalizers
            .entry(dependency)
            .or_default()
            .push(FixtureFinalizer::Dependent(fixture));
    }
}

pub(super) fn register_latest_fixture_dependency_finalizer(
    runtime: &SharedRuntime,
    fixture: FixtureId,
) {
    let mut state = runtime.lock();
    let dependency = state
        .resolving
        .last()
        .and_then(|fixture| fixture.dependencies.last())
        .cloned();
    if let Some(dependency) = dependency {
        state
            .pending_finalizers
            .entry(dependency)
            .or_default()
            .push(FixtureFinalizer::Dependent(fixture));
    }
}
