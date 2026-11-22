//! Read-only fixture facts from the compiled closure and cache identities.

use super::request::NativeIndependentRequest;
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;

/// Name, selected definition's declared scope, any nonempty visible parameters,
/// and whether the test explicitly requests the fixture as a function argument.
pub(super) type FixtureInfoRow = (String, &'static str, bool, bool);

pub(super) fn snapshot(
    py: Python<'_>,
    request: &NativeIndependentRequest,
) -> PyResult<Vec<FixtureInfoRow>> {
    let (owner, context) = request.parts()?;
    let suite = owner.suite()?;
    let state = owner.state.borrow();
    let index = context.test_index;
    if !context.lifetime.get()
        || !state
            .tests
            .get(&index)
            .is_some_and(|active| active.lifetime.get())
    {
        return Err(PyAssertionError::new_err(
            "fixture info requires an active test request",
        ));
    }
    let resolved = request.fixture_definitions(py)?;
    let test = &suite.plan.tests()[index];
    let fixtures = suite.plan.fixtures();
    let mut result = Vec::new();
    for name in test.static_fixture_names(fixtures) {
        if name == "request" {
            continue;
        }
        let key = resolved.bind(py).borrow().key(name).ok_or_else(|| {
            PyAssertionError::new_err(format!(
                "fixture info requires completed setup for '{name}'"
            ))
        })?;
        if !state
            .instance(&key)
            .is_some_and(|cached| cached.lifetime.get())
        {
            return Err(PyAssertionError::new_err(format!(
                "fixture info is unavailable for inactive fixture '{name}'"
            )));
        }
        // Cache keys are minted only for plan fixtures in `Executor::resolve`.
        let scope = fixtures[key.identity.fixture.index()].declared_scope;
        let mut parameterized = false;
        let mut depth = 0;
        while let Some(resolution) = test.resolve_fixture(fixtures, name, depth) {
            let binding = &suite.fixtures[resolution.use_.fixture.index()];
            parameterized |= binding
                .parameters
                .as_ref()
                .is_some_and(|parameters| !parameters.is_empty());
            depth += 1;
        }
        result.push((
            name.to_owned(),
            scope.as_str(),
            parameterized,
            test.argument_names.iter().any(|argument| argument == name),
        ));
    }
    Ok(result)
}
