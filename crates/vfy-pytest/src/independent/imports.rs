//! Module identity and search-path policy for independent collection.

use std::path::{Path, PathBuf};

use pyo3::prelude::*;

#[derive(Clone, Copy)]
pub(super) enum ImportPurpose {
    Conftest,
    TestModule,
    /// Doctest modules report their import exception without pytest's module wrapper.
    Doctest,
}

impl ImportPurpose {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Conftest => "conftest",
            Self::TestModule => "test_module",
            Self::Doctest => "doctest",
        }
    }
}

pub(super) fn load_module(
    py: Python<'_>,
    path: &Path,
    root: &Path,
    mode: ImportMode,
    purpose: ImportPurpose,
) -> PyResult<Py<PyAny>> {
    let plan =
        ImportPlan::new(path, root, mode).map_err(pyo3::exceptions::PyValueError::new_err)?;
    if matches!(purpose, ImportPurpose::Conftest)
        && plan.module_name == "conftest"
        && path.file_name().is_some_and(|name| name == "conftest.py")
    {
        let modules = py.import("sys")?.getattr("modules")?;
        if modules.contains("conftest")? {
            modules.del_item("conftest")?;
        }
    }
    Ok(py
        .import("vfy.independent.imports")?
        .call_method1(
            "load_module",
            (
                plan.module_name,
                path.to_string_lossy().as_ref(),
                plan.search_directory
                    .map(|path| path.to_string_lossy().into_owned()),
                mode.as_str(),
                purpose.as_str(),
            ),
        )?
        .unbind())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ImportMode {
    Prepend,
    Append,
    Importlib,
}

impl ImportMode {
    pub(super) fn parse(value: &str) -> Result<Self, String> {
        match value {
            "prepend" => Ok(Self::Prepend),
            "append" => Ok(Self::Append),
            "importlib" => Ok(Self::Importlib),
            _ => Err(format!(
                "invalid import mode {value:?}; choose prepend, append, or importlib"
            )),
        }
    }

    pub(super) const fn as_str(self) -> &'static str {
        match self {
            Self::Prepend => "prepend",
            Self::Append => "append",
            Self::Importlib => "importlib",
        }
    }
}

pub(super) struct ImportPlan {
    pub(super) module_name: String,
    pub(super) search_directory: Option<PathBuf>,
}

impl ImportPlan {
    pub(super) fn new(path: &Path, root: &Path, mode: ImportMode) -> Result<Self, String> {
        let stem = path
            .file_stem()
            .and_then(|value| value.to_str())
            .ok_or_else(|| format!("cannot determine Python module name for {}", path.display()))?;
        let mut parts = vec![stem.to_owned()];
        let mut parent = path.parent();
        while let Some(directory) = parent {
            if !directory.join("__init__.py").is_file() {
                break;
            }
            let part = directory
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| format!("invalid package directory {}", directory.display()))?;
            parts.push(part.to_owned());
            parent = directory.parent();
        }
        parts.reverse();
        if mode == ImportMode::Importlib && parts.len() == 1 {
            let relative = path.strip_prefix(root).unwrap_or(path);
            let without_suffix = relative.with_extension("");
            parts = without_suffix
                .components()
                .filter_map(|component| match component {
                    std::path::Component::Normal(value) => {
                        Some(value.to_string_lossy().replace('.', "_"))
                    }
                    _ => None,
                })
                .collect();
        }
        // A package initializer imports as its package, as in pytest.
        if parts.len() > 1 && parts.last().is_some_and(|part| part == "__init__") {
            parts.pop();
        }
        Ok(Self {
            module_name: parts.join("."),
            search_directory: if mode == ImportMode::Importlib {
                None
            } else {
                parent.map(Path::to_path_buf)
            },
        })
    }
}

impl super::Collector<'_> {
    pub(super) fn refresh_import_mode(&mut self) -> PyResult<()> {
        if let Some(context) = &self.context {
            let value = context
                .config
                .bind(self.py)
                .call_method1("getoption", ("import_mode", "prepend"))?;
            self.import_mode = ImportMode::parse(&value.extract::<String>()?)
                .map_err(crate::IndependentUsageError::new_err)?;
        }
        Ok(())
    }
}

/// Import a source file with the session's root and import mode, as test modules are.
#[pyfunction]
pub(crate) fn independent_import_path(
    py: Python<'_>,
    config: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let root = std::path::PathBuf::from(config.getattr("rootpath")?.str()?.to_str()?);
    let mode = config
        .call_method1("getoption", ("import_mode", "prepend"))?
        .extract::<String>()?;
    let mode = ImportMode::parse(&mode).map_err(crate::IndependentUsageError::new_err)?;
    let path = std::path::PathBuf::from(path.str()?.to_str()?);
    load_module(py, &path, &root, mode, ImportPurpose::Doctest)
}

/// Apply `python_functions` or `python_classes` naming, as `PyCollector` filters do.
#[pyfunction]
pub(crate) fn independent_matches_python_name(name: &str, patterns: Vec<String>) -> PyResult<bool> {
    vfy_core::independent_collection::matches_python_name(name, patterns)
        .map_err(crate::IndependentUsageError::new_err)
}
