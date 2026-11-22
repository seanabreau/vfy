use super::fixture_cache::FixtureCacheStorage;
use super::state::{ExecutionState, FixtureInstance, FixtureSlot};
use crate::independent::fixture_binding::NativeFixtureBinding;
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::cell::Cell;
use std::rc::Rc;
use vfy_core::{
    CollectionNodeId, FixtureCacheIdentity, FixtureCacheKey, FixtureGeneration, FixtureId,
    FixtureOwner, FixtureScope,
};

fn instance(py: Python<'_>, key: FixtureCacheKey) -> PyResult<FixtureInstance> {
    let binding = NativeFixtureBinding::new(
        py,
        key.identity.fixture,
        "resource".to_owned(),
        FixtureScope::Module,
        py.import("builtins")?.getattr("object")?.unbind(),
    )?;
    Ok(FixtureInstance {
        order: 0,
        key,
        storage: FixtureCacheStorage::new(binding),
        finalizers: PyList::empty(py).unbind(),
        lifetime: Rc::new(Cell::new(true)),
    })
}

#[test]
fn fixture_instances_keep_overlapping_generations_and_full_identities() -> PyResult<()> {
    Python::initialize();
    Python::attach(|py| {
        let first_generation = FixtureGeneration::default()
            .checked_next()
            .expect("first fixture generation");
        let second_generation = first_generation
            .checked_next()
            .expect("second fixture generation");
        let third_generation = second_generation
            .checked_next()
            .expect("third fixture generation");
        let first = FixtureCacheKey {
            identity: FixtureCacheIdentity {
                fixture: FixtureId::from_index(0),
                owner: FixtureOwner::Collection(CollectionNodeId::from_index(1)),
                scope: FixtureScope::Module,
            },
            generation: first_generation,
        };
        let mut second = first.clone();
        second.generation = third_generation;
        second.identity.owner = FixtureOwner::Collection(CollectionNodeId::from_index(2));
        let mut other = first.clone();
        other.identity.fixture = FixtureId::from_index(1);
        other.generation = second_generation;
        let mut wrong_owner = second.clone();
        wrong_owner.identity.owner = first.identity.owner.clone();
        let mut state = ExecutionState {
            cache: vec![FixtureSlot::Empty, FixtureSlot::Empty],
            ..ExecutionState::default()
        };
        assert!(state.insert_instance(instance(py, first.clone())?).is_ok());
        assert!(state.insert_instance(instance(py, other.clone())?).is_ok());
        assert!(state.insert_instance(instance(py, second.clone())?).is_ok());
        assert!(state.instance(&first).is_some());
        assert!(state.instance(&second).is_some());
        assert!(state.instance(&other).is_some());
        assert!(state.instance(&wrong_owner).is_none());
        assert_eq!(
            state
                .fixture(first.identity.fixture)
                .map(|value| &value.key),
            Some(&first)
        );
        let removed = state
            .take_instance(&first)
            .expect("first generation retained");
        assert_eq!(removed.key, first);
        assert!(state.instance(&first).is_none());
        assert!(state.instance(&second).is_some());
        assert!(state.instance(&other).is_some());
        assert!(matches!(&state.cache[0], FixtureSlot::One(_)));
        let removed = state
            .take_instance(&second)
            .expect("second generation retained");
        assert_eq!(removed.key, second);
        assert!(matches!(&state.cache[0], FixtureSlot::Empty));
        assert!(state.instance(&other).is_some());
        Ok(())
    })
}
