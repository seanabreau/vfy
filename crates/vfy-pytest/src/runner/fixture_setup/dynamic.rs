//! Compiles fixtures requested dynamically and records their resolved names.

use crate::runner::compile::case_item;
use crate::runner::fixture_lookup::fixture_lookup_error;
use crate::runner::model::{
    CompiledFixtureDefinition, FixtureStack, asyncio_mode, optional_mapping_item,
    parse_fixture_scope,
};
use crate::runner::runtime::SharedRuntime;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::{Bound, PyAnyMethods, PyResult, Python};
use pyo3::types::PyAny;
use std::collections::HashMap;
use vfy_core::FixtureId;
use vfy_core::test_plan::{DynamicFixturePlanEntry, TestId};

fn prepare_dynamic_fixture_definitions(
    py: Python<'_>,
    config: &Bound<'_, PyAny>,
    definitions: &Bound<'_, PyAny>,
) -> PyResult<Vec<CompiledFixtureDefinition>> {
    let inspect = py.import("inspect")?;
    let pytest_python = py.import("_pytest.python")?;
    let direct_callable = pytest_python.getattr("get_direct_param_fixture_func")?;
    let item_mode = asyncio_mode(config)?;
    let mut prepared = Vec::new();
    for definition in definitions.try_iter()? {
        let definition = definition?;
        prepared.push(CompiledFixtureDefinition::from_py(
            config,
            &inspect,
            &direct_callable,
            item_mode,
            &definition,
        )?);
    }
    Ok(prepared)
}

pub(super) fn compile_dynamic_fixture(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    fixture_stack: &FixtureStack,
) -> PyResult<()> {
    let item = case_item(py, runtime, test_index)?.into_bound(py);
    let manager = item.getattr("session")?.getattr("_fixturemanager")?;
    // pytest 8.0 takes a node ID; 8.1 and later take the collection node.
    let fixturedefs_use_nodeid = runtime.lock().compatibility.fixturedefs_use_nodeid;
    let definitions = if fixturedefs_use_nodeid {
        manager.call_method1("getfixturedefs", (name, item.getattr("nodeid")?))?
    } else {
        manager.call_method1("getfixturedefs", (name, &item))?
    };
    if definitions.is_none() {
        return Err(fixture_lookup_error(
            py,
            runtime,
            test_index,
            name,
            fixture_stack,
        )?);
    }

    let config = item.getattr("config")?;
    let prepared = prepare_dynamic_fixture_definitions(py, &config, &definitions)?;
    if prepared.is_empty() {
        return Err(fixture_lookup_error(
            py,
            runtime,
            test_index,
            name,
            fixture_stack,
        )?);
    }

    let callspec = item.getattr("callspec").ok();
    let params = callspec
        .as_ref()
        .map(|value| value.getattr("params"))
        .transpose()?;
    let scopes = callspec
        .as_ref()
        .map(|value| value.getattr("_arg2scope"))
        .transpose()?;
    let param = params
        .as_ref()
        .map(|params| optional_mapping_item(params, name))
        .transpose()?
        .flatten()
        .map(Bound::unbind);
    let dynamic_scope = scopes
        .as_ref()
        .map(|scopes| optional_mapping_item(scopes, name))
        .transpose()?
        .flatten()
        .map(|scope| scope.getattr("value")?.extract::<String>())
        .transpose()?
        .map(|scope| parse_fixture_scope(&scope))
        .transpose()?;

    let mut state = runtime.lock();
    let mut entries = Vec::with_capacity(prepared.len());
    let mut new_bindings = Vec::new();
    let mut identities = Vec::with_capacity(prepared.len());
    let mut dynamic_params = Vec::with_capacity(prepared.len());
    let mut staged_ids = HashMap::new();
    let first_staged_index = state.suite.fixtures().len();
    for definition in prepared {
        identities.push(definition.identity);
        let cache_scope = dynamic_scope.unwrap_or(definition.scope);
        if let Some(fixture) = state.suite.fixture_ids.get(&definition.identity) {
            entries.push(DynamicFixturePlanEntry::Existing {
                fixture: FixtureId::from_index(*fixture),
                cache_scope,
            });
        } else if let Some(fixture) = staged_ids.get(&definition.identity) {
            entries.push(DynamicFixturePlanEntry::Pending {
                fixture: *fixture,
                cache_scope,
            });
        } else {
            let fixture_id = FixtureId::from_index(first_staged_index + new_bindings.len());
            staged_ids.insert(definition.identity, fixture_id);
            let (fixture, binding) = definition.into_plan_and_binding(fixture_id);
            entries.push(DynamicFixturePlanEntry::New {
                fixture,
                cache_scope,
            });
            new_bindings.push(binding);
        }
        let captured = param
            .as_ref()
            .filter(|_| state.suite.test_bindings[test_index].compact.is_some());
        dynamic_params.push(captured.map(|value| value.clone_ref(py)));
    }
    let fixture_ids = state
        .suite
        .core
        .register_dynamic_fixture_chain(TestId::from_index(test_index), name, entries)
        .map_err(|detail| {
            PyRuntimeError::new_err(format!("invalid dynamic fixture plan: {detail}"))
        })?;
    state.suite.fixture_bindings.extend(new_bindings);
    for (identity, fixture) in identities.into_iter().zip(fixture_ids) {
        state.suite.fixture_ids.insert(identity, fixture.index());
    }
    state.suite.test_bindings[test_index].insert_dynamic_fixture_params(name, dynamic_params);
    Ok(())
}

pub(super) fn record_resolved_fixture_name(runtime: &SharedRuntime, test_index: usize, name: &str) {
    let mut state = runtime.lock();
    if state.suite.tests()[test_index]
        .eager_fixtures
        .iter()
        .any(|static_name| static_name == name)
    {
        return;
    }
    state
        .resolved_fixture_keys
        .entry(test_index)
        .or_default()
        .record_dynamic_name(name);
}
