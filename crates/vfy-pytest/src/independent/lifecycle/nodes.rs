//! Construct Python collector nodes from scope owners.

use pyo3::prelude::*;
use pyo3::types::PyList;
use std::collections::BTreeSet;
use vfy_core::test_plan::TestPlan;

pub(in crate::independent) fn fixture_names(
    test: &TestPlan,
    fixtures: &[vfy_core::test_plan::FixturePlan],
) -> Vec<String> {
    let mut names = test.static_fixture_closure_with_fallback(
        fixtures,
        vfy_core::test_plan::FixtureClosureMode::IncludeDirectParameters,
        |_| &[],
    );
    names.sort_by_key(|name| {
        std::cmp::Reverse(
            test.resolve_fixture(fixtures, name, 0)
                .map_or(vfy_core::FixtureScope::Function, |resolution| {
                    fixtures[resolution.use_.fixture.index()].declared_scope
                })
                .rank(),
        )
    });
    names.into_iter().map(str::to_owned).collect()
}

pub(in crate::independent) fn prune_fixture_names(
    names: &Bound<'_, PyList>,
    closure: &[String],
    direct_parameters: &BTreeSet<String>,
) -> PyResult<()> {
    let mut reachable = closure.iter().map(String::as_str).collect::<BTreeSet<_>>();
    if !direct_parameters.is_empty() {
        // Direct-parameter fixture definitions depend on request. Pruning may
        // retain an existing request entry, but must not add it to the list.
        reachable.insert("request");
    }
    let retained = PyList::empty(names.py());
    for name in names.iter() {
        if reachable.contains(name.extract::<String>()?.as_str()) {
            retained.append(name)?;
        }
    }
    names.set_slice(0, names.len(), &retained)
}

pub(in crate::independent) fn attach_parameters(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    callspec: Option<Py<PyAny>>,
    start: usize,
) -> PyResult<()> {
    if let Some(callspec) = callspec {
        let parameter_marks = callspec.bind(py).getattr("marks")?.cast_into::<PyList>()?;
        item.getattr("own_markers")?
            .cast_into::<PyList>()?
            .set_slice(start, start + parameter_marks.len(), &parameter_marks)?;
        item.setattr("callspec", callspec.bind(py))?;
    }
    Ok(())
}
