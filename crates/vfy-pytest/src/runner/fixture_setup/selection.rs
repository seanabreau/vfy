//! Selects the fixture definition that satisfies a request.

use super::dynamic::compile_dynamic_fixture;
use super::{FixtureRequestOrigin, SelectedFixture, live_callspec_parameter};
use crate::runner::compile::case_item;
use crate::runner::fixture_lookup::{
    FixtureScopeMismatch, fixture_lookup_error, fixture_scope_mismatch,
    missing_dynamic_fixture_parameter_error, select_fixture_use,
};
use crate::runner::model::{CacheKey, FixtureStack, FixtureUse, package_scope_node};
use crate::runner::runtime::{FixtureOutcome, SharedRuntime};
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::{Py, PyAnyMethods, PyResult, Python};
use pyo3::types::PyAny;
use vfy_core::FixtureScope;
use vfy_core::test_plan::TestId;

fn refresh_fixture_selection(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    selection: Option<&mut FixtureUse>,
) -> PyResult<()> {
    let Some(selection) = selection else {
        return Ok(());
    };
    let (live_item, scope_lookup, callspec_reader) = {
        let state = runtime.lock();
        let bindings = &state.suite.test_bindings[test_index];
        (
            bindings
                .compact
                .is_none()
                .then(|| bindings.item.clone_ref(py)),
            state.compatibility.scope_override_lookup,
            state.suite.callspec_reader.clone(),
        )
    };
    if let Some(item) = live_item {
        // Pytest captures each SubRequest's parameter before running that fixture's dependencies.
        let live = live_callspec_parameter(
            item.bind(py),
            name,
            scope_lookup,
            callspec_reader.as_deref(),
        )?;
        let state = runtime.lock();
        selection.cache_scope = live
            .as_ref()
            .and_then(|parameter| parameter.cache_scope)
            .unwrap_or(state.suite.fixtures()[selection.fixture.index()].declared_scope);
        state.suite.tests()[test_index]
            .scope_owner(selection.cache_scope)
            .unwrap_or_default()
            .clone_into(&mut selection.owner);
        drop(state);
        selection.param = live.map(|parameter| parameter.value);
    }
    if selection.cache_scope == FixtureScope::Package {
        let (item, package_baseid) = {
            let state = runtime.lock();
            (
                state.suite.test_bindings[test_index].item.clone_ref(py),
                state.suite.fixtures()[selection.fixture.index()]
                    .package_baseid
                    .clone(),
            )
        };
        // Public node methods can re-enter a saved request during owner lookup.
        selection.owner = package_scope_node(py, item.bind(py), &package_baseid)?
            .bind(py)
            .getattr(intern!(py, "nodeid"))?
            .extract()?;
    }
    Ok(())
}

pub(super) fn select_requested_fixture(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    requesting_scope: Option<FixtureScope>,
    requesting_stack: &FixtureStack,
    origin: FixtureRequestOrigin,
) -> PyResult<SelectedFixture> {
    if !runtime.lock().suite.tests()[test_index].contains_fixture(name) {
        compile_dynamic_fixture(py, runtime, test_index, name, requesting_stack)?;
    }
    let (mut selection, same_name_depth) = {
        let state = runtime.lock();
        let depth = requesting_stack.count_name(state.suite.fixtures(), name);
        let selected = select_fixture_use(
            py,
            &state.suite.tests()[test_index],
            &state.suite.test_bindings[test_index],
            state.suite.fixtures(),
            name,
            depth,
        )?;
        (selected, depth)
    };
    refresh_fixture_selection(py, runtime, test_index, name, selection.as_mut())?;
    let Some(fixture_use) = selection else {
        return Err(fixture_lookup_error(
            py,
            runtime,
            test_index,
            name,
            requesting_stack,
        )?);
    };
    let (fixture_scope, parameterized, request_scope, definition, check_scope) = {
        let state = runtime.lock();
        let fixture = state
            .suite
            .fixtures()
            .get(fixture_use.fixture.index())
            .ok_or_else(|| PyRuntimeError::new_err("requested fixture has no definition"))?;
        let repeated = state
            .resolved_fixture_keys
            .get(&test_index)
            .is_some_and(|keys| keys.get(name).is_some());
        let checked_scope = state.compatibility.scope_checks.checked_scope(
            fixture.declared_scope,
            fixture_use.cache_scope,
            repeated,
        );
        (
            checked_scope.unwrap_or(fixture.declared_scope),
            fixture.parameterized,
            fixture_use.cache_scope,
            state.suite.fixture_bindings[fixture_use.fixture.index()]
                .definition
                .clone_ref(py),
            checked_scope.is_some(),
        )
    };
    if fixture_use.param.is_none() && parameterized {
        let item = case_item(py, runtime, test_index)?;
        return Err(missing_dynamic_fixture_parameter_error(
            py,
            item.bind(py),
            name,
            &definition,
            origin,
            requesting_stack,
        )?);
    }
    if let Some(requesting_scope) = requesting_scope
        && check_scope
        && requesting_scope.rank() > fixture_scope.rank()
    {
        return Err(fixture_scope_mismatch(
            py,
            runtime,
            test_index,
            FixtureScopeMismatch {
                name,
                requesting_scope,
                fixture_scope,
                fixture_definition: &definition,
                fixture_stack: requesting_stack,
            },
        )?);
    }
    Ok(SelectedFixture {
        fixture_use,
        request_scope,
        same_name_depth,
    })
}

pub(super) fn active_fixture_outcome(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    fixture_use: &FixtureUse,
    same_name_depth: usize,
) -> PyResult<Option<(CacheKey, PyResult<Py<PyAny>>)>> {
    let mut state = runtime.lock();
    if state
        .resolving
        .iter()
        .any(|active| active.fixture == fixture_use.fixture.index())
        && same_name_depth == 0
    {
        return Err(PyRuntimeError::new_err(format!(
            "recursive fixture dependency while resolving {name:?}"
        )));
    }
    let cached = state
        .active_by_fixture
        .get(&fixture_use.fixture)
        .map(|key| -> PyResult<Option<(CacheKey, FixtureOutcome)>> {
            let instance = state
                .cache
                .get(key)
                .ok_or_else(|| PyRuntimeError::new_err("active fixture has no cached instance"))?;
            let fixture = &state.suite.fixtures()[fixture_use.fixture.index()];
            // An unparameterized leaf has no dependency invalidations or user
            // equality callbacks to replay on the next test. Keep requests from
            // inactive owners, async runners, and legacy finalizer ordering on
            // the full setup path.
            let reusable_leaf = instance.last_resolved_test != test_index
                && !state.compatibility.legacy_fixture_order
                && !fixture.parameterized
                && fixture_use.param.is_none()
                && instance.param.is_none()
                && fixture.dependencies.iter().all(|name| name == "request")
                && state.suite.fixture_bindings[fixture_use.fixture.index()]
                    .callable
                    .async_loop_scope
                    .is_none()
                && state.active_function == Some(TestId::from_index(test_index))
                && state.active_nodes.contains(&fixture_use.owner)
                && matches!(&instance.outcome, FixtureOutcome::Value(_));
            if (instance.last_resolved_test != test_index && !reusable_leaf)
                || (state.compatibility.legacy_fixture_order && instance.registered_owner.is_none())
            {
                return Ok(None);
            }
            Ok(Some((key.clone(), instance.outcome.clone_ref(py))))
        })
        .transpose()
        .map(Option::flatten)?;
    if let Some((key, FixtureOutcome::Value(_))) = &cached
        && let Some(instance) = state.cache.get_mut(key)
    {
        instance.last_resolved_test = test_index;
    }
    let restore_traceback = state.compatibility.cached_fixture_error_traceback;
    drop(state);
    Ok(cached.map(|(key, outcome)| (key, outcome.clone_result(py, restore_traceback))))
}
