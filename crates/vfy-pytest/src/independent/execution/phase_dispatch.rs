//! Setup, call and teardown phase dispatch.

use super::super::asyncio;
use super::super::global_capture::Capture;
use super::super::reporting::call::PhaseCompletion;
use super::callbacks::{
    FixtureCaptureAction, PhaseCallback, PhaseCallbackOwner, PhaseInvocation, PhaseOperation,
};
use super::capture::ExecutionCapture;
use super::executor::Executor;
use super::node_finalizers::ActiveNode;
use super::request::{FinalizerTarget, RequestContext};
use super::{collected_item, collection_node, current_test};
use crate::hooks::{NativeHookDefault, NativeHookRegistry};
use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple, PyWeakrefReference};
use std::cell::RefCell;
use std::rc::Rc;
use vfy_core::test_plan::{FixtureExecution, TestBody};

impl Executor {
    pub(super) fn setup(self: &Rc<Self>, py: Python<'_>, context: &RequestContext) -> PyResult<()> {
        let suite = self.suite()?;
        // Invalid modes remain setup errors when no fixture or async runner is needed.
        if asyncio::enabled(Some(&suite.context), py)? {
            asyncio::mode(&suite.context, py)?;
        }
        let test = &suite.plan.tests()[context.test_index];
        let arena = self.collection_arena(py)?;
        let needed = arena.borrow().ancestry(collection_node(test)?)?;
        let node_state = self.node_state(py)?;
        let existing = {
            let nodes = node_state.get().locked()?;
            if nodes.len() > needed.len()
                || nodes
                    .iter()
                    .zip(&needed)
                    .any(|(node, id)| node.identity != *id)
            {
                return Err(PyAssertionError::new_err(
                    "previous item was not torn down properly",
                ));
            }
            nodes.len()
        };
        let added = needed[existing..]
            .iter()
            .map(|identity| {
                Ok(ActiveNode {
                    identity: *identity,
                    node: arena.borrow().object(py, *identity)?,
                    callbacks: None,
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        for node in added {
            let canonical = node_state.get().canonical.bind(py);
            if canonical.contains(node.node.bind(py))? {
                return Err(PyAssertionError::new_err(()));
            }
            canonical.set_item(node.node.bind(py), node.node.bind(py))?;
            node_state.get().locked()?.push(node);
        }
        // Setup owns the function before preparing its instance or fixtures.
        // An aborted protocol leaves this same owner for the next setup check.
        let plugin_item = if test.body == TestBody::Item {
            let finalizers = self.node_callbacks(py, self.node_identity(context)?)?;
            let item = collected_item(&suite, context.test_index)
                .ok_or_else(|| PyAssertionError::new_err("plugin item is not collected"))?
                .clone_ref(py);
            // Registered first, so it runs after every fixture finalizer.
            finalizers
                .bind(py)
                .append(item.bind(py).getattr("teardown")?)?;
            Some(item)
        } else {
            let instance = collected_item(&suite, context.test_index)
                .ok_or_else(|| PyAssertionError::new_err("test has no actual collected item"))?
                .bind(py)
                .getattr(pyo3::intern!(py, "instance"))?
                .unbind();
            let removed = {
                let mut state = self.state.borrow_mut();
                let test = state
                    .tests
                    .get_mut(&context.test_index)
                    .ok_or_else(|| PyAssertionError::new_err("test arguments are unavailable"))?;
                test.instance.replace(instance)
            };
            drop(removed);
            None
        };
        let (arguments, resolved) = {
            let state = self.state.borrow();
            let test = state
                .tests
                .get(&context.test_index)
                .ok_or_else(|| PyAssertionError::new_err("test arguments are unavailable"))?;
            (test.arguments.clone_ref(py), test.resolved.clone_ref(py))
        };
        let request = self.request(py, context, FinalizerTarget::Node, &resolved)?;
        if let Some(item) = collected_item(&suite, context.test_index) {
            item.bind(py)
                .setattr(pyo3::intern!(py, "_request"), &request)?;
        }
        let names = context.names.borrow().clone();
        for name in names {
            if name == "request" {
                arguments.bind(py).set_item(name, &request)?;
            } else {
                arguments.bind(py).set_item(
                    &name,
                    self.resolve(py, context, &name, request.bind(py).as_any(), &resolved)?,
                )?;
            }
        }
        if let Some(item) = plugin_item {
            item.bind(py).call_method0("setup")?;
        }
        Ok(())
    }

    pub(super) fn call(self: &Rc<Self>, py: Python<'_>, context: &RequestContext) -> PyResult<()> {
        let suite = self.suite()?;
        let test = &suite.plan.tests()[context.test_index];
        let item = collected_item(&suite, context.test_index);
        if test.body == TestBody::Item {
            // A plug-in item runs its own test through `Item.runtest`.
            let item =
                item.ok_or_else(|| PyAssertionError::new_err("plugin item is not collected"))?;
            item.bind(py).call_method0("runtest")?;
            return Ok(());
        }
        let unittest = if suite.tests[context.test_index].class.is_some() {
            let arena = self.collection_arena(py)?;
            arena.borrow().is_unittest_item(collection_node(test)?)?
        } else {
            false
        };
        if unittest {
            let item = item.ok_or_else(|| {
                PyAssertionError::new_err("unittest requires its actual collected item")
            })?;
            return super::super::unittest::run(py, item.bind(py));
        }
        let kwargs = PyDict::new(py);
        if let Some(item) = item
            && plugin_pyfunc_call(py, suite.context.registry.bind(py), item.bind(py))?
        {
            return Ok(());
        }
        let arguments = if let Some(item) = item {
            item.bind(py)
                .getattr(pyo3::intern!(py, "funcargs"))?
                .cast_into::<PyDict>()?
                .unbind()
        } else {
            self.state
                .borrow()
                .tests
                .get(&context.test_index)
                .map(|test| test.arguments.clone_ref(py))
                .ok_or_else(|| PyAssertionError::new_err("test arguments are unavailable"))?
        };
        for argument in suite.tests[context.test_index]
            .argument_keys
            .bind(py)
            .iter()
        {
            if let Some(value) = arguments.bind(py).get_item(&argument)? {
                kwargs.set_item(&argument, value)?;
            }
        }
        let callable = item.map_or_else(
            || self.callable(py, context.test_index),
            |item| Ok(item.bind(py).getattr(pyo3::intern!(py, "obj"))?.unbind()),
        )?;
        let kind = asyncio::classify(callable.bind(py))?;
        if matches!(
            kind,
            FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
        ) {
            if !asyncio::enabled(Some(&suite.context), py)? {
                return asyncio::unhandled_test(py);
            }
            // `classify` evaluates `iscoroutinefunction` first and returns
            // `AsyncCoroutine` exactly when it is true, so no second probe is needed.
            if kind == FixtureExecution::AsyncCoroutine {
                let item = item
                    .ok_or_else(|| PyAssertionError::new_err("test item is unavailable"))?
                    .bind(py);
                let scope = asyncio::live_test_scope(Some(&suite.context), item, &test.nodeid)?;
                let Some(scope) = scope else {
                    return asyncio::unhandled_test(py);
                };
                let request = item.getattr(pyo3::intern!(py, "_request"))?;
                let resolved = self.resolved(py, context.test_index)?;
                let runner = self.resolve(
                    py,
                    context,
                    &asyncio::runner_name(scope),
                    &request,
                    &resolved,
                )?;
                py.import("vfy.asyncio_adapter")?.call_method1(
                    "run_async_test",
                    (runner, callable.bind(py), PyTuple::empty(py), &kwargs),
                )?;
                return Ok(());
            }
        }
        let result = callable.bind(py).call((), Some(&kwargs))?;
        if result.is_none() {
            return Ok(());
        }
        if result.hasattr(pyo3::intern!(py, "__await__"))?
            || result.hasattr(pyo3::intern!(py, "__aiter__"))?
        {
            return asyncio::unhandled_test(py);
        }
        Ok(())
    }

    pub(super) fn perform_phase(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        operation: PhaseOperation,
    ) -> PyResult<()> {
        let suite = self.suite()?;
        let item = collected_item(&suite, context.test_index)
            .ok_or_else(|| PyValueError::new_err("test hook item is unavailable"))?
            .bind(py);
        current_test::update(item, Some(operation.phase()))?;
        match operation {
            PhaseOperation::Setup => self.setup(py, context),
            PhaseOperation::Call => self.call(py, context),
            PhaseOperation::Teardown { next } => {
                self.finalize(py, next.and_then(|index| suite.plan.tests().get(index)))?;
                current_test::update(item, None)
            }
        }
    }

    fn dispatch_phase(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        operation: PhaseOperation,
    ) -> PyResult<()> {
        let suite = self.suite()?;
        let protocol = &suite.context;
        let item = protocol
            .items
            .get(context.test_index)
            .ok_or_else(|| PyValueError::new_err("test hook item is unavailable"))?
            .bind(py);
        let mut arguments = match operation {
            PhaseOperation::Teardown { next } => {
                crate::hooks::invocation::CallArguments::ItemNext {
                    item,
                    nextitem: next
                        .and_then(|index| protocol.items.get(index))
                        .map(|item| item.bind(py)),
                }
            }
            _ => crate::hooks::invocation::CallArguments::Item(item),
        };
        let callback_owner = RefCell::new(None);
        let result = NativeHookRegistry::call_subset_with_lazy_default_arguments(
            protocol.registry.bind(py),
            operation.hook_name(),
            &mut arguments,
            &NativeHookRegistry::item_exclusions(protocol.registry.bind(py), item)?,
            false,
            "runner",
            NativeHookDefault {
                execute: || {
                    self.perform_phase(py, context, operation)?;
                    Ok(py.None())
                },
                materialize: || {
                    let owner = Py::new(
                        py,
                        PhaseCallbackOwner {
                            pending: RefCell::new(Some(PhaseInvocation {
                                owner: Rc::clone(self),
                                context: context.clone(),
                                operation,
                            })),
                        },
                    )?;
                    let callback = Py::new(
                        py,
                        PhaseCallback {
                            owner: PyWeakrefReference::new(owner.bind(py))?.unbind(),
                            thread: std::thread::current().id(),
                        },
                    )?;
                    *callback_owner.borrow_mut() = Some(owner);
                    Ok(callback.into_any())
                },
            },
        );
        drop(callback_owner);
        result.map(|_| ())
    }
}

/// Plug-ins such as anyio call the test through `pytest_pyfunc_call`; a result
/// other than `None` means a plug-in called it.
fn plugin_pyfunc_call(
    py: Python<'_>,
    registry: &Bound<'_, NativeHookRegistry>,
    item: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if !registry
        .borrow()
        .has_implementations(py, "pytest_pyfunc_call")
    {
        return Ok(false);
    }
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "pyfuncitem"), item)?;
    let removed = NativeHookRegistry::item_exclusions(registry, item)?;
    let result =
        NativeHookRegistry::call_subset(registry, "pytest_pyfunc_call", &kwargs, &removed, true);
    drop(removed);
    drop(kwargs);
    Ok(!result?.is_none(py))
}

/// Runs one phase between its capture boundary and returns it before publication.
pub(super) fn run_phase(
    executor: &Rc<Executor>,
    py: Python<'_>,
    protocol_capture: &ExecutionCapture,
    context: &RequestContext,
    operation: PhaseOperation,
    capture: Capture,
) -> PyResult<PhaseCompletion> {
    let mut completion = protocol_capture.phase(py, operation.phase(), || {
        let result = executor.dispatch_phase(py, context, operation);
        let closed = executor.fixture_capture(py, FixtureCaptureAction::Close);
        result.and(closed)
    });
    capture.retain(&mut completion.capture)?;
    drop(capture);
    Ok(completion)
}
