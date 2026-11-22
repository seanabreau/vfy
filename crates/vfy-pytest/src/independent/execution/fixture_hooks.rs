//! Dispatch fixture hooks with the request and its authoritative binding.

use super::super::fixture_binding::{FixtureOutcome, NativeFixtureBinding, NativeFixtureOutcome};
use super::asynchronous;
use super::executor::Executor;
use super::request::{NativeIndependentRequest, RequestContext};
use crate::hooks::{NativeHookDefault, NativeHookRegistry};
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::{PyTraverseError, PyVisit};
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::{Mutex, PoisonError};
use vfy_core::FixtureCacheKey;
use vfy_core::test_plan::FixturePlan;

use super::super::thread_bound::ThreadBound;

struct SetupObjects {
    request: Py<NativeIndependentRequest>,
    binding: Py<NativeFixtureBinding>,
}

/// The executor state a setup callback acts on, which belongs to the test thread.
struct SetupState {
    owner: Rc<Executor>,
    context: RequestContext,
    key: FixtureCacheKey,
    active: Rc<Cell<bool>>,
    called: Cell<bool>,
}

/// A garbage collection on any thread may release the callback, as with
/// pytest's own callables; only its executor state is thread-bound.
#[pyclass(frozen)]
struct FixtureSetupCallback {
    state: ThreadBound<SetupState>,
    objects: Mutex<Option<SetupObjects>>,
}

#[pymethods]
impl FixtureSetupCallback {
    #[getter]
    fn __signature__(_slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let state = self.state.get()?;
        if !state.active.get() || state.called.replace(true) {
            return Err(PyAssertionError::new_err(
                "fixture setup callback is no longer available",
            ));
        }
        let (request, binding) = {
            let objects = self.objects.lock().unwrap_or_else(PoisonError::into_inner);
            let objects = objects.as_ref().ok_or_else(|| {
                PyAssertionError::new_err("fixture setup callback has been cleared")
            })?;
            (objects.request.clone_ref(py), objects.binding.clone_ref(py))
        };
        execute_fixture_setup(
            py,
            &state.owner,
            &state.context,
            &state.key,
            &request,
            &binding,
        )
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_lock()
            && let Some(objects) = objects.as_ref()
        {
            visit.call(&objects.request)?;
            visit.call(&objects.binding)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let objects = self
            .objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(objects);
    }
}

fn execute_fixture_setup(
    py: Python<'_>,
    owner: &Rc<Executor>,
    context: &RequestContext,
    key: &FixtureCacheKey,
    request: &Py<NativeIndependentRequest>,
    binding: &Py<NativeFixtureBinding>,
) -> PyResult<Py<PyAny>> {
    let suite = owner.suite()?;
    let fixture = &suite.plan.fixtures()[key.identity.fixture.index()];
    let kwargs = owner.fixture_arguments(py, context, fixture, request)?;
    let callable = super::fixture_function::resolve(
        py,
        binding.bind(py),
        request.bind(py),
        fixture.execution,
        suite.fixtures[fixture.id.index()].definition.bind(py),
    )?
    .into_bound(py);
    let finalizers = binding.bind(py).borrow().finalizers(py)?;
    let parameter = binding
        .bind(py)
        .call_method1(pyo3::intern!(py, "cache_key"), (request,))?;
    let result = owner.invoke_fixture(
        py,
        context,
        key,
        fixture,
        asynchronous::FixtureCall {
            callable: &callable,
            kwargs: &kwargs,
            finalizers: finalizers.bind(py),
            request,
        },
    )?;
    let (value, failure) = match &result {
        Ok(value) => (value.clone_ref(py), None),
        Err(error) => (
            py.None(),
            Some((
                error.value(py).clone().into_any().unbind(),
                error
                    .traceback(py)
                    .map_or_else(|| py.None(), |traceback| traceback.into_any().unbind()),
            )),
        ),
    };
    binding
        .bind(py)
        .borrow()
        .replace_outcome(FixtureOutcome::Native(NativeFixtureOutcome {
            value,
            parameter: parameter.unbind(),
            failure,
        }))?;
    result
}

#[pyclass(frozen, unsendable)]
struct PostFixtureFinalizer {
    objects: RefCell<Option<(Py<NativeIndependentRequest>, Py<NativeFixtureBinding>)>>,
}

#[pymethods]
impl PostFixtureFinalizer {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let objects = self
            .objects
            .borrow()
            .as_ref()
            .map(|(request, binding)| (request.clone_ref(py), binding.clone_ref(py)));
        let Some((request, binding)) = objects else {
            return Ok(());
        };
        let (owner, context) = request.bind(py).get().parts()?;
        let suite = owner.suite()?;
        let protocol = &suite.context;
        let kwargs = PyDict::new(py);
        kwargs.set_item(pyo3::intern!(py, "fixturedef"), binding)?;
        kwargs.set_item(pyo3::intern!(py, "request"), request)?;
        let removed = owner.fixture_removed_plugins(py, &context)?;
        let result = NativeHookRegistry::call_subset(
            protocol.registry.bind(py),
            "pytest_fixture_post_finalizer",
            &kwargs,
            removed.bind(py),
            false,
        );
        drop(removed);
        drop(kwargs);
        result?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_borrow()
            && let Some((request, binding)) = objects.as_ref()
        {
            visit.call(request)?;
            visit.call(binding)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let objects = self.objects.borrow_mut().take();
        drop(objects);
    }
}

impl Executor {
    fn fixture_removed_plugins(
        &self,
        py: Python<'_>,
        context: &RequestContext,
    ) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let protocol = &suite.context;
        let identity = self.node_identity(context)?;
        let arena = self.collection_arena(py)?;
        let path = arena.borrow().path(identity)?.to_path_buf();
        protocol.removed_plugins(py, &path)
    }

    pub(super) fn dispatch_fixture_setup(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        key: &FixtureCacheKey,
        fixture: &FixturePlan,
        request: &Py<NativeIndependentRequest>,
    ) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let binding = suite.fixtures[fixture.id.index()].runtime.clone_ref(py);
        binding
            .bind(py)
            .borrow()
            .finalizers(py)?
            .bind(py)
            .append(Py::new(
                py,
                PostFixtureFinalizer {
                    objects: RefCell::new(Some((request.clone_ref(py), binding.clone_ref(py)))),
                },
            )?)?;
        let callback_context = RequestContext {
            parameter: None,
            ..context.clone()
        };
        let active = RefCell::new(None);
        let result = {
            let protocol = &suite.context;
            let arguments = PyDict::new(py);
            arguments.set_item(pyo3::intern!(py, "fixturedef"), &binding)?;
            arguments.set_item(pyo3::intern!(py, "request"), request)?;
            let removed = self.fixture_removed_plugins(py, context)?;
            NativeHookRegistry::call_subset_with_lazy_default(
                protocol.registry.bind(py),
                "pytest_fixture_setup",
                &arguments,
                removed.bind(py).as_any(),
                true,
                "fixtures",
                NativeHookDefault {
                    execute: || {
                        execute_fixture_setup(py, self, &callback_context, key, request, &binding)
                    },
                    materialize: || {
                        let enabled = Rc::new(Cell::new(true));
                        let callback = Py::new(
                            py,
                            FixtureSetupCallback {
                                state: ThreadBound::new(SetupState {
                                    owner: Rc::clone(self),
                                    context: callback_context.clone(),
                                    key: key.clone(),
                                    active: Rc::clone(&enabled),
                                    called: Cell::new(false),
                                }),
                                objects: Mutex::new(Some(SetupObjects {
                                    request: request.clone_ref(py),
                                    binding: binding.clone_ref(py),
                                })),
                            },
                        )?;
                        *active.borrow_mut() = Some(enabled);
                        Ok(callback.into_any())
                    },
                },
            )
        };
        if let Some(active) = active.into_inner() {
            active.set(false);
        }
        result
    }
}
