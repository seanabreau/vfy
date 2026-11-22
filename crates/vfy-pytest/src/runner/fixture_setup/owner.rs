//! Tracks the owner that finalizes a fixture instance.

use super::{FixtureSetupPlan, fixture_request};
use crate::runner::model::scope_node;
use crate::runner::runtime::{OwnerFinalizer, Runtime, SharedRuntime};
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::{Py, PyAnyMethods, PyResult, Python};
use pyo3::types::PyAny;
use vfy_core::FixtureScope;
use vfy_core::test_plan::TestId;

pub(super) fn fixture_owner_is_active(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    owner: &str,
    scope: FixtureScope,
    package_baseid: Option<&str>,
) -> PyResult<bool> {
    let (active, item) = {
        let state = runtime.lock();
        if !state.active_nodes.iter().any(|active| active == owner) {
            return Ok(false);
        }
        if state.active_function == Some(TestId::from_index(test_index)) {
            return Ok(true);
        }
        if scope == FixtureScope::Function
            || (scope == FixtureScope::Class && owner == state.suite.tests()[test_index].nodeid)
        {
            return Ok(false);
        }
        (
            state.owner_nodes.get(owner).map(|node| node.clone_ref(py)),
            state.suite.test_bindings[test_index].item.clone_ref(py),
        )
    };
    let Some(active) = active else {
        return Ok(false);
    };
    let requested = scope_node(py, item.bind(py), scope, package_baseid)?;
    Ok(active.bind(py).is(requested.bind(py)))
}

pub(super) fn register_fixture_owner(
    state: &mut Runtime,
    plan: &FixtureSetupPlan,
    owner_active: bool,
) -> bool {
    if !owner_active {
        return false;
    }
    if let Some(instance) = state.cache.get_mut(&plan.key) {
        instance.registered_owner = Some(plan.fixture_use.owner.clone());
    }
    let finalizers = state
        .owner_finalizers
        .entry(plan.fixture_use.owner.clone())
        .or_default();
    if state.compatibility.legacy_fixture_order {
        // Pytest 8.0-8.1 preserves the original fixture registration position
        // when a new parameter generation replaces an earlier cached instance.
        if let Some(OwnerFinalizer::Fixture(key)) = finalizers.iter_mut().find(|finalizer| {
            matches!(finalizer, OwnerFinalizer::Fixture(key) if key.identity == plan.key.identity)
        }) {
            key.clone_from(&plan.key);
            return true;
        }
    }
    finalizers.push(OwnerFinalizer::Fixture(plan.key.clone()));
    true
}

pub(super) fn inactive_fixture_owner_error(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
) -> PyResult<Py<PyAny>> {
    let request = fixture_request(py, runtime, test_index, plan)?;
    let node = request.bind(py).getattr("node")?;
    Err(PyAssertionError::new_err(node.unbind()))
}
