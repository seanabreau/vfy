//! Fixture setup, teardown and finalization for the executor.

use super::callbacks::FinishFixture;
use super::executor::Executor;
use super::fixture_cache::{CacheRead, FixtureCacheStorage};
use super::request::{FinalizerTarget, NativeIndependentRequest, RequestContext};
use super::resolved::NativeResolvedFixtureDefinitions;
use super::state::{ExecutionState, FixtureInstance};
use super::{collected_item, outcome};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use std::cell::RefCell;
use std::rc::Rc;
use vfy_core::FixtureCacheKey;
use vfy_core::test_plan::{FixturePlan, TestPlan};

impl Executor {
    pub(super) fn setup_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        key: &FixtureCacheKey,
        fixture: &FixturePlan,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let finalizers = suite.fixtures[fixture.id.index()]
            .runtime
            .bind(py)
            .borrow()
            .finalizers(py)?
            .into_bound(py);
        let request = self.request(
            py,
            context,
            FinalizerTarget::Fixture(finalizers.clone().unbind()),
            resolved,
        )?;
        if fixture.dependencies.iter().all(|name| name == "request") {
            // No dependency values need resolving or retaining across cache comparison.
            // Keep the request's cleared-state check performed by fixture_arguments.
            request.bind(py).get().fixture_definitions(py)?;
        } else {
            self.fixture_arguments(py, context, fixture, &request)?;
        }
        let comparison = self.reusable_fixture(py, context, fixture)?;
        if let Some(cached) = comparison.key.clone() {
            let value = self.cached(py, &cached, CacheRead::ExecuteReuse);
            comparison.finish();
            drop(value?);
            self.remember(py, context, &fixture.name, &cached, resolved)?;
            return self.cached(py, &cached, CacheRead::RequestValue);
        }
        for dependency in &fixture.dependencies {
            if dependency != "request" {
                self.register_dependency(py, dependency, key, &request, resolved)?;
            }
        }
        if !finalizers.is_empty() {
            return Err(PyAssertionError::new_err(()));
        }
        let binding = suite.fixtures[fixture.id.index()].runtime.clone_ref(py);
        let inserted = self.state.borrow_mut().insert_instance(FixtureInstance {
            order: 0,
            key: key.clone(),
            storage: FixtureCacheStorage::new(binding.clone_ref(py)),
            finalizers: finalizers.clone().unbind(),
            lifetime: Rc::clone(&context.lifetime),
        });
        if let Err(failed) = inserted {
            let (error, instance) = *failed;
            drop(instance);
            return Err(error);
        }
        let finalizer = self.finish_fixture(py, key, &request)?;
        let setup = self.dispatch_fixture_setup(py, context, key, fixture, &request);
        let registered = match &setup {
            Ok(_) => FinishFixture::_register(finalizer.bind(py), py),
            Err(error) => (|| {
                py.import("vfy._hook_callbacks")?
                    .getattr("call_while_handling")?
                    .call1((finalizer.bind(py).getattr("_register")?, error.value(py)))?;
                Ok(())
            })(),
        };
        comparison.finish();
        let storage = FixtureCacheStorage::new(binding);
        if !storage.ready(py)? {
            let removed = self.state.borrow_mut().take_instance(key);
            drop(removed);
        }
        registered?;
        drop(setup?);
        self.remember(py, context, &fixture.name, key, resolved)?;
        storage.value(py, CacheRead::RequestValue)
    }

    pub(super) fn finish_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        key: &FixtureCacheKey,
        request: &Py<NativeIndependentRequest>,
    ) -> PyResult<Py<FinishFixture>> {
        Py::new(
            py,
            FinishFixture {
                owner: Rc::clone(self),
                key: key.clone(),
                request: RefCell::new(Some(request.clone_ref(py))),
            },
        )
    }

    pub(super) fn finalize_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        key: &FixtureCacheKey,
    ) -> PyResult<()> {
        let finalizers = {
            let mut state = self.state.borrow_mut();
            if state.finishing.contains(key) {
                return Ok(());
            }
            let Some(finalizers) = state
                .instance(key)
                .map(|cached| cached.finalizers.clone_ref(py))
            else {
                return Ok(());
            };
            state.finishing.push(key.clone());
            finalizers
        };
        let result = self.run_fixture_finalizers(py, key, &finalizers);
        let removed = {
            let mut state = self.state.borrow_mut();
            state.finishing.retain(|finishing| finishing != key);
            match result {
                Ok(Some(_)) => state.take_instance(key),
                _ => None,
            }
        };
        if let Some(removed) = removed {
            removed.lifetime.set(false);
        }
        result?.map_or(Ok(()), |errors| {
            outcome(errors, py, "errors while tearing down fixture")
        })
    }

    /// Run a fixture's finalizers. Returns `None` when its storage is not ready,
    /// so the fixture stays cached, else the finalizer errors.
    fn run_fixture_finalizers(
        &self,
        py: Python<'_>,
        key: &FixtureCacheKey,
        finalizers: &Py<pyo3::types::PyList>,
    ) -> PyResult<Option<Vec<PyErr>>> {
        let storage = self
            .state
            .borrow()
            .instance(key)
            .map(|cached| cached.storage.clone_ref(py));
        if let Some(storage) = &storage
            && !storage.ready(py)?
        {
            return Ok(None);
        }
        let mut errors = Vec::new();
        while !finalizers.bind(py).is_empty() {
            errors.extend(
                finalizers
                    .bind(py)
                    .call_method0(pyo3::intern!(py, "pop"))?
                    .call0()
                    .err(),
            );
        }
        if let Some(storage) = storage {
            storage.clear(py)?;
        }
        finalizers
            .bind(py)
            .call_method0(pyo3::intern!(py, "clear"))?;
        Ok(Some(errors))
    }

    pub(super) fn finalize(
        self: &Rc<Self>,
        py: Python<'_>,
        next: Option<&TestPlan>,
    ) -> PyResult<()> {
        self.state.borrow_mut().teardown = true;
        let node_state = self.node_state(py)?;
        let mut errors = Vec::new();
        loop {
            let identity = node_state.get().locked()?.last().map(|node| node.identity);
            let Some(identity) = identity else {
                break;
            };
            if let Some(next) = next
                && self.node_belongs_to(py, identity, next)?
            {
                break;
            }
            let popped = node_state
                .get()
                .canonical
                .bind(py)
                .call_method0("popitem")?;
            let popped_node = popped.get_item(0)?;
            let removed = node_state.get().locked()?.pop();
            if removed
                .as_ref()
                .is_none_or(|node| !node.node.bind(py).is(&popped_node))
            {
                return Err(PyRuntimeError::new_err(
                    "node finalizer stack is inconsistent",
                ));
            }
            drop(popped_node);
            drop(popped);
            if let Some(node) = removed
                && let Some(callbacks) = node.callbacks
            {
                while !callbacks.bind(py).is_empty() {
                    errors.extend(
                        callbacks
                            .bind(py)
                            .call_method0(pyo3::intern!(py, "pop"))?
                            .call0()
                            .err(),
                    );
                }
            }
        }
        outcome(errors, py, "errors during test teardown")
    }

    pub(super) fn end_test(&self, py: Python<'_>, index: usize) -> PyResult<()> {
        let removed = {
            let mut state = self.state.borrow_mut();
            state.tests.remove(&index).map(|test| {
                test.lifetime.set(false);
                (test.instance, test.arguments, test.resolved)
            })
        };
        let suite = self.suite()?;
        if let Some(item) = collected_item(&suite, index) {
            item.bind(py)
                .setattr(pyo3::intern!(py, "_request"), false)?;
            item.bind(py)
                .setattr(pyo3::intern!(py, "funcargs"), py.None())?;
        }
        drop(removed);
        Ok(())
    }

    pub(super) fn close(&self) {
        let mut state = self.state.replace(ExecutionState::default());
        let mut instances = Vec::new();
        for slot in state.cache.drain(..) {
            slot.append_to(&mut instances);
        }
        instances.sort_unstable_by_key(|instance| instance.order);
        for cached in &instances {
            cached.lifetime.set(false);
        }
        for test in state.tests.values() {
            test.lifetime.set(false);
        }
        let suite = self.suite.borrow_mut().take();
        let capture = self.capture.borrow_mut().take();
        let providers = self.provider_scopes.take();
        Python::attach(|py| {
            if let Ok(node_state) = self.node_state(py) {
                node_state.get().__clear__(py);
            }
        });
        drop(instances);
        state.release_test_objects();
        drop(state);
        drop(providers);
        drop(suite);
        drop(capture);
    }
}
