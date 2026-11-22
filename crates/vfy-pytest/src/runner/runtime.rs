//! Owns fixture outcomes, teardown failures, and the shared execution runtime.

use super::model::{
    CacheKey, CachedFixtureErrorTraceback, FixtureStack, PytestCompatibilityPolicy, SuiteBindings,
};
use super::report::{format_pyerr, is_control_error, is_skip_error, is_xfail_error};
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::{Bound, Py, PyAnyMethods, PyErr, PyResult, Python};
use pyo3::types::PyAny;
use serde::Serialize;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, Weak};
use vfy_core::test_plan::TestId;
use vfy_core::{FixtureGeneration, FixtureId};

#[derive(Serialize)]
#[expect(
    clippy::struct_field_names,
    reason = "every field ends in `_ms`; the names are the serialized JSON keys"
)]
pub(super) struct StageTiming {
    pub(super) compile_ms: f64,
    pub(super) warning_setup_ms: f64,
    pub(super) execute_ms: f64,
    pub(super) aggregate_ms: f64,
    pub(super) total_ms: f64,
}

pub(super) struct TeardownFailure {
    pub(super) detail: String,
    pub(super) outcome: TeardownOutcome,
    pub(super) control: Option<PyErr>,
    pub(super) exception: Option<PyErr>,
    pub(super) grouping: TeardownGrouping,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum TeardownGrouping {
    Original,
    Fixture,
    Owner,
    Run,
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum TeardownOutcome {
    Error,
    Skipped,
    Xfailed,
}

impl TeardownFailure {
    pub(super) fn from_pyerr(py: Python<'_>, error: &PyErr) -> Self {
        let outcome = if is_skip_error(py, error) {
            TeardownOutcome::Skipped
        } else if is_xfail_error(py, error) {
            TeardownOutcome::Xfailed
        } else {
            TeardownOutcome::Error
        };
        let control = is_control_error(py, error).then(|| error.clone_ref(py));
        Self {
            // Pytest renders propagated control exceptions at its session boundary.
            detail: if control.is_some()
                || (outcome == TeardownOutcome::Skipped && error.traceback(py).is_some())
            {
                String::new()
            } else {
                format_pyerr(py, error)
            },
            outcome,
            control,
            exception: Some(error.clone_ref(py)),
            grouping: TeardownGrouping::Original,
        }
    }

    pub(super) fn into_pyerr(self, py: Python<'_>) -> PyErr {
        if let Some(error) = self.control {
            return error;
        }
        if let Some(error) = self.exception {
            return error;
        }
        let exception = match self.outcome {
            TeardownOutcome::Error => return PyRuntimeError::new_err(self.detail),
            TeardownOutcome::Skipped => "Skipped",
            TeardownOutcome::Xfailed => "XFailed",
        };
        if let Ok(value) = py
            .import("_pytest.outcomes")
            .and_then(|module| module.getattr(exception))
            .and_then(|class| class.call1((&self.detail,)))
        {
            return PyErr::from_value(value);
        }
        PyRuntimeError::new_err(self.detail)
    }

    pub(super) fn aggregate_members(self, py: Python<'_>) -> PyResult<Vec<Py<PyAny>>> {
        let combined = self.grouping == TeardownGrouping::Run;
        let error = self.into_pyerr(py);
        if combined {
            error.value(py).getattr("exceptions")?.extract()
        } else {
            Ok(vec![error.value(py).clone().into_any().unbind()])
        }
    }

    pub(super) fn escapes_owner(&self, py: Python<'_>) -> bool {
        self.exception.as_ref().is_some_and(|error| {
            py.import("_pytest.outcomes")
                .and_then(|module| module.getattr("TEST_OUTCOME"))
                .is_ok_and(|outcomes| !error.matches(py, outcomes).unwrap_or(false))
        })
    }
}

pub(super) enum FixtureOutcome {
    Value(Py<PyAny>),
    Error {
        exception: Py<PyAny>,
        traceback: Py<PyAny>,
    },
}

impl FixtureOutcome {
    pub(super) fn from_setup(
        py: Python<'_>,
        setup: PyResult<(Py<PyAny>, Option<FixtureFinalizer>)>,
    ) -> (Self, Option<FixtureFinalizer>) {
        match setup {
            Ok((value, finalizer)) => (Self::Value(value), finalizer),
            Err(error) => (
                Self::Error {
                    exception: error.value(py).clone().into_any().unbind(),
                    traceback: error.traceback(py).map_or_else(|| py.None(), Py::from),
                },
                None,
            ),
        }
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Value(value) => Self::Value(value.clone_ref(py)),
            Self::Error {
                exception,
                traceback,
            } => Self::Error {
                exception: exception.clone_ref(py),
                traceback: traceback.clone_ref(py),
            },
        }
    }

    pub(super) fn clone_result(
        &self,
        py: Python<'_>,
        traceback_policy: CachedFixtureErrorTraceback,
    ) -> PyResult<Py<PyAny>> {
        match self {
            Self::Value(value) => Ok(value.clone_ref(py)),
            Self::Error {
                exception,
                traceback,
            } if matches!(traceback_policy, CachedFixtureErrorTraceback::Restore) => {
                Err(PyErr::from_value(
                    exception
                        .bind(py)
                        .call_method1("with_traceback", (traceback.bind(py),))?,
                ))
            }
            Self::Error { exception, .. } => Err(PyErr::from_value(exception.bind(py).clone())),
        }
    }
}

pub(super) struct FixtureInstance {
    pub(super) outcome: FixtureOutcome,
    pub(super) finalizers: Vec<FixtureFinalizer>,
    pub(super) param: Option<Py<PyAny>>,
    pub(super) last_resolved_test: usize,
    pub(super) retained_request_test_index: Option<usize>,
    pub(super) registered_owner: Option<String>,
}

pub(super) struct PackageSetup {
    pub(super) node: Py<PyAny>,
    pub(super) outcome: FixtureOutcome,
}

pub(super) enum OwnerFinalizer {
    Callable(Py<PyAny>),
    Fixture(CacheKey),
}

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum FixtureTeardownReason {
    ParameterSwitch,
    DependencyFinished,
    OwnerFinished,
}

pub(super) enum FixtureFinalizer {
    Dependent(FixtureId),
    Callable(Py<PyAny>),
    Generator {
        fixture: Py<PyAny>,
        generator: Py<PyAny>,
    },
}

pub(super) struct ResolvingFixture {
    pub(super) fixture: usize,
    pub(super) fixture_stack: FixtureStack,
    pub(super) dependencies: Vec<CacheKey>,
}

// Live requests keep their resolution history after their test completes, while
// the suite releases its copy to avoid accumulating completed compact cases.
#[derive(Clone, Default)]
pub(super) struct ResolvedFixtureKeys(Arc<Mutex<ResolvedFixtures>>);

#[derive(Default)]
struct ResolvedFixtures {
    keys: HashMap<String, CacheKey>,
    dynamic_names: Vec<String>,
}

impl ResolvedFixtureKeys {
    pub(super) fn get(&self, name: &str) -> Option<CacheKey> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .keys
            .get(name)
            .cloned()
    }

    pub(super) fn insert(&self, name: &str, key: CacheKey) {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .keys
            .insert(name.to_owned(), key);
    }

    pub(super) fn record_dynamic_name(&self, name: &str) {
        let mut resolved = self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if !resolved
            .dynamic_names
            .iter()
            .any(|existing| existing == name)
        {
            resolved.dynamic_names.push(name.to_owned());
        }
    }

    pub(super) fn dynamic_names(&self) -> Vec<String> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .dynamic_names
            .clone()
    }
}

pub(super) struct Runtime {
    pub(super) suite: SuiteBindings,
    pub(super) compatibility: PytestCompatibilityPolicy,
    pub(super) cache: HashMap<CacheKey, FixtureInstance>,
    pub(super) active: Vec<CacheKey>,
    pub(super) active_by_fixture: HashMap<FixtureId, CacheKey>,
    pub(super) pending_finalizers: HashMap<CacheKey, Vec<FixtureFinalizer>>,
    pub(super) owner_finalizers: HashMap<String, Vec<OwnerFinalizer>>,
    pub(super) active_nodes: Vec<String>,
    pub(super) owner_nodes: HashMap<String, Py<PyAny>>,
    pub(super) package_setups: HashMap<String, PackageSetup>,
    pub(super) active_function: Option<TestId>,
    pub(super) resolved_fixture_keys: HashMap<usize, ResolvedFixtureKeys>,
    pub(super) retained_request_counts: HashMap<usize, usize>,
    pub(super) resolving: Vec<ResolvingFixture>,
    pub(super) next_cache_generation: FixtureGeneration,
}

#[derive(Clone)]
pub(super) struct SharedRuntime(Arc<Mutex<Runtime>>);

#[derive(Clone)]
pub(super) struct WeakRuntime(Weak<Mutex<Runtime>>);

impl SharedRuntime {
    pub(super) fn new(runtime: Runtime) -> Self {
        Self(Arc::new(Mutex::new(runtime)))
    }

    pub(super) fn current_fixture_stack(&self) -> FixtureStack {
        self.lock()
            .resolving
            .last()
            .map_or_else(FixtureStack::default, |active| active.fixture_stack.clone())
    }

    pub(super) fn fixture_keys(&self, test_index: usize) -> ResolvedFixtureKeys {
        self.lock()
            .resolved_fixture_keys
            .entry(test_index)
            .or_default()
            .clone()
    }

    pub(super) fn downgrade(&self) -> WeakRuntime {
        WeakRuntime(Arc::downgrade(&self.0))
    }

    pub(super) fn cached_fixture_result(
        &self,
        py: Python<'_>,
        key: &CacheKey,
    ) -> Option<PyResult<Py<PyAny>>> {
        let cached = {
            let state = self.lock();
            state.cache.get(key).map(|instance| {
                (
                    instance.outcome.clone_ref(py),
                    state.compatibility.cached_fixture_error_traceback,
                )
            })
        };
        cached.map(|(outcome, policy)| outcome.clone_result(py, policy))
    }

    pub(super) fn lock(&self) -> MutexGuard<'_, Runtime> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl WeakRuntime {
    pub(super) fn upgrade(&self) -> Option<SharedRuntime> {
        self.0.upgrade().map(SharedRuntime)
    }
}

pub(super) fn register_node_finalizer(
    py: Python<'_>,
    runtime: &SharedRuntime,
    node: &Bound<'_, PyAny>,
    finalizer: Py<PyAny>,
) -> PyResult<()> {
    if !finalizer.bind(py).is_callable() {
        return Err(PyAssertionError::new_err("finalizer must be callable"));
    }
    let owner = node.getattr("nodeid")?.extract::<String>()?;
    let previous = {
        let mut state = runtime.lock();
        if !state.active_nodes.contains(&owner)
            || !state
                .owner_nodes
                .get(&owner)
                .is_some_and(|active| active.bind(py).is(node))
        {
            drop(state);
            return Err(PyAssertionError::new_err(node.clone().unbind()));
        }
        state
            .owner_finalizers
            .entry(owner.clone())
            .or_default()
            .push(OwnerFinalizer::Callable(finalizer));
        state.owner_nodes.insert(owner, node.clone().unbind())
    };
    drop(previous);
    Ok(())
}
