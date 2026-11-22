//! Lazily retain frequently used modules while resolving their attributes live.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyCFunction, PyString};

pub(crate) enum Module {
    Builtins,
    Os,
    Inspect,
    Pathlib,
    Functools,
    Protocol,
    TerminalResources,
    Sys,
    Warnings,
    Operator,
    OsPath,
    UnicodeData,
    Facade,
    Capture,
    Collectors,
    Parameters,
    Subtests,
}

impl Module {
    pub(crate) fn get(self, py: Python<'_>) -> PyResult<Bound<'_, PyModule>> {
        static BUILTINS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static OS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static INSPECT: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static PATHLIB: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static FUNCTOOLS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static PROTOCOL: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static TERMINAL_RESOURCES: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static SYS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static WARNINGS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static OPERATOR: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static OSPATH: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static UNICODEDATA: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static FACADE: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static CAPTURE: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static COLLECTORS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static PARAMETERS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        static SUBTESTS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();
        let (cell, name) = match self {
            Module::Builtins => (&BUILTINS, "builtins"),
            Module::Os => (&OS, "os"),
            Module::Inspect => (&INSPECT, "inspect"),
            Module::Pathlib => (&PATHLIB, "pathlib"),
            Module::Functools => (&FUNCTOOLS, "functools"),
            Module::Protocol => (&PROTOCOL, "vfy.independent.protocol"),
            Module::TerminalResources => {
                (&TERMINAL_RESOURCES, "vfy.independent.terminal_resources")
            }
            Module::Sys => (&SYS, "sys"),
            Module::Warnings => (&WARNINGS, "warnings"),
            Module::Operator => (&OPERATOR, "operator"),
            Module::OsPath => (&OSPATH, "os.path"),
            Module::UnicodeData => (&UNICODEDATA, "unicodedata"),
            Module::Facade => (&FACADE, "vfy.independent.facade"),
            Module::Capture => (&CAPTURE, "vfy.independent.capture"),
            Module::Collectors => (&COLLECTORS, "vfy.independent.collectors"),
            Module::Parameters => (&PARAMETERS, "vfy.independent.parameters"),
            Module::Subtests => (&SUBTESTS, "vfy.independent.subtests_provider"),
        };
        if let Some(module) = cell.get(py) {
            return Ok(module.bind(py).clone());
        }
        // Import can invoke Python and reenter this function, so do it outside the lock.
        let module = py.import(name)?;
        let _ = cell.set(py, module.clone().unbind());
        Ok(module)
    }
}

/// A real builtin uses `CPython`'s optional-attribute lookup without creating
/// an `AttributeError` for the common missing case on the Python 3.12 ABI.
enum OptionalGetattr {
    Builtin {
        callable: Py<PyCFunction>,
        sentinel: Py<PyAny>,
    },
    Direct,
}

static OPTIONAL_GETATTR: PyOnceLock<OptionalGetattr> = PyOnceLock::new();

/// Capture before plugins load; a replaced builtin is never called by native lookups.
pub(crate) fn initialize_optional_getattr(py: Python<'_>) -> PyResult<()> {
    if OPTIONAL_GETATTR.get(py).is_some() {
        return Ok(());
    }
    let builtins = py.import("builtins")?;
    let candidate = builtins.dict().get_item("getattr")?;
    let cached = match candidate {
        Some(candidate)
            if candidate.cast_exact::<PyCFunction>().is_ok()
                && candidate.getattr("__self__")?.is(&builtins)
                && candidate.getattr("__name__")?.extract::<String>()? == "getattr" =>
        {
            OptionalGetattr::Builtin {
                callable: candidate.cast_into_exact::<PyCFunction>()?.unbind(),
                sentinel: pyo3::types::PyDict::new(py).into_any().unbind(),
            }
        }
        _ => OptionalGetattr::Direct,
    };
    let _ = OPTIONAL_GETATTR.set(py, cached);
    Ok(())
}

pub(crate) fn optional_attr<'py>(
    value: &Bound<'py, PyAny>,
    key: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = value.py();
    match OPTIONAL_GETATTR.get(py) {
        Some(OptionalGetattr::Builtin { callable, sentinel }) => {
            let result = callable.bind(py).call1((value, key, sentinel.bind(py)))?;
            Ok(if result.is(sentinel) {
                None
            } else {
                Some(result)
            })
        }
        Some(OptionalGetattr::Direct) | None => value.getattr_opt(key),
    }
}

/// Presence checks release a descriptor result before the caller proceeds.
pub(crate) fn has_attr(value: &Bound<'_, PyAny>, key: &Bound<'_, PyString>) -> PyResult<bool> {
    let result = optional_attr(value, key)?;
    let present = result.is_some();
    drop(result);
    Ok(present)
}
