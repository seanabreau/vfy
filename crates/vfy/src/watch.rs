//! `PyO3` adapters for test watching and staged artifact publication.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyfunction]
fn source_tree_identity(
    py: Python<'_>,
    roots: Vec<String>,
    ignored: Vec<String>,
) -> PyResult<String> {
    let roots = roots.into_iter().map(PathBuf::from).collect::<Vec<_>>();
    let ignored = ignored.into_iter().map(PathBuf::from).collect::<Vec<_>>();
    py.detach(|| vfy_core::source_tree_identity(&roots, &ignored))
        .map_err(|error| watch_error(&error))
}

#[pyclass(name = "WatchRun", frozen)]
struct NativeWatchRun {
    #[pyo3(get)]
    input_identity: String,
    #[pyo3(get)]
    full_run: bool,
}

fn run_outcome(status: i32, full_run: bool) -> vfy_core::RunOutcome {
    match (status, full_run) {
        (0, true) => vfy_core::RunOutcome::PassedFull,
        (0, false) => vfy_core::RunOutcome::PassedSubset,
        _ => vfy_core::RunOutcome::Failed,
    }
}

fn watch_error(error: &std::io::Error) -> PyErr {
    PyRuntimeError::new_err(format!("failed to watch the source tree: {error}"))
}

fn staging_error(operation: &str, path: &Path, error: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(format!(
        "failed to {operation} `{}`: {error}",
        path.display()
    ))
}

#[pyclass(name = "WatchSession", unsendable)]
pub(crate) struct NativeWatchSession {
    inner: vfy_core::WatchSession,
}

#[pymethods]
impl NativeWatchSession {
    #[new]
    #[pyo3(signature = (roots, ignored=Vec::new(), debounce_ms=200))]
    fn new(roots: Vec<String>, ignored: Vec<String>, debounce_ms: u64) -> PyResult<Self> {
        let roots: Vec<PathBuf> = roots.into_iter().map(PathBuf::from).collect();
        if roots.is_empty() {
            return Err(PyRuntimeError::new_err("watch session requires a root"));
        }
        let ignored: Vec<PathBuf> = ignored.into_iter().map(PathBuf::from).collect();
        let inner =
            vfy_core::WatchSession::new(&roots, &ignored, Some(debounce_ms)).map_err(|error| {
                PyRuntimeError::new_err(format!("failed to watch project: {error}"))
            })?;
        Ok(Self { inner })
    }

    fn begin_run(&mut self, py: Python<'_>) -> PyResult<NativeWatchRun> {
        py.detach(|| self.inner.begin_run())
            .map(|run| NativeWatchRun {
                input_identity: run.input_identity,
                full_run: run.full_run,
            })
            .map_err(|error| watch_error(&error))
    }

    fn is_current(&mut self, py: Python<'_>) -> PyResult<bool> {
        py.detach(|| self.inner.is_current())
            .map_err(|error| watch_error(&error))
    }

    fn finish_run(&mut self, py: Python<'_>, status: i32, full_run: bool) -> PyResult<bool> {
        let outcome = run_outcome(status, full_run);
        py.detach(|| self.inner.finish_run(outcome))
            .map(|currency| currency == vfy_core::RunCurrency::Obsolete)
            .map_err(|error| watch_error(&error))
    }

    fn finalize(
        &mut self,
        py: Python<'_>,
        stage: String,
        artifacts: Vec<(String, String)>,
        status: i32,
        full_run: bool,
    ) -> PyResult<bool> {
        let stage = PathBuf::from(stage);
        let artifacts: Vec<_> = artifacts
            .into_iter()
            .map(|(source, destination)| (PathBuf::from(source), PathBuf::from(destination)))
            .collect();
        py.detach(|| {
            finalize_staged(
                &mut self.inner,
                &stage,
                &artifacts,
                run_outcome(status, full_run),
            )
        })
        .map(|currency| currency == vfy_core::RunCurrency::Obsolete)
        .map_err(|error| watch_error(&error))
    }

    fn stop(&mut self) {
        self.inner.stop();
    }

    fn set_ignored(&mut self, paths: Vec<String>) {
        let paths: Vec<PathBuf> = paths.into_iter().map(PathBuf::from).collect();
        self.inner.set_ignored(&paths);
    }

    fn set_resolved_ignored(&mut self, paths: Vec<String>) {
        let paths: Vec<_> = paths.into_iter().map(PathBuf::from).collect();
        self.inner.set_resolved_ignored(&paths);
    }

    fn set_roots(&mut self, roots: Vec<String>) -> PyResult<()> {
        let roots: Vec<PathBuf> = roots.into_iter().map(PathBuf::from).collect();
        self.inner.set_roots(&roots).map_err(|error| {
            PyRuntimeError::new_err(format!("failed to watch source root: {error}"))
        })
    }

    fn failure(&self) -> Option<String> {
        self.inner.failure().map(str::to_owned)
    }

    fn wait_for_batch(&mut self, py: Python<'_>, timeout_ms: u64) -> PyResult<Py<PyAny>> {
        let Some(batch) = py.detach(|| self.inner.wait_for_batch(timeout_ms)) else {
            return Ok(PyDict::new(py).into_any().unbind());
        };
        let result = PyDict::new(py);
        result.set_item("generation", batch.generation)?;
        result.set_item("paths", batch.paths)?;
        result.set_item("full_run", batch.full_run)?;
        result.set_item("reasons", batch.reasons)?;
        Ok(result.into_any().unbind())
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(source_tree_identity, module)?)?;
    module.add_class::<NativeWatchSession>()?;
    module.add_function(wrap_pyfunction!(stage_watch_outputs, module)?)
}

/// Adapt pytest's resolved destinations, including addopts, before its writers run.
#[pyfunction]
fn stage_watch_outputs(
    stage: String,
    outputs: Vec<(String, String)>,
) -> PyResult<Vec<(String, String)>> {
    let stage = PathBuf::from(stage)
        .canonicalize()
        .map_err(|error| watch_error(&error))?;
    let manifest = stage.join("resolved-outputs.json");
    let mut destinations: BTreeMap<String, String> = if manifest.is_file() {
        serde_json::from_slice(
            &std::fs::read(&manifest)
                .map_err(|error| staging_error("read watch output manifest", &manifest, error))?,
        )
        .map_err(|error| staging_error("parse watch output manifest", &manifest, error))?
    } else {
        BTreeMap::new()
    };
    let mut result = Vec::new();
    for (kind, output) in outputs {
        if output == "-" {
            result.push((kind, output));
            continue;
        }
        let destination = vfy_core::absolute_path(&PathBuf::from(&output));
        if destination.starts_with(&stage) {
            result.push((kind, output));
            continue;
        }
        let digest = digest(destination.as_os_str().as_encoded_bytes());
        let target_root = stage.join("resolved-reports").join(digest);
        let target = if kind == "pytest-html" {
            let filename = destination
                .file_name()
                .ok_or_else(|| PyRuntimeError::new_err("pytest HTML report requires a filename"))?;
            target_root.join(filename)
        } else {
            target_root.clone()
        };
        if kind == "markdown-append" && destination.is_file() && !target.exists() {
            let parent = target.parent().unwrap_or(&target_root);
            std::fs::create_dir_all(parent)
                .map_err(|error| staging_error("create watch staging directory", parent, error))?;
            std::fs::copy(&destination, &target).map_err(|error| {
                staging_error("copy Markdown report for appending", &destination, error)
            })?;
        }
        if kind == "pytest-html" {
            let parent = destination.parent().ok_or_else(|| {
                PyRuntimeError::new_err("pytest HTML report requires a parent directory")
            })?;
            destinations.insert(
                target_root.join("assets").to_string_lossy().into_owned(),
                parent.join("assets").to_string_lossy().into_owned(),
            );
        }
        destinations.insert(
            target.to_string_lossy().into_owned(),
            if kind == "annotate-default" {
                String::new()
            } else {
                destination.to_string_lossy().into_owned()
            },
        );
        result.push((kind, target.to_string_lossy().into_owned()));
    }
    let bytes = serde_json::to_vec(&destinations)
        .map_err(|error| staging_error("serialize watch output manifest", &manifest, error))?;
    vfy_core::atomic_write(&manifest, &bytes)
        .map_err(|error| staging_error("replace watch output manifest", &manifest, error))?;
    Ok(result)
}

fn digest(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        let _ = write!(output, "{byte:02x}");
    }
    output
}

fn finalize_staged(
    session: &mut vfy_core::WatchSession,
    stage: &Path,
    artifacts: &[(PathBuf, PathBuf)],
    outcome: vfy_core::RunOutcome,
) -> std::io::Result<vfy_core::RunCurrency> {
    if !session.is_current()? {
        return session.finish_run(outcome);
    }
    let data = stage.join("data.coverage");
    if data.is_file() {
        crate::data::CoverageData::stamp_watch_identity(&data, session.active_run_identity()?)
            .map_err(std::io::Error::other)?;
    }
    prepare_baseline(stage, artifacts)?;
    session.finalize(stage, artifacts, outcome)
}

fn prepare_baseline(stage: &Path, artifacts: &[(PathBuf, PathBuf)]) -> std::io::Result<()> {
    let baseline = stage.join("mutation-baseline.json");
    if !baseline.is_file() {
        return Ok(());
    }
    let mut value: serde_json::Value = serde_json::from_slice(&std::fs::read(&baseline)?)?;
    for name in ["data.coverage", "history.json"] {
        let source = stage.join(name);
        let Some((_, destination)) = artifacts.iter().find(|(path, _)| path == &source) else {
            continue;
        };
        let hash = digest(&std::fs::read(&source)?);
        if name == "data.coverage" {
            value["data_file"] = serde_json::json!(destination);
            value["data_hash"] = serde_json::json!(hash);
        }
        let records = value["suite_state"]
            .as_array_mut()
            .ok_or_else(|| std::io::Error::other("invalid mutation baseline suite state"))?;
        for record in records {
            if record.get(0).and_then(serde_json::Value::as_str) == source.to_str() {
                *record = serde_json::json!([destination, hash]);
            }
        }
    }
    vfy_core::atomic_write(&baseline, &serde_json::to_vec(&value)?)
}

#[cfg(test)]
mod tests;
