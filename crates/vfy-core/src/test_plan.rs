//! Fixture and test plans independent of pytest object identity.
//!
//! The adapter keeps Python objects in tables aligned with these records.

use crate::{AsyncioLoopScope, FixtureId, FixtureScope};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

/// Stable test identity within a compiled suite.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct TestId(usize);

impl TestId {
    /// Construct an identity from its index in the compiled suite.
    #[must_use]
    pub const fn from_index(index: usize) -> Self {
        Self(index)
    }

    /// Return the test table index.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0
    }
}

/// Kind of test body the native runner invokes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TestBody {
    Function,
    Unittest,
    Doctest,
    /// A plugin item executed through its own `setup`, `runtest`, and `teardown`.
    Item,
}

/// How a fixture definition is executed by the native runner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FixtureExecution {
    Callable,
    Generator,
    DirectParameter,
    AsyncCoroutine,
    AsyncGenerator,
    UnsupportedAsync,
}

/// Fixture metadata. The adapter stores its callable and definition at the same `FixtureId`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct FixturePlan {
    pub id: FixtureId,
    pub name: String,
    pub package_baseid: String,
    pub dependencies: Vec<String>,
    pub declared_scope: FixtureScope,
    pub parameterized: bool,
    pub async_loop_scope: Option<AsyncioLoopScope>,
    pub execution: FixtureExecution,
}

/// Ordered visibility chain for a fixture name.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct FixtureChains {
    pub ordered_names: Vec<String>,
    pub by_name: HashMap<String, Vec<FixtureId>>,
}

/// Per-test scope override for one fixture name.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FixtureOverride {
    pub cache_scope: Option<FixtureScope>,
}

/// A test's selected fixture, before the adapter attaches its parameter and owner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FixtureUse {
    pub fixture: FixtureId,
    pub cache_scope: FixtureScope,
}

/// One definition in a dynamically discovered fixture chain.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DynamicFixturePlanEntry {
    Existing {
        fixture: FixtureId,
        cache_scope: FixtureScope,
    },
    Pending {
        fixture: FixtureId,
        cache_scope: FixtureScope,
    },
    New {
        fixture: FixturePlan,
        cache_scope: FixtureScope,
    },
}

/// Resolved fixture location, used by the adapter to find its parameter value.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FixtureResolution {
    pub use_: FixtureUse,
    pub source: FixtureChainSource,
    pub chain_index: usize,
}

/// Which fixture chain supplied a resolved use.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FixtureChainSource {
    Collected,
    Dynamic,
}

/// Test scope-owner identities needed for fixture cache planning.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScopeOwners {
    /// The containing class, or the test itself when there is no class collector.
    pub class: Option<Arc<str>>,
    pub module: Arc<str>,
}

/// Per-test parameter overrides and dynamically discovered fixture chains.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TestFixtureState {
    pub fixture_overrides: HashMap<String, FixtureOverride>,
    pub dynamic_fixture_uses: HashMap<String, Vec<FixtureUse>>,
    pub direct_parameter_names: HashSet<String>,
}

/// One executable test and its fixture plan.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TestPlan {
    pub id: TestId,
    /// Collection node in the independent frontend; absent in the pytest frontend.
    pub collection_node: Option<crate::CollectionNodeId>,
    pub nodeid: String,
    pub body: TestBody,
    pub async_loop_scope: Option<AsyncioLoopScope>,
    pub argument_names: Arc<[String]>,
    pub eager_fixtures: Arc<[String]>,
    pub fixture_chains: Arc<FixtureChains>,
    pub fixture_state: Option<Arc<TestFixtureState>>,
    pub owners: ScopeOwners,
}

/// Whether a fixture closure exposes direct parameters as terminal names.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FixtureClosureMode {
    FixturesOnly,
    IncludeDirectParameters,
}

impl TestPlan {
    /// Return statically requested names in dependency discovery order.
    ///
    /// Follow an overridden definition only when its replacement requests that
    /// name. Direct parameter values are excluded, and dynamic request lookups
    /// do not add roots to this query.
    #[must_use]
    pub fn static_fixture_names<'a>(&'a self, fixtures: &'a [FixturePlan]) -> Vec<&'a str> {
        self.static_fixture_closure(fixtures, FixtureClosureMode::FixturesOnly)
    }

    /// Traverse the compiled fixture graph, optionally retaining direct parameters as leaves.
    #[must_use]
    pub fn static_fixture_closure<'a>(
        &'a self,
        fixtures: &'a [FixturePlan],
        mode: FixtureClosureMode,
    ) -> Vec<&'a str> {
        self.static_fixture_closure_with_fallback(fixtures, mode, |_| &[])
    }

    /// Use dependency facts supplied by the execution frontend for unresolved fixture names.
    #[must_use]
    pub fn static_fixture_closure_with_fallback<'a>(
        &'a self,
        fixtures: &'a [FixturePlan],
        mode: FixtureClosureMode,
        fallback: impl Fn(&str) -> &'a [&'a str],
    ) -> Vec<&'a str> {
        enum Visit<'a> {
            Enter(&'a str),
            Leave(&'a str, usize),
        }

        let mut pending = self
            .eager_fixtures
            .iter()
            .chain(self.argument_names.iter())
            .rev()
            .map(|name| Visit::Enter(name.as_str()))
            .collect::<Vec<_>>();
        let mut active = HashMap::<&str, usize>::new();
        let mut completed = HashSet::new();
        let mut names = Vec::new();
        while let Some(visit) = pending.pop() {
            let name = match visit {
                Visit::Enter(name) => name,
                Visit::Leave(name, depth) => {
                    if depth == 0 {
                        active.remove(name);
                        completed.insert(name);
                    } else {
                        active.insert(name, depth);
                    }
                    continue;
                }
            };
            if completed.contains(name) {
                continue;
            }
            if self
                .fixture_state
                .as_ref()
                .is_some_and(|state| state.direct_parameter_names.contains(name))
            {
                if mode == FixtureClosureMode::IncludeDirectParameters {
                    names.push(name);
                }
                completed.insert(name);
                continue;
            }
            let depth = active.get(name).copied().unwrap_or_default();
            if depth == 0 {
                names.push(name);
            }
            let (declared, supplied): (&[String], &[&str]) =
                self.resolve_fixture(fixtures, name, depth).map_or_else(
                    || (&[][..], fallback(name)),
                    |resolved| {
                        (
                            &fixtures[resolved.use_.fixture.index()].dependencies[..],
                            &[][..],
                        )
                    },
                );
            if declared.is_empty() && supplied.is_empty() {
                if depth == 0 {
                    completed.insert(name);
                }
                continue;
            }
            active.insert(name, depth + 1);
            pending.push(Visit::Leave(name, depth));
            pending.extend(
                declared
                    .iter()
                    .map(String::as_str)
                    .chain(supplied.iter().copied())
                    .rev()
                    .map(Visit::Enter),
            );
        }
        names
    }

    /// Return the fixture owner available in the compiled plan.
    ///
    /// Function ownership and the no-class fallback use the case's node ID,
    /// so compact cases do not retain another copy of that identity.
    #[must_use]
    pub fn scope_owner(&self, scope: FixtureScope) -> Option<&str> {
        match scope {
            FixtureScope::Function => Some(&self.nodeid),
            FixtureScope::Class => Some(self.owners.class.as_deref().unwrap_or(&self.nodeid)),
            FixtureScope::Module => Some(&self.owners.module),
            FixtureScope::Package | FixtureScope::Session => None,
        }
    }

    /// Whether this test can resolve a fixture by name.
    #[must_use]
    pub fn contains_fixture(&self, name: &str) -> bool {
        self.fixture_state
            .as_ref()
            .is_some_and(|state| state.dynamic_fixture_uses.contains_key(name))
            || self.fixture_chains.by_name.contains_key(name)
    }

    /// Resolve a fixture name at a shadowing depth, preferring dynamically
    /// discovered definitions over the collected shared chain.
    #[must_use]
    pub fn resolve_fixture(
        &self,
        fixtures: &[FixturePlan],
        name: &str,
        same_name_depth: usize,
    ) -> Option<FixtureResolution> {
        let state = self.fixture_state.as_ref();
        let dynamic = state.and_then(|state| state.dynamic_fixture_uses.get(name));
        let shared = self.fixture_chains.by_name.get(name);
        let chain_len = dynamic.map_or_else(|| shared.map_or(0, Vec::len), Vec::len);
        let chain_index = chain_len.checked_sub(same_name_depth.checked_add(1)?)?;
        if let Some(uses) = dynamic {
            return Some(FixtureResolution {
                use_: *uses.get(chain_index)?,
                source: FixtureChainSource::Dynamic,
                chain_index,
            });
        }
        let fixture = *shared?.get(chain_index)?;
        Some(FixtureResolution {
            use_: FixtureUse {
                fixture,
                cache_scope: state
                    .and_then(|state| state.fixture_overrides.get(name))
                    .and_then(|override_| override_.cache_scope)
                    .unwrap_or(fixtures.get(fixture.index())?.declared_scope),
            },
            source: FixtureChainSource::Collected,
            chain_index,
        })
    }
}

/// Validated plan for one native suite.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SuitePlan {
    tests: Vec<TestPlan>,
    fixtures: Vec<FixturePlan>,
    no_cover_cases: Option<HashSet<usize>>,
}

impl SuitePlan {
    /// Build and validate a complete suite plan.
    ///
    /// # Errors
    ///
    /// Returns a diagnostic when test/fixture handles or fixture chains do
    /// not agree with the plan tables.
    pub fn new(
        tests: Vec<TestPlan>,
        fixtures: Vec<FixturePlan>,
        no_cover_cases: Option<HashSet<usize>>,
    ) -> Result<Self, String> {
        let plan = Self {
            tests,
            fixtures,
            no_cover_cases,
        };
        plan.validate()?;
        Ok(plan)
    }

    /// Return tests in collection order.
    #[must_use]
    pub fn tests(&self) -> &[TestPlan] {
        &self.tests
    }

    /// Return fixtures indexed by `FixtureId`.
    #[must_use]
    pub fn fixtures(&self) -> &[FixturePlan] {
        &self.fixtures
    }

    /// Return the set of test indices excluded from coverage, if supplied.
    #[must_use]
    pub fn no_cover_cases(&self) -> Option<&HashSet<usize>> {
        self.no_cover_cases.as_ref()
    }

    /// Select tests by their current indices, preserving the requested order.
    /// Repeated indices produce independent test plans. Coverage exclusions are
    /// remapped to the selected positions.
    ///
    /// # Errors
    ///
    /// Returns an error without changing the plan if an index is out of range.
    pub fn select_tests(&mut self, order: &[usize]) -> Result<(), String> {
        if let Some(&index) = order.iter().find(|&&index| index >= self.tests.len()) {
            return Err(format!("selection references missing test {index}"));
        }
        let no_cover_cases = self.no_cover_cases.as_ref().map(|cases| {
            order
                .iter()
                .enumerate()
                .filter_map(|(position, index)| cases.contains(index).then_some(position))
                .collect()
        });
        let mut remaining = vec![0usize; self.tests.len()];
        for &index in order {
            remaining[index] += 1;
        }
        let mut old = std::mem::take(&mut self.tests)
            .into_iter()
            .map(Some)
            .collect::<Vec<_>>();
        let mut selected = Vec::with_capacity(order.len());
        for (position, &index) in order.iter().enumerate() {
            remaining[index] -= 1;
            let test = if remaining[index] == 0 {
                old[index].take()
            } else {
                old[index].clone()
            };
            let Some(mut test) = test else {
                return Err(format!("selection has unavailable test {index}"));
            };
            test.id = TestId::from_index(position);
            selected.push(test);
        }
        self.tests = selected;
        self.no_cover_cases = no_cover_cases;
        Ok(())
    }

    /// Atomically append newly discovered fixture definitions and install
    /// their test-local resolution chain.
    ///
    /// # Errors
    ///
    /// Returns an error when the test, existing fixture handles, or fixture
    /// names are inconsistent. On error, the suite plan is unchanged.
    pub fn register_dynamic_fixture_chain(
        &mut self,
        test: TestId,
        name: &str,
        entries: Vec<DynamicFixturePlanEntry>,
    ) -> Result<Vec<FixtureId>, String> {
        if test.index() >= self.tests.len() {
            return Err(format!(
                "dynamic fixture chain references missing test {}",
                test.index()
            ));
        }

        let mut new_fixtures: Vec<FixturePlan> = Vec::new();
        let mut fixture_ids = Vec::with_capacity(entries.len());
        let mut uses = Vec::with_capacity(entries.len());
        for entry in entries {
            let (fixture_id, cache_scope) = match entry {
                DynamicFixturePlanEntry::Existing {
                    fixture,
                    cache_scope,
                } => {
                    let Some(existing) = self.fixtures.get(fixture.index()) else {
                        return Err(format!(
                            "dynamic fixture chain {name:?} references missing fixture {}",
                            fixture.index()
                        ));
                    };
                    if existing.name != name {
                        return Err(format!(
                            "dynamic fixture chain {name:?} references fixture named {:?}",
                            existing.name
                        ));
                    }
                    (fixture, cache_scope)
                }
                DynamicFixturePlanEntry::Pending {
                    fixture,
                    cache_scope,
                } => {
                    let pending_offset = fixture
                        .index()
                        .checked_sub(self.fixtures.len())
                        .ok_or_else(|| {
                            format!(
                                "pending dynamic fixture handle {} is not a newly staged fixture",
                                fixture.index()
                            )
                        })?;
                    let Some(pending) = new_fixtures.get(pending_offset) else {
                        return Err(format!(
                            "pending dynamic fixture handle {} is not staged yet",
                            fixture.index()
                        ));
                    };
                    if pending.name != name {
                        return Err(format!(
                            "dynamic fixture chain {name:?} references pending fixture named {:?}",
                            pending.name
                        ));
                    }
                    (fixture, cache_scope)
                }
                DynamicFixturePlanEntry::New {
                    mut fixture,
                    cache_scope,
                } => {
                    if fixture.name != name {
                        return Err(format!(
                            "dynamic fixture chain {name:?} contains new fixture named {:?}",
                            fixture.name
                        ));
                    }
                    let index = self
                        .fixtures
                        .len()
                        .checked_add(new_fixtures.len())
                        .ok_or_else(|| "dynamic fixture identity space exhausted".to_owned())?;
                    let fixture_id = FixtureId::from_index(index);
                    fixture.id = fixture_id;
                    new_fixtures.push(fixture);
                    (fixture_id, cache_scope)
                }
            };
            fixture_ids.push(fixture_id);
            uses.push(FixtureUse {
                fixture: fixture_id,
                cache_scope,
            });
        }

        self.fixtures.extend(new_fixtures);
        let state = self.tests[test.index()]
            .fixture_state
            .get_or_insert_with(Arc::default);
        Arc::make_mut(state)
            .dynamic_fixture_uses
            .insert(name.to_owned(), uses);
        Ok(fixture_ids)
    }

    fn validate(&self) -> Result<(), String> {
        for (index, fixture) in self.fixtures.iter().enumerate() {
            if fixture.id.index() != index {
                return Err(format!(
                    "fixture at index {index} has identity {}",
                    fixture.id.index()
                ));
            }
        }
        let mut validated_chains = HashSet::new();
        for (index, test) in self.tests.iter().enumerate() {
            if test.id.index() != index {
                return Err(format!(
                    "test at index {index} has identity {}",
                    test.id.index()
                ));
            }
            let chain_identity = Arc::as_ptr(&test.fixture_chains);
            if validated_chains.insert(chain_identity) {
                validate_fixture_chains(&self.fixtures, &test.fixture_chains)?;
            }
            let Some(state) = &test.fixture_state else {
                continue;
            };
            let mut dynamic_names = state.dynamic_fixture_uses.keys().collect::<Vec<_>>();
            dynamic_names.sort_unstable();
            for name in dynamic_names {
                let uses = &state.dynamic_fixture_uses[name];
                for use_ in uses {
                    let Some(fixture) = self.fixtures.get(use_.fixture.index()) else {
                        return Err(format!(
                            "dynamic fixture use references missing fixture {}",
                            use_.fixture.index()
                        ));
                    };
                    if fixture.name != *name {
                        return Err(format!(
                            "dynamic fixture chain {name:?} references fixture named {:?}",
                            fixture.name
                        ));
                    }
                }
            }
        }
        if let Some(cases) = &self.no_cover_cases
            && let Some(index) = cases
                .iter()
                .copied()
                .filter(|index| *index >= self.tests.len())
                .min()
        {
            return Err(format!("no-cover marker references missing test {index}"));
        }
        Ok(())
    }
}

fn validate_fixture_chains(fixtures: &[FixturePlan], chains: &FixtureChains) -> Result<(), String> {
    if chains.ordered_names.len() != chains.by_name.len() {
        return Err("ordered fixture names and fixture chains have different lengths".to_owned());
    }
    let mut seen = HashSet::with_capacity(chains.ordered_names.len());
    for name in &chains.ordered_names {
        if !seen.insert(name.as_str()) {
            return Err(format!("fixture name {name:?} appears more than once"));
        }
        let Some(chain) = chains.by_name.get(name) else {
            return Err(format!("ordered fixture name {name:?} has no chain"));
        };
        for fixture in chain {
            let Some(actual) = fixtures.get(fixture.index()) else {
                return Err(format!(
                    "fixture chain {name:?} references missing index {}",
                    fixture.index()
                ));
            };
            if actual.name != *name {
                return Err(format!(
                    "fixture chain {name:?} references fixture named {:?} at index {}",
                    actual.name,
                    fixture.index()
                ));
            }
        }
    }
    if let Some(name) = chains
        .by_name
        .keys()
        .filter(|name| !seen.contains(name.as_str()))
        .min()
    {
        return Err(format!(
            "fixture chain {name:?} is missing from ordered names"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
