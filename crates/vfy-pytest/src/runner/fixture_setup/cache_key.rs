//! Chooses the cache key for a fixture use, tearing down on parameter switches.

use crate::runner::model::{
    CacheKey, FixtureParameterComparison, FixtureUse, PytestCompatibilityPolicy,
};
use crate::runner::runtime::{FixtureTeardownReason, SharedRuntime};
use crate::runner::teardown::teardown_keys;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::{Py, PyAnyMethods, PyResult, Python};
use pyo3::pyclass::CompareOp;
use pyo3::types::PyAny;
use vfy_core::{FixtureCacheIdentity, FixtureCacheKey};

fn same_parameter(
    py: Python<'_>,
    previous: Option<&Py<PyAny>>,
    next: Option<&Py<PyAny>>,
    compatibility: PytestCompatibilityPolicy,
) -> PyResult<bool> {
    let none = py.None();
    let previous = previous.unwrap_or(&none).bind(py);
    let next = next.unwrap_or(&none).bind(py);
    // pytest 8.0-8.2 uses object identity for FixtureDef.cache_key.
    if matches!(
        compatibility.parameter_comparison,
        FixtureParameterComparison::Identity
    ) {
        return Ok(previous.is(next));
    }
    match next
        .rich_compare(previous, CompareOp::Eq)
        .and_then(|value| value.is_truthy())
    {
        Ok(equal) => Ok(equal),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyRuntimeError>(py) =>
        {
            Ok(previous.is(next))
        }
        Err(error) => Err(error),
    }
}

pub(super) fn cache_key_for_use(
    py: Python<'_>,
    runtime: &SharedRuntime,
    fixture_use: &FixtureUse,
) -> PyResult<CacheKey> {
    let active = {
        let state = runtime.lock();
        state
            .active_by_fixture
            .get(&fixture_use.fixture)
            .map(|key| -> PyResult<(CacheKey, Option<Py<PyAny>>)> {
                let instance = state.cache.get(key).ok_or_else(|| {
                    PyRuntimeError::new_err("active fixture has no cached instance")
                })?;
                Ok((
                    key.clone(),
                    instance.param.as_ref().map(|param| param.clone_ref(py)),
                ))
            })
            .transpose()?
    };
    if let Some((active_key, active_param)) = active {
        let compatibility = runtime.lock().compatibility;
        if same_parameter(
            py,
            active_param.as_ref(),
            fixture_use.param.as_ref(),
            compatibility,
        )? {
            return Ok(active_key);
        }
        if let Some(error) = teardown_keys(
            py,
            runtime,
            vec![active_key],
            FixtureTeardownReason::ParameterSwitch,
        ) {
            return Err(error.into_pyerr(py));
        }
    }
    let mut state = runtime.lock();
    let generation = state
        .next_cache_generation
        .checked_next()
        .ok_or_else(|| PyRuntimeError::new_err("fixture cache generation exhausted"))?;
    state.next_cache_generation = generation;
    Ok(FixtureCacheKey {
        identity: FixtureCacheIdentity {
            fixture: fixture_use.fixture,
            owner: vfy_core::FixtureOwner::Path(fixture_use.owner.clone()),
            scope: fixture_use.cache_scope,
        },
        generation,
    })
}
