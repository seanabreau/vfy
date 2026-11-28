//! Expand function definitions into their executable resources.

use super::arena;
use crate::hooks::NativeHookRegistry;
use crate::independent::parameters::{ParameterGeneration, expand_fixture_rows, parameter_rows};
use crate::independent::{
    Collector, TestBinding, arguments_with_keys, asyncio, callspec, function_marks, lifecycle,
    metadata, parameters,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::sync::Arc;
use vfy_core::independent_collection::CollectionFile;
use vfy_core::test_plan::{
    FixtureChains, FixtureOverride, ScopeOwners, TestBody, TestFixtureState, TestId, TestPlan,
};
use vfy_core::{AsyncioLoopScope, FixtureScope};

/// Module and class owners of the test named ``name`` (class-qualified when ``class`` is set).
fn scope_owners(
    file: &CollectionFile,
    name: &str,
    class: Option<&Bound<'_, PyAny>>,
) -> ScopeOwners {
    ScopeOwners {
        module: Arc::from(file.nodeid.as_str()),
        class: class.map(|_| {
            Arc::from(format!(
                "{}::{}",
                file.nodeid,
                name.split("::").next().unwrap_or(name)
            ))
        }),
    }
}

struct FunctionParameters<'a, 'py> {
    definition: arena::CollectionNodeId,
    file: &'a CollectionFile,
    name: &'a str,
    callable: &'a Bound<'py, PyAny>,
    class: Option<&'a Bound<'py, PyAny>>,
    chains: &'a Arc<FixtureChains>,
    arguments: Arc<[String]>,
    argument_keys: Py<PyTuple>,
    eager: Arc<[String]>,
    owners: ScopeOwners,
    marks: Bound<'py, PyList>,
    function_mark_count: usize,
    fixture_names: Vec<String>,
    fixture_name_list: Py<PyList>,
    direct_chains: HashMap<Vec<(String, FixtureScope)>, ParameterShape>,
    fixture_states: HashMap<Vec<ParameterBinding>, Arc<TestFixtureState>>,
}

#[derive(Eq, Hash, PartialEq)]
struct ParameterBinding {
    name: String,
    scope: FixtureScope,
    direct: bool,
}

fn test_fixture_state(shape: &[ParameterBinding]) -> TestFixtureState {
    let mut state = TestFixtureState::default();
    for binding in shape {
        state.fixture_overrides.insert(
            binding.name.clone(),
            FixtureOverride {
                cache_scope: Some(binding.scope),
            },
        );
        if binding.direct {
            state.direct_parameter_names.insert(binding.name.clone());
        }
    }
    state
}

struct ParameterShape {
    chains: Arc<FixtureChains>,
    closures: Vec<(Option<AsyncioLoopScope>, Vec<String>)>,
}

struct FunctionDraft<'a, 'py> {
    test: &'a TestPlan,
    callable: &'a Bound<'py, PyAny>,
    class: Option<&'a Bound<'py, PyAny>>,
    marks: Bound<'py, PyList>,
    callspec: Option<Py<PyAny>>,
    argument_keys: &'a Bound<'py, PyTuple>,
    own_mark_count: usize,
    parameter_mark_start: usize,
    fixture_names: Bound<'py, PyList>,
}

impl<'py> Collector<'py> {
    fn generate_tests(
        &self,
        callable: &Bound<'py, PyAny>,
        class: Option<&Bound<'py, PyAny>>,
        definition: &TestPlan,
        marks: &Bound<'py, PyList>,
    ) -> PyResult<arena::CollectionNodeId> {
        let Some(context) = &self.context else {
            return Err(PyRuntimeError::new_err(
                "parameter generation requires a session",
            ));
        };
        let parent = self
            .current_node
            .ok_or_else(|| PyRuntimeError::new_err("generation has no owning collector"))?;
        let fixture_names = PyList::new(
            self.py,
            lifecycle::nodes::fixture_names(definition, &self.fixtures),
        )?;
        let id = arena::function(
            self.py,
            context,
            parent,
            arena::FunctionInput {
                kind: arena::CollectionNodeKind::FunctionDefinition,
                plan: definition,
                fixture_names: &fixture_names,
                callable,
                class,
                marks,
                own_mark_count: function_marks(callable)?.len(),
            },
        )?;
        let arena = arena::for_context(self.py, context)?;
        let item = arena.borrow().object(self.py, id)?;
        let module = item.bind(self.py).getattr("module")?;
        let metafunc = self
            .py
            .import("vfy.independent.protocol")?
            .getattr("Metafunc")?
            .call1((&item, class, &module))?;
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("metafunc", &metafunc)?;
        context.registry.bind(self.py).call_method1(
            "call_subset",
            (
                "pytest_generate_tests",
                kwargs,
                NativeHookRegistry::item_exclusions(
                    context.registry.bind(self.py),
                    item.bind(self.py),
                )?,
            ),
        )?;
        if let Some(hook) = metadata(&module, "pytest_generate_tests")? {
            hook.call1((&metafunc,))?;
        }
        if let Some(class) = class
            && class.hasattr("pytest_generate_tests")?
        {
            class
                .call0()?
                .getattr("pytest_generate_tests")?
                .call1((&metafunc,))?;
        }
        for mark in metafunc.getattr("_marks")?.try_iter()? {
            marks.append(mark?)?;
        }
        Ok(id)
    }

    pub(super) fn add_test(
        &mut self,
        file: &CollectionFile,
        name: &str,
        callable: &Bound<'py, PyAny>,
        class: Option<&Bound<'py, PyAny>>,
        chains: &Arc<FixtureChains>,
    ) -> PyResult<Vec<arena::CollectionNodeId>> {
        if callable.hasattr("__test__")? && !callable.getattr("__test__")?.is_truthy()? {
            return Ok(Vec::new());
        }
        self.validate_test_callable(callable, file, name)?;
        let marks = self.test_marks(callable, class)?;
        let binding = self.test_argument_binding(class, name)?;
        let (arguments, argument_keys) = arguments_with_keys(callable, binding)?;
        let eager = self.eager_fixtures(chains, &marks)?;

        let mut roots = arguments
            .iter()
            .chain(eager.iter())
            .cloned()
            .collect::<Vec<_>>();
        let observed_marks = PyList::new(self.py, marks.iter())?;
        let function_mark_count = function_marks(callable)?.len();
        let mut definition_plan = TestPlan {
            id: TestId::from_index(self.tests.len()),
            collection_node: None,
            nodeid: format!("{}::{name}", file.nodeid),
            body: TestBody::Function,
            async_loop_scope: None,
            argument_names: arguments.clone().into(),
            eager_fixtures: eager.clone().into(),
            fixture_chains: Arc::clone(chains),
            fixture_state: Some(Arc::new(vfy_core::test_plan::TestFixtureState {
                direct_parameter_names: parameters::direct_parameter_names(&marks)?
                    .into_iter()
                    .collect(),
                ..vfy_core::test_plan::TestFixtureState::default()
            })),
            owners: scope_owners(file, name, class),
        };
        let marker_count = marks.len();
        let definition = self.generate_tests(callable, class, &definition_plan, &marks)?;
        let escaping = self.test_id_escaping()?;
        let empty_policy = self.ini_string("empty_parameter_set_mark")?;
        let generation = ParameterGeneration {
            callable,
            nodeid: &definition_plan.nodeid,
            empty_policy: &empty_policy,
            escaping,
            marker_count,
        };
        let rows = parameter_rows(&generation, &marks)?;
        self.asyncio_roots(callable, &marks, &rows, function_mark_count, &mut roots)?;
        let rows = expand_fixture_rows(
            &generation,
            rows,
            &self.fixtures,
            &self.fixture_bindings,
            chains,
            &roots,
        )?;
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("function collection requires a session"))?;
        let arena = arena::for_context(self.py, context)?;
        let definition_object = arena.borrow().object(self.py, definition)?;
        let fixture_name_list = definition_object
            .bind(self.py)
            .getattr("fixturenames")?
            .cast_into::<PyList>()?
            .unbind();
        let direct_parameters = parameters::direct_parameter_names(&marks)?;
        Arc::make_mut(
            definition_plan
                .fixture_state
                .get_or_insert_with(Arc::default),
        )
        .direct_parameter_names = direct_parameters.iter().cloned().collect();
        let fixture_names = lifecycle::nodes::fixture_names(&definition_plan, &self.fixtures);
        if rows.iter().any(|row| !row.parameter_order.is_empty()) {
            lifecycle::nodes::prune_fixture_names(
                fixture_name_list.bind(self.py),
                &fixture_names,
                &direct_parameters,
            )?;
        }
        let mut input = FunctionParameters {
            definition,
            file,
            name,
            callable,
            class,
            chains,
            arguments: arguments.into(),
            argument_keys,
            eager: eager.into(),
            owners: definition_plan.owners.clone(),
            marks: observed_marks,
            function_mark_count,
            fixture_names,
            fixture_name_list,
            direct_chains: HashMap::new(),
            fixture_states: HashMap::new(),
        };
        rows.into_iter()
            .map(|row| self.add_parameterized_function(&mut input, row))
            .collect()
    }

    pub(super) fn add_unittest(
        &mut self,
        file: &CollectionFile,
        name: &str,
        callable: &Bound<'py, PyAny>,
        class: &Bound<'py, PyAny>,
        chains: &Arc<FixtureChains>,
    ) -> PyResult<arena::CollectionNodeId> {
        let marks = self.test_marks(callable, Some(class))?;
        let own_mark_count = function_marks(callable)?.len();
        let test = TestPlan {
            id: TestId::from_index(self.tests.len()),
            collection_node: None,
            nodeid: format!("{}::{name}", file.nodeid),
            body: TestBody::Function,
            async_loop_scope: None,
            argument_names: Vec::new().into(),
            eager_fixtures: self.eager_fixtures(chains, &marks)?.into(),
            fixture_chains: Arc::clone(chains),
            fixture_state: Some(Arc::new(vfy_core::test_plan::TestFixtureState::default())),
            owners: scope_owners(file, name, Some(class)),
        };
        let binding = self.bind_function(FunctionDraft {
            test: &test,
            callable,
            class: Some(class),
            marks,
            callspec: None,
            argument_keys: &PyTuple::empty(self.py),
            own_mark_count,
            parameter_mark_start: own_mark_count,
            fixture_names: PyList::new(
                self.py,
                lifecycle::nodes::fixture_names(&test, &self.fixtures),
            )?,
        })?;
        Ok(self.push_test(test, binding))
    }

    /// Store a test plan with its binding, which owns the plan's collection node.
    pub(super) fn push_test(
        &mut self,
        mut test: TestPlan,
        binding: TestBinding,
    ) -> arena::CollectionNodeId {
        let id = binding.collection_node;
        test.collection_node = Some(id);
        self.tests.push(test);
        self.test_bindings.push(binding);
        id
    }

    fn add_parameterized_function(
        &mut self,
        input: &mut FunctionParameters<'_, 'py>,
        row: parameters::ParameterRow,
    ) -> PyResult<arena::CollectionNodeId> {
        let callspec = callspec::from_row(self.py, &row, &self.fixtures)?;
        let direct_shape = row
            .parameters
            .keys()
            .map(|name| (name.clone(), row.scopes[name]))
            .collect::<Vec<_>>();
        let shape = match input.direct_chains.entry(direct_shape) {
            Entry::Occupied(entry) => entry.into_mut(),
            Entry::Vacant(entry) => {
                // Values and indices belong to each callspec; provider identities
                // depend only on this definition's direct names and scopes.
                let chains = self.direct_parameter_chains(input.definition, &row, input.chains)?;
                entry.insert(ParameterShape {
                    chains,
                    closures: Vec::new(),
                })
            }
        };
        let test_chains = Arc::clone(&shape.chains);
        let marks = PyList::new(self.py, input.marks.iter())?;
        let own_mark_count = input.function_mark_count + row.marks.len();
        for (index, mark) in row.marks.iter().enumerate() {
            marks.insert(input.function_mark_count + index, mark.bind(self.py))?;
        }
        let name = format!("{}{}", input.name, row.suffix);
        let nodeid = format!("{}::{name}", input.file.nodeid);
        let state_shape = row
            .scopes
            .into_iter()
            .map(|(name, scope)| {
                let direct = row.parameters.contains_key(&name);
                ParameterBinding {
                    name,
                    scope,
                    direct,
                }
            })
            .collect::<Vec<_>>();
        let state = match input.fixture_states.entry(state_shape) {
            Entry::Occupied(entry) => Arc::clone(entry.get()),
            Entry::Vacant(entry) => {
                let state = Arc::new(test_fixture_state(entry.key()));
                entry.insert(Arc::clone(&state));
                state
            }
        };
        let (async_loop_scope, own_mark_count) =
            self.asyncio_test_plan(input.callable, &name, &marks, own_mark_count)?;
        let eager_fixtures = if let Some(scope) = async_loop_scope {
            let mut eager_fixtures = input.eager.to_vec();
            eager_fixtures.push(asyncio::runner_name(scope));
            eager_fixtures.into()
        } else {
            Arc::clone(&input.eager)
        };
        let test = TestPlan {
            id: TestId::from_index(self.tests.len()),
            collection_node: None,
            nodeid,
            body: TestBody::Function,
            async_loop_scope,
            argument_names: Arc::clone(&input.arguments),
            eager_fixtures,
            fixture_chains: test_chains,
            fixture_state: Some(state),
            owners: input.owners.clone(),
        };
        // The chains and direct parameter names are fixed by this shape; the
        // async scope is the only other per-row input to the fixture closure.
        let closures = &mut shape.closures;
        let closure_index = closures
            .iter()
            .position(|(scope, _)| *scope == async_loop_scope)
            .unwrap_or_else(|| {
                closures.push((
                    async_loop_scope,
                    lifecycle::nodes::fixture_names(&test, &self.fixtures),
                ));
                closures.len() - 1
            });
        let fixture_names = &closures[closure_index].1;
        let fixture_names = if *fixture_names == input.fixture_names {
            input.fixture_name_list.bind(self.py).clone()
        } else {
            PyList::new(self.py, fixture_names)?
        };
        let binding = self.bind_function(FunctionDraft {
            test: &test,
            callable: input.callable,
            class: input.class,
            marks,
            callspec,
            argument_keys: input.argument_keys.bind(self.py),
            own_mark_count,
            parameter_mark_start: input.function_mark_count,
            fixture_names,
        })?;
        Ok(self.push_test(test, binding))
    }

    fn bind_function(&self, draft: FunctionDraft<'_, 'py>) -> PyResult<TestBinding> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("function collection requires a session"))?;
        let parent = self
            .current_node
            .ok_or_else(|| PyRuntimeError::new_err("function has no owning collector"))?;
        let id = arena::function(
            self.py,
            context,
            parent,
            arena::FunctionInput {
                kind: arena::CollectionNodeKind::Function,
                plan: draft.test,
                fixture_names: &draft.fixture_names,
                callable: draft.callable,
                class: draft.class,
                marks: &draft.marks,
                own_mark_count: draft.own_mark_count,
            },
        )?;
        let binding = TestBinding {
            collection_node: id,
            provider_scope: 0,
            callable: draft.callable.clone().unbind(),
            class: draft.class.map(|class| class.clone().unbind()),
            argument_keys: draft.argument_keys.clone().unbind(),
        };
        let arena = arena::for_context(self.py, context)?;
        let item = arena.borrow().object(self.py, id)?;
        lifecycle::nodes::attach_parameters(
            self.py,
            item.bind(self.py),
            draft.callspec,
            draft.parameter_mark_start,
        )?;
        Ok(binding)
    }
}
