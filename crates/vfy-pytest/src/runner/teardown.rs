//! Owns fixture and item teardown.

use super::compile::case_item;
use super::execute::merge_errors;
use super::model::{CacheKey, FixtureFinalizerErrors, FixtureStack, XfailMode};
use super::report::{take_unittest_exception, xfail_matches_error};
use super::request::NativeRequest;
use super::runtime::{
    FixtureFinalizer, FixtureTeardownReason, OwnerFinalizer, SharedRuntime, TeardownFailure,
    TeardownGrouping, TeardownOutcome,
};
use pyo3::exceptions::{PyBaseExceptionGroup, PyRuntimeError};
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, Python};
use pyo3::types::{PyAny, PyDict, PyList};
use vfy_core::test_execution::ExecutionOutcome;
use vfy_core::test_plan::TestBody;

pub(super) fn run_test_finalizers(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> Option<TeardownFailure> {
    let owner = runtime.lock().suite.tests()[test_index].nodeid.clone();
    run_owner_finalizers(py, runtime, &owner, Some(test_index))
}

fn run_owner_finalizers(
    py: Python<'_>,
    runtime: &SharedRuntime,
    owner: &str,
    test_index: Option<usize>,
) -> Option<TeardownFailure> {
    let (finalizers, node, package_setup) = {
        let mut state = runtime.lock();
        if state
            .active_function
            .is_some_and(|test| state.suite.tests()[test.index()].nodeid == owner)
        {
            state.active_function = None;
        }
        if let Some(position) = state.active_nodes.iter().position(|node| node == owner) {
            state.active_nodes.truncate(position);
        }
        (
            state.owner_finalizers.remove(owner).unwrap_or_default(),
            state.owner_nodes.remove(owner),
            state.package_setups.remove(owner),
        )
    };
    let failure = run_finalizers(py, runtime, owner, node.as_ref(), test_index, finalizers);
    // Keep a cached setup exception alive through its owner's teardown, as pytest does.
    drop(package_setup);
    failure
}

pub(super) fn run_item_teardown(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> PyResult<Option<TeardownFailure>> {
    let body = runtime.lock().suite.tests()[test_index].body;
    let item = case_item(py, runtime, test_index)?;
    if body == TestBody::Doctest {
        return Ok(item
            .bind(py)
            .call_method0(intern!(py, "teardown"))
            .map(|_| ())
            .map_err(|error| TeardownFailure::from_pyerr(py, &error))
            .err());
    }
    if body != TestBody::Unittest {
        return Ok(None);
    }
    let state = item
        .bind(py)
        .getattr(intern!(py, "__dict__"))?
        .cast_into::<PyDict>()?;
    // Pytest tears down only items that entered setup. A skip marker raises
    // before `prepare_unittest_item`, while collection or skip evaluation can
    // already have bound `_instance`.
    if state.get_item(intern!(py, "_explicit_tearDown"))?.is_none() {
        return Ok(None);
    }
    let has_instance = state
        .get_item(intern!(py, "_instance"))?
        .is_some_and(|instance| !instance.is_none());
    let has_testcase = state
        .get_item(intern!(py, "_testcase"))?
        .is_some_and(|testcase| !testcase.is_none());
    if !has_instance && !has_testcase {
        return Ok(None);
    }
    let item_failure = item
        .bind(py)
        .call_method0(intern!(py, "teardown"))
        .map(|_| ())
        .map_err(|error| TeardownFailure::from_pyerr(py, &error))
        .err();
    let reported_failure = take_unittest_exception(item.bind(py))?
        .map(|error| TeardownFailure::from_pyerr(py, &error));
    Ok(merge_errors(py, item_failure, reported_failure))
}

pub(super) fn teardown_failed_item(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> PyResult<Option<TeardownFailure>> {
    let body = runtime.lock().suite.tests()[test_index].body;
    let mut failure = run_test_finalizers(py, runtime, test_index);
    if body != TestBody::Unittest {
        return Ok(merge_errors(py, failure, teardown_all(py, runtime)));
    }
    failure = merge_errors(py, failure, run_item_teardown(py, runtime, test_index)?);
    Ok(merge_errors(py, failure, teardown_all(py, runtime)))
}

pub(super) fn plan_teardown_for_next(
    py: Python<'_>,
    runtime: &SharedRuntime,
    next_test: usize,
) -> PyResult<Vec<String>> {
    let (active, next) = {
        let state = runtime.lock();
        let next = state.suite.tests().get(next_test).map(|test| {
            (
                state.suite.test_bindings[next_test].item.clone_ref(py),
                test.nodeid.clone(),
            )
        });
        let active = state
            .active_nodes
            .iter()
            .map(|owner| {
                (
                    owner.clone(),
                    state.owner_nodes.get(owner).map(|node| node.clone_ref(py)),
                )
            })
            .collect::<Vec<_>>();
        (active, next)
    };
    let Some((next_item, next_nodeid)) = next else {
        return Ok(active.into_iter().rev().map(|(owner, _)| owner).collect());
    };
    let mut needed = next_item
        .bind(py)
        .call_method0("listchain")?
        .try_iter()?
        .map(|node| {
            let node = node?;
            Ok((node.getattr("nodeid")?.extract::<String>()?, node.unbind()))
        })
        .collect::<PyResult<Vec<_>>>()?;
    if let Some((last, _)) = needed.last_mut() {
        last.clone_from(&next_nodeid);
    }
    let common = active
        .iter()
        .zip(&needed)
        .take_while(|((owner, current), (next_owner, next))| {
            owner == next_owner
                && current
                    .as_ref()
                    .is_some_and(|current| current.bind(py).is(next.bind(py)))
        })
        .count();
    Ok(active
        .into_iter()
        .skip(common)
        .rev()
        .map(|(owner, _)| owner)
        .collect())
}

pub(super) fn teardown_report_outcome(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    failure: Option<&TeardownFailure>,
) -> PyResult<(ExecutionOutcome, Option<String>)> {
    let Some(failure) = failure else {
        return Ok((ExecutionOutcome::Passed, None));
    };
    let disabled = runtime.lock().suite.test_bindings[test_index].xfail_mode == XfailMode::Disabled;
    let outcome = match failure.outcome {
        TeardownOutcome::Error => ExecutionOutcome::Error,
        TeardownOutcome::Skipped => ExecutionOutcome::Skipped,
        TeardownOutcome::Xfailed if disabled => ExecutionOutcome::Error,
        TeardownOutcome::Xfailed => ExecutionOutcome::XFailed,
    };
    let xfail = {
        let state = runtime.lock();
        let bindings = &state.suite.test_bindings[test_index];
        if bindings.xfail_mode == XfailMode::Run {
            None
        } else {
            bindings.xfail.as_ref().map(|xfail| xfail.clone_ref(py))
        }
    };
    if outcome == ExecutionOutcome::Error
        && let Some(xfail) = xfail
        && let Some(error) = &failure.exception
        && xfail_matches_error(py, error, &xfail)?
    {
        return Ok((ExecutionOutcome::XFailed, Some(xfail.reason)));
    }
    Ok((outcome, None))
}

pub(super) fn teardown_all(py: Python<'_>, runtime: &SharedRuntime) -> Option<TeardownFailure> {
    let owners = runtime.lock().active_nodes.iter().rev().cloned().collect();
    teardown_owner_nodes(py, runtime, owners)
}

pub(super) fn teardown_owner_nodes(
    py: Python<'_>,
    runtime: &SharedRuntime,
    owners: Vec<String>,
) -> Option<TeardownFailure> {
    let mut failure = None;
    for owner in owners {
        let owner_failure = run_owner_finalizers(py, runtime, &owner, None);
        if owner_failure
            .as_ref()
            .is_some_and(|failure| failure.escapes_owner(py))
        {
            return owner_failure;
        }
        failure = merge_errors(py, failure, owner_failure);
    }
    failure
}

pub(super) fn teardown_keys(
    py: Python<'_>,
    runtime: &SharedRuntime,
    keys: Vec<CacheKey>,
    reason: FixtureTeardownReason,
) -> Option<TeardownFailure> {
    let mut failures = FixtureTeardownFailures {
        fixture_errors: runtime.lock().compatibility.fixture_finalizer_errors,
        invalidated: (reason != FixtureTeardownReason::OwnerFinished)
            .then(|| keys.last().cloned())
            .flatten(),
        errors: Vec::new(),
        owners: Vec::new(),
        invalidated_owner: None,
    };
    for key in keys {
        let instance = {
            let mut state = runtime.lock();
            let owner = state
                .cache
                .get(&key)
                .and_then(|instance| instance.registered_owner.as_ref())
                .cloned();
            state.cache.get_mut(&key).map(|instance| {
                (
                    std::mem::take(&mut instance.finalizers),
                    instance.retained_request_test_index,
                    owner,
                )
            })
        };
        if let Some((mut finalizers, retained_request_test_index, owner)) = instance {
            finalizers.extend(
                runtime
                    .lock()
                    .pending_finalizers
                    .remove(&key)
                    .unwrap_or_default(),
            );
            let failure = run_fixture_finalizers(py, runtime, &key, finalizers);
            let escaped = match failure {
                Some(failure)
                    if reason == FixtureTeardownReason::OwnerFinished
                        && failure.escapes_owner(py) =>
                {
                    Some(failure)
                }
                Some(failure) => {
                    failures.record(py, runtime, &key, failure);
                    None
                }
                None => None,
            };
            let removed = {
                let mut state = runtime.lock();
                let removed = state.cache.remove(&key);
                if state.active_by_fixture.get(&key.identity.fixture) == Some(&key) {
                    state.active_by_fixture.remove(&key.identity.fixture);
                }
                if reason == FixtureTeardownReason::ParameterSwitch
                    && !state.compatibility.legacy_fixture_order
                    && let Some(finalizers) = owner
                        .as_ref()
                        .and_then(|owner| state.owner_finalizers.get_mut(owner))
                {
                    finalizers.retain(|finalizer| {
                        !matches!(finalizer, OwnerFinalizer::Fixture(registered) if registered == &key)
                    });
                }
                if state.active.last() == Some(&key) {
                    state.active.pop();
                } else {
                    state.active.retain(|active| active != &key);
                }
                removed
            };
            // Releasing Python values can invoke __del__, which may resolve fixtures.
            drop(removed);
            if let Some(test_index) = retained_request_test_index {
                release_retained_request(runtime, test_index);
            }
            if let Some(failure) = escaped {
                abandon_fixture_owner(runtime, owner.as_deref());
                return Some(failure);
            }
        }
    }
    failures.finish(py)
}

struct FixtureTeardownFailures {
    fixture_errors: FixtureFinalizerErrors,
    invalidated: Option<CacheKey>,
    errors: Vec<PyErr>,
    owners: Vec<(Py<PyAny>, Vec<PyErr>)>,
    invalidated_owner: Option<(String, Py<PyAny>)>,
}

impl FixtureTeardownFailures {
    fn record(
        &mut self,
        py: Python<'_>,
        runtime: &SharedRuntime,
        key: &CacheKey,
        failure: TeardownFailure,
    ) {
        if let Some(root) = &self.invalidated {
            if self.invalidated_owner.is_none() {
                self.invalidated_owner = fixture_teardown_node(py, runtime, root).ok();
            }
            let flatten = root == key && failure.grouping == TeardownGrouping::Fixture;
            let error = failure.into_pyerr(py);
            if flatten {
                match error
                    .value(py)
                    .getattr("exceptions")
                    .and_then(|members| members.extract::<Vec<Py<PyAny>>>())
                {
                    Ok(members) => self.errors.extend(
                        members
                            .into_iter()
                            .rev()
                            .map(|error| PyErr::from_value(error.into_bound(py))),
                    ),
                    Err(error) => self.errors.push(error),
                }
            } else {
                self.errors.push(error);
            }
        } else {
            match fixture_teardown_node(py, runtime, key) {
                Ok((_, node)) => {
                    if let Some((_, errors)) = self
                        .owners
                        .iter_mut()
                        .find(|(owner, _)| owner.bind(py).is(node.bind(py)))
                    {
                        errors.push(failure.into_pyerr(py));
                    } else {
                        self.owners.push((node, vec![failure.into_pyerr(py)]));
                    }
                }
                Err(error) => self.errors.push(error),
            }
        }
    }

    fn finish(mut self, py: Python<'_>) -> Option<TeardownFailure> {
        if self.invalidated.is_some() {
            self.fixture_errors.select(&mut self.errors);
            return finalizer_failure(py, self.errors, TeardownGrouping::Fixture, || {
                let (name, node) = self.invalidated_owner.ok_or_else(|| {
                    PyRuntimeError::new_err("invalidated fixture has no teardown owner")
                })?;
                Ok(format!(
                    "errors while tearing down fixture \"{name}\" of {}",
                    node.bind(py).str()?
                ))
            });
        }
        let mut failure = finalizer_failure(py, self.errors, TeardownGrouping::Owner, || {
            Ok("errors during test teardown".to_owned())
        });
        for (node, errors) in self.owners {
            let owner_failure = finalizer_failure(py, errors, TeardownGrouping::Owner, || {
                Ok(format!(
                    "errors while tearing down {}",
                    node.bind(py).repr()?
                ))
            });
            failure = merge_errors(py, failure, owner_failure);
        }
        failure
    }
}

fn abandon_fixture_owner(runtime: &SharedRuntime, nodeid: Option<&str>) {
    let Some(nodeid) = nodeid else {
        return;
    };
    let mut state = runtime.lock();
    for instance in state.cache.values_mut() {
        if instance
            .registered_owner
            .as_ref()
            .is_some_and(|owner| owner == nodeid)
        {
            instance.registered_owner = None;
        }
    }
}

fn release_retained_request(runtime: &SharedRuntime, test_index: usize) {
    let mut state = runtime.lock();
    let Some(count) = state.retained_request_counts.get_mut(&test_index) else {
        return;
    };
    *count -= 1;
    if *count == 0 {
        state.retained_request_counts.remove(&test_index);
        state.resolved_fixture_keys.remove(&test_index);
    }
}

fn run_fixture_finalizers(
    py: Python<'_>,
    runtime: &SharedRuntime,
    key: &CacheKey,
    mut finalizers: Vec<FixtureFinalizer>,
) -> Option<TeardownFailure> {
    let mut errors = Vec::new();
    while let Some(finalizer) = finalizers.pop() {
        let result = match finalizer {
            FixtureFinalizer::Dependent(dependent) => {
                let key = runtime.lock().active_by_fixture.get(&dependent).cloned();
                if let Some(key) = key {
                    match teardown_keys(
                        py,
                        runtime,
                        vec![key],
                        FixtureTeardownReason::DependencyFinished,
                    ) {
                        Some(failure) => Err(failure.into_pyerr(py)),
                        None => Ok(()),
                    }
                } else {
                    Ok(())
                }
            }
            // Drop ignored return values before draining finalizers registered by their destructors.
            FixtureFinalizer::Callable(finalizer) => finalizer.bind(py).call0().map(|_| ()),
            FixtureFinalizer::Generator { fixture, generator } => {
                match generator.bind(py).call_method0(intern!(py, "__next__")) {
                    Err(error) if error.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                        Ok(())
                    }
                    Ok(_) => match multiple_yield_fixture_error(py, fixture.bind(py)) {
                        Ok(error) | Err(error) => Err(error),
                    },
                    Err(error) => Err(error),
                }
            }
        };
        if let Err(error) = result {
            errors.push(error);
        }
        finalizers.extend(
            runtime
                .lock()
                .pending_finalizers
                .remove(key)
                .unwrap_or_default(),
        );
    }
    let error_policy = runtime.lock().compatibility.fixture_finalizer_errors;
    error_policy.select(&mut errors);
    finalizer_failure(py, errors, TeardownGrouping::Fixture, || {
        let (name, node) = fixture_teardown_node(py, runtime, key)?;
        Ok(format!(
            "errors while tearing down fixture \"{name}\" of {}",
            node.bind(py).str()?
        ))
    })
}

fn finalizer_failure(
    py: Python<'_>,
    mut errors: Vec<PyErr>,
    grouping: TeardownGrouping,
    message: impl FnOnce() -> PyResult<String>,
) -> Option<TeardownFailure> {
    if errors.is_empty() {
        return None;
    }
    let multiple = errors.len() > 1;
    let error = if multiple {
        let grouped = (|| -> PyResult<PyErr> {
            let members = errors.iter().rev().map(|error| error.value(py));
            Ok(PyBaseExceptionGroup::new_err((
                message()?,
                PyList::new(py, members)?.unbind(),
            )))
        })();
        grouped.unwrap_or_else(|error| error)
    } else {
        errors.pop()?
    };
    let mut failure = TeardownFailure::from_pyerr(py, &error);
    if multiple {
        failure.grouping = grouping;
    }
    Some(failure)
}

fn fixture_teardown_node(
    py: Python<'_>,
    runtime: &SharedRuntime,
    key: &CacheKey,
) -> PyResult<(String, Py<PyAny>)> {
    let (test_index, name, package_baseid) = {
        let state = runtime.lock();
        let instance = state
            .cache
            .get(key)
            .ok_or_else(|| PyRuntimeError::new_err("finalizing fixture has no cached instance"))?;
        let fixture = &state.suite.fixtures()[key.identity.fixture.index()];
        (
            instance.last_resolved_test,
            fixture.name.clone(),
            fixture.package_baseid.clone(),
        )
    };
    let mut request = NativeRequest::for_test(
        runtime,
        case_item(py, runtime, test_index)?,
        test_index,
        FixtureStack::default(),
    );
    request.requesting_scope = key.identity.scope;
    request.package_baseid = Some(package_baseid);
    Ok((name, request.node(py)?))
}

fn multiple_yield_fixture_error(py: Python<'_>, fixture: &Bound<'_, PyAny>) -> PyResult<PyErr> {
    let fixtures = py.import("_pytest.fixtures")?;
    let source = fixtures
        .getattr("Source")?
        .call1((fixture,))?
        .call_method0("indent")?
        .str()?
        .to_string();
    let location = fixtures.getattr("getfslineno")?.call1((fixture,))?;
    let path = location.get_item(0)?.str()?.to_string();
    let lineno = location.get_item(1)?.extract::<i64>()?;
    let message = format!(
        "fixture function has more than one 'yield':\n\n{source}\n{path}:{}",
        lineno + 1
    );
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    let error = py
        .import("_pytest.outcomes")?
        .getattr("Failed")?
        .call((message,), Some(&kwargs))?;
    Ok(PyErr::from_value(error))
}

fn run_finalizers(
    py: Python<'_>,
    runtime: &SharedRuntime,
    owner: &str,
    node: Option<&Py<PyAny>>,
    test_index: Option<usize>,
    mut finalizers: Vec<OwnerFinalizer>,
) -> Option<TeardownFailure> {
    let mut errors = Vec::new();
    while let Some(finalizer) = finalizers.pop() {
        match finalizer {
            OwnerFinalizer::Callable(finalizer) => {
                if let Err(error) = finalizer.bind(py).call0() {
                    let failure = TeardownFailure::from_pyerr(py, &error);
                    if failure.escapes_owner(py) {
                        abandon_fixture_owner(runtime, Some(owner));
                        return Some(TeardownFailure::from_pyerr(py, &error));
                    }
                    errors.push(error);
                }
            }
            OwnerFinalizer::Fixture(key) => {
                if let Some(failure) =
                    teardown_keys(py, runtime, vec![key], FixtureTeardownReason::OwnerFinished)
                {
                    if failure.escapes_owner(py) {
                        abandon_fixture_owner(runtime, Some(owner));
                        return Some(failure);
                    }
                    errors.push(failure.into_pyerr(py));
                }
            }
        }
        finalizers.extend(
            runtime
                .lock()
                .owner_finalizers
                .remove(owner)
                .unwrap_or_default(),
        );
    }
    finalizer_failure(py, errors, TeardownGrouping::Owner, || {
        let item = if let Some(test_index) = test_index {
            case_item(py, runtime, test_index)?
        } else {
            node.map(|node| node.clone_ref(py))
                .ok_or_else(|| PyRuntimeError::new_err("finalizing node has no Python binding"))?
        };
        Ok(format!(
            "errors while tearing down {}",
            item.bind(py).repr()?
        ))
    })
}
