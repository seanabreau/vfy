//! Fixture cache slots and per-test execution state.

use super::fixture_cache::FixtureCacheStorage;
use super::resolved::NativeResolvedFixtureDefinitions;
use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::cell::Cell;
use std::collections::BTreeMap;
use std::rc::Rc;
use vfy_core::{FixtureCacheKey, FixtureGeneration, FixtureId};

pub(super) struct FixtureInstance {
    pub(super) order: u64,
    pub(super) key: FixtureCacheKey,
    pub(super) storage: FixtureCacheStorage,
    pub(super) finalizers: Py<PyList>,
    pub(super) lifetime: Rc<Cell<bool>>,
}

pub(super) struct TestExecution {
    pub(super) instance: Option<Py<PyAny>>,
    pub(super) arguments: Py<PyDict>,
    // Completed requests retain their own canonical registry after this row is removed.
    pub(super) resolved: Py<NativeResolvedFixtureDefinitions>,
    pub(super) lifetime: Rc<Cell<bool>>,
}

pub(super) enum FixtureSlot {
    Empty,
    One(FixtureInstance),
    Many(Vec<FixtureInstance>),
}

impl FixtureSlot {
    pub(super) fn first(&self) -> Option<&FixtureInstance> {
        match self {
            Self::Empty => None,
            Self::One(instance) => Some(instance),
            Self::Many(instances) => instances.first(),
        }
    }

    pub(super) fn get(&self, key: &FixtureCacheKey) -> Option<&FixtureInstance> {
        match self {
            Self::Empty => None,
            Self::One(instance) => (instance.key == *key).then_some(instance),
            Self::Many(instances) => instances.iter().find(|instance| instance.key == *key),
        }
    }

    pub(super) fn insert(&mut self, instance: FixtureInstance) {
        *self = match std::mem::replace(self, Self::Empty) {
            Self::Empty => Self::One(instance),
            Self::One(previous) => Self::Many(vec![previous, instance]),
            Self::Many(mut instances) => {
                instances.push(instance);
                Self::Many(instances)
            }
        };
    }

    pub(super) fn take(&mut self, key: &FixtureCacheKey) -> Option<FixtureInstance> {
        match self {
            Self::One(instance) if instance.key == *key => {
                let Self::One(instance) = std::mem::replace(self, Self::Empty) else {
                    return None;
                };
                Some(instance)
            }
            Self::Empty | Self::One(_) => None,
            Self::Many(instances) => {
                let index = instances.iter().position(|instance| instance.key == *key)?;
                let removed = instances.remove(index);
                match instances.len() {
                    0 => *self = Self::Empty,
                    1 => {
                        if let Some(remaining) = instances.pop() {
                            *self = Self::One(remaining);
                        }
                    }
                    _ => {}
                }
                Some(removed)
            }
        }
    }

    pub(super) fn append_to(self, instances: &mut Vec<FixtureInstance>) {
        match self {
            Self::Empty => {}
            Self::One(instance) => instances.push(instance),
            Self::Many(mut values) => instances.append(&mut values),
        }
    }
}

#[derive(Default)]
pub(super) struct ExecutionState {
    pub(super) cache: Vec<FixtureSlot>,
    pub(super) next_instance_order: u64,
    pub(super) active: Vec<FixtureId>,
    pub(super) finishing: Vec<FixtureCacheKey>,
    pub(super) tests: BTreeMap<usize, TestExecution>,
    pub(super) generation: FixtureGeneration,
    pub(super) teardown: bool,
}

impl ExecutionState {
    pub(super) fn release_test_objects(&mut self) {
        let tests = std::mem::take(&mut self.tests);
        if tests.is_empty() {
            return;
        }
        // Preserve the former independent maps' release order on aborted runs.
        let mut instances = Vec::with_capacity(tests.len());
        let mut arguments = Vec::with_capacity(tests.len());
        let mut resolved = Vec::with_capacity(tests.len());
        for (_, test) in tests {
            if let Some(instance) = test.instance {
                instances.push(instance);
            }
            arguments.push(test.arguments);
            resolved.push(test.resolved);
        }
        drop(instances);
        drop(arguments);
        drop(resolved);
    }

    pub(super) fn fixture(&self, fixture: FixtureId) -> Option<&FixtureInstance> {
        self.cache.get(fixture.index())?.first()
    }

    pub(super) fn instance(&self, key: &FixtureCacheKey) -> Option<&FixtureInstance> {
        self.cache.get(key.identity.fixture.index())?.get(key)
    }

    pub(super) fn take_instance(&mut self, key: &FixtureCacheKey) -> Option<FixtureInstance> {
        self.cache.get_mut(key.identity.fixture.index())?.take(key)
    }

    pub(super) fn insert_instance(
        &mut self,
        mut instance: FixtureInstance,
    ) -> Result<(), Box<(PyErr, FixtureInstance)>> {
        let Some(slot) = self.cache.get_mut(instance.key.identity.fixture.index()) else {
            return Err(Box::new((
                PyAssertionError::new_err("fixture instance references an unavailable fixture"),
                instance,
            )));
        };
        let Some(order) = self.next_instance_order.checked_add(1) else {
            return Err(Box::new((
                PyValueError::new_err("fixture instance order limit exceeded"),
                instance,
            )));
        };
        self.next_instance_order = order;
        instance.order = order;
        slot.insert(instance);
        Ok(())
    }
}
