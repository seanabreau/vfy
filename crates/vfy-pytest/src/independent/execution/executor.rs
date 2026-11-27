//! Executor ownership and fixture request resolution.

use super::super::capture::TestCapture;
use super::super::thread_bound::ThreadBound;
use super::super::{CollectedSuite, asyncio, callspec};
use super::callbacks::FixtureCaptureAction;
use super::fixture_cache::CacheRead;
use super::node_finalizers::NativeNodeFinalizers;
use super::request::{
    FinalizerTarget, NativeIndependentRequest, RequestContext, RequestObjects, RequestParts,
};
use super::resolved::NativeResolvedFixtureDefinitions;
use super::state::{ExecutionState, TestExecution};
use super::{
    collected_item, collection_node, lookup_stack, provider_scopes, validate_fixture_scope,
};
use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyWeakrefMethods, PyWeakrefReference};
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::Mutex;
use vfy_core::test_plan::{TestBody, TestPlan};
use vfy_core::{CollectionNodeId, FixtureCacheIdentity, FixtureCacheKey, FixtureScope};

pub(super) struct Executor {
    pub(super) suite: RefCell<Option<Rc<CollectedSuite>>>,
    pub(super) root: String,
    pub(super) state: RefCell<ExecutionState>,
    pub(super) node_state: Py<PyWeakrefReference>,
    pub(super) capture: RefCell<Option<Rc<TestCapture>>>,
    pub(super) provider_scopes: RefCell<Vec<Py<provider_scopes::NativeProviderScope>>>,
}

impl Executor {
    pub(super) fn node_state(&self, py: Python<'_>) -> PyResult<Py<NativeNodeFinalizers>> {
        let state =
            self.node_state.bind(py).upgrade().ok_or_else(|| {
                PyAssertionError::new_err("node finalizer state has been released")
            })?;
        Ok(state.cast_into::<NativeNodeFinalizers>()?.unbind())
    }

    pub(super) fn fixture_capture(
        &self,
        py: Python<'_>,
        action: FixtureCaptureAction,
    ) -> PyResult<()> {
        let suite = self.suite()?;
        let Some(owner) = super::super::global_capture::owner(suite.context.config.bind(py))?
        else {
            return Ok(());
        };
        if let Some(fixture) = owner.get().fixture(py)? {
            fixture.bind(py).call_method0(match action {
                FixtureCaptureAction::Start => "_start",
                FixtureCaptureAction::Close => "close",
            })?;
        }
        Ok(())
    }

    pub(super) fn suite(&self) -> PyResult<Rc<CollectedSuite>> {
        self.suite
            .borrow()
            .as_ref()
            .map(Rc::clone)
            .ok_or_else(|| PyAssertionError::new_err("fixture request belongs to a finished run"))
    }

    pub(super) fn begin_test(&self, py: Python<'_>, index: usize) -> PyResult<RequestContext> {
        let suite = self.suite()?;
        let test = &suite.plan.tests()[index];
        let node = collection_node(test)?;
        let (owner, direct_item) = {
            let arena = self.collection_arena(py)?;
            let arena = arena.borrow();
            (
                arena.scope_owner(node, FixtureScope::Function, "")?,
                test.body == TestBody::Item && arena.is_file_hook_item(node)?,
            )
        };
        // A bare Item owns its setup. Its plug-in may use fixturenames as metadata.
        let mut names = if direct_item {
            Vec::new()
        } else if let Some(item) = collected_item(&suite, index) {
            item.bind(py)
                .getattr(pyo3::intern!(py, "fixturenames"))?
                .extract::<Vec<String>>()?
        } else {
            super::super::lifecycle::nodes::fixture_names(test, suite.plan.fixtures())
        };
        if let Some(scope) = test.async_loop_scope
            && asyncio::enabled(Some(&suite.context), py)?
        {
            let runner = asyncio::runner_name(scope);
            names.retain(|name| name != &runner);
            names.push(runner);
        }
        let lifetime = Rc::new(Cell::new(true));
        let arguments = PyDict::new(py).unbind();
        if let Some(item) = collected_item(&suite, index) {
            item.bind(py)
                .setattr(pyo3::intern!(py, "funcargs"), arguments.bind(py))?;
        }
        let resolved = NativeResolvedFixtureDefinitions::new(py)?;
        let previous = {
            let mut state = self.state.borrow_mut();
            let previous = match state.tests.entry(index) {
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(TestExecution {
                        instance: None,
                        arguments,
                        resolved,
                        lifetime: Rc::clone(&lifetime),
                    });
                    None
                }
                std::collections::btree_map::Entry::Occupied(mut entry) => {
                    let test = entry.get_mut();
                    let previous_resolved = std::mem::replace(&mut test.resolved, resolved);
                    test.lifetime = Rc::clone(&lifetime);
                    let previous_arguments = std::mem::replace(&mut test.arguments, arguments);
                    Some((previous_arguments, previous_resolved))
                }
            };
            state.teardown = false;
            previous
        };
        drop(previous);
        Ok(RequestContext {
            test_index: index,
            scope: FixtureScope::Function,
            fixture: None,
            owner,
            stack: Vec::new(),
            lifetime,
            names: Rc::new(RefCell::new(names)),
            parameter: None,
        })
    }

    pub(super) fn collection_arena<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<Bound<'py, super::super::collection::arena::NativeCollectionArena>> {
        let suite = self.suite()?;
        let protocol = &suite.context;
        super::super::collection::arena::for_context(py, protocol)
    }

    pub(super) fn node_identity(&self, context: &RequestContext) -> PyResult<CollectionNodeId> {
        // Retained requests must still reject access after their suite closes.
        self.suite()?;
        Ok(context.owner)
    }

    pub(super) fn node_belongs_to(
        &self,
        py: Python<'_>,
        identity: CollectionNodeId,
        test: &TestPlan,
    ) -> PyResult<bool> {
        let item = collection_node(test)?;
        self.collection_arena(py)?.borrow().contains(identity, item)
    }

    pub(super) fn node_callbacks(
        &self,
        py: Python<'_>,
        identity: CollectionNodeId,
    ) -> PyResult<Py<PyList>> {
        self.node_state(py)?.get().callbacks(py, identity)
    }

    pub(super) fn callable(&self, py: Python<'_>, index: usize) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let binding = &suite.tests[index];
        if binding.class.is_none() {
            return Ok(binding.callable.clone_ref(py));
        }
        let instance = self
            .state
            .borrow()
            .tests
            .get(&index)
            .and_then(|test| test.instance.as_ref())
            .map(|instance| instance.clone_ref(py))
            .ok_or_else(|| PyValueError::new_err("test class instance was not prepared"))?;
        Ok(binding
            .callable
            .bind(py)
            .call_method1(pyo3::intern!(py, "__get__"), (instance.bind(py),))?
            .unbind())
    }

    fn request_node(&self, py: Python<'_>, context: &RequestContext) -> PyResult<Py<PyAny>> {
        let identity = self.node_identity(context)?;
        let arena = self.collection_arena(py)?;
        let node = arena.borrow().object(py, identity)?;
        Ok(node)
    }

    pub(super) fn request(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        finalizers: FinalizerTarget,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<Py<NativeIndependentRequest>> {
        let suite = self.suite()?;
        let binding = &suite.tests[context.test_index];
        let item = collected_item(&suite, context.test_index);
        let module_name = binding
            .class
            .as_ref()
            .unwrap_or(&binding.callable)
            .bind(py)
            .getattr("__module__")?;
        let module = Module::Sys
            .get(py)?
            .getattr("modules")?
            .get_item(module_name)?
            .unbind();
        let node = self.request_node(py, context)?;
        let parameter = context.parameter.as_deref();
        let default_index = 0usize.into_pyobject(py)?.into_any().unbind();
        let param_index = context
            .fixture
            .map(|_| parameter.map_or(default_index, |parameter| parameter.index.clone_ref(py)));
        let item =
            item.ok_or_else(|| PyAssertionError::new_err("fixture request item is unavailable"))?;
        let stack = lookup_stack(py, &suite, context);
        let providers = self
            .provider_scopes
            .borrow()
            .get(binding.provider_scope)
            .map(|providers| providers.clone_ref(py))
            .ok_or_else(|| {
                PyAssertionError::new_err("fixture request providers are unavailable")
            })?;
        Py::new(
            py,
            NativeIndependentRequest {
                parts: ThreadBound::new(RequestParts {
                    owner: Rc::clone(self),
                    // RequestObjects owns the captured value after this setup context is released.
                    context: RequestContext {
                        parameter: None,
                        ..context.clone()
                    },
                }),
                scope: context.scope,
                fixture_name: context
                    .fixture
                    .map(|id| suite.plan.fixtures()[id.index()].name.clone()),
                objects: Mutex::new(Some(RequestObjects {
                    resolved: resolved.clone_ref(py),
                    node,
                    item: item.clone_ref(py),
                    stack,
                    providers,
                    module,
                    param: parameter.map(|parameter| parameter.value.clone_ref(py)),
                    param_index,
                    finalizers,
                    config: Some(suite.context.config.clone_ref(py)),
                    session: Some(suite.context.session.clone_ref(py)),
                })),
            },
        )
    }

    pub(super) fn resolved(
        &self,
        py: Python<'_>,
        index: usize,
    ) -> PyResult<Py<NativeResolvedFixtureDefinitions>> {
        self.state
            .borrow()
            .tests
            .get(&index)
            .map(|test| test.resolved.clone_ref(py))
            .ok_or_else(|| PyAssertionError::new_err("fixture request has no resolved registry"))
    }

    pub(super) fn remember(
        &self,
        py: Python<'_>,
        context: &RequestContext,
        name: &str,
        key: &FixtureCacheKey,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<()> {
        let suite = self.suite()?;
        let binding = suite.fixtures[key.identity.fixture.index()]
            .runtime
            .clone_ref(py);
        resolved.bind(py).borrow().remember(name, key, binding);
        let mut names = context.names.borrow_mut();
        if !names.iter().any(|value| value == name) {
            names.push(name.to_owned());
        }
        Ok(())
    }

    pub(super) fn cached(
        &self,
        py: Python<'_>,
        key: &FixtureCacheKey,
        read: CacheRead,
    ) -> PyResult<Py<PyAny>> {
        let storage = self
            .state
            .borrow()
            .instance(key)
            .map(|cached| cached.storage.clone_ref(py))
            .ok_or_else(|| {
                PyAssertionError::new_err(
                    "fixture value is not available because it has already been torn down",
                )
            })?;
        storage.value(py, read)
    }

    fn next_key(&self, identity: FixtureCacheIdentity) -> PyResult<FixtureCacheKey> {
        let mut state = self.state.borrow_mut();
        let generation = state
            .generation
            .checked_next()
            .ok_or_else(|| PyValueError::new_err("fixture generation limit exceeded"))?;
        state.generation = generation;
        Ok(FixtureCacheKey {
            identity,
            generation,
        })
    }

    pub(super) fn resolve(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        name: &str,
        request: &Bound<'_, PyAny>,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let remembered = resolved.bind(py).borrow().key(name);
        if let Some(key) = remembered {
            return self.remembered_fixture(py, context, name, &key, &suite);
        }
        let owner_active = context.fixture.is_some() && {
            let identity = self.node_identity(context)?;
            self.node_state(py)?
                .get()
                .locked()?
                .iter()
                .any(|node| node.identity == identity)
        };
        if (!context.lifetime.get() && !owner_active) || self.state.borrow().teardown {
            return Err(PyAssertionError::new_err(format!(
                "fixture '{name}' cannot be set up after its request has finished"
            )));
        }
        let test = &suite.plan.tests()[context.test_index];
        let depth = context
            .stack
            .iter()
            .filter(|id| suite.plan.fixtures()[id.index()].name == name)
            .count();
        let Some(resolution) = test.resolve_fixture(suite.plan.fixtures(), name, depth) else {
            return Err(super::super::fixture_lookup::error(py, name, request)?);
        };
        let fixture = &suite.plan.fixtures()[resolution.use_.fixture.index()];
        let parameter = callspec::parameter(py, &suite, context.test_index, name)?;
        let scope = parameter
            .as_ref()
            .map_or(fixture.declared_scope, |parameter| parameter.scope);
        validate_fixture_scope(py, context.scope, fixture.declared_scope, name)?;
        let arena = self.collection_arena(py)?;
        let item = collection_node(test)?;
        let owner = arena
            .borrow()
            .scope_owner(item, scope, &fixture.package_baseid)?;
        let identity = FixtureCacheIdentity {
            fixture: fixture.id,
            owner: vfy_core::FixtureOwner::Collection(owner),
            scope,
        };
        let (active, storage) = {
            let state = self.state.borrow();
            (
                state.active.contains(&fixture.id),
                state
                    .fixture(fixture.id)
                    .map(|cached| cached.storage.clone_ref(py)),
            )
        };
        let recursively_uncached = active
            && !storage
                .as_ref()
                .map_or(Ok(false), |storage| storage.ready(py))?;
        if recursively_uncached {
            return Err(super::super::fixture_lookup::error(py, name, request)?);
        }
        if suite.fixtures[fixture.id.index()].parameters.is_some() && parameter.is_none() {
            return Err(self.missing_parameter(py, fixture, request)?);
        }
        let key = self.next_key(identity)?;
        let mut child = RequestContext {
            scope,
            fixture: Some(fixture.id),
            owner,
            parameter: parameter.map(Rc::new),
            lifetime: Rc::new(Cell::new(true)),
            ..context.clone()
        };
        child.stack.push(fixture.id);
        self.state.borrow_mut().active.push(fixture.id);
        let result = self.setup_fixture(py, &child, &key, fixture, resolved);
        self.state.borrow_mut().active.pop();
        result
    }

    pub(super) fn builtin_resource(
        &self,
        py: Python<'_>,
        name: &str,
    ) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        py.import("vfy.independent.helpers")?
            .call_method1("create_builtin", (name, &self.root))?
            .extract()
    }
}
