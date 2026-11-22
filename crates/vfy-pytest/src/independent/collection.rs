//! Active collection callbacks and initial conftest loading.

pub(in crate::independent) mod arena;
mod functions;
pub(in crate::independent) mod objects;
mod traversal;

use super::Collector;
use crate::module_cache::Module;
use pyo3::exceptions::{PyAttributeError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::{PyTraverseError, PyVisit};
use std::path::{Path, PathBuf};
use vfy_core::independent_collection::{
    CollectionCandidate, CollectionExclusions, CollectionIgnoreSettings, ConftestPathValue,
    DirectoryPolicy, DiscoveryError, collection_directories, initial_conftest_anchors,
};

/// Import only stock's initial anchors before final plugin option parsing.
pub(super) fn preload(
    collector: &mut Collector<'_>,
    invocation_dir: &Path,
    arguments: &[String],
) -> PyResult<()> {
    let context = collector.context.as_ref().expect("initialized context");
    let testpaths = context
        .config
        .bind(collector.py)
        .call_method1("getini", ("testpaths",))?
        .extract::<Vec<String>>()?;
    let cutoff = collector.conftest_cutoff()?;
    let anchors = initial_conftest_anchors(&collector.root, invocation_dir, arguments, &testpaths)
        .map_err(crate::IndependentUsageError::new_err)?;
    for anchor in anchors {
        let directory = if anchor.is_file() {
            anchor.parent().unwrap_or(&anchor)
        } else {
            &anchor
        };
        load_conftests(collector, directory, &cutoff)?;
    }
    Ok(())
}

fn load_conftests(collector: &mut Collector<'_>, directory: &Path, cutoff: &Path) -> PyResult<()> {
    let context = collector
        .context
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("conftest loading requires a configured session"))?;
    let arena = arena::for_context(collector.py, context)?;
    if arena.borrow().conftests_loaded(directory) {
        return Ok(());
    }
    for parent in collection_directories(cutoff, directory) {
        let path = parent.join("conftest.py");
        if path.is_file() {
            collector.import_file(&path)?;
        }
    }
    arena.borrow_mut().complete_conftests(directory);
    Ok(())
}

/// Refresh naming after module import, before native object discovery.
pub(super) fn refresh_naming(collector: &mut Collector<'_>) -> PyResult<()> {
    let Some(context) = &collector.context else {
        return Ok(());
    };
    let config = context.config.bind(collector.py);
    let values = |name| {
        config
            .call_method1("getini", (name,))?
            .extract::<Vec<String>>()
    };
    if let Some(specification) = &mut collector.specification {
        specification
            .update_naming(
                values("python_files")?,
                values("python_classes")?,
                values("python_functions")?,
            )
            .map_err(crate::IndependentUsageError::new_err)?;
    }
    Ok(())
}

pub(super) use traversal::collect;

struct NativeVisitor<'a, 'py> {
    conftest_cutoff: PathBuf,
    collector: &'a mut Collector<'py>,
}

impl DirectoryPolicy for NativeVisitor<'_, '_> {
    type Error = PyErr;

    fn enter_directory(&mut self, path: &Path) -> PyResult<()> {
        load_conftests(self.collector, path, &self.conftest_cutoff)
    }

    fn ignore(&mut self, candidate: &CollectionCandidate<'_>) -> PyResult<bool> {
        ignore_candidate(self.collector, candidate.path)
    }

    fn accept_file(&mut self, path: &Path, _default_matches: bool) -> PyResult<bool> {
        // Collect-file plugins decide every remaining file; otherwise the live
        // `python_files` policy decides.
        let accepted =
            self.collector.collect_file_hooked()? || self.collector.matches_python_files(path)?;
        Ok(accepted && !self.collector.outside_collection_scope(path)?)
    }
}

fn ignore_candidate(collector: &mut Collector<'_>, path: &Path) -> PyResult<bool> {
    let py = collector.py;
    let context = collector.context.as_ref().expect("initialized context");
    let parent = path.parent().unwrap_or(&collector.root);
    let mut modules = collector
        .modules
        .iter()
        .filter(|(path, _)| {
            path.file_name().is_some_and(|name| name == "conftest.py")
                && path
                    .parent()
                    .is_some_and(|directory| parent.starts_with(directory))
        })
        .map(|(path, module)| (path.clone(), module.clone_ref(py)))
        .collect::<Vec<_>>();
    modules.sort_by_key(|(path, _)| path.components().count());
    let callback = Py::new(py, BuiltinIgnore { modules })?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("config", &context.config)?;
    kwargs.set_item("collection_path", path_object(py, path)?)?;
    let removed = context.removed_plugins(py, parent)?;
    context
        .registry
        .bind(py)
        .call_method1(
            "call_subset_with_default",
            (
                "pytest_ignore_collect",
                kwargs,
                removed,
                callback,
                true,
                "main",
            ),
        )?
        .is_truthy()
}

/// A `String` rejected by the Rust collection model, surfaced as a usage error.
fn usage_error(message: String) -> PyErr {
    crate::IndependentUsageError::new_err(message)
}

fn discovery_error(error: DiscoveryError<PyErr>) -> PyErr {
    match error {
        DiscoveryError::CollectionArenaRequired => crate::UnsupportedFeatureError::new_err(
            "keep-duplicates requires live collector identities",
        ),
        DiscoveryError::Visitor(error) => error,
        DiscoveryError::Filesystem { path, source } => match source.raw_os_error() {
            Some(code) => PyOSError::new_err((
                code,
                source.to_string(),
                path.to_string_lossy().into_owned(),
            )),
            None => PyErr::from(source),
        },
        DiscoveryError::RecursiveDirectory(path) => PyValueError::new_err(format!(
            "recursive symlink directory during test discovery: {}",
            path.display()
        )),
    }
}

#[pyclass(unsendable)]
struct BuiltinIgnore {
    modules: Vec<(PathBuf, Py<PyAny>)>,
}

#[pymethods]
impl BuiltinIgnore {
    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let inspect = Module::Inspect.get(py)?;
        let parameter = inspect.getattr("Parameter")?;
        let kind = parameter.getattr("POSITIONAL_OR_KEYWORD")?;
        let parameters = PyList::empty(py);
        for name in ["collection_path", "config"] {
            parameters.append(parameter.call1((name, &kind))?)?;
        }
        Ok(inspect.getattr("Signature")?.call1((parameters,))?.unbind())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for (_, module) in &self.modules {
            visit.call(module)?;
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = std::mem::take(&mut slf.borrow_mut().modules);
        drop(removed);
    }

    fn __call__(
        slf: &Bound<'_, Self>,
        collection_path: &Bound<'_, PyAny>,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<Option<bool>> {
        let py = slf.py();
        let modules = slf
            .borrow()
            .modules
            .iter()
            .map(|(path, module)| (path.clone(), module.clone_ref(py)))
            .collect::<Vec<_>>();
        builtin_ignore(py, &modules, collection_path, config)
    }
}

fn builtin_ignore(
    py: Python<'_>,
    modules: &[(PathBuf, Py<PyAny>)],
    collection_path: &Bound<'_, PyAny>,
    config: &Bound<'_, PyAny>,
) -> PyResult<Option<bool>> {
    let path = PathBuf::from(
        Module::Os
            .get(py)?
            .call_method1("fspath", (collection_path,))?
            .extract::<String>()?,
    );
    if CollectionExclusions::is_bytecode_cache(&path) {
        return Ok(Some(true));
    }
    let invocation_dir = std::env::current_dir().map_err(PyErr::from)?;
    let mut exclusions = CollectionExclusions::from_resolved(CollectionIgnoreSettings {
        invocation_dir: invocation_dir.clone(),
        ignore: Vec::new(),
        ignore_glob: Vec::new(),
        norecursedirs: Vec::new(),
        collect_in_virtualenv: true,
    })
    .map_err(usage_error)?;
    if let Some((base, values)) = conftest_paths(py, modules, "collect_ignore")? {
        exclusions.add_paths(&base, &values).map_err(usage_error)?;
    }
    let values = option_paths(config, "ignore")?;
    exclusions
        .add_paths(&invocation_dir, &values)
        .map_err(usage_error)?;
    if exclusions.is_ignored(&path) {
        return Ok(Some(true));
    }
    if let Some((base, values)) = conftest_paths(py, modules, "collect_ignore_glob")? {
        exclusions.add_globs(&base, &values).map_err(usage_error)?;
    }
    let values = option_paths(config, "ignore_glob")?;
    exclusions
        .add_globs(&invocation_dir, &values)
        .map_err(usage_error)?;
    if exclusions.is_ignored(&path) {
        return Ok(Some(true));
    }
    if !config
        .call_method1("getoption", ("collect_in_virtualenv", false))?
        .is_truthy()?
        && CollectionExclusions::is_virtual_environment(&path)
    {
        return Ok(Some(true));
    }
    if path.is_dir() {
        let values = config
            .call_method1("getini", ("norecursedirs",))?
            .extract()?;
        if CollectionExclusions::matches_norecursedirs(&path, values).map_err(usage_error)? {
            return Ok(Some(true));
        }
    }
    Ok(None)
}

fn conftest_paths(
    py: Python<'_>,
    modules: &[(PathBuf, Py<PyAny>)],
    name: &str,
) -> PyResult<Option<(PathBuf, Vec<ConftestPathValue>)>> {
    let pathlike = Module::Os.get(py)?.getattr("PathLike")?;
    for (source, module) in modules.iter().rev() {
        let values = match module.bind(py).getattr(name) {
            Ok(values) => values,
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => continue,
            Err(error) => return Err(error),
        };
        let mut paths = Vec::new();
        for value in values.try_iter()? {
            let value = value?;
            paths.push(if value.is_instance(&pathlike)? {
                let path = Module::Os.get(py)?.call_method1("fspath", (&value,))?;
                ConftestPathValue::PathLike(PathBuf::from(path.extract::<String>()?))
            } else {
                let separator = std::path::MAIN_SEPARATOR_STR;
                let text = value.call_method1("replace", ("/", separator))?;
                ConftestPathValue::String(text.extract()?)
            });
        }
        let base = source.parent().expect("conftest directory").to_path_buf();
        return Ok(Some((base, paths)));
    }
    Ok(None)
}

fn option_paths(config: &Bound<'_, PyAny>, name: &str) -> PyResult<Vec<ConftestPathValue>> {
    let values = config.call_method1("getoption", (name,))?;
    if values.is_none() {
        return Ok(Vec::new());
    }
    let os = config.py().import("os")?;
    values
        .try_iter()?
        .map(|value| {
            let path = os.call_method1("fspath", (value?,))?.extract::<String>()?;
            Ok(ConftestPathValue::String(path))
        })
        .collect()
}

/// The ``pathlib.Path`` value that plug-in hooks receive for ``path``.
fn path_object<'py>(py: Python<'py>, path: &Path) -> PyResult<Bound<'py, PyAny>> {
    Module::Pathlib
        .get(py)?
        .getattr("Path")?
        .call1((path.to_string_lossy().as_ref(),))
}
