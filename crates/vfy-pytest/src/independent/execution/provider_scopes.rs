//! Fixture providers belong to lexical collection scopes, not test requests.

use super::super::CollectedSuite;
use super::super::fixture_binding::NativeFixtureBinding;
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Mutex, PoisonError};
use vfy_core::FixtureId;
use vfy_core::test_plan::FixtureExecution;

struct ScopeObjects {
    parent: Option<Py<NativeProviderScope>>,
    local: Vec<(FixtureId, Py<NativeFixtureBinding>)>,
}

/// Requests keep this scope alive after the executor releases its own tree.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeProviderScope {
    objects: Mutex<Option<ScopeObjects>>,
}

impl NativeProviderScope {
    pub(super) fn names(scope: &Py<Self>, py: Python<'_>) -> PyResult<Vec<String>> {
        let mut scope = Some(scope.clone_ref(py));
        let mut providers = Vec::new();
        while let Some(current) = scope {
            let (parent, local) = {
                let state = current
                    .bind(py)
                    .get()
                    .objects
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner);
                let state = state.as_ref().ok_or_else(|| {
                    PyAssertionError::new_err("fixture provider scope has been cleared")
                })?;
                (
                    state.parent.as_ref().map(|parent| parent.clone_ref(py)),
                    state
                        .local
                        .iter()
                        .map(|(id, binding)| (*id, binding.clone_ref(py)))
                        .collect::<Vec<_>>(),
                )
            };
            providers.extend(local);
            scope = parent;
        }
        providers.sort_unstable_by_key(|(id, _)| id.index());
        let mut names = BTreeSet::new();
        for (_, provider) in providers {
            names.insert(provider.bind(py).getattr("argname")?.extract::<String>()?);
        }
        Ok(names.into_iter().collect())
    }
}

#[pymethods]
impl NativeProviderScope {
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(state) = self.objects.try_lock()
            && let Some(state) = state.as_ref()
        {
            visit.call(&state.parent)?;
            for (_, binding) in &state.local {
                visit.call(binding)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(removed);
    }
}

/// Find the most specific collected baseid accepted by fixture visibility.
fn owner(nodeid: &str, scopes: &BTreeMap<String, usize>) -> usize {
    let mut owner = 0;
    for (index, character) in nodeid.char_indices() {
        if index > 0
            && (character == '/' || (character == ':' && nodeid[index..].starts_with("::")))
            && let Some(&scope) = scopes.get(&nodeid[..index])
        {
            owner = scope;
        }
    }
    if let Some(&scope) = scopes.get(nodeid) {
        owner = scope;
    }
    owner
}

/// Construct final collection scopes and attach one scope identity to each test.
pub(super) fn build(
    py: Python<'_>,
    suite: &mut CollectedSuite,
) -> PyResult<Vec<Py<NativeProviderScope>>> {
    if suite.tests.len() != suite.plan.tests().len() {
        return Err(PyAssertionError::new_err(
            "test bindings do not match the suite plan",
        ));
    }
    let mut grouped = BTreeMap::<String, Vec<FixtureId>>::new();
    grouped.entry(String::new()).or_default();
    for fixture in suite.plan.fixtures() {
        if fixture.execution != FixtureExecution::DirectParameter {
            grouped
                .entry(fixture.package_baseid.clone())
                .or_default()
                .push(fixture.id);
        }
    }
    let mut grouped = grouped.into_iter().collect::<Vec<_>>();
    grouped.sort_by_key(|(baseid, _)| baseid.len());
    let mut scopes = Vec::<Py<NativeProviderScope>>::with_capacity(grouped.len());
    let mut by_baseid = BTreeMap::new();
    for (baseid, ids) in grouped {
        let parent = if baseid.is_empty() {
            None
        } else {
            Some(scopes[owner(&baseid, &by_baseid)].clone_ref(py))
        };
        let local = ids
            .into_iter()
            .map(|id| (id, suite.fixtures[id.index()].runtime.clone_ref(py)))
            .collect();
        let scope = Py::new(
            py,
            NativeProviderScope {
                objects: Mutex::new(Some(ScopeObjects { parent, local })),
            },
        )?;
        by_baseid.insert(baseid, scopes.len());
        scopes.push(scope);
    }
    for (test, binding) in suite.plan.tests().iter().zip(&mut suite.tests) {
        binding.provider_scope = owner(&test.nodeid, &by_baseid);
    }
    Ok(scopes)
}
