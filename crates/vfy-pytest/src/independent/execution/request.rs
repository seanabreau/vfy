//! Fixture request objects exposed to Python.

use super::super::callspec;
use super::super::fixture_binding::NativeFixtureBinding;
use super::super::thread_bound::ThreadBound;
use super::executor::Executor;
use super::resolved::NativeResolvedFixtureDefinitions;
use super::{fixture_info, provider_scopes};
use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyAttributeError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::sync::{Mutex, PoisonError};
use vfy_core::{CollectionNodeId, FixtureId, FixtureScope};

#[derive(Clone)]
pub(super) struct RequestContext {
    pub(super) test_index: usize,
    pub(super) scope: FixtureScope,
    pub(super) fixture: Option<FixtureId>,
    pub(super) owner: CollectionNodeId,
    pub(super) stack: Vec<FixtureId>,
    pub(super) lifetime: Rc<Cell<bool>>,
    pub(super) names: Rc<RefCell<Vec<String>>>,
    pub(super) parameter: Option<Rc<callspec::Parameter>>,
}

pub(super) enum FinalizerTarget {
    Node,
    Fixture(Py<PyList>),
}

pub(super) struct RequestObjects {
    pub(super) resolved: Py<NativeResolvedFixtureDefinitions>,
    pub(super) node: Py<PyAny>,
    pub(super) item: Py<PyAny>,
    pub(super) stack: Vec<Py<NativeFixtureBinding>>,
    pub(super) providers: Py<provider_scopes::NativeProviderScope>,
    pub(super) module: Py<PyAny>,
    pub(super) param: Option<Py<PyAny>>,
    pub(super) param_index: Option<Py<PyAny>>,
    pub(super) finalizers: FinalizerTarget,
    pub(super) config: Option<Py<PyAny>>,
    pub(super) session: Option<Py<PyAny>>,
}

/// The executor and context a request acts on, which belong to the test thread.
pub(super) struct RequestParts {
    pub(super) owner: Rc<Executor>,
    pub(super) context: RequestContext,
}

/// A request keeps its execution owner alive without borrowing it during callbacks.
///
/// Like pytest's request, the Python object may be released by a garbage
/// collection on any thread, so only its executor parts are thread-bound.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeIndependentRequest {
    pub(super) parts: ThreadBound<RequestParts>,
    pub(super) scope: FixtureScope,
    pub(super) fixture_name: Option<String>,
    pub(super) objects: Mutex<Option<RequestObjects>>,
}

impl NativeIndependentRequest {
    /// The request's executor and context, on the thread that runs the tests.
    pub(super) fn parts(&self) -> PyResult<(Rc<Executor>, RequestContext)> {
        let parts = self.parts.get()?;
        Ok((Rc::clone(&parts.owner), parts.context.clone()))
    }

    pub(in crate::independent) fn report_source(
        &self,
        py: Python<'_>,
    ) -> PyResult<super::super::reporting::origin::ReportSource> {
        let (owner, request_context) = self.parts()?;
        let suite = owner.suite()?;
        let context = &suite.context;
        super::super::reporting::origin::ReportSource::bound(
            py,
            context,
            request_context.test_index,
        )
    }

    pub(super) fn object<T>(&self, select: impl FnOnce(&RequestObjects) -> T) -> PyResult<T> {
        self.objects
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(select)
            .ok_or_else(|| PyAssertionError::new_err("fixture request has been cleared"))
    }

    fn optional(
        &self,
        py: Python<'_>,
        select: impl FnOnce(&RequestObjects) -> Option<&Py<PyAny>>,
        missing: &'static str,
    ) -> PyResult<Py<PyAny>> {
        self.object(|objects| select(objects).map(|value| value.clone_ref(py)))?
            .ok_or_else(|| PyAttributeError::new_err(missing))
    }

    fn require_scope(&self, name: &str, maximum: FixtureScope) -> PyResult<()> {
        if self.scope.rank() > maximum.rank() {
            return Err(PyAttributeError::new_err(format!(
                "{name} not available in {}-scoped context",
                self.scope.as_str()
            )));
        }
        Ok(())
    }

    /// An attribute of the test item, available only within `maximum` scope.
    fn item_attribute(
        &self,
        py: Python<'_>,
        name: &str,
        attribute: &str,
        maximum: FixtureScope,
    ) -> PyResult<Py<PyAny>> {
        self.require_scope(name, maximum)?;
        Ok(self.pyfuncitem(py)?.bind(py).getattr(attribute)?.unbind())
    }
}

#[pymethods]
impl NativeIndependentRequest {
    fn __repr__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
        let representation = if let Some(name) = &self.fixture_name {
            let item = self.object(|objects| objects.item.clone_ref(py))?;
            PyString::new(py, "<SubRequest {!r} for {!r}>")
                .call_method1("format", (PyString::new(py, name), item))?
        } else {
            PyString::new(py, "<FixtureRequest for {!r}>")
                .call_method1("format", (self.node(py)?,))?
        };
        representation.cast_into::<PyString>().map_err(Into::into)
    }

    #[getter(_fixture_defs)]
    pub(super) fn fixture_definitions(
        &self,
        py: Python<'_>,
    ) -> PyResult<Py<NativeResolvedFixtureDefinitions>> {
        self.object(|objects| objects.resolved.clone_ref(py))
    }

    #[getter(_pyfuncitem)]
    fn pyfuncitem(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.object(|objects| objects.item.clone_ref(py))
    }

    fn _get_fixturestack(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        let stack = self.object(|objects| {
            objects
                .stack
                .iter()
                .map(|binding| binding.clone_ref(py))
                .collect::<Vec<_>>()
        })?;
        Ok(PyList::new(py, stack)?.unbind())
    }

    fn _vfy_available_fixture_names(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        let providers = self.object(|objects| objects.providers.clone_ref(py))?;
        provider_scopes::NativeProviderScope::names(&providers, py)
    }

    fn raiseerror(slf: PyRef<'_, Self>, py: Python<'_>, msg: &Bound<'_, PyAny>) -> PyResult<()> {
        let request = slf.into_pyobject(py)?.into_any().unbind();
        Err(super::super::fixture_lookup::raiseerror(
            py,
            request.bind(py),
            msg,
        )?)
    }

    fn getfixturevalue(slf: PyRef<'_, Self>, py: Python<'_>, argname: &str) -> PyResult<Py<PyAny>> {
        if argname == "request" {
            return Ok(slf.into_pyobject(py)?.into_any().unbind());
        }
        let (owner, context) = slf.parts()?;
        let resolved = slf.fixture_definitions(py)?;
        let request = slf.into_pyobject(py)?.into_any().unbind();
        owner.resolve(py, &context, argname, request.bind(py), &resolved)
    }

    fn addfinalizer(&self, py: Python<'_>, finalizer: &Bound<'_, PyAny>) -> PyResult<()> {
        let target = self.object(|objects| match &objects.finalizers {
            FinalizerTarget::Node => Err(objects.node.clone_ref(py)),
            FinalizerTarget::Fixture(finalizers) => Ok(finalizers.clone_ref(py)),
        })?;
        match target {
            Err(node) => {
                node.bind(py)
                    .call_method1(pyo3::intern!(py, "addfinalizer"), (finalizer,))?;
                Ok(())
            }
            Ok(finalizers) => finalizers.bind(py).append(finalizer),
        }
    }

    #[getter]
    fn scope<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        super::super::scope_text(py, self.scope)
    }

    #[getter]
    fn fixturename(&self) -> Option<&str> {
        self.fixture_name.as_deref()
    }

    #[getter]
    fn fixturenames(&self) -> PyResult<Vec<String>> {
        let (_, context) = self.parts()?;
        let names = context.names.borrow().clone();
        Ok(names)
    }

    fn fixture_info(&self, py: Python<'_>) -> PyResult<Vec<fixture_info::FixtureInfoRow>> {
        fixture_info::snapshot(py, self)
    }

    fn applymarker(&self, py: Python<'_>, marker: &Bound<'_, PyAny>) -> PyResult<()> {
        self.node(py)?
            .bind(py)
            .call_method1("add_marker", (marker,))?;
        Ok(())
    }

    #[getter]
    fn node(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.object(|objects| objects.node.clone_ref(py))
    }

    #[getter]
    fn function(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.item_attribute(py, "function", "obj", FixtureScope::Function)
    }

    #[getter]
    fn module(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.require_scope("module", FixtureScope::Module)?;
        self.object(|objects| objects.module.clone_ref(py))
    }

    #[getter]
    fn path(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.item_attribute(py, "path", "path", FixtureScope::Package)
    }

    #[getter]
    fn config(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.optional(
            py,
            |objects| objects.config.as_ref(),
            "fixture request has no configuration",
        )
    }

    #[getter]
    fn session(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.optional(
            py,
            |objects| objects.session.as_ref(),
            "fixture request has no session",
        )
    }

    #[getter]
    fn cls(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.require_scope("cls", FixtureScope::Class)?;
        let item = self.pyfuncitem(py)?;
        let getparent = item.bind(py).getattr("getparent")?;
        let class_type = Module::Collectors.get(py)?.getattr("Class")?;
        let class = getparent.call1((class_type,))?;
        if class.is_truthy()? {
            Ok(class.getattr("obj")?.unbind())
        } else {
            Ok(py.None())
        }
    }

    #[getter]
    fn instance(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.scope != FixtureScope::Function {
            return Ok(py.None());
        }
        Ok(self
            .pyfuncitem(py)?
            .bind(py)
            .getattr_opt("instance")?
            .map_or_else(|| py.None(), Bound::unbind))
    }

    #[getter]
    fn param(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.optional(
            py,
            |objects| objects.param.as_ref(),
            "fixture request has no parameter",
        )
    }

    #[getter]
    fn param_index(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.optional(
            py,
            |objects| objects.param_index.as_ref(),
            "test request has no parameter index",
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
            visit.call(&objects.resolved)?;
            visit.call(&objects.param_index)?;
            visit.call(&objects.node)?;
            visit.call(&objects.item)?;
            for binding in &objects.stack {
                visit.call(binding)?;
            }
            visit.call(&objects.providers)?;
            visit.call(&objects.module)?;
            visit.call(&objects.param)?;
            if let FinalizerTarget::Fixture(finalizers) = &objects.finalizers {
                visit.call(finalizers)?;
            }
            visit.call(&objects.config)?;
            visit.call(&objects.session)?;
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
