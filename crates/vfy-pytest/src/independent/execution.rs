//! Fixture setup, call and teardown for one collected suite, with pytest request semantics.

use super::CollectedSuite;
use super::fixture_binding::NativeFixtureBinding;
use crate::module_cache::Module;
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::PyList;
use request::RequestContext;
use vfy_core::test_plan::TestPlan;
use vfy_core::{CollectionNodeId, FixtureScope};

mod asynchronous;
mod builtin_resources;
pub(crate) use builtin_resources::independent_fixture_resource;
mod callbacks;
mod capture;
mod current_test;
mod executor;
mod fixture_cache;
mod fixture_function;
mod fixture_hooks;
mod fixture_info;
mod fixture_lifecycle;
mod fixture_outcomes;
mod invocation;
mod node_finalizers;
pub(super) use node_finalizers::install_node_finalizers;
mod parameters;
mod phase_dispatch;
mod protocol;
pub(super) use protocol::SessionFailure;
mod provider_scopes;
mod report_sections;
mod request;
pub(crate) use request::NativeIndependentRequest;
mod resolved;
pub(crate) use resolved::NativeResolvedFixtureDefinitions;
mod run;
pub(super) use run::{NativeDeferredCleanup, run_cases};
mod state;
#[cfg(test)]
mod tests;

fn outcome(errors: Vec<PyErr>, py: Python<'_>, message: &str) -> PyResult<()> {
    if errors.is_empty() {
        return Ok(());
    }
    if errors.len() == 1 {
        return errors.into_iter().next().map_or(Ok(()), Err);
    }
    let values = PyList::empty(py);
    for error in errors.iter().rev() {
        values.append(error.value(py))?;
    }
    let group = Module::Builtins
        .get(py)?
        .getattr("BaseExceptionGroup")?
        .call1((message, values))?;
    Err(PyErr::from_value(group))
}

fn validate_fixture_scope(
    py: Python<'_>,
    requested: FixtureScope,
    declared: FixtureScope,
    name: &str,
) -> PyResult<()> {
    if requested.rank() > declared.rank() {
        let message = format!(
            "ScopeMismatch: {} fixture cannot access {} fixture {name}",
            requested.as_str(),
            declared.as_str()
        );
        return Err(fixture_outcomes::failed(py, &message)?);
    }
    Ok(())
}

/// The collected pytest item at `index`.
fn collected_item(suite: &CollectedSuite, index: usize) -> Option<&Py<PyAny>> {
    suite.context.items.get(index)
}

fn collection_node(test: &TestPlan) -> PyResult<CollectionNodeId> {
    test.collection_node
        .ok_or_else(|| PyAssertionError::new_err("test has no actual collection node"))
}

fn lookup_stack(
    py: Python<'_>,
    suite: &CollectedSuite,
    context: &RequestContext,
) -> Vec<Py<NativeFixtureBinding>> {
    context
        .stack
        .iter()
        .map(|id| {
            let runtime = suite.fixtures[id.index()].runtime.clone_ref(py);
            debug_assert_eq!(runtime.bind(py).borrow().identity(), *id);
            runtime
        })
        .collect()
}
