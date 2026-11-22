//! Owns fixture resolution, selection, and setup execution.

mod cache_key;
mod dynamic;
mod execution;
mod owner;
mod selection;

use self::cache_key::cache_key_for_use;
use self::dynamic::record_resolved_fixture_name;
use self::execution::{
    execute_fixture_setup, fixture_callable_for_setup,
    register_latest_fixture_dependency_finalizer, resolve_fixture_runner,
};
use self::owner::{fixture_owner_is_active, inactive_fixture_owner_error, register_fixture_owner};
use self::selection::{active_fixture_outcome, select_requested_fixture};
use super::compile::case_item;
use super::fixture_lookup::{FixtureScopeMismatch, fixture_scope_mismatch};
use super::model::{
    CacheKey, DynamicFixtureTeardown, FixtureCallable, FixtureScopeOverrideLookup, FixtureStack,
    FixtureUse, package_scope_node, parse_fixture_scope,
};
use super::report::is_skip_error;
use super::request::NativeRequest;
use super::runtime::{
    FixtureFinalizer, FixtureInstance, FixtureOutcome, ResolvingFixture, SharedRuntime,
};
use crate::dense_reader::DenseCallSpecReader;
use pyo3::exceptions::{PyAssertionError, PyKeyError, PyRuntimeError};
use pyo3::intern;
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, Python};
use pyo3::types::{PyAny, PyDict};
use vfy_core::test_plan::FixtureExecution;
use vfy_core::{FixtureId, FixtureScope};

pub(super) struct FixtureSetupPlan {
    fixture_use: FixtureUse,
    key: CacheKey,
    fixture_stack: FixtureStack,
    name: String,
    package_baseid: String,
    pub(super) definition: Py<PyAny>,
    request_scope: FixtureScope,
    retained_request_test_index: Option<usize>,
    arguments: Vec<(String, Py<PyAny>)>,
    declared_dependency_count: usize,
    async_runner: Option<Py<PyAny>>,
    callable: FixtureCallable,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum FixtureRequestOrigin {
    Declared,
    Dynamic,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DynamicNameRecording {
    Record,
    Skip,
}

#[derive(Clone, Copy)]
pub(super) struct FixtureResolution {
    origin: FixtureRequestOrigin,
    // Dynamic requests expose transitive fixture names, but invalidation edges
    // still follow each fixture's declared arguments.
    dynamic_names: DynamicNameRecording,
}

impl FixtureResolution {
    pub(super) const DECLARED: Self = Self::declared(DynamicNameRecording::Skip);
    pub(super) const DYNAMIC: Self = Self {
        origin: FixtureRequestOrigin::Dynamic,
        dynamic_names: DynamicNameRecording::Record,
    };

    const fn declared(dynamic_names: DynamicNameRecording) -> Self {
        Self {
            origin: FixtureRequestOrigin::Declared,
            dynamic_names,
        }
    }
}

pub(super) fn fixture_request(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
) -> PyResult<Py<PyAny>> {
    Py::new(
        py,
        NativeRequest {
            runtime: runtime.downgrade(),
            resolved_fixture_keys: runtime.fixture_keys(test_index),
            item: Some(case_item(py, runtime, test_index)?),
            package_baseid: Some(plan.package_baseid.clone()),
            test_index,
            target: Some(plan.key.clone()),
            requesting_scope: plan.request_scope,
            fixture_name: Some(plan.name.clone()),
            param: plan
                .fixture_use
                .param
                .as_ref()
                .map(|value| value.clone_ref(py)),
            fixture_stack: plan.fixture_stack.clone(),
        },
    )
    .map(Py::into_any)
}

struct SelectedFixture {
    fixture_use: FixtureUse,
    request_scope: FixtureScope,
    same_name_depth: usize,
}

pub(super) struct LiveFixtureParameter {
    pub(super) value: Py<PyAny>,
    cache_scope: Option<FixtureScope>,
}

pub(super) fn live_callspec_parameter(
    item: &Bound<'_, PyAny>,
    name: &str,
    scope_lookup: FixtureScopeOverrideLookup,
    reader: Option<&DenseCallSpecReader>,
) -> PyResult<Option<LiveFixtureParameter>> {
    let Some(callspec) = item.getattr_opt(intern!(item.py(), "callspec"))? else {
        return Ok(None);
    };
    if callspec.is_none() {
        return Ok(None);
    }
    let dense_fields = reader
        .map(|reader| reader.parameter_fields(&callspec, name))
        .transpose()?
        .flatten();
    let value = if let Some(fields) = dense_fields {
        let Some(value) = fields.value else {
            return Ok(None);
        };
        if !fields.index_present {
            return Err(PyKeyError::new_err(name.to_owned()));
        }
        value
    } else {
        let params = callspec.getattr(intern!(item.py(), "params"))?;
        let value = if params.is_exact_instance_of::<PyDict>() {
            params
                .cast::<PyDict>()?
                .get_item(name)
                .map(|value| value.map(Bound::unbind))?
        } else if params.contains(name)? {
            Some(params.get_item(name)?.unbind())
        } else {
            None
        };
        let Some(value) = value else {
            return Ok(None);
        };
        callspec
            .getattr(intern!(item.py(), "indices"))?
            .get_item(name)?;
        value
    };
    let scope = match callspec
        .getattr(intern!(item.py(), "_arg2scope"))?
        .get_item(name)
    {
        Ok(scope) => Some(scope),
        Err(error)
            if matches!(scope_lookup, FixtureScopeOverrideLookup::Optional)
                && error.is_instance_of::<PyKeyError>(item.py()) =>
        {
            None
        }
        Err(error) => return Err(error),
    };
    Ok(Some(LiveFixtureParameter {
        value,
        cache_scope: scope
            .map(|scope| {
                parse_fixture_scope(
                    &scope
                        .getattr(intern!(item.py(), "value"))?
                        .extract::<String>()?,
                )
            })
            .transpose()?,
    }))
}

fn begin_fixture_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    fixture_use: FixtureUse,
    request_scope: FixtureScope,
    requesting_stack: &FixtureStack,
    resolution: FixtureResolution,
) -> PyResult<FixtureSetupPlan> {
    let fixture_stack = requesting_stack.push(fixture_use.fixture.index());
    let (name, dependencies, package_baseid, definition, legacy_setup_order) = {
        let mut state = runtime.lock();
        let plan = &state.suite.fixtures()[fixture_use.fixture.index()];
        let result = (
            plan.name.clone(),
            plan.dependencies.clone(),
            plan.package_baseid.clone(),
            state.suite.fixture_bindings[fixture_use.fixture.index()]
                .definition
                .clone_ref(py),
            state.compatibility.legacy_fixture_order,
        );
        state.resolving.push(ResolvingFixture {
            fixture: fixture_use.fixture.index(),
            fixture_stack: fixture_stack.clone(),
            dependencies: Vec::new(),
        });
        result
    };
    let prepared = (|| {
        // A changed dependency can finalize this fixture. Compare its parameter
        // only after those invalidations, as FixtureDef.execute does.
        let mut arguments = Vec::with_capacity(dependencies.len());
        for dependency in dependencies.iter().filter(|name| *name != "request") {
            let value = resolve_named_fixture(
                py,
                runtime,
                test_index,
                dependency,
                Some(request_scope),
                Some(fixture_stack.clone()),
                FixtureResolution::declared(resolution.dynamic_names),
            )?;
            if legacy_setup_order {
                // Before pytest 8.2, each dependency receives its finish callback
                // before the next dependency or parameter-switch teardown runs.
                register_latest_fixture_dependency_finalizer(runtime, fixture_use.fixture);
            }
            arguments.push((dependency.clone(), value));
        }
        let declared_dependency_count = runtime
            .lock()
            .resolving
            .last()
            .map_or(0, |fixture| fixture.dependencies.len());
        let callable = fixture_callable_for_setup(
            py,
            runtime,
            test_index,
            fixture_use.fixture,
            definition.bind(py),
        )?;
        let async_runner = resolve_fixture_runner(
            py,
            runtime,
            test_index,
            callable.async_loop_scope,
            request_scope,
            &fixture_stack,
            FixtureResolution::declared(resolution.dynamic_names),
        )?;
        let key = cache_key_for_use(py, runtime, &fixture_use)?;
        let request_position = dependencies.iter().position(|name| name == "request");
        let mut plan = FixtureSetupPlan {
            fixture_use,
            key,
            fixture_stack,
            name,
            package_baseid,
            definition,
            request_scope,
            retained_request_test_index: (request_scope != FixtureScope::Function
                && request_position.is_some())
            .then_some(test_index),
            arguments,
            declared_dependency_count,
            async_runner,
            callable,
        };
        add_fixture_request_argument(py, runtime, test_index, &mut plan, request_position)?;
        Ok(plan)
    })();
    let mut state = runtime.lock();
    match &prepared {
        Ok(plan) if resolution.origin == FixtureRequestOrigin::Declared => {
            let parent_index = state.resolving.len().checked_sub(2);
            if let Some(parent) = parent_index.and_then(|index| state.resolving.get_mut(index)) {
                parent.dependencies.push(plan.key.clone());
            }
        }
        Err(_) => {
            state.resolving.pop();
        }
        Ok(_) => {}
    }
    drop(state);
    prepared
}

fn add_fixture_request_argument(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &mut FixtureSetupPlan,
    request_position: Option<usize>,
) -> PyResult<()> {
    if plan.callable.execution != FixtureExecution::DirectParameter
        && let Some(position) = request_position
        && !runtime.lock().cache.contains_key(&plan.key)
    {
        let request = fixture_request(py, runtime, test_index, plan)?;
        plan.arguments
            .insert(position, ("request".to_owned(), request));
    }
    Ok(())
}

fn mark_fixture_skip_location(
    py: Python<'_>,
    setup: &PyResult<(Py<PyAny>, Option<FixtureFinalizer>)>,
) -> PyResult<()> {
    if let Err(error) = setup
        && is_skip_error(py, error)
    {
        error.value(py).setattr("_use_item_location", true)?;
    }
    Ok(())
}

fn cached_fixture_outcome(
    py: Python<'_>,
    runtime: &SharedRuntime,
    fixture: FixtureId,
) -> PyResult<Option<(CacheKey, PyResult<Py<PyAny>>)>> {
    let state = runtime.lock();
    // Pytest requests remember a FixtureDef, whose cache can hold a later
    // instance when an escaped request is used by another test.
    let Some(key) = state.active_by_fixture.get(&fixture) else {
        return Ok(None);
    };
    let instance = state
        .cache
        .get(key)
        .ok_or_else(|| PyRuntimeError::new_err("active fixture has no cached instance"))?;
    let cached = (key.clone(), instance.outcome.clone_ref(py));
    let restore_traceback = state.compatibility.cached_fixture_error_traceback;
    drop(state);
    Ok(Some((
        cached.0,
        cached.1.clone_result(py, restore_traceback),
    )))
}

fn finalized_fixture_error(name: &str) -> PyErr {
    PyAssertionError::new_err(format!(
        "The fixture value for \"{name}\" is not available.  This can happen when the fixture has already been torn down."
    ))
}

fn check_cached_fixture_scope(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    fixture: FixtureId,
    requesting_scope: Option<FixtureScope>,
    fixture_stack: &FixtureStack,
) -> PyResult<()> {
    let Some(requesting_scope) = requesting_scope else {
        return Ok(());
    };
    let mismatch = {
        let state = runtime.lock();
        let scope = state.suite.fixtures()[fixture.index()].declared_scope;
        state
            .compatibility
            .scope_checks
            .checked_scope(scope, scope, true)
            .filter(|scope| requesting_scope.rank() > scope.rank())
            .map(|scope| {
                (
                    scope,
                    state.suite.fixture_bindings[fixture.index()]
                        .definition
                        .clone_ref(py),
                )
            })
    };
    if let Some((scope, definition)) = mismatch {
        return Err(fixture_scope_mismatch(
            py,
            runtime,
            test_index,
            FixtureScopeMismatch {
                name,
                requesting_scope,
                fixture_scope: scope,
                fixture_definition: &definition,
                fixture_stack,
            },
        )?);
    }
    Ok(())
}

fn check_teardown_fixture_lookup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    requesting_scope: Option<FixtureScope>,
    fixture_stack: &FixtureStack,
    fixture_use: &FixtureUse,
) -> PyResult<()> {
    let scope = requesting_scope.unwrap_or(FixtureScope::Function);
    let (warn, planned_owner, package_baseid) = {
        let state = runtime.lock();
        (
            matches!(
                state.compatibility.dynamic_fixture_teardown,
                DynamicFixtureTeardown::WarnAndCheckOwner
            ),
            state.suite.tests()[test_index]
                .scope_owner(scope)
                .unwrap_or_default()
                .to_owned(),
            fixture_stack
                .fixture_indices()
                .last()
                .map(|&index| state.suite.fixtures()[index].package_baseid.clone()),
        )
    };
    if !warn {
        return Ok(());
    }
    let item = case_item(py, runtime, test_index)?;
    let owner = if scope == FixtureScope::Package {
        package_scope_node(
            py,
            item.bind(py),
            package_baseid.as_deref().unwrap_or_default(),
        )?
        .bind(py)
        .getattr("nodeid")?
        .extract::<String>()?
    } else {
        planned_owner
    };
    let request_active = fixture_owner_is_active(
        py,
        runtime,
        test_index,
        &owner,
        scope,
        package_baseid.as_deref(),
    )?;
    if request_active {
        return Ok(());
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item("argname", name)?;
    let warning = py
        .import("_pytest.fixtures")?
        .getattr("FIXTURE_GETFIXTUREVALUE_DURING_TEARDOWN")?
        .call_method("format", (), Some(&kwargs))?;
    py.import("warnings")?.getattr("warn")?.call1((warning,))?;
    let fixture_package_baseid = runtime.lock().suite.fixtures()[fixture_use.fixture.index()]
        .package_baseid
        .clone();
    if fixture_owner_is_active(
        py,
        runtime,
        test_index,
        &fixture_use.owner,
        fixture_use.cache_scope,
        Some(&fixture_package_baseid),
    )? {
        return Ok(());
    }
    let message = format!(
        "The fixture value for \"{name}\" is not available during teardown because it was not previously requested.\nOnly fixtures that were already active can be retrieved during teardown.\nRequest the fixture before teardown begins by declaring it in the fixture signature or by calling request.getfixturevalue() before the fixture yields."
    );
    let request = Py::new(
        py,
        NativeRequest::for_test(runtime, item, test_index, fixture_stack.clone()),
    )?;
    let error = py
        .import("_pytest.fixtures")?
        .getattr("FixtureLookupError")?
        .call1((name, request, message))?;
    Err(PyErr::from_value(error))
}

pub(super) fn resolve_named_fixture(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    requesting_scope: Option<FixtureScope>,
    requesting_stack: Option<FixtureStack>,
    resolution: FixtureResolution,
) -> PyResult<Py<PyAny>> {
    let requesting_stack = requesting_stack.unwrap_or_else(|| runtime.current_fixture_stack());
    let resolved_key = runtime
        .lock()
        .resolved_fixture_keys
        .get(&test_index)
        .and_then(|fixtures| fixtures.get(name));
    if let Some(previous_key) = resolved_key {
        check_cached_fixture_scope(
            py,
            runtime,
            test_index,
            name,
            previous_key.identity.fixture,
            requesting_scope,
            &requesting_stack,
        )?;
        let (key, outcome) = cached_fixture_outcome(py, runtime, previous_key.identity.fixture)?
            .ok_or_else(|| finalized_fixture_error(name))?;
        if resolution.origin == FixtureRequestOrigin::Declared
            && let Some(parent) = runtime.lock().resolving.last_mut()
        {
            parent.dependencies.push(key);
        }
        if resolution.dynamic_names == DynamicNameRecording::Record && outcome.is_ok() {
            record_resolved_fixture_name(runtime, test_index, name);
        }
        return outcome;
    }
    let selected = select_requested_fixture(
        py,
        runtime,
        test_index,
        name,
        requesting_scope,
        &requesting_stack,
        resolution.origin,
    )?;
    check_teardown_fixture_lookup(
        py,
        runtime,
        test_index,
        name,
        requesting_scope,
        &requesting_stack,
        &selected.fixture_use,
    )?;
    if let Some((key, outcome)) = active_fixture_outcome(
        py,
        runtime,
        test_index,
        name,
        &selected.fixture_use,
        selected.same_name_depth,
    )? {
        if resolution.origin == FixtureRequestOrigin::Declared
            && let Some(parent) = runtime.lock().resolving.last_mut()
        {
            parent.dependencies.push(key.clone());
        }
        if outcome.is_ok() {
            runtime
                .lock()
                .resolved_fixture_keys
                .entry(test_index)
                .or_default()
                .insert(name, key);
        }
        if resolution.dynamic_names == DynamicNameRecording::Record && outcome.is_ok() {
            record_resolved_fixture_name(runtime, test_index, name);
        }
        return outcome;
    }
    let setup_plan = begin_fixture_setup(
        py,
        runtime,
        test_index,
        selected.fixture_use,
        selected.request_scope,
        &requesting_stack,
        resolution,
    )?;
    let setup = execute_fixture_setup(py, runtime, test_index, &setup_plan);
    runtime.lock().resolving.pop();
    complete_fixture_setup(
        py,
        runtime,
        test_index,
        &setup_plan,
        setup,
        resolution.dynamic_names,
    )
}

fn complete_fixture_setup(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    setup_plan: &FixtureSetupPlan,
    setup: PyResult<(Py<PyAny>, Option<FixtureFinalizer>)>,
    dynamic_names: DynamicNameRecording,
) -> PyResult<Py<PyAny>> {
    mark_fixture_skip_location(py, &setup)?;
    let (outcome, finalizer) = FixtureOutcome::from_setup(py, setup);
    let returned = match &outcome {
        FixtureOutcome::Value(value) => Ok(value.clone_ref(py)),
        FixtureOutcome::Error { exception, .. } => {
            Err(PyErr::from_value(exception.bind(py).clone()))
        }
    };
    let successful = returned.is_ok();
    let owner_active = fixture_owner_is_active(
        py,
        runtime,
        test_index,
        &setup_plan.fixture_use.owner,
        setup_plan.fixture_use.cache_scope,
        Some(&setup_plan.package_baseid),
    )?;
    let mut state = runtime.lock();
    let legacy_setup_order = state.compatibility.legacy_fixture_order;
    if let Some(instance) = state.cache.get_mut(&setup_plan.key) {
        if successful {
            instance.last_resolved_test = test_index;
        }
        let owner_registered =
            !legacy_setup_order || register_fixture_owner(&mut state, setup_plan, owner_active);
        if owner_registered && successful {
            state
                .resolved_fixture_keys
                .entry(test_index)
                .or_default()
                .insert(&setup_plan.name, setup_plan.key.clone());
        }
        drop(state);
        if !owner_registered {
            return inactive_fixture_owner_error(py, runtime, test_index, setup_plan);
        }
        if dynamic_names == DynamicNameRecording::Record && successful {
            record_resolved_fixture_name(runtime, test_index, &setup_plan.name);
        }
        return returned;
    }
    let mut finalizers = state
        .pending_finalizers
        .remove(&setup_plan.key)
        .unwrap_or_default();
    if let Some(finalizer) = finalizer {
        finalizers.push(finalizer);
    }
    state.cache.insert(
        setup_plan.key.clone(),
        FixtureInstance {
            outcome,
            finalizers,
            param: setup_plan
                .fixture_use
                .param
                .as_ref()
                .map(|param| param.clone_ref(py)),
            last_resolved_test: test_index,
            retained_request_test_index: setup_plan.retained_request_test_index,
            registered_owner: None,
        },
    );
    if let Some(retained_test_index) = setup_plan.retained_request_test_index {
        *state
            .retained_request_counts
            .entry(retained_test_index)
            .or_default() += 1;
    }
    let owner_registered = register_fixture_owner(&mut state, setup_plan, owner_active);
    if owner_registered && successful {
        state
            .resolved_fixture_keys
            .entry(test_index)
            .or_default()
            .insert(&setup_plan.name, setup_plan.key.clone());
    }
    state.active.push(setup_plan.key.clone());
    state
        .active_by_fixture
        .insert(setup_plan.fixture_use.fixture, setup_plan.key.clone());
    drop(state);
    if !owner_registered {
        return inactive_fixture_owner_error(py, runtime, test_index, setup_plan);
    }
    if dynamic_names == DynamicNameRecording::Record && successful {
        record_resolved_fixture_name(runtime, test_index, &setup_plan.name);
    }
    returned
}
