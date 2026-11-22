//! One reversible mutation journal behind the Python monkeypatch protocol.
//!
//! Journals retain the mutable Python lists exposed by the adapter.
//! Each completed undo stage clears that same list; failed stages retain it.
//! Clone references before invoking Python and drop replaced references after
//! releasing the state borrow, including references released during GC.

use pyo3::exceptions::{PyAttributeError, PyKeyError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString};

#[derive(Clone, Copy)]
enum Journal {
    Attribute,
    Item,
}

// CPython keeps these loop locals through the remaining undo stages. The
// attribute and item loops share `value`; assigning the first item releases
// the final attribute value. Field order matches the frame's local cleanup.
#[derive(Default)]
struct UndoBindings {
    attribute_target: Option<Py<PyAny>>,
    attribute_name: Option<Py<PyAny>>,
    value: Option<Py<PyAny>>,
    mapping: Option<Py<PyAny>>,
    key: Option<Py<PyAny>>,
}

impl UndoBindings {
    fn assign_attribute(&mut self, record: &Bound<'_, PyAny>) -> PyResult<()> {
        let (target, name, value): (Py<PyAny>, Py<PyAny>, Py<PyAny>) = record.extract()?;
        drop(self.attribute_target.replace(target));
        drop(self.attribute_name.replace(name));
        drop(self.value.replace(value));
        Ok(())
    }

    fn assign_item(&mut self, record: &Bound<'_, PyAny>) -> PyResult<()> {
        let (mapping, key, value): (Py<PyAny>, Py<PyAny>, Py<PyAny>) = record.extract()?;
        drop(self.mapping.replace(mapping));
        drop(self.key.replace(key));
        drop(self.value.replace(value));
        Ok(())
    }

    fn current(value: Option<&Py<PyAny>>) -> PyResult<&Py<PyAny>> {
        value.ok_or_else(|| PyRuntimeError::new_err("monkeypatch undo binding is not initialized"))
    }
}

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeMonkeyPatchState {
    attributes: Option<Py<PyList>>,
    items: Option<Py<PyList>>,
    missing: Option<Py<PyAny>>,
    path: Option<Py<PyAny>>,
    cwd: Option<Py<PyAny>>,
}

impl NativeMonkeyPatchState {
    fn journal(slf: &Bound<'_, Self>, kind: Journal) -> PyResult<Py<PyList>> {
        let value = {
            let state = slf.borrow();
            match kind {
                Journal::Attribute => &state.attributes,
                Journal::Item => &state.items,
            }
            .as_ref()
            .map(|value| value.clone_ref(slf.py()))
        };
        value.ok_or_else(|| PyRuntimeError::new_err("monkeypatch state is cleared"))
    }

    fn missing(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let value = slf
            .borrow()
            .missing
            .as_ref()
            .map(|value| value.clone_ref(slf.py()));
        value.ok_or_else(|| PyRuntimeError::new_err("monkeypatch state is cleared"))
    }

    fn replace_journal(slf: &Bound<'_, Self>, kind: Journal, value: Py<PyList>) {
        let previous = {
            let mut state = slf.borrow_mut();
            match kind {
                Journal::Attribute => state.attributes.replace(value),
                Journal::Item => state.items.replace(value),
            }
        };
        drop(previous);
    }

    fn warn_legacy_namespace(
        py: Python<'_>,
        resources: &Bound<'_, PyAny>,
        path: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let has_packages = py
            .import("builtins")?
            .call_method1("hasattr", (resources, "_namespace_packages"))?;
        if !has_packages.is_truthy()? || !resources.getattr("_namespace_packages")?.is_truthy()? {
            return Ok(());
        }
        let directory = py
            .import("pathlib")?
            .getattr("Path")?
            .call1((path.str()?,))?;
        let separator = py.import("os")?.getattr("sep")?;
        for package in resources.getattr("_namespace_packages")?.try_iter()? {
            let package = package?;
            if package.is_none() {
                continue;
            }
            let suffix = package.call_method1("replace", (".", &separator))?;
            let location = directory.call_method1("__truediv__", (suffix,))?;
            if location.call_method0("is_dir")?.is_truthy()? {
                let warning = py.import("vfy.independent.facade")?.getattr("PytestRemovedIn10Warning")?.call1((
                    "monkeypatch.syspath_prepend() called with pkg_resources legacy namespace packages detected.\nLegacy namespace packages (using pkg_resources.declare_namespace) are deprecated.\nPlease use native namespace packages (PEP 420) instead.\nSee https://docs.pytest.org/en/stable/deprecations.html#monkeypatch-fixup-namespace-packages",
                ))?;
                let keywords = PyDict::new(py);
                keywords.set_item("stacklevel", 2)?;
                py.import("warnings")?
                    .call_method("warn", (warning,), Some(&keywords))?;
                break;
            }
        }
        Ok(())
    }

    fn clear_journal(slf: &Bound<'_, Self>, kind: Journal) -> PyResult<()> {
        let py = slf.py();
        let journal = Self::journal(slf, kind)?;
        let entire = py.get_type::<PySlice>().call1((py.None(),))?;
        journal
            .bind(py)
            .as_any()
            .set_item(entire, PyList::empty(py))
    }
}

#[pymethods]
impl NativeMonkeyPatchState {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Self> {
        let missing = py
            .import("vfy.independent.compat")?
            .getattr("NOTSET")?
            .unbind();
        Ok(Self {
            attributes: Some(PyList::empty(py).unbind()),
            items: Some(PyList::empty(py).unbind()),
            missing: Some(missing),
            path: None,
            cwd: None,
        })
    }

    #[getter]
    fn attributes(slf: &Bound<'_, Self>) -> PyResult<Py<PyList>> {
        Self::journal(slf, Journal::Attribute)
    }

    #[setter]
    fn set_attributes(slf: &Bound<'_, Self>, value: Py<PyList>) {
        Self::replace_journal(slf, Journal::Attribute, value);
    }

    #[getter]
    fn items(slf: &Bound<'_, Self>) -> PyResult<Py<PyList>> {
        Self::journal(slf, Journal::Item)
    }

    #[setter]
    fn set_items(slf: &Bound<'_, Self>, value: Py<PyList>) {
        Self::replace_journal(slf, Journal::Item, value);
    }

    fn setattr(
        slf: &Bound<'_, Self>,
        target: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        raising: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let missing = Self::missing(slf)?;
        let builtins = py.import("builtins")?;
        let mut previous = builtins.call_method1("getattr", (target, name, missing.bind(py)))?;
        if raising.is_truthy()? && previous.is(missing.bind(py)) {
            let message = PyString::new(py, "{} has no attribute {}")
                .call_method1("format", (target.repr()?, name.repr()?))?;
            return Err(PyAttributeError::new_err(message.unbind()));
        }
        if builtins
            .call_method1("isinstance", (target, builtins.getattr("type")?))?
            .is_truthy()?
        {
            previous = target
                .getattr("__dict__")?
                .call_method1("get", (name, missing.bind(py)))?;
        }
        builtins.call_method1("setattr", (target, name, value))?;
        Self::journal(slf, Journal::Attribute)?
            .bind(py)
            .call_method1("append", ((target, name, previous),))?;
        Ok(())
    }

    fn delattr(
        slf: &Bound<'_, Self>,
        target: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
        raising: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let builtins = py.import("builtins")?;
        if !builtins
            .call_method1("hasattr", (target, name))?
            .is_truthy()?
        {
            return if raising.is_truthy()? {
                Err(PyAttributeError::new_err((name.clone().unbind(),)))
            } else {
                Ok(())
            };
        }
        let missing = Self::missing(slf)?;
        let mut previous = builtins.call_method1("getattr", (target, name, missing.bind(py)))?;
        if builtins
            .call_method1("isinstance", (target, builtins.getattr("type")?))?
            .is_truthy()?
        {
            previous = target
                .getattr("__dict__")?
                .call_method1("get", (name, missing.bind(py)))?;
        }
        Self::journal(slf, Journal::Attribute)?
            .bind(py)
            .call_method1("append", ((target, name, previous),))?;
        builtins.call_method1("delattr", (target, name))?;
        Ok(())
    }

    fn setitem(
        slf: &Bound<'_, Self>,
        mapping: &Bound<'_, PyAny>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        let journal = Self::journal(slf, Journal::Item)?;
        let append = journal.bind(py).getattr("append")?;
        let missing = Self::missing(slf)?;
        let previous = mapping.call_method1("get", (key, missing.bind(py)))?;
        append.call1(((mapping, key, previous),))?;
        mapping.set_item(key, value)
    }

    fn delitem(
        slf: &Bound<'_, Self>,
        mapping: &Bound<'_, PyAny>,
        key: &Bound<'_, PyAny>,
        raising: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let py = slf.py();
        if !mapping.contains(key)? {
            return if raising.is_truthy()? {
                Err(PyKeyError::new_err((key.clone().unbind(),)))
            } else {
                Ok(())
            };
        }
        let journal = Self::journal(slf, Journal::Item)?;
        let append = journal.bind(py).getattr("append")?;
        let missing = Self::missing(slf)?;
        let previous = mapping.call_method1("get", (key, missing.bind(py)))?;
        append.call1(((mapping, key, previous),))?;
        mapping.del_item(key)
    }

    fn syspath_prepend(slf: &Bound<'_, Self>, path: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let sys = py.import("sys")?;
        let paths = sys.getattr("path")?;
        let capture_path = slf.borrow().path.is_none();
        if capture_path {
            let entire = py.get_type::<PySlice>().call1((py.None(),))?;
            let snapshot = paths.get_item(entire)?.unbind();
            let previous = slf.borrow_mut().path.replace(snapshot);
            drop(previous);
        }
        let insert = paths.getattr("insert")?;
        let text = path.str()?;
        insert.call1((0, &text))?;
        if sys.getattr("modules")?.contains("pkg_resources")? {
            let resources = py.import("pkg_resources")?;
            let fixup = resources.getattr("fixup_namespace_packages")?;
            Self::warn_legacy_namespace(py, &resources, path)?;
            fixup.call1((path.str()?,))?;
        }
        py.import("importlib")?.call_method0("invalidate_caches")?;
        Ok(())
    }

    fn chdir(slf: &Bound<'_, Self>, path: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let os = py.import("os")?;
        let capture_cwd = slf.borrow().cwd.is_none();
        if capture_cwd {
            let current = os.call_method0("getcwd")?.unbind();
            let previous = slf.borrow_mut().cwd.replace(current);
            drop(previous);
        }
        os.call_method1("chdir", (path,))?;
        Ok(())
    }

    fn undo(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let missing = Self::missing(slf)?;
        let builtins = py.import("builtins")?;
        let mut bindings = UndoBindings::default();
        {
            let attributes = Self::journal(slf, Journal::Attribute)?;
            for record in attributes
                .bind(py)
                .call_method0("__reversed__")?
                .try_iter()?
            {
                bindings.assign_attribute(&record?)?;
                let target = UndoBindings::current(bindings.attribute_target.as_ref())?;
                let name = UndoBindings::current(bindings.attribute_name.as_ref())?;
                let value = UndoBindings::current(bindings.value.as_ref())?;
                if value.bind(py).is(missing.bind(py)) {
                    builtins.call_method1("delattr", (target, name))?;
                } else {
                    builtins.call_method1("setattr", (target, name, value))?;
                }
            }
        }
        Self::clear_journal(slf, Journal::Attribute)?;
        {
            let items = Self::journal(slf, Journal::Item)?;
            for record in items.bind(py).call_method0("__reversed__")?.try_iter()? {
                bindings.assign_item(&record?)?;
                let mapping = UndoBindings::current(bindings.mapping.as_ref())?.bind(py);
                let key = UndoBindings::current(bindings.key.as_ref())?;
                let value = UndoBindings::current(bindings.value.as_ref())?;
                if value.bind(py).is(missing.bind(py)) {
                    match mapping.del_item(key) {
                        Err(error) if error.is_instance_of::<PyKeyError>(py) => (),
                        result => result?,
                    }
                } else {
                    mapping.set_item(key, value)?;
                }
            }
        }
        Self::clear_journal(slf, Journal::Item)?;
        let path = slf.borrow().path.as_ref().map(|value| value.clone_ref(py));
        if let Some(path) = path {
            let entire = py.get_type::<PySlice>().call1((py.None(),))?;
            py.import("sys")?.getattr("path")?.set_item(entire, path)?;
            let previous = slf.borrow_mut().path.take();
            drop(previous);
        }
        let cwd = slf.borrow().cwd.as_ref().map(|value| value.clone_ref(py));
        if let Some(cwd) = cwd {
            py.import("os")?.call_method1("chdir", (cwd,))?;
            let previous = slf.borrow_mut().cwd.take();
            drop(previous);
        }
        drop(bindings);
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 supplies its GC visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.attributes)?;
        visit.call(&self.items)?;
        visit.call(&self.missing)?;
        visit.call(&self.path)?;
        visit.call(&self.cwd)
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let previous = {
            let mut state = slf.borrow_mut();
            (
                state.attributes.take(),
                state.items.take(),
                state.missing.take(),
                state.path.take(),
                state.cwd.take(),
            )
        };
        drop(previous);
    }
}
