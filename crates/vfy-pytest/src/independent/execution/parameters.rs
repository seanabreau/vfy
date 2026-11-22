//! Resolve dependencies before comparing the request's captured cache parameter.

use super::super::CollectedSuite;
use super::executor::Executor;
use super::fixture_cache::CacheRead;
use super::request::{NativeIndependentRequest, RequestContext};
use super::resolved::NativeResolvedFixtureDefinitions;
use super::{collected_item, validate_fixture_scope};
use crate::module_cache::Module;
use pyo3::basic::CompareOp;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use std::rc::Rc;
use vfy_core::FixtureCacheKey;
use vfy_core::test_plan::FixturePlan;

fn relative_location(path: &Bound<'_, PyAny>, root: &Bound<'_, PyAny>) -> PyResult<String> {
    let path = match path.call_method1("relative_to", (root,)) {
        Ok(relative) => relative,
        Err(error) if error.is_instance_of::<PyValueError>(path.py()) => path.clone(),
        Err(error) => return Err(error),
    };
    Ok(path.str()?.to_str()?.to_owned())
}

fn same_parameter(
    py: Python<'_>,
    previous: Option<&Py<PyAny>>,
    next: Option<&Py<PyAny>>,
) -> PyResult<bool> {
    let none = py.None();
    let previous = previous.unwrap_or(&none).bind(py);
    let next = next.unwrap_or(&none).bind(py);
    match next
        .rich_compare(previous, CompareOp::Eq)
        .and_then(|equal| equal.is_truthy())
    {
        Ok(equal) => Ok(equal),
        Err(error)
            if error.is_instance_of::<PyValueError>(py)
                || error.is_instance_of::<PyRuntimeError>(py) =>
        {
            Ok(next.is(previous))
        }
        Err(error) => Err(error),
    }
}

#[derive(Default)]
pub(super) struct CacheComparison {
    pub(super) key: Option<FixtureCacheKey>,
    // FixtureDef.execute retains its cache_key local until the replacement setup returns.
    previous_parameter: Option<Py<PyAny>>,
}

impl CacheComparison {
    pub(super) fn finish(self) {
        drop(self.previous_parameter);
    }
}

impl Executor {
    pub(super) fn missing_parameter(
        &self,
        py: Python<'_>,
        fixture: &FixturePlan,
        request: &Bound<'_, PyAny>,
    ) -> PyResult<PyErr> {
        let item = request.getattr("_pyfuncitem")?;
        let nofuncargs = item
            .getattr_opt("nofuncargs")?
            .map(|value| value.is_truthy())
            .transpose()?
            .unwrap_or(false);
        let nodeid = item.getattr("nodeid")?.extract::<String>()?;
        if nofuncargs {
            let name = item.getattr("name")?.str()?.to_str()?.to_owned();
            let kind = item.get_type().name()?;
            return super::fixture_outcomes::failed(
                py,
                &format!(
                    "{name} does not support fixtures, maybe unittest.TestCase subclass?\nNode id: {nodeid}\nFunction type: {kind}"
                ),
            );
        }
        // Native getfixturevalue adds no Python frames. The current frame is its
        // actual requesting caller, including helpers and fixture functions.
        let frame = Module::Sys.get(py)?.call_method0("_getframe")?;
        let inspect = Module::Inspect.get(py)?;
        let frameinfo = inspect.call_method1("getframeinfo", (frame,))?;
        let path_type = Module::Pathlib.get(py)?.getattr("Path")?;
        let source_path = path_type.call1((Module::OsPath
            .get(py)?
            .call_method1("abspath", (frameinfo.getattr("filename")?,))?,))?;
        let root = item.getattr("config")?.getattr("rootpath")?;
        let source_path = relative_location(&source_path, &root)?;
        let source_line = frameinfo.getattr("lineno")?.extract::<usize>()?;
        let suite = self.suite()?;
        let callable = suite.fixtures[fixture.id.index()]
            .runtime
            .bind(py)
            .borrow()
            .callable(py)?;
        let function = super::super::fixture_lookup::real_function(callable.bind(py))?;
        let fixture_path = path_type.call1((inspect.call_method1("getfile", (&function,))?,))?;
        let fixture_path = relative_location(&fixture_path, &root)?;
        let fixture_line = function
            .getattr("__code__")?
            .getattr("co_firstlineno")?
            .add(1)?;
        super::fixture_outcomes::failed(
            py,
            &format!(
                "The requested fixture has no parameter defined for test:\n    {nodeid}\n\nRequested fixture '{}' defined in:\n{fixture_path}:{}\n\nRequested here:\n{source_path}:{source_line}",
                fixture.name,
                fixture_line.str()?.to_str()?,
            ),
        )
    }

    pub(super) fn remembered_fixture(
        &self,
        py: Python<'_>,
        context: &RequestContext,
        name: &str,
        key: &FixtureCacheKey,
        suite: &CollectedSuite,
    ) -> PyResult<Py<PyAny>> {
        // Cache keys are minted only for plan fixtures in `Executor::resolve`.
        let declared = suite.plan.fixtures()[key.identity.fixture.index()].declared_scope;
        validate_fixture_scope(py, context.scope, declared, name)?;
        let current = {
            let state = self.state.borrow();
            state
                .instance(key)
                .or_else(|| state.fixture(key.identity.fixture))
                .map(|cached| cached.key.clone())
        };
        self.cached(py, current.as_ref().unwrap_or(key), CacheRead::RequestValue)
    }

    pub(super) fn temporary_path(
        &self,
        py: Python<'_>,
        context: &RequestContext,
        factory: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let suite = self.suite()?;
        let name = if let Some(item) = collected_item(&suite, context.test_index) {
            item.bind(py).getattr("name")?.extract::<String>()?
        } else {
            suite.plan.tests()[context.test_index]
                .nodeid
                .rsplit("::")
                .next()
                .unwrap_or("test")
                .to_owned()
        };
        let mut basename = String::new();
        for character in name.chars().take(30) {
            // CPython versions use different Unicode databases; classify exact builtin strings.
            let alphanumeric = PyString::new(py, &character.to_string())
                .call_method0("isalnum")?
                .is_truthy()?;
            basename.push(if character == '_' || alphanumeric {
                character
            } else {
                '_'
            });
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("numbered", true)?;
        Ok(factory
            .call_method("mktemp", (basename,), Some(&kwargs))?
            .unbind())
    }

    pub(super) fn fixture_arguments<'py>(
        self: &Rc<Self>,
        py: Python<'py>,
        context: &RequestContext,
        fixture: &FixturePlan,
        request: &Py<NativeIndependentRequest>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let resolved = request.bind(py).get().fixture_definitions(py)?;
        let kwargs = PyDict::new(py);
        for dependency in &fixture.dependencies {
            if dependency == "request" {
                kwargs.set_item(dependency, request)?;
            } else {
                kwargs.set_item(
                    dependency,
                    self.resolve(
                        py,
                        context,
                        dependency,
                        request.bind(py).as_any(),
                        &resolved,
                    )?,
                )?;
            }
        }
        Ok(kwargs)
    }

    pub(super) fn reusable_fixture(
        self: &Rc<Self>,
        py: Python<'_>,
        context: &RequestContext,
        fixture: &FixturePlan,
    ) -> PyResult<CacheComparison> {
        let previous = self
            .state
            .borrow()
            .fixture(fixture.id)
            .map(|cached| (cached.key.clone(), cached.storage.clone_ref(py)));
        let Some((key, storage)) = previous else {
            return Ok(CacheComparison::default());
        };
        let Some(param) = storage.parameter(py)? else {
            return Ok(CacheComparison::default());
        };
        // Both rich comparison and bool(result) can re-enter saved requests.
        let same = same_parameter(
            py,
            Some(&param),
            context.parameter.as_ref().map(|parameter| &parameter.value),
        )?;
        if same {
            return Ok(CacheComparison {
                key: Some(key),
                previous_parameter: Some(param),
            });
        }
        self.finalize_fixture(py, &key)?;
        Ok(CacheComparison {
            key: None,
            previous_parameter: Some(param),
        })
    }

    /// The finalizer list of the cached fixture that `name` resolves to in this test.
    pub(super) fn dependency_callbacks(
        &self,
        py: Python<'_>,
        name: &str,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> Option<Py<PyList>> {
        let dependency = resolved.bind(py).borrow().key(name);
        let dependency = dependency?;
        self.state
            .borrow()
            .instance(&dependency)
            .map(|cached| cached.finalizers.clone_ref(py))
    }

    pub(super) fn register_dependency(
        self: &Rc<Self>,
        py: Python<'_>,
        name: &str,
        key: &FixtureCacheKey,
        request: &Py<NativeIndependentRequest>,
        resolved: &Py<NativeResolvedFixtureDefinitions>,
    ) -> PyResult<()> {
        if let Some(callbacks) = self.dependency_callbacks(py, name, resolved) {
            callbacks
                .bind(py)
                .append(self.finish_fixture(py, key, request)?)?;
        }
        Ok(())
    }
}
