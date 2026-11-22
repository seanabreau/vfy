//! Owns the compiled suite model: fixture bindings and definitions, test
//! bindings, case call specifications, xfail plans, and the pytest
//! compatibility policy.

use crate::dense_reader::DenseCallSpecReader;
use crate::results::ReportLocation;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, PyStringMethods, Python,
};
use pyo3::types::{PyAny, PyDict, PyFunction, PyList, PyString, PyTuple};
use std::collections::{HashMap, HashSet};
use std::hash::{DefaultHasher, Hash, Hasher};
use std::sync::Arc;
use vfy_core::test_plan::{
    FixtureChains, FixtureExecution, FixturePlan, ScopeOwners, SuitePlan as CoreSuitePlan, TestPlan,
};
use vfy_core::{AsyncioLoopScope, AsyncioMode, FixtureCacheKey, FixtureId, FixtureScope};

pub(super) type CacheKey = FixtureCacheKey;

pub(super) fn parse_fixture_scope(value: &str) -> PyResult<FixtureScope> {
    FixtureScope::parse(value).ok_or_else(|| {
        PyTypeError::new_err(format!(
            "native engine does not recognize fixture scope {value:?}"
        ))
    })
}

#[derive(Clone, Copy, Default)]
pub(super) enum UnhandledAsyncFixture {
    Allow,
    Warn,
    #[default]
    Error,
}

#[derive(Clone, Copy)]
pub(super) enum ReturnedTestValue {
    WarnValue,
    FailValue,
    WarnType,
}

impl ReturnedTestValue {
    fn from_policy(policy: &Bound<'_, PyAny>) -> PyResult<Self> {
        match policy
            .getattr("returned_test_value")?
            .extract::<String>()?
            .as_str()
        {
            "warn_value" => Ok(Self::WarnValue),
            "fail_value" => Ok(Self::FailValue),
            "warn_type" => Ok(Self::WarnType),
            value => Err(PyValueError::new_err(format!(
                "unknown returned test value policy {value:?}"
            ))),
        }
    }

    pub(super) fn message(self, result: &Bound<'_, PyAny>, nodeid: &str) -> PyResult<String> {
        Ok(match self {
            Self::WarnValue => format!(
                "Expected None, but {nodeid} returned {}, which will be an error in a \
                 future version of pytest.  Did you mean to use `assert` instead of `return`?",
                result.repr()?,
            ),
            Self::FailValue => format!(
                "Expected None, but test returned {}. \
                 Did you mean to use `assert` instead of `return`?",
                result.repr()?,
            ),
            Self::WarnType => format!(
                "Test functions should return None, but {nodeid} returned {}.\n\
                 Did you mean to use `assert` instead of `return`?\n\
                 See https://docs.pytest.org/en/stable/how-to/assert.html#return-not-none for more information.",
                result.get_type().repr()?,
            ),
        })
    }
}

#[derive(Clone, Copy)]
pub(super) enum FixtureScopeChecks {
    Parametrization,
    ParametrizationThenDeclared,
    Declared,
}

impl FixtureScopeChecks {
    pub(super) fn checked_scope(
        self,
        declared: FixtureScope,
        effective: FixtureScope,
        repeated: bool,
    ) -> Option<FixtureScope> {
        match (self, repeated) {
            (Self::Parametrization, true) => None,
            (Self::Parametrization | Self::ParametrizationThenDeclared, false) => Some(effective),
            (Self::ParametrizationThenDeclared, true) | (Self::Declared, _) => Some(declared),
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum FixtureParameterComparison {
    Identity,
    Equality,
}

#[derive(Clone, Copy)]
pub(super) enum FixtureScopeOverrideLookup {
    Optional,
    Required,
}

impl FixtureScopeOverrideLookup {
    fn from_policy(policy: &Bound<'_, PyAny>) -> PyResult<Self> {
        match policy
            .getattr("fixture_scope_override_lookup")?
            .extract::<String>()?
            .as_str()
        {
            "optional" => Ok(Self::Optional),
            "required" => Ok(Self::Required),
            value => Err(PyValueError::new_err(format!(
                "unknown fixture scope override lookup {value:?}"
            ))),
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum CachedFixtureErrorTraceback {
    Preserve,
    Restore,
}

#[derive(Clone, Copy)]
pub(super) enum DynamicFixtureTeardown {
    Allow,
    WarnAndCheckOwner,
}

#[derive(Clone, Copy)]
pub(super) enum FixtureFinalizerErrors {
    First,
    Group,
}

impl FixtureFinalizerErrors {
    pub(super) fn select(self, errors: &mut Vec<PyErr>) {
        if matches!(self, Self::First) {
            errors.truncate(1);
        }
    }
}

#[derive(Clone, Copy)]
pub(super) struct PytestCompatibilityPolicy {
    pub(super) fixturedefs_use_nodeid: bool,
    pub(super) legacy_fixture_order: bool,
    pub(super) fixture_finalizer_errors: FixtureFinalizerErrors,
    pub(super) unittest_wide_scope_instance: bool,
    pub(super) parameter_comparison: FixtureParameterComparison,
    pub(super) scope_override_lookup: FixtureScopeOverrideLookup,
    pub(super) cached_fixture_error_traceback: CachedFixtureErrorTraceback,
    pub(super) scope_checks: FixtureScopeChecks,
    pub(super) dynamic_fixture_teardown: DynamicFixtureTeardown,
    pub(super) unhandled_async_fixture: UnhandledAsyncFixture,
    pub(super) returned_test_value: ReturnedTestValue,
}

impl PytestCompatibilityPolicy {
    pub(super) fn from_items(py: Python<'_>, items: &Bound<'_, PyAny>) -> PyResult<Self> {
        let first = items.try_iter()?.next().transpose()?;
        let policy = if let Some(first) = first {
            py.import("vfy._pytest.native_pytest")?
                .getattr("compatibility_policy")?
                .call1((first.getattr("config")?,))?
        } else {
            // Empty suites perform no fixture operations.
            return Ok(Self {
                fixturedefs_use_nodeid: false,
                legacy_fixture_order: false,
                fixture_finalizer_errors: FixtureFinalizerErrors::Group,
                unittest_wide_scope_instance: false,
                parameter_comparison: FixtureParameterComparison::Equality,
                scope_override_lookup: FixtureScopeOverrideLookup::Required,
                cached_fixture_error_traceback: CachedFixtureErrorTraceback::Restore,
                scope_checks: FixtureScopeChecks::Declared,
                dynamic_fixture_teardown: DynamicFixtureTeardown::Allow,
                unhandled_async_fixture: UnhandledAsyncFixture::Error,
                returned_test_value: ReturnedTestValue::WarnType,
            });
        };
        let unhandled_async_fixture = match policy
            .getattr("unhandled_async_fixture")?
            .extract::<String>()?
            .as_str()
        {
            "allow" => UnhandledAsyncFixture::Allow,
            "warn" => UnhandledAsyncFixture::Warn,
            "error" => UnhandledAsyncFixture::Error,
            value => {
                return Err(PyValueError::new_err(format!(
                    "unknown unhandled async fixture policy {value:?}"
                )));
            }
        };
        Ok(Self {
            fixturedefs_use_nodeid: policy.getattr("fixturedefs_use_nodeid")?.extract()?,
            legacy_fixture_order: policy.getattr("legacy_fixture_order")?.extract()?,
            fixture_finalizer_errors: if policy
                .getattr("group_fixture_finalizer_errors")?
                .extract()?
            {
                FixtureFinalizerErrors::Group
            } else {
                FixtureFinalizerErrors::First
            },
            unittest_wide_scope_instance: policy
                .getattr("unittest_wide_scope_instance")?
                .extract()?,
            parameter_comparison: if policy.getattr("fixture_parameter_identity")?.extract()? {
                FixtureParameterComparison::Identity
            } else {
                FixtureParameterComparison::Equality
            },
            scope_override_lookup: FixtureScopeOverrideLookup::from_policy(&policy)?,
            cached_fixture_error_traceback: if policy
                .getattr("cached_fixture_error_traceback")?
                .extract()?
            {
                CachedFixtureErrorTraceback::Restore
            } else {
                CachedFixtureErrorTraceback::Preserve
            },
            scope_checks: if !policy
                .getattr("initial_fixture_scope_uses_parametrization")?
                .extract::<bool>()?
            {
                FixtureScopeChecks::Declared
            } else if policy.getattr("repeated_fixture_scope_check")?.extract()? {
                FixtureScopeChecks::ParametrizationThenDeclared
            } else {
                FixtureScopeChecks::Parametrization
            },
            unhandled_async_fixture,
            returned_test_value: ReturnedTestValue::from_policy(&policy)?,
            dynamic_fixture_teardown: if policy
                .getattr("warn_dynamic_fixture_teardown")?
                .extract()?
            {
                DynamicFixtureTeardown::WarnAndCheckOwner
            } else {
                DynamicFixtureTeardown::Allow
            },
        })
    }
}

pub(super) struct FixtureBindings {
    pub(super) definition: Py<PyAny>,
    pub(super) callable: FixtureCallable,
}

pub(super) struct FixtureCallable {
    pub(super) callable: Py<PyAny>,
    code: Option<Py<PyAny>>,
    coroutine_marker: Option<Py<PyAny>>,
    cacheable: bool,
    explicitly_async: bool,
    explicit_loop_scope: Option<Py<PyAny>>,
    pub(super) execution: FixtureExecution,
    pub(super) async_loop_scope: Option<AsyncioLoopScope>,
}

impl FixtureCallable {
    pub(super) fn from_definition(
        config: &Bound<'_, PyAny>,
        inspect: &Bound<'_, PyAny>,
        direct_callable: &Bound<'_, PyAny>,
        mode: AsyncioMode,
        definition: &Bound<'_, PyAny>,
        dependencies: &[String],
    ) -> PyResult<Self> {
        let callable = definition.getattr(intern!(definition.py(), "func"))?;
        let execution = fixture_execution(inspect, &callable, dependencies, direct_callable, mode)?;
        let explicitly_async = callable
            .getattr(intern!(definition.py(), "_force_asyncio_fixture"))
            .is_ok_and(|value| value.is_truthy().unwrap_or(false));
        let async_loop_scope = if explicitly_async
            || matches!(
                execution,
                FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
            ) {
            let scope = parse_fixture_scope(
                &definition
                    .getattr(intern!(definition.py(), "scope"))?
                    .extract::<String>()?,
            )?;
            Some(fixture_async_loop_scope(config, definition, scope)?)
        } else {
            None
        };
        Ok(Self {
            code: callable
                .getattr_opt(intern!(definition.py(), "__code__"))?
                .map(Bound::unbind),
            coroutine_marker: callable
                .getattr_opt(intern!(definition.py(), "_is_coroutine_marker"))?
                .map(Bound::unbind),
            cacheable: callable.is_exact_instance_of::<PyFunction>()
                || inspect
                    .call_method1(intern!(definition.py(), "ismethod"), (&callable,))?
                    .is_truthy()?,
            explicit_loop_scope: if async_loop_scope.is_some() {
                callable
                    .getattr_opt(intern!(definition.py(), "_loop_scope"))?
                    .map(Bound::unbind)
            } else {
                None
            },
            explicitly_async,
            callable: callable.unbind(),
            execution,
            async_loop_scope,
        })
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            callable: self.callable.clone_ref(py),
            code: self.code.as_ref().map(|code| code.clone_ref(py)),
            coroutine_marker: self
                .coroutine_marker
                .as_ref()
                .map(|marker| marker.clone_ref(py)),
            cacheable: self.cacheable,
            explicitly_async: self.explicitly_async,
            explicit_loop_scope: self
                .explicit_loop_scope
                .as_ref()
                .map(|scope| scope.clone_ref(py)),
            execution: self.execution,
            async_loop_scope: self.async_loop_scope,
        }
    }

    pub(super) fn is_current(&self, callable: &Bound<'_, PyAny>) -> PyResult<bool> {
        if !self.cacheable || !self.callable.bind(callable.py()).is(callable) {
            return Ok(false);
        }
        let code = callable.getattr_opt(intern!(callable.py(), "__code__"))?;
        if !self
            .code
            .as_ref()
            .zip(code.as_ref())
            .is_some_and(|(cached, current)| cached.bind(callable.py()).is(current))
        {
            return Ok(false);
        }
        let marker = callable.getattr_opt(intern!(callable.py(), "_is_coroutine_marker"))?;
        match (&self.coroutine_marker, marker) {
            (None, None) => {}
            (Some(cached), Some(current)) if cached.bind(callable.py()).is(&current) => {}
            _ => return Ok(false),
        }
        let explicitly_async = callable
            .getattr_opt(intern!(callable.py(), "_force_asyncio_fixture"))?
            .map(|value| value.is_truthy())
            .transpose()?
            .unwrap_or(false);
        if self.explicitly_async != explicitly_async {
            return Ok(false);
        }
        if self.async_loop_scope.is_none() {
            return Ok(true);
        }
        let loop_scope = callable.getattr_opt(intern!(callable.py(), "_loop_scope"))?;
        let same_loop_scope = match (&self.explicit_loop_scope, loop_scope) {
            (None, None) => true,
            (Some(cached), Some(current)) => cached.bind(callable.py()).is(&current),
            _ => false,
        };
        Ok(same_loop_scope)
    }
}

pub(super) fn optional_mapping_item<'py>(
    mapping: &Bound<'py, PyAny>,
    key: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if mapping.is_exact_instance_of::<PyDict>() {
        return mapping.cast::<PyDict>()?.get_item(key);
    }
    Ok(mapping.get_item(key).ok())
}

pub(super) struct CompiledFixtureDefinition {
    pub(super) identity: usize,
    name: String,
    package_baseid: String,
    definition: Py<PyAny>,
    dependencies: Vec<String>,
    pub(super) scope: FixtureScope,
    parameterized: bool,
    callable: FixtureCallable,
}

impl CompiledFixtureDefinition {
    pub(super) fn from_py(
        config: &Bound<'_, PyAny>,
        inspect: &Bound<'_, PyAny>,
        direct_callable: &Bound<'_, PyAny>,
        mode: AsyncioMode,
        definition: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let dependencies = definition
            .getattr(intern!(definition.py(), "argnames"))?
            .extract::<Vec<String>>()?;
        let scope = parse_fixture_scope(
            &definition
                .getattr(intern!(definition.py(), "scope"))?
                .extract::<String>()?,
        )?;
        let callable = FixtureCallable::from_definition(
            config,
            inspect,
            direct_callable,
            mode,
            definition,
            &dependencies,
        )?;
        let package_baseid = definition
            .getattr(intern!(definition.py(), "baseid"))?
            .extract()?;
        Ok(Self {
            identity: definition.as_ptr() as usize,
            name: definition
                .getattr(intern!(definition.py(), "argname"))?
                .extract()?,
            package_baseid,
            definition: definition.clone().unbind(),
            dependencies,
            scope,
            parameterized: !definition
                .getattr(intern!(definition.py(), "params"))?
                .is_none(),
            callable,
        })
    }

    pub(super) fn into_plan_and_binding(self, id: FixtureId) -> (FixturePlan, FixtureBindings) {
        (
            FixturePlan {
                id,
                name: self.name,
                package_baseid: self.package_baseid,
                dependencies: self.dependencies,
                declared_scope: self.scope,
                parameterized: self.parameterized,
                async_loop_scope: self.callable.async_loop_scope,
                execution: self.callable.execution,
            },
            FixtureBindings {
                definition: self.definition,
                callable: self.callable,
            },
        )
    }
}

pub(super) fn asyncio_mode(config: &Bound<'_, PyAny>) -> PyResult<AsyncioMode> {
    let value = config.call_method1(intern!(config.py(), "getoption"), ("asyncio_mode",))?;
    let value = if value.is_none() {
        config.call_method1(intern!(config.py(), "getini"), ("asyncio_mode",))?
    } else {
        value
    };
    AsyncioMode::parse(&value.extract::<String>()?).map_err(PyValueError::new_err)
}

fn planned_asyncio_scope(
    explicit: Option<&str>,
    configured: Option<&str>,
    fixture_scope: FixtureScope,
) -> PyResult<AsyncioLoopScope> {
    vfy_core::plan_asyncio_loop_scope(explicit, configured, fixture_scope.as_str())
        .map_err(PyValueError::new_err)
}

fn optional_string(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    let value = value.extract::<String>()?;
    Ok((!value.is_empty()).then_some(value))
}

fn fixture_async_loop_scope(
    config: &Bound<'_, PyAny>,
    definition: &Bound<'_, PyAny>,
    fixture_scope: FixtureScope,
) -> PyResult<AsyncioLoopScope> {
    let callable = definition.getattr(intern!(config.py(), "func"))?;
    let explicit = callable
        .getattr(intern!(config.py(), "_loop_scope"))
        .ok()
        .map(|value| optional_string(&value))
        .transpose()?
        .flatten();
    let configured = optional_string(&config.call_method1(
        intern!(config.py(), "getini"),
        ("asyncio_default_fixture_loop_scope",),
    )?)?;
    planned_asyncio_scope(explicit.as_deref(), configured.as_deref(), fixture_scope)
}

pub(super) fn test_async_loop_scope(
    item: &Bound<'_, PyAny>,
    callspec: Option<&Bound<'_, PyAny>>,
    config: &Bound<'_, PyAny>,
    mode: AsyncioMode,
) -> PyResult<Option<AsyncioLoopScope>> {
    let mut marker = item.call_method1(intern!(item.py(), "get_closest_marker"), ("asyncio",))?;
    if let Some(callspec) = callspec {
        // Function marks precede parameter marks, which precede inherited marks.
        'marks: for marks in [
            item.getattr(intern!(item.py(), "own_markers"))?,
            callspec.getattr(intern!(item.py(), "marks"))?,
        ] {
            for mark in marks.try_iter()? {
                let mark = mark?;
                if mark
                    .getattr(intern!(item.py(), "name"))?
                    .extract::<String>()?
                    == "asyncio"
                {
                    marker = mark;
                    break 'marks;
                }
            }
        }
    }
    if marker.is_none() && mode == AsyncioMode::Strict {
        return Ok(None);
    }
    let explicit = if marker.is_none() {
        None
    } else {
        let kwargs = marker
            .getattr(intern!(item.py(), "kwargs"))?
            .cast_into::<PyDict>()?;
        kwargs
            .get_item("loop_scope")?
            .or(kwargs.get_item("scope")?)
            .map(|value| optional_string(&value))
            .transpose()?
            .flatten()
    };
    let configured = optional_string(&config.call_method1(
        intern!(item.py(), "getini"),
        ("asyncio_default_test_loop_scope",),
    )?)?;
    let default_scope = configured.as_deref().unwrap_or("function");
    let fixture_scope = parse_fixture_scope(default_scope)?;
    planned_asyncio_scope(explicit.as_deref(), Some(default_scope), fixture_scope).map(Some)
}

fn fixture_execution(
    inspect: &Bound<'_, PyAny>,
    callable: &Bound<'_, PyAny>,
    dependencies: &[String],
    direct_callable: &Bound<'_, PyAny>,
    async_mode: AsyncioMode,
) -> PyResult<FixtureExecution> {
    let coroutine = inspect
        .call_method1(intern!(callable.py(), "iscoroutinefunction"), (callable,))?
        .is_truthy()?;
    let async_generator = inspect
        .call_method1(intern!(callable.py(), "isasyncgenfunction"), (callable,))?
        .is_truthy()?;
    if coroutine || async_generator {
        let marked = match callable.getattr(intern!(callable.py(), "_force_asyncio_fixture")) {
            Ok(value) => value.is_truthy()?,
            Err(error) if error.is_instance_of::<PyAttributeError>(callable.py()) => false,
            Err(error) => return Err(error),
        };
        if async_mode == AsyncioMode::Auto || marked {
            return Ok(if async_generator {
                FixtureExecution::AsyncGenerator
            } else {
                FixtureExecution::AsyncCoroutine
            });
        }
        return Ok(FixtureExecution::UnsupportedAsync);
    }
    if callable.is(direct_callable) && dependencies.len() == 1 && dependencies[0] == "request" {
        return Ok(FixtureExecution::DirectParameter);
    }
    if inspect
        .call_method1(intern!(callable.py(), "isgeneratorfunction"), (callable,))?
        .is_truthy()?
    {
        Ok(FixtureExecution::Generator)
    } else {
        Ok(FixtureExecution::Callable)
    }
}

pub(super) struct FixtureUse {
    pub(super) fixture: FixtureId,
    pub(super) owner: String,
    pub(super) cache_scope: FixtureScope,
    pub(super) param: Option<Py<PyAny>>,
}

#[derive(Clone, Default)]
pub(super) struct FixtureStack(pub(super) Option<Arc<FixtureStackFrame>>);

pub(super) struct FixtureStackFrame {
    fixture: usize,
    parent: FixtureStack,
}

impl FixtureStack {
    pub(super) fn push(&self, fixture: usize) -> Self {
        Self(Some(Arc::new(FixtureStackFrame {
            fixture,
            parent: self.clone(),
        })))
    }

    pub(super) fn fixture_indices(&self) -> Vec<usize> {
        let mut indices = Vec::new();
        let mut frame = self.0.as_deref();
        while let Some(current) = frame {
            indices.push(current.fixture);
            frame = current.parent.0.as_deref();
        }
        indices.reverse();
        indices
    }

    pub(super) fn count_name(&self, fixtures: &[FixturePlan], name: &str) -> usize {
        let mut count = 0;
        let mut frame = self.0.as_deref();
        while let Some(current) = frame {
            if fixtures[current.fixture].name == name {
                count += 1;
            }
            frame = current.parent.0.as_deref();
        }
        count
    }
}

pub(super) struct XfailPlan {
    pub(super) reason: String,
    pub(super) run: bool,
    pub(super) strict: bool,
    pub(super) raises: Option<Py<PyAny>>,
}

impl XfailPlan {
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            reason: self.reason.clone(),
            run: self.run,
            strict: self.strict,
            raises: self.raises.as_ref().map(|raises| raises.clone_ref(py)),
        }
    }
}

pub(super) struct CaseCallSpec {
    pub(super) params: Vec<(String, Py<PyAny>)>,
    indices: Vec<(String, usize)>,
    scopes: Vec<(String, FixtureScope)>,
    idlist: Vec<String>,
    idlist_is_tuple: bool,
    pub(super) marks: Vec<Py<PyAny>>,
}

impl CaseCallSpec {
    pub(super) fn from_py(callspec: &Bound<'_, PyAny>) -> PyResult<Self> {
        let params = callspec
            .getattr("params")?
            .cast_into::<PyDict>()?
            .iter()
            .map(|(name, value)| Ok((name.extract()?, value.unbind())))
            .collect::<PyResult<_>>()?;
        let indices = callspec
            .getattr("indices")?
            .cast_into::<PyDict>()?
            .iter()
            .map(|(name, value)| Ok((name.extract()?, value.extract()?)))
            .collect::<PyResult<_>>()?;
        let scopes = callspec
            .getattr("_arg2scope")?
            .cast_into::<PyDict>()?
            .iter()
            .map(|(name, scope)| {
                Ok((
                    name.extract()?,
                    parse_fixture_scope(&scope.getattr("value")?.extract::<String>()?)?,
                ))
            })
            .collect::<PyResult<_>>()?;
        let idlist = callspec.getattr("_idlist")?;
        Ok(Self {
            params,
            indices,
            scopes,
            idlist: idlist.extract()?,
            idlist_is_tuple: idlist.is_exact_instance_of::<PyTuple>(),
            marks: callspec
                .getattr("marks")?
                .try_iter()?
                .map(|mark| mark.map(Bound::unbind))
                .collect::<PyResult<_>>()?,
        })
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            params: self
                .params
                .iter()
                .map(|(name, value)| (name.clone(), value.clone_ref(py)))
                .collect(),
            indices: self.indices.clone(),
            scopes: self.scopes.clone(),
            idlist: self.idlist.clone(),
            idlist_is_tuple: self.idlist_is_tuple,
            marks: self.marks.iter().map(|mark| mark.clone_ref(py)).collect(),
        }
    }

    pub(super) fn into_py(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let params = PyDict::new(py);
        for (name, value) in self.params {
            params.set_item(name, value)?;
        }
        let indices = PyDict::new(py);
        for (name, index) in self.indices {
            indices.set_item(name, index)?;
        }
        let scope_type = py.import("_pytest.scope")?.getattr("Scope")?;
        let scopes = PyDict::new(py);
        for (name, scope) in self.scopes {
            let member = match scope {
                FixtureScope::Function => "Function",
                FixtureScope::Class => "Class",
                FixtureScope::Module => "Module",
                FixtureScope::Package => "Package",
                FixtureScope::Session => "Session",
            };
            scopes.set_item(name, scope_type.getattr(member)?)?;
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("params", params)?;
        kwargs.set_item("indices", indices)?;
        kwargs.set_item("_arg2scope", scopes)?;
        if self.idlist_is_tuple {
            kwargs.set_item("_idlist", PyTuple::new(py, self.idlist)?)?;
        } else {
            kwargs.set_item("_idlist", PyList::new(py, self.idlist)?)?;
        }
        kwargs.set_item("marks", self.marks)?;
        Ok(py
            .import("_pytest.python")?
            .getattr("CallSpec2")?
            .call((), Some(&kwargs))?
            .unbind())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum XfailMode {
    Evaluate,
    Run,
    Disabled,
}

pub(super) struct CompactCaseBindings {
    pub(super) materialized_item: Option<Py<PyAny>>,
    pub(super) callable: Py<PyAny>,
    pub(super) callspec: CaseCallSpec,
    pub(super) case_name_start: usize,
    pub(super) originalname: Arc<str>,
    pub(super) class_method: bool,
    pub(super) reuse_template_instance: bool,
    pub(super) class_instance: Option<Py<PyAny>>,
}

#[derive(Default)]
pub(super) struct TestFixtureBindings {
    pub(super) dynamic_fixture_params: HashMap<String, Vec<Option<Py<PyAny>>>>,
    pub(super) direct_parameters: Option<Arc<HashMap<String, Py<PyAny>>>>,
}

pub(super) struct TestBindings {
    pub(super) item: Py<PyAny>,
    pub(super) compact: Option<Box<CompactCaseBindings>>,
    pub(super) warning_profile: usize,
    pub(super) fixture_state: Option<Box<TestFixtureBindings>>,
    pub(super) xfail: Option<Box<XfailPlan>>,
    pub(super) xfail_mode: XfailMode,
}

impl TestBindings {
    pub(super) fn insert_dynamic_fixture_params(
        &mut self,
        name: &str,
        params: Vec<Option<Py<PyAny>>>,
    ) {
        self.fixture_state
            .get_or_insert_with(Box::default)
            .dynamic_fixture_params
            .insert(name.to_owned(), params);
    }
}

#[derive(Default)]
pub(super) struct ScopeOwnerKeys {
    keys: HashSet<Arc<str>>,
}

impl ScopeOwnerKeys {
    pub(super) fn intern(&mut self, key: &str) -> Arc<str> {
        if let Some(shared) = self.keys.get(key) {
            return Arc::clone(shared);
        }
        let shared = Arc::<str>::from(key);
        self.keys.insert(Arc::clone(&shared));
        shared
    }
}

pub(super) fn scope_owners_from_item(
    item: &Bound<'_, PyAny>,
    keys: &mut ScopeOwnerKeys,
) -> PyResult<ScopeOwners> {
    let module_type = item
        .py()
        .import("_pytest.python")?
        .getattr(intern!(item.py(), "Module"))?;
    let module_collector = item.call_method1(intern!(item.py(), "getparent"), (module_type,))?;
    let module = if module_collector.is_none() {
        keys.intern(&item.getattr(intern!(item.py(), "path"))?.str()?.to_string())
    } else {
        keys.intern(
            module_collector
                .getattr(intern!(item.py(), "nodeid"))?
                .cast::<PyString>()?
                .to_str()?,
        )
    };
    let class_type = item
        .py()
        .import("_pytest.python")?
        .getattr(intern!(item.py(), "Class"))?;
    let class_collector = item.call_method1(intern!(item.py(), "getparent"), (class_type,))?;
    let class = if class_collector.is_none() {
        None
    } else {
        Some(
            keys.intern(
                class_collector
                    .getattr(intern!(item.py(), "nodeid"))?
                    .cast::<PyString>()?
                    .to_str()?,
            ),
        )
    };
    Ok(ScopeOwners { class, module })
}

pub(super) fn package_scope_node(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    package_baseid: &str,
) -> PyResult<Py<PyAny>> {
    let package_type = py
        .import("_pytest.python")?
        .getattr(intern!(py, "Package"))?;
    for parent in item.call_method0(intern!(py, "listchain"))?.try_iter()? {
        let parent = parent?;
        if parent.is_instance(&package_type)?
            && parent.getattr(intern!(py, "nodeid"))?.extract::<String>()? == package_baseid
        {
            return Ok(parent.unbind());
        }
    }
    Ok(item.getattr(intern!(py, "session"))?.unbind())
}

pub(super) fn scope_node(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    scope: FixtureScope,
    package_baseid: Option<&str>,
) -> PyResult<Py<PyAny>> {
    match scope {
        FixtureScope::Function => Ok(item.clone().unbind()),
        FixtureScope::Class => {
            let class = py.import("_pytest.python")?.getattr(intern!(py, "Class"))?;
            let node = item.call_method1(intern!(py, "getparent"), (class,))?;
            Ok(if node.is_none() { item.clone() } else { node }.unbind())
        }
        FixtureScope::Module => {
            let module = py
                .import("_pytest.python")?
                .getattr(intern!(py, "Module"))?;
            item.call_method1(intern!(py, "getparent"), (module,))
                .map(Bound::unbind)
        }
        FixtureScope::Package => package_scope_node(
            py,
            item,
            package_baseid.ok_or_else(|| {
                PyRuntimeError::new_err("package-scoped request has no package identity")
            })?,
        ),
        FixtureScope::Session => {
            let session = py.import("_pytest.main")?.getattr(intern!(py, "Session"))?;
            item.call_method1(intern!(py, "getparent"), (session,))
                .map(Bound::unbind)
        }
    }
}

pub(super) struct SuiteBindings {
    pub(super) callspec_reader: Option<Arc<DenseCallSpecReader>>,
    pub(super) core: CoreSuitePlan,
    pub(super) test_bindings: Vec<TestBindings>,
    pub(super) fixture_bindings: Vec<FixtureBindings>,
    pub(super) fixture_ids: HashMap<usize, usize>,
    pub(super) report_locations: Option<Vec<(String, ReportLocation)>>,
}

impl SuiteBindings {
    pub(super) fn new(
        tests: Vec<TestPlan>,
        test_bindings: Vec<TestBindings>,
        fixtures: Vec<FixturePlan>,
        fixture_bindings: Vec<FixtureBindings>,
        fixture_ids: HashMap<usize, usize>,
        no_cover_cases: Option<HashSet<usize>>,
        report_locations: Option<Vec<(String, ReportLocation)>>,
    ) -> PyResult<Self> {
        let core = CoreSuitePlan::new(tests, fixtures, no_cover_cases).map_err(|detail| {
            PyRuntimeError::new_err(format!("native suite plan is invalid: {detail}"))
        })?;
        if core.tests().len() != test_bindings.len() {
            return Err(PyRuntimeError::new_err(format!(
                "native suite plan has {} semantic tests but {} Python bindings",
                core.tests().len(),
                test_bindings.len()
            )));
        }
        if core.fixtures().len() != fixture_bindings.len() {
            return Err(PyRuntimeError::new_err(format!(
                "native suite plan has {} semantic fixtures but {} Python bindings",
                core.fixtures().len(),
                fixture_bindings.len()
            )));
        }
        for (test, bindings) in core.tests().iter().zip(&test_bindings) {
            let planned_names = test
                .fixture_state
                .as_ref()
                .map(|state| &state.direct_parameter_names);
            let parameters = bindings
                .fixture_state
                .as_ref()
                .and_then(|state| state.direct_parameters.as_ref());
            if bindings.compact.is_some()
                && (planned_names.map_or(0, HashSet::len)
                    != parameters.map_or(0, |parameters| parameters.len())
                    || !planned_names
                        .into_iter()
                        .flat_map(|names| names.iter())
                        .all(|name| {
                            parameters.is_some_and(|parameters| parameters.contains_key(name))
                        }))
            {
                return Err(PyRuntimeError::new_err(format!(
                    "native suite plan direct-parameter names do not align for case {}",
                    test.nodeid
                )));
            }
        }
        let plan = Self {
            callspec_reader: None,
            core,
            test_bindings,
            fixture_bindings,
            fixture_ids,
            report_locations,
        };
        plan.validate_binding_references()?;
        Ok(plan)
    }

    fn validate_binding_references(&self) -> PyResult<()> {
        if let Some(fixture) = self
            .fixture_ids
            .values()
            .copied()
            .filter(|fixture| *fixture >= self.core.fixtures().len())
            .min()
        {
            return Err(PyRuntimeError::new_err(format!(
                "native suite plan fixture identity points to missing fixture index {fixture}"
            )));
        }

        if let Some(report_locations) = &self.report_locations {
            if report_locations.len() != self.core.tests().len() {
                return Err(PyRuntimeError::new_err(format!(
                    "native suite plan has {} report locations for {} cases",
                    report_locations.len(),
                    self.core.tests().len()
                )));
            }
            for (test, (nodeid, _)) in self.core.tests().iter().zip(report_locations) {
                if test.nodeid != *nodeid {
                    return Err(PyRuntimeError::new_err(format!(
                        "native suite plan report location for {:?} is aligned with case {:?}",
                        nodeid, test.nodeid
                    )));
                }
            }
        }
        Ok(())
    }

    pub(super) fn tests(&self) -> &[TestPlan] {
        self.core.tests()
    }

    pub(super) fn fixtures(&self) -> &[FixturePlan] {
        self.core.fixtures()
    }

    pub(super) fn no_cover_cases(&self) -> Option<&HashSet<usize>> {
        self.core.no_cover_cases()
    }
}

#[derive(Clone, Copy)]
pub(super) struct CompileOptions {
    pub(super) track_vfy_coverage: bool,
    pub(super) retain_passing_report_locations: bool,
}

pub(super) struct CachedFixtureInfo {
    // Retain the Python object so its identity cannot be reused during compilation.
    pub(super) _owner: Py<PyAny>,
    pub(super) argument_names: Arc<[String]>,
    pub(super) chains: Arc<FixtureChains>,
}

pub(super) struct CachedNames {
    pub(super) _owner: Py<PyAny>,
    pub(super) names: Arc<[String]>,
}

#[derive(Default)]
pub(super) struct FixtureSchemas {
    names: HashSet<Arc<[String]>>,
    chains: HashMap<u64, Vec<Arc<FixtureChains>>>,
}

impl FixtureSchemas {
    pub(super) fn intern_names(&mut self, names: Vec<String>) -> Arc<[String]> {
        if let Some(shared) = self.names.get(names.as_slice()) {
            return shared.clone();
        }
        let shared = Arc::from(names);
        self.names.insert(Arc::clone(&shared));
        shared
    }

    pub(super) fn intern_chains(
        &mut self,
        rows: Vec<(String, Vec<FixtureId>)>,
    ) -> Arc<FixtureChains> {
        let mut hasher = DefaultHasher::new();
        rows.hash(&mut hasher);
        let bucket = self.chains.entry(hasher.finish()).or_default();
        if let Some(shared) = bucket.iter().find(|chains| {
            chains.ordered_names.len() == rows.len()
                && chains.by_name.len() == rows.len()
                && rows
                    .iter()
                    .zip(&chains.ordered_names)
                    .all(|((name, chain), ordered)| {
                        name == ordered && chains.by_name.get(name) == Some(chain)
                    })
        }) {
            return shared.clone();
        }
        let mut ordered_names = Vec::with_capacity(rows.len());
        let mut by_name = HashMap::with_capacity(rows.len());
        for (name, chain) in rows {
            ordered_names.push(name.clone());
            by_name.insert(name, chain);
        }
        let shared = Arc::new(FixtureChains {
            ordered_names,
            by_name,
        });
        bucket.push(shared.clone());
        shared
    }
}
