//! Owns the native setup state and pytest request objects.

use super::fixture_setup::{FixtureResolution, resolve_named_fixture};
use super::model::{CacheKey, FixtureStack, scope_node};
use super::runtime::{
    FixtureFinalizer, ResolvedFixtureKeys, SharedRuntime, WeakRuntime, register_node_finalizer,
};
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::{
    Bound, IntoPyObject, Py, PyAnyMethods, PyErr, PyRef, PyResult, Python, pyclass, pymethods,
};
use pyo3::pyclass::{PyTraverseError, PyVisit};
use pyo3::types::{PyAny, PyList, PyString};
use vfy_core::FixtureScope;

// Real pytest Nodes delegate their public addfinalizer method to this session
// service. Keep their registration on the same Rust owner stack as fixtures.
#[pyclass(module = "vfy._native._pytest")]
struct NativeSetupState {
    runtime: WeakRuntime,
    original: Option<Py<PyAny>>,
}

#[pymethods]
impl NativeSetupState {
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3's GC traverse protocol owns PyVisit"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.original)
    }

    fn __clear__(&mut self) {
        self.original = None;
    }

    fn addfinalizer(
        &self,
        py: Python<'_>,
        finalizer: Py<PyAny>,
        node: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let runtime = self.runtime.upgrade().ok_or_else(|| {
            PyRuntimeError::new_err("native pytest setup state is no longer active")
        })?;
        register_node_finalizer(py, &runtime, node, finalizer)
    }

    fn is_node_active(&self, node: &Bound<'_, PyAny>) -> PyResult<bool> {
        let owner = node.getattr("nodeid")?.extract::<String>()?;
        Ok(self.runtime.upgrade().is_some_and(|runtime| {
            let state = runtime.lock();
            state.active_nodes.contains(&owner)
                && state
                    .owner_nodes
                    .get(&owner)
                    .is_some_and(|active| active.bind(node.py()).is(node))
        }))
    }

    fn __getattr__(&self, py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
        self.original
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("native pytest setup state has been cleared"))?
            .bind(py)
            .getattr(name)
            .map(Bound::unbind)
    }
}

pub(super) struct SetupStateGuard {
    session: Py<PyAny>,
    original: Option<Py<PyAny>>,
}

impl SetupStateGuard {
    pub(super) fn install(py: Python<'_>, runtime: &SharedRuntime) -> PyResult<Option<Self>> {
        let item = runtime
            .lock()
            .suite
            .test_bindings
            .first()
            .map(|bindings| bindings.item.clone_ref(py));
        let Some(item) = item else {
            return Ok(None);
        };
        let session = item.bind(py).getattr("session")?;
        let original = session.getattr("_setupstate")?.unbind();
        let native = Py::new(
            py,
            NativeSetupState {
                runtime: runtime.downgrade(),
                original: Some(original.clone_ref(py)),
            },
        )?;
        session.setattr("_setupstate", native)?;
        Ok(Some(Self {
            session: session.unbind(),
            original: Some(original),
        }))
    }

    pub(super) fn restore(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(original) = &self.original {
            self.session
                .bind(py)
                .setattr("_setupstate", original.bind(py))?;
            self.original = None;
        }
        Ok(())
    }
}

impl Drop for SetupStateGuard {
    fn drop(&mut self) {
        let _ = Python::try_attach(|py| self.restore(py));
    }
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeRequest {
    pub(super) runtime: WeakRuntime,
    pub(super) resolved_fixture_keys: ResolvedFixtureKeys,
    pub(super) item: Option<Py<PyAny>>,
    pub(super) package_baseid: Option<String>,
    pub(super) test_index: usize,
    pub(super) target: Option<CacheKey>,
    pub(super) requesting_scope: FixtureScope,
    pub(super) fixture_name: Option<String>,
    pub(super) param: Option<Py<PyAny>>,
    pub(super) fixture_stack: FixtureStack,
}

#[pymethods]
impl NativeRequest {
    fn __repr__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
        let item = self.item()?.bind(py);
        let representation = if let Some(name) = &self.fixture_name {
            PyString::new(py, "<SubRequest {!r} for {!r}>")
                .call_method1(intern!(py, "format"), (PyString::new(py, name), item))?
        } else {
            PyString::new(py, "<FixtureRequest for {!r}>")
                .call_method1(intern!(py, "format"), (item,))?
        };
        representation.cast_into::<PyString>().map_err(Into::into)
    }

    #[pyo3(signature = (msg))]
    fn raiseerror(slf: PyRef<'_, Self>, py: Python<'_>, msg: Option<&str>) -> PyResult<()> {
        let error = py
            .import("_pytest.fixtures")?
            .getattr("FixtureLookupError")?
            .call1((py.None(), slf.into_pyobject(py)?, msg))?;
        Err(PyErr::from_value(error))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3's GC traverse protocol passes PyVisit by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.item)?;
        visit.call(&self.param)?;
        Ok(())
    }

    fn __clear__(&mut self) {
        self.item = None;
        self.param = None;
    }

    fn getfixturevalue(slf: PyRef<'_, Self>, py: Python<'_>, argname: &str) -> PyResult<Py<PyAny>> {
        if argname == "request" {
            return Ok(slf.into_pyobject(py)?.into_any().unbind());
        }
        let runtime = slf.active_runtime()?;
        let previous = runtime
            .lock()
            .resolved_fixture_keys
            .insert(slf.test_index, slf.resolved_fixture_keys.clone());
        let result = resolve_named_fixture(
            py,
            &runtime,
            slf.test_index,
            argname,
            Some(slf.requesting_scope),
            Some(slf.fixture_stack.clone()),
            FixtureResolution::DYNAMIC,
        );
        let mut state = runtime.lock();
        if let Some(previous) = previous {
            state.resolved_fixture_keys.insert(slf.test_index, previous);
        } else {
            state.resolved_fixture_keys.remove(&slf.test_index);
        }
        result
    }

    fn applymarker(&self, py: Python<'_>, marker: &Bound<'_, PyAny>) -> PyResult<()> {
        self.node(py)?
            .bind(py)
            .call_method1("add_marker", (marker,))?;
        Ok(())
    }

    fn _get_fixturestack(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let runtime = self.active_runtime()?;
        let indices = self.fixture_stack.fixture_indices();
        let definitions = {
            let state = runtime.lock();
            indices
                .iter()
                .map(|fixture_index| {
                    state
                        .suite
                        .fixture_bindings
                        .get(*fixture_index)
                        .map(|fixture| fixture.definition.clone_ref(py))
                        .ok_or_else(|| {
                            PyRuntimeError::new_err("active fixture has no compiled definition")
                        })
                })
                .collect::<PyResult<Vec<_>>>()?
        };
        PyList::new(py, definitions).map(|stack| stack.into_any().unbind())
    }

    #[getter]
    fn _pyfuncitem(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.item()?.clone_ref(py))
    }

    #[getter]
    fn _fixturemanager(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.item()?
            .bind(py)
            .getattr("session")?
            .getattr("_fixturemanager")
            .map(Bound::unbind)
    }

    fn addfinalizer(&self, py: Python<'_>, finalizer: Py<PyAny>) -> PyResult<()> {
        if !finalizer.bind(py).is_callable() {
            return Err(PyTypeError::new_err("finalizer must be callable"));
        }
        let runtime = self.active_runtime()?;
        if let Some(target) = &self.target {
            let mut state = runtime.lock();
            let target = state
                .active_by_fixture
                .get(&target.identity.fixture)
                .unwrap_or(target)
                .clone();
            state
                .pending_finalizers
                .entry(target)
                .or_default()
                .push(FixtureFinalizer::Callable(finalizer));
        } else {
            register_node_finalizer(py, &runtime, self.node(py)?.bind(py), finalizer)?;
        }
        Ok(())
    }

    #[getter]
    fn param(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.param
            .as_ref()
            .map(|value| value.clone_ref(py))
            .ok_or_else(|| PyAttributeError::new_err("param"))
    }

    #[getter]
    fn scope(&self) -> &'static str {
        self.requesting_scope.as_str()
    }

    #[getter]
    fn fixturename(&self) -> Option<&str> {
        self.fixture_name.as_deref()
    }

    #[getter]
    pub(super) fn node(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        scope_node(
            py,
            self.item()?.bind(py),
            self.requesting_scope,
            self.package_baseid.as_deref(),
        )
    }

    #[getter]
    fn config(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.item()?.bind(py).getattr("config")?.unbind())
    }

    #[getter]
    fn session(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.item()?.bind(py).getattr("session")?.unbind())
    }

    #[getter]
    fn function(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.requesting_scope != FixtureScope::Function {
            return Err(PyAttributeError::new_err(format!(
                "function not available in {}-scoped context",
                self.requesting_scope.as_str()
            )));
        }
        Ok(self.item()?.bind(py).getattr("obj")?.unbind())
    }

    #[getter]
    fn instance(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.requesting_scope != FixtureScope::Function {
            // pytest 8.0-8.1 exposed the unittest case even in wider fixture scopes.
            let item = self.item()?.bind(py);
            let expose_instance = self
                .active_runtime()?
                .lock()
                .compatibility
                .unittest_wide_scope_instance;
            if expose_instance && item.hasattr("_testcase")? {
                return Ok(item.getattr("_testcase")?.unbind());
            }
            return Ok(py.None());
        }
        Ok(self
            .item()?
            .bind(py)
            .getattr_opt("instance")?
            .map_or_else(|| py.None(), Bound::unbind))
    }

    #[getter]
    fn cls(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if !matches!(
            self.requesting_scope,
            FixtureScope::Class | FixtureScope::Function
        ) {
            return Err(PyAttributeError::new_err(format!(
                "cls not available in {}-scoped context",
                self.requesting_scope.as_str()
            )));
        }
        let class = py.import("_pytest.python")?.getattr("Class")?;
        let class_collector = self.item()?.bind(py).call_method1("getparent", (class,))?;
        if class_collector.is_none() {
            Ok(py.None())
        } else {
            Ok(class_collector.getattr("obj")?.unbind())
        }
    }

    #[getter]
    fn module(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if !matches!(
            self.requesting_scope,
            FixtureScope::Function | FixtureScope::Class | FixtureScope::Module
        ) {
            return Err(PyAttributeError::new_err(format!(
                "module not available in {}-scoped context",
                self.requesting_scope.as_str()
            )));
        }
        let module = py.import("_pytest.python")?.getattr("Module")?;
        let module_collector = self.item()?.bind(py).call_method1("getparent", (module,))?;
        module_collector.getattr("obj").map(Bound::unbind)
    }

    #[getter]
    fn path(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.requesting_scope == FixtureScope::Session {
            return Err(PyAttributeError::new_err(
                "path not available in session-scoped context",
            ));
        }
        Ok(self.item()?.bind(py).getattr("path")?.unbind())
    }

    #[getter]
    fn fspath(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let fixture_request = py.import("_pytest.fixtures")?.getattr("FixtureRequest")?;
        if !fixture_request.hasattr("fspath")? {
            return Err(PyAttributeError::new_err("fspath"));
        }
        let path = self.path(py)?;
        py.import("_pytest.compat")?
            .getattr("legacy_path")?
            .call1((path,))
            .map(Bound::unbind)
    }

    #[getter]
    fn keywords(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.node(py)?.bind(py).getattr("keywords")?.unbind())
    }

    #[getter]
    fn fixturenames(&self, py: Python<'_>) -> PyResult<Vec<String>> {
        let item = self.item()?.bind(py);
        let mut names = item.getattr("fixturenames")?.extract::<Vec<String>>()?;
        for name in self.resolved_fixture_keys.dynamic_names() {
            if !names.contains(&name) {
                names.push(name);
            }
        }
        Ok(names)
    }
}

impl NativeRequest {
    pub(super) fn for_test(
        runtime: &SharedRuntime,
        item: Py<PyAny>,
        test_index: usize,
        fixture_stack: FixtureStack,
    ) -> Self {
        Self {
            runtime: runtime.downgrade(),
            resolved_fixture_keys: runtime.fixture_keys(test_index),
            item: Some(item),
            package_baseid: None,
            test_index,
            target: None,
            requesting_scope: FixtureScope::Function,
            fixture_name: None,
            param: None,
            fixture_stack,
        }
    }

    fn active_runtime(&self) -> PyResult<SharedRuntime> {
        self.runtime
            .upgrade()
            .ok_or_else(|| PyRuntimeError::new_err("native pytest request is no longer active"))
    }

    fn item(&self) -> PyResult<&Py<PyAny>> {
        self.item
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("native pytest request has been cleared"))
    }
}
