//! Native `Cache` service: pytest cache files, last-failed and stepwise state, and atomic writes.

use crate::results::{CacheReportKind, NativeRunResult};
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString};
use pyo3::{PyTraverseError, PyVisit};
use rustix::fs::{CWD, Mode, OFlags, RenameFlags, open, renameat_with};
use serde::Deserializer as _;
use serde::Serialize;
use serde::de::{Deserialize, Visitor};
use serde_json::{Deserializer, Value};
use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, hash_map::Entry};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use tempfile::Builder;
use vfy_core::selection::TestOutcome;
use vfy_core::test_execution::ExecutionPhase;

const CACHE_DIR_FILES: [(&str, &[u8]); 3] = [
    (
        "README.md",
        b"# pytest cache directory #\n\nThis directory contains data from the pytest's cache plugin,\nwhich provides the `--lf` and `--ff` options, as well as the `cache` fixture.\n\n**Do not** commit this to version control.\n\nSee [the docs](https://docs.pytest.org/en/stable/how-to/cache.html) for more information.\n",
    ),
    (".gitignore", b"# Created by pytest automatically.\n*\n"),
    (
        "CACHEDIR.TAG",
        b"Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by pytest.\n# For information about the cache directory tag, see:\n#\thttps://bford.info/cachedir/spec.html\n",
    ),
];

const MAX_CACHE_JSON_DEPTH: usize = 512;
const MAX_COLLECTION_SNAPSHOT_BYTES: usize = 128 * 1024 * 1024;
const COLLECTION_SNAPSHOT_VERSION: u8 = 1;
static NEXT_CACHE_MARKER_NONCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NonFiniteFloat {
    NaN,
    PositiveInfinity,
    NegativeInfinity,
}

struct CacheMarkers {
    nan: String,
    positive_infinity: String,
    negative_infinity: String,
}

#[derive(Default)]
struct CacheReadMarkers {
    numbers: Option<CacheMarkers>,
    strings: BTreeMap<String, Vec<u8>>,
}

impl CacheMarkers {
    fn new(nonce: u64) -> Self {
        Self {
            nan: format!("__vfy_cache_nan_{nonce}__"),
            positive_infinity: format!("__vfy_cache_positive_infinity_{nonce}__"),
            negative_infinity: format!("__vfy_cache_negative_infinity_{nonce}__"),
        }
    }

    fn value(&self, number: NonFiniteFloat) -> &str {
        match number {
            NonFiniteFloat::NaN => &self.nan,
            NonFiniteFloat::PositiveInfinity => &self.positive_infinity,
            NonFiniteFloat::NegativeInfinity => &self.negative_infinity,
        }
    }

    fn matches(&self, value: &str) -> Option<NonFiniteFloat> {
        if value == self.nan {
            Some(NonFiniteFloat::NaN)
        } else if value == self.positive_infinity {
            Some(NonFiniteFloat::PositiveInfinity)
        } else if value == self.negative_infinity {
            Some(NonFiniteFloat::NegativeInfinity)
        } else {
            None
        }
    }
}

#[pyclass(name = "NativeCacheService", module = "vfy._native._pytest")]
pub(crate) struct NativeCacheService {
    cachedir: PathBuf,
}

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct CacheSelectionState {
    nodeids: BTreeSet<String>,
    last_failed: BTreeMap<String, Value>,
    cached_nodeids: BTreeSet<String>,
    stepwise: StepwiseCacheState,
}

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct StepwiseCacheState {
    last_failed: Option<String>,
    mode: StepwiseSelection,
    test_count: usize,
}

#[derive(Clone, Copy, PartialEq, Eq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum StepwiseCacheFormat {
    Nodeid,
    Metadata,
}

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct CacheCollectionReport {
    nodeid: String,
    outcome: CacheOutcome,
    child_nodeids: Vec<String>,
}

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct LastFailedCollection {
    paths: BTreeSet<PathBuf>,
    last_failed: BTreeMap<String, Value>,
    found_failure: bool,
}

#[derive(Clone, Copy, PartialEq, Eq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum CacheOutcome {
    Passed,
    Skipped,
    Failed,
}

impl CacheOutcome {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "passed" => Some(Self::Passed),
            "skipped" => Some(Self::Skipped),
            "failed" => Some(Self::Failed),
            _ => None,
        }
    }
}

fn cache_phase_outcome(
    phase: &str,
    outcome: &str,
) -> Result<(ExecutionPhase, CacheOutcome), String> {
    let phase = match phase {
        "setup" => ExecutionPhase::Setup,
        "call" => ExecutionPhase::Call,
        "teardown" => ExecutionPhase::Teardown,
        _ => return Err(format!("unknown native cache phase {phase:?}")),
    };
    CacheOutcome::parse(outcome)
        .map(|outcome| (phase, outcome))
        .ok_or_else(|| format!("unknown native cache outcome {outcome:?}"))
}

#[derive(Clone, Copy)]
enum LastFailedUpdate {
    Failed,
    Cleared,
}

fn last_failed_update(phase: ExecutionPhase, outcome: CacheOutcome) -> Option<LastFailedUpdate> {
    match outcome {
        CacheOutcome::Failed => Some(LastFailedUpdate::Failed),
        CacheOutcome::Skipped => Some(LastFailedUpdate::Cleared),
        CacheOutcome::Passed if phase == ExecutionPhase::Call => Some(LastFailedUpdate::Cleared),
        CacheOutcome::Passed => None,
    }
}

fn update_last_failed(
    last_failed: &mut BTreeMap<String, Value>,
    nodeid: &str,
    update: LastFailedUpdate,
) {
    match update {
        LastFailedUpdate::Failed => {
            last_failed.insert(nodeid.to_owned(), Value::Bool(true));
        }
        LastFailedUpdate::Cleared => {
            last_failed.remove(nodeid);
        }
    }
}

#[pyclass(name = "NativeCacheSession", module = "vfy._native._pytest")]
pub(crate) struct NativeCacheSession {
    cache: NativeCacheService,
    state: Option<CacheSelectionState>,
    collection_reports: Vec<CacheCollectionReport>,
    stepwise_cache_format: StepwiseCacheFormat,
    pending_stepwise: Option<StepwiseCacheState>,
    last_failed_collection: Option<LastFailedCollection>,
    collection_snapshot_adopted: bool,
    // Borrow Python node-ID storage until validated completion, including long parameter IDs.
    xdist_last_failed_updates: BTreeMap<PyBackedStr, LastFailedUpdate>,
    xdist_invalid: Option<String>,
    xdist_conversion_failure: Option<Py<PyAny>>,
}

struct CacheSnapshotWriter(Vec<u8>);

impl Write for CacheSnapshotWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > MAX_COLLECTION_SNAPSHOT_BYTES.saturating_sub(self.0.len()) {
            return Err(io::Error::other(
                "native cache collection snapshot exceeds 128 MiB",
            ));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

// Borrow the collection inventories while exporting; serialization must not
// clone the full node ID sets retained by the worker.
#[derive(serde::Serialize)]
struct CacheCollectionSnapshotView<'a> {
    version: u8,
    cachedir: &'a Path,
    stepwise_cache_format: StepwiseCacheFormat,
    state: &'a Option<CacheSelectionState>,
    collection_reports: &'a [CacheCollectionReport],
    pending_stepwise: &'a Option<StepwiseCacheState>,
    last_failed_collection: &'a Option<LastFailedCollection>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct CacheCollectionSnapshot {
    version: u8,
    cachedir: PathBuf,
    stepwise_cache_format: StepwiseCacheFormat,
    state: Option<CacheSelectionState>,
    collection_reports: Vec<CacheCollectionReport>,
    pending_stepwise: Option<StepwiseCacheState>,
    last_failed_collection: Option<LastFailedCollection>,
}

#[pymethods]
impl NativeCacheService {
    #[new]
    fn new(cachedir: &str) -> Self {
        Self {
            cachedir: PathBuf::from(cachedir),
        }
    }

    fn get(&self, py: Python<'_>, key: &str, default: Py<PyAny>) -> PyResult<Py<PyAny>> {
        let relative = value_key(key)?;
        let Some(path) = existing_cache_path(&self.cachedir, "v", &relative)? else {
            return Ok(default);
        };
        let Ok(mut bytes) = read_regular_file_without_symlinks(&path) else {
            return Ok(default);
        };
        let Ok((value, markers)) = parse_cache_json(&mut bytes) else {
            return Ok(default);
        };
        match json_to_python(py, &value, &markers) {
            // Python's JSON decoder applies the interpreter's integer digit limit.
            // Pytest treats that ValueError like other invalid cached JSON.
            Err(error) if error.is_instance_of::<PyValueError>(py) => Ok(default),
            result => result,
        }
    }

    fn set(&self, key: &str, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let relative = value_key(key)?;
        let path = self
            .prepare_write_path("v", &relative)
            .map_err(|error| crate::CachePathError::new_err(error.to_string()))?;
        // Cache.set accepts arbitrary Python containers. Their callbacks and the
        // interpreter's JSON encoder determine mutation, subclass, and error
        // semantics; key snapshots cannot reproduce its dictionary traversal.
        // Rust still owns validation and atomic publication of the encoded value.
        let py = value.py();
        let options = PyDict::new(py);
        options.set_item("indent", 2)?;
        options.set_item("ensure_ascii", false)?;
        let encoded = py
            .import("json")?
            .call_method("dumps", (value,), Some(&options))?
            .extract::<String>()?;
        write_cache_file(&path, |file| file.write_all(encoded.as_bytes()))
            .map_err(|error| crate::CacheWriteError::new_err(error.to_string()))
    }

    fn mkdir(&self, name: &str) -> PyResult<String> {
        let relative = single_directory_name(name)?;
        let path = self.prepare_write_path("d", &relative).map_err(io_error)?;
        create_directory_without_symlinks(&self.cachedir, &path).map_err(io_error)?;
        Ok(path.to_string_lossy().into_owned())
    }

    fn initialize(&self) -> PyResult<()> {
        ensure_cache_directory(&self.cachedir).map_err(io_error)
    }
}

#[pymethods]
impl NativeCacheSession {
    #[new]
    #[pyo3(signature = (cachedir, *, stepwise_cache_format="metadata"))]
    fn new(cachedir: &str, stepwise_cache_format: &str) -> PyResult<Self> {
        let stepwise_cache_format = match stepwise_cache_format {
            "nodeid" => StepwiseCacheFormat::Nodeid,
            "metadata" => StepwiseCacheFormat::Metadata,
            value => {
                return Err(PyValueError::new_err(format!(
                    "unknown stepwise cache format {value:?}"
                )));
            }
        };
        Ok(Self {
            cache: NativeCacheService::new(cachedir),
            state: None,
            collection_reports: Vec::new(),
            stepwise_cache_format,
            pending_stepwise: None,
            last_failed_collection: None,
            collection_snapshot_adopted: false,
            xdist_last_failed_updates: BTreeMap::new(),
            xdist_invalid: None,
            xdist_conversion_failure: None,
        })
    }

    fn export_collection_snapshot(&self) -> PyResult<String> {
        let mut writer = CacheSnapshotWriter(Vec::new());
        serde_json::to_writer(
            &mut writer,
            &CacheCollectionSnapshotView {
                version: COLLECTION_SNAPSHOT_VERSION,
                cachedir: &self.cache.cachedir,
                stepwise_cache_format: self.stepwise_cache_format,
                state: &self.state,
                collection_reports: &self.collection_reports,
                pending_stepwise: &self.pending_stepwise,
                last_failed_collection: &self.last_failed_collection,
            },
        )
        .map_err(|error| {
            PyValueError::new_err(format!("cannot encode native cache snapshot: {error}"))
        })?;
        String::from_utf8(writer.0).map_err(|error| {
            PyValueError::new_err(format!("invalid native cache snapshot encoding: {error}"))
        })
    }

    fn adopt_collection_snapshot(&mut self, snapshot: &str) -> PyResult<()> {
        if self.collection_snapshot_adopted
            || self.state.is_some()
            || self.pending_stepwise.is_some()
        {
            return Err(PyRuntimeError::new_err(
                "native cache session already owns collection selection",
            ));
        }
        if snapshot.len() > MAX_COLLECTION_SNAPSHOT_BYTES
            || !json_within_depth(snapshot.as_bytes(), MAX_CACHE_JSON_DEPTH + 8)
        {
            return Err(PyValueError::new_err(
                "native cache collection snapshot exceeds its size or depth limit",
            ));
        }
        let mut decoder = Deserializer::from_str(snapshot);
        decoder.disable_recursion_limit();
        let incoming = CacheCollectionSnapshot::deserialize(&mut decoder).map_err(|error| {
            PyValueError::new_err(format!("invalid native cache collection snapshot: {error}"))
        })?;
        decoder.end().map_err(|error| {
            PyValueError::new_err(format!("invalid native cache collection snapshot: {error}"))
        })?;
        if incoming.version != COLLECTION_SNAPSHOT_VERSION
            || incoming.cachedir != self.cache.cachedir
            || incoming.stepwise_cache_format != self.stepwise_cache_format
            || (incoming.state.is_some() && incoming.pending_stepwise.is_some())
        {
            return Err(PyValueError::new_err(
                "native cache collection snapshot identity or selection state does not match the controller",
            ));
        }
        // Reports and last-failed state have already been applied by the worker.
        // Replay would apply recovery twice; adoption does not touch cache files.
        self.state = incoming.state;
        self.collection_reports = incoming.collection_reports;
        self.pending_stepwise = incoming.pending_stepwise;
        self.last_failed_collection = incoming.last_failed_collection;
        self.collection_snapshot_adopted = true;
        Ok(())
    }

    fn release_worker_collection(&mut self) -> PyResult<()> {
        if self.collection_snapshot_adopted {
            return Err(PyRuntimeError::new_err(
                "the controller owns adopted cache selection until session completion",
            ));
        }
        // The worker has published selection and never persists it itself.
        // Public Cache operations keep using the independent disk service.
        self.state = None;
        self.pending_stepwise = None;
        self.collection_reports = Vec::new();
        self.last_failed_collection = None;
        Ok(())
    }

    #[getter]
    fn has_selection(&self) -> bool {
        self.state.is_some()
    }

    fn configure_last_failed_collection(&mut self, rootpath: &str) {
        let last_failed = cache_object(self.cache.read_json_value("cache/lastfailed"));
        let rootpath = Path::new(rootpath);
        let mut paths = BTreeSet::new();
        for nodeid in last_failed.keys() {
            let path = rootpath.join(nodeid.split("::").next().unwrap_or(""));
            paths.extend(
                path.ancestors()
                    .filter(|path| path.exists())
                    .map(Path::to_path_buf),
            );
        }
        self.last_failed_collection = Some(LastFailedCollection {
            paths,
            last_failed,
            found_failure: false,
        });
    }

    fn skip_collection_file(&self, path: &str) -> bool {
        self.last_failed_collection
            .as_ref()
            .is_some_and(|state| state.found_failure && !state.paths.contains(Path::new(path)))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the Python collection records into an owned vector"
    )]
    fn collection_report_indices(
        &mut self,
        kind: &str,
        path: &str,
        children: Vec<(String, String, bool, bool)>,
    ) -> PyResult<(Vec<usize>, bool)> {
        let mut indices: Vec<usize> = (0..children.len()).collect();
        let Some(state) = self.last_failed_collection.as_mut() else {
            return Ok((indices, false));
        };
        let previously_found = state.found_failure;
        match kind {
            "directory" => {
                indices.sort_by_key(|index| !state.paths.contains(Path::new(&children[*index].1)));
            }
            "file" if state.paths.contains(Path::new(path)) => {
                if !state.found_failure {
                    if !children
                        .iter()
                        .any(|child| state.last_failed.contains_key(&child.0))
                    {
                        return Ok((indices, false));
                    }
                    state.found_failure = true;
                }
                indices.retain(|index| {
                    let (nodeid, _, collector, initial_path) = &children[*index];
                    state.last_failed.contains_key(nodeid) || *collector || *initial_path
                });
            }
            "file" | "other" => {}
            value => {
                return Err(PyValueError::new_err(format!(
                    "unknown collection kind {value:?}"
                )));
            }
        }
        Ok((indices, state.found_failure && !previously_found))
    }

    fn collect_report(
        &mut self,
        nodeid: String,
        outcome: &str,
        child_nodeids: Vec<String>,
    ) -> PyResult<()> {
        let outcome = CacheOutcome::parse(outcome).ok_or_else(|| {
            PyValueError::new_err(format!("unknown pytest collection outcome {outcome:?}"))
        })?;
        let report = CacheCollectionReport {
            nodeid,
            outcome,
            child_nodeids,
        };
        if let Some(state) = self.last_failed_collection.as_mut() {
            apply_collection_reports(&mut state.last_failed, std::slice::from_ref(&report));
        }
        if let Some(state) = self.state.as_mut() {
            apply_collection_reports(&mut state.last_failed, std::slice::from_ref(&report));
        }
        self.collection_reports.push(report);
        Ok(())
    }

    fn finish_collection(&mut self) -> PyResult<()> {
        let last_failed = if let Some(state) = self.state.as_ref() {
            state.last_failed.clone()
        } else {
            let mut last_failed = cache_object(self.cache.read_json_value("cache/lastfailed"));
            apply_collection_reports(&mut last_failed, &self.collection_reports);
            last_failed
        };
        let saved_last_failed = cache_object(self.cache.read_json_value("cache/lastfailed"));
        if last_failed != saved_last_failed {
            self.cache
                .write_json_value("cache/lastfailed", &last_failed)?;
        }
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the Python node ID list into an owned vector"
    )]
    fn select_stepwise(
        &mut self,
        nodeids: Vec<String>,
        flags: Vec<String>,
    ) -> PyResult<Vec<usize>> {
        if self.pending_stepwise.is_some() || self.state.is_some() {
            return Err(PyRuntimeError::new_err(
                "native cache session already selected stepwise items",
            ));
        }
        let flags = parse_selection_flags(flags)?;
        let (state, selected) = self.stepwise_selection(&nodeids, flags.stepwise_mode);
        self.pending_stepwise = Some(state);
        Ok(selected)
    }

    fn select_items(
        &mut self,
        items: &Bound<'_, PyAny>,
        file_mtimes: Vec<i64>,
        flags: Vec<String>,
    ) -> PyResult<Vec<usize>> {
        let mut nodeids = Vec::new();
        let mut families = BTreeMap::new();
        for item in items.try_iter()? {
            let item = item?;
            let nodeid: String = item.getattr("nodeid")?.extract()?;
            if item.hasattr("_native_callspecs")? {
                let mut case_nodeids = Vec::new();
                for callspec in item.getattr("_native_callspecs")?.try_iter()? {
                    let callspec = callspec?;
                    case_nodeids.push(if callspec.getattr("_idlist")?.len()? == 0 {
                        nodeid.clone()
                    } else {
                        format!("{nodeid}[{}]", callspec.getattr("id")?.extract::<String>()?)
                    });
                }
                families.insert(nodeid.clone(), case_nodeids);
            }
            nodeids.push(nodeid);
        }
        let selected = self.select(nodeids, file_mtimes, flags)?;
        if !families.is_empty() {
            let state = self
                .state
                .as_mut()
                .ok_or_else(|| PyRuntimeError::new_err("native cache session has no selection"))?;
            let mut expanded = BTreeSet::new();
            for nodeid in std::mem::take(&mut state.nodeids) {
                if let Some(case_nodeids) = families.remove(&nodeid) {
                    expanded.extend(case_nodeids);
                } else {
                    expanded.insert(nodeid);
                }
            }
            state.nodeids = expanded;
        }
        if let Some(state) = self.state.as_mut() {
            // Expansion must precede the union: a cached bare family ID remains
            // historical evidence even when its selected item expands into cases.
            state.cached_nodeids.append(&mut state.nodeids);
        }
        Ok(selected)
    }

    fn finish(
        &mut self,
        outcomes: Vec<(String, String, String)>,
        cache_timestamp: &str,
    ) -> PyResult<()> {
        let outcomes: Vec<(String, ExecutionPhase, CacheOutcome)> = outcomes
            .into_iter()
            .map(|(nodeid, phase, outcome)| {
                cache_phase_outcome(&phase, &outcome)
                    .map(|(phase, outcome)| (nodeid, phase, outcome))
                    .map_err(PyValueError::new_err)
            })
            .collect::<PyResult<_>>()?;
        self.finish_outcomes(
            outcomes.iter().map(|(nodeid, phase, outcome)| {
                (nodeid.as_str(), *phase, *outcome, CacheReportKind::Phase)
            }),
            cache_timestamp,
        )
    }

    #[pyo3(name = "_append_xdist_report")]
    fn append_xdist_report(
        &mut self,
        nodeid: &Bound<'_, PyAny>,
        phase: &Bound<'_, PyAny>,
        outcome: &Bound<'_, PyAny>,
    ) {
        if self.xdist_conversion_failure.is_some() {
            return;
        }
        let mut converting = nodeid;
        let converted = (|| {
            let nodeid = nodeid.cast::<PyString>()?.to_str()?;
            converting = phase;
            let phase = phase.cast::<PyString>()?.to_str()?;
            converting = outcome;
            let outcome = outcome.cast::<PyString>()?.to_str()?;
            Ok::<_, PyErr>((nodeid, phase, outcome))
        })();
        let Ok((nodeid_text, phase, outcome)) = converted else {
            self.xdist_conversion_failure = Some(converting.clone().unbind());
            return;
        };
        // Batch extraction precedes validation, even after an earlier invalid phase.
        if self.xdist_invalid.is_some() {
            return;
        }
        match cache_phase_outcome(phase, outcome) {
            Ok((phase, outcome)) => {
                if let Some(update) = last_failed_update(phase, outcome) {
                    if let Some(pending) = self.xdist_last_failed_updates.get_mut(nodeid_text) {
                        *pending = update;
                    } else {
                        let Ok(key) = nodeid.extract::<PyBackedStr>() else {
                            self.xdist_conversion_failure = Some(nodeid.clone().unbind());
                            return;
                        };
                        self.xdist_last_failed_updates.insert(key, update);
                    }
                }
            }
            Err(error) => self.xdist_invalid = Some(error),
        }
    }

    #[pyo3(name = "_finish_xdist_reports")]
    fn finish_xdist_reports(&mut self, py: Python<'_>, cache_timestamp: &str) -> PyResult<()> {
        if let Some(value) = &self.xdist_conversion_failure {
            // TypeError depends on the field's live class at batch publication.
            value.bind(py).cast::<PyString>()?.to_str()?;
        }
        if let Some(error) = &self.xdist_invalid {
            return Err(PyValueError::new_err(error.clone()));
        }
        let state = self
            .state
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("native cache session has no selection"))?;
        if state.stepwise.mode.enabled() {
            return Err(PyRuntimeError::new_err(
                "xdist cache reports do not support stepwise",
            ));
        }
        for (nodeid, update) in std::mem::take(&mut self.xdist_last_failed_updates) {
            // The reporter retains subclass owners beyond this Rust borrow.
            update_last_failed(&mut state.last_failed, &nodeid, update);
        }
        self.finish_outcomes(std::iter::empty(), cache_timestamp)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 requires this GC slot signature"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for key in self.xdist_last_failed_updates.keys() {
            visit.call(key.as_py_str())?;
        }
        if let Some(value) = &self.xdist_conversion_failure {
            visit.call(value)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.xdist_last_failed_updates.clear();
        self.xdist_conversion_failure = None;
    }

    fn finish_result(&mut self, result: &NativeRunResult, cache_timestamp: &str) -> PyResult<()> {
        self.finish_outcomes(
            result
                .cache_phase_outcomes()
                .map(|(nodeid, phase, outcome, kind)| {
                    let outcome = match outcome.report_outcome() {
                        TestOutcome::Passed => CacheOutcome::Passed,
                        TestOutcome::Failed => CacheOutcome::Failed,
                        TestOutcome::Skipped => CacheOutcome::Skipped,
                    };
                    (nodeid, phase, outcome, kind)
                }),
            cache_timestamp,
        )
    }
}

impl NativeCacheSession {
    fn finish_outcomes<'a>(
        &mut self,
        outcomes: impl Iterator<Item = (&'a str, ExecutionPhase, CacheOutcome, CacheReportKind)> + Clone,
        cache_timestamp: &str,
    ) -> PyResult<()> {
        let mut state = self
            .state
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("native cache session has no selection"))?;
        let mut updated_last_failed = state.last_failed;
        for (nodeid, phase, outcome, _) in outcomes.clone() {
            if let Some(update) = last_failed_update(phase, outcome) {
                update_last_failed(&mut updated_last_failed, nodeid, update);
            }
        }
        let saved_last_failed = cache_object(self.cache.read_json_value("cache/lastfailed"));
        if updated_last_failed != saved_last_failed {
            self.cache
                .write_json_value("cache/lastfailed", &updated_last_failed)?;
        }

        state.cached_nodeids.append(&mut state.nodeids);
        let write_nodeids = match self.cache.read_json_value("cache/nodeids") {
            Some(Value::Array(values)) => !values.iter().map(Value::as_str).eq(state
                .cached_nodeids
                .iter()
                .map(|nodeid| Some(nodeid.as_str()))),
            _ => true,
        };
        if write_nodeids {
            self.cache
                .write_json_value("cache/nodeids", &state.cached_nodeids)?;
        }

        if state.stepwise.mode.enabled() {
            let mut skip_first_failure = state.stepwise.mode.skip_first();
            let mut stop_after_parent_call = false;
            for (nodeid, phase, outcome, kind) in outcomes {
                if outcome == CacheOutcome::Failed {
                    if skip_first_failure {
                        if state.stepwise.last_failed.as_deref() == Some(nodeid) {
                            state.stepwise.last_failed = None;
                        }
                        skip_first_failure = false;
                        continue;
                    }
                    state.stepwise.last_failed = Some(nodeid.to_owned());
                    if kind == CacheReportKind::Phase {
                        break;
                    }
                    // Pytest still reports the parent call after an interrupting
                    // subtest failure; a passed parent clears the resume point.
                    stop_after_parent_call = true;
                } else if phase == ExecutionPhase::Call
                    && state.stepwise.last_failed.as_deref() == Some(nodeid)
                {
                    state.stepwise.last_failed = None;
                }
                if stop_after_parent_call
                    && kind == CacheReportKind::Phase
                    && phase == ExecutionPhase::Call
                {
                    break;
                }
            }
            let stepwise = match self.stepwise_cache_format {
                StepwiseCacheFormat::Nodeid => serde_json::json!(state.stepwise.last_failed),
                StepwiseCacheFormat::Metadata => serde_json::json!({
                    "last_failed": state.stepwise.last_failed,
                    "last_test_count": state.stepwise.test_count,
                    "last_cache_date_str": cache_timestamp,
                }),
            };
            if self.cache.read_json_value("cache/stepwise").as_ref() != Some(&stepwise) {
                self.cache.write_json_value("cache/stepwise", &stepwise)?;
            }
        }
        Ok(())
    }
    fn select(
        &mut self,
        nodeids: Vec<String>,
        file_mtimes: Vec<i64>,
        flags: Vec<String>,
    ) -> PyResult<Vec<usize>> {
        if self.state.is_some() {
            return Err(PyRuntimeError::new_err(
                "native cache session already selected items",
            ));
        }
        let flags = parse_selection_flags(flags)?;
        if flags.new_first && file_mtimes.len() != nodeids.len() {
            return Err(PyValueError::new_err(
                "new-first selection requires one file modification time per item",
            ));
        }
        if !flags.new_first && !file_mtimes.is_empty() {
            return Err(PyValueError::new_err(
                "file modification times are only accepted for new-first selection",
            ));
        }
        let file_mtimes = file_mtimes.into_boxed_slice();

        let mut last_failed = cache_object(self.cache.read_json_value("cache/lastfailed"));
        apply_collection_reports(&mut last_failed, &self.collection_reports);
        let cached_nodeids = cache_string_set(self.cache.read_json_value("cache/nodeids"));
        let (stepwise, mut selected) = if let Some(state) = self.pending_stepwise.take() {
            (state, (0..nodeids.len()).collect())
        } else {
            self.stepwise_selection(&nodeids, flags.stepwise_mode)
        };

        if (flags.last_failed || flags.failed_first) && last_failed.is_empty() {
            if flags.last_failed_no_failures == LastFailedNoFailures::None {
                selected.clear();
            }
        } else if flags.last_failed {
            let failed: Vec<usize> = selected
                .iter()
                .copied()
                .filter(|index| last_failed.contains_key(&nodeids[*index]))
                .collect();
            if !failed.is_empty() {
                selected = failed;
            }
        } else if flags.failed_first {
            let (mut failed, mut passed): (Vec<_>, Vec<_>) = selected
                .into_iter()
                .partition(|index| last_failed.contains_key(&nodeids[*index]));
            if failed.is_empty() {
                selected = passed;
            } else {
                failed.append(&mut passed);
                selected = failed;
            }
        }

        if flags.new_first {
            // pytest's new-first dictionaries keep each node ID's last item
            // at the position established by its first occurrence.
            let mut unique = Vec::with_capacity(selected.len());
            {
                let mut positions = HashMap::with_capacity(selected.len());
                for index in selected {
                    match positions.entry(nodeids[index].as_str()) {
                        Entry::Occupied(position) => unique[*position.get()] = index,
                        Entry::Vacant(position) => {
                            position.insert(unique.len());
                            unique.push(index);
                        }
                    }
                }
            }
            let (mut new, mut old): (Vec<_>, Vec<_>) = unique
                .into_iter()
                .partition(|index| !cached_nodeids.contains(&nodeids[*index]));
            new.sort_by_key(|index| Reverse(file_mtimes[*index]));
            old.sort_by_key(|index| Reverse(file_mtimes[*index]));
            new.append(&mut old);
            selected = new;
        }

        let cached_nodeids_input = if selected.len() == nodeids.len() {
            nodeids
        } else {
            selected
                .iter()
                .map(|index| nodeids[*index].clone())
                .collect()
        };
        self.state = Some(CacheSelectionState {
            nodeids: cached_nodeids_input.into_iter().collect(),
            last_failed,
            cached_nodeids,
            stepwise,
        });
        Ok(selected)
    }

    fn stepwise_selection(
        &self,
        nodeids: &[String],
        mode: StepwiseSelection,
    ) -> (StepwiseCacheState, Vec<usize>) {
        let (mut last_failed, previous_count) = stepwise_state(
            self.cache.read_json_value("cache/stepwise"),
            self.stepwise_cache_format,
        );
        if mode.enabled()
            && (mode.resets() || previous_count.is_some_and(|count| count != nodeids.len()))
        {
            last_failed = None;
        }
        let mut selected: Vec<usize> = (0..nodeids.len()).collect();
        if mode.enabled()
            && let Some(failed_nodeid) = last_failed.as_deref()
            && let Some(failed_index) = nodeids.iter().position(|nodeid| nodeid == failed_nodeid)
        {
            selected.retain(|index| *index >= failed_index);
        }
        (
            StepwiseCacheState {
                last_failed,
                mode,
                test_count: nodeids.len(),
            },
            selected,
        )
    }
}

#[derive(Clone, Copy, PartialEq, Eq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum StepwiseSelection {
    Disabled,
    Continue,
    Reset,
    SkipFirst,
    ResetSkipFirst,
}

impl StepwiseSelection {
    fn enabled(self) -> bool {
        self != Self::Disabled
    }

    fn resets(self) -> bool {
        matches!(self, Self::Reset | Self::ResetSkipFirst)
    }

    fn skip_first(self) -> bool {
        matches!(self, Self::SkipFirst | Self::ResetSkipFirst)
    }

    fn with_reset(self) -> Self {
        if self.skip_first() {
            Self::ResetSkipFirst
        } else {
            Self::Reset
        }
    }

    fn with_skip_first(self) -> Self {
        if self.resets() {
            Self::ResetSkipFirst
        } else {
            Self::SkipFirst
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum LastFailedNoFailures {
    All,
    None,
}

struct SelectionFlags {
    last_failed: bool,
    failed_first: bool,
    new_first: bool,
    stepwise_mode: StepwiseSelection,
    last_failed_no_failures: LastFailedNoFailures,
}

fn parse_selection_flags(values: Vec<String>) -> PyResult<SelectionFlags> {
    let mut flags = SelectionFlags {
        last_failed: false,
        failed_first: false,
        new_first: false,
        stepwise_mode: StepwiseSelection::Disabled,
        last_failed_no_failures: LastFailedNoFailures::All,
    };
    for value in values {
        match value.as_str() {
            "last-failed" => flags.last_failed = true,
            "failed-first" => flags.failed_first = true,
            "new-first" => flags.new_first = true,
            "stepwise" => {
                if flags.stepwise_mode == StepwiseSelection::Disabled {
                    flags.stepwise_mode = StepwiseSelection::Continue;
                }
            }
            "stepwise-reset" => flags.stepwise_mode = flags.stepwise_mode.with_reset(),
            "stepwise-skip" => flags.stepwise_mode = flags.stepwise_mode.with_skip_first(),
            "last-failed-no-failures-none" => {
                flags.last_failed_no_failures = LastFailedNoFailures::None;
            }
            _ => {
                return Err(PyValueError::new_err(format!(
                    "unknown native cache selector {value:?}"
                )));
            }
        }
    }
    Ok(flags)
}

fn apply_collection_reports(
    last_failed: &mut BTreeMap<String, Value>,
    reports: &[CacheCollectionReport],
) {
    for report in reports {
        match report.outcome {
            CacheOutcome::Passed | CacheOutcome::Skipped => {
                if last_failed.remove(&report.nodeid).is_some() {
                    last_failed.extend(
                        report
                            .child_nodeids
                            .iter()
                            .cloned()
                            .map(|nodeid| (nodeid, Value::Bool(true))),
                    );
                }
            }
            CacheOutcome::Failed => {
                last_failed.insert(report.nodeid.clone(), Value::Bool(true));
            }
        }
    }
}

fn cache_object(value: Option<Value>) -> BTreeMap<String, Value> {
    match value {
        Some(Value::Object(object)) => object.into_iter().collect(),
        _ => BTreeMap::new(),
    }
}

fn cache_string_set(value: Option<Value>) -> BTreeSet<String> {
    match value {
        Some(Value::Array(values)) => values
            .into_iter()
            .filter_map(|value| match value {
                Value::String(value) => Some(value),
                _ => None,
            })
            .collect(),
        _ => BTreeSet::new(),
    }
}

fn stepwise_state(
    value: Option<Value>,
    format: StepwiseCacheFormat,
) -> (Option<String>, Option<usize>) {
    if matches!(format, StepwiseCacheFormat::Nodeid) {
        return (
            value.and_then(|value| value.as_str().map(str::to_owned)),
            None,
        );
    }
    let Some(Value::Object(value)) = value else {
        return (None, None);
    };
    let last_failed = value
        .get("last_failed")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let test_count = value
        .get("last_test_count")
        .and_then(Value::as_u64)
        .and_then(|count| usize::try_from(count).ok());
    let date = value.get("last_cache_date_str").and_then(Value::as_str);
    if date.is_none() {
        return (None, None);
    }
    (last_failed, test_count)
}

impl NativeCacheService {
    fn read_json_value(&self, key: &str) -> Option<Value> {
        let relative = value_key(key).ok()?;
        let path = existing_cache_path(&self.cachedir, "v", &relative).ok()??;
        let mut bytes = read_regular_file_without_symlinks(&path).ok()?;
        let (value, markers) = parse_cache_json(&mut bytes).ok()?;
        // Native selector identities are UTF-8 strings; surrogate-containing
        // public cache values must never leak their temporary marker identities.
        markers.strings.is_empty().then_some(value)
    }

    fn write_json_value<T: Serialize>(&self, key: &str, value: &T) -> PyResult<()> {
        let relative = value_key(key)?;
        let path = self
            .prepare_write_path("v", &relative)
            .map_err(|error| crate::CachePathError::new_err(error.to_string()))?;
        write_cache_file(&path, |file| {
            serde_json::to_writer_pretty(file, value).map_err(io::Error::from)
        })
        .map_err(|error| crate::CacheWriteError::new_err(error.to_string()))
    }

    fn prepare_write_path(&self, prefix: &str, key: &Path) -> io::Result<PathBuf> {
        ensure_cache_directory(&self.cachedir)?;
        let relative = Path::new(prefix).join(key);
        let parent = relative
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing cache parent"))?;
        let parent = create_safe_parent(&self.cachedir, parent)?;
        let name = relative
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing cache name"))?;
        Ok(parent.join(name))
    }
}

fn value_key(key: &str) -> PyResult<PathBuf> {
    let path = Path::new(key);
    if key.is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(PyValueError::new_err(
            "cache key must be a non-empty relative slash-separated path",
        ));
    }
    Ok(path.to_path_buf())
}

fn single_directory_name(name: &str) -> PyResult<PathBuf> {
    let path = Path::new(name);
    if name.is_empty()
        || path.is_absolute()
        || !matches!(
            path.components().collect::<Vec<_>>().as_slice(),
            [Component::Normal(_)]
        )
    {
        return Err(PyValueError::new_err(
            "name is not allowed to contain path separators",
        ));
    }
    Ok(path.to_path_buf())
}

fn existing_cache_path(root: &Path, prefix: &str, key: &Path) -> PyResult<Option<PathBuf>> {
    let Ok(canonical_root) = root.canonicalize() else {
        return Ok(None);
    };
    let relative = Path::new(prefix).join(key);
    let parent = relative
        .parent()
        .ok_or_else(|| PyValueError::new_err("cache key has no parent directory"))?;
    let mut current = canonical_root.clone();
    for component in parent.components() {
        let Component::Normal(part) = component else {
            return Err(PyValueError::new_err(
                "cache key escapes the cache directory",
            ));
        };
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return Ok(None);
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Ok(None),
        }
    }
    if !current
        .canonicalize()
        .is_ok_and(|path| path.starts_with(&canonical_root))
    {
        return Ok(None);
    }
    let Some(name) = relative.file_name() else {
        return Err(PyValueError::new_err("cache key has no file name"));
    };
    let path = current.join(name);
    match fs::symlink_metadata(&path) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => Ok(None),
        Ok(_) => Ok(Some(path)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Ok(None),
    }
}

fn read_regular_file_without_symlinks(path: &Path) -> io::Result<Vec<u8>> {
    // NOFOLLOW closes the race between the metadata check and opening the value.
    let descriptor = open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )?;
    let mut file = File::from(descriptor);
    if !file.metadata()?.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "cache value is not a regular file",
        ));
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

struct SpecialNumberToken {
    range: std::ops::Range<usize>,
    number: NonFiniteFloat,
}

fn next_cache_marker_nonce() -> Option<u64> {
    NEXT_CACHE_MARKER_NONCE
        .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            value.checked_add(1)
        })
        .ok()
}

fn json_string_token(value: &str) -> Vec<u8> {
    let mut token = Vec::with_capacity(value.len() + 2);
    token.push(b'"');
    token.extend_from_slice(value.as_bytes());
    token.push(b'"');
    token
}

fn parse_cache_json(bytes: &mut Vec<u8>) -> Result<(Value, CacheReadMarkers), ()> {
    if !cache_json_within_depth(bytes) {
        return Err(());
    }
    let strings = replace_surrogate_strings(bytes)?;
    let special_numbers = find_special_numbers(bytes);
    let numbers = if special_numbers.is_empty() {
        None
    } else {
        Some(replace_special_numbers(bytes, special_numbers)?)
    };
    let mut deserializer = Deserializer::from_slice(bytes);
    deserializer.disable_recursion_limit();
    let value = Value::deserialize(&mut deserializer).map_err(|_| ())?;
    deserializer.end().map_err(|_| ())?;
    Ok((value, CacheReadMarkers { numbers, strings }))
}

fn replace_surrogate_strings(bytes: &mut Vec<u8>) -> Result<BTreeMap<String, Vec<u8>>, ()> {
    if !bytes.windows(4).any(|window| {
        matches!(
            window,
            [
                b'\\',
                b'u',
                b'd' | b'D',
                b'8'..=b'9' | b'a'..=b'f' | b'A'..=b'F',
            ]
        )
    }) {
        return Ok(BTreeMap::new());
    }
    std::str::from_utf8(bytes).map_err(|_| ())?;
    let mut strings = HashSet::new();
    let mut replacements = Vec::new();
    for range in json_string_ranges(bytes) {
        let token = &bytes[range.clone()];
        // serde_json's byte-string decoder preserves escaped lone surrogates
        // as WTF-8, but intentionally permits literal control bytes.
        if token.iter().any(|byte| *byte < 0x20) {
            return Err(());
        }
        let decoded = decode_json_string_bytes(token)?;
        if std::str::from_utf8(&decoded).is_err() {
            replacements.push((range, decoded.clone()));
        }
        strings.insert(decoded);
    }
    if replacements.is_empty() {
        return Ok(BTreeMap::new());
    }
    let names = loop {
        let nonce = next_cache_marker_nonce().ok_or(())?;
        let names: Vec<String> = (0..replacements.len())
            .map(|index| format!("__vfy_cache_string_{nonce}_{index}__"))
            .collect();
        if names.iter().all(|name| !strings.contains(name.as_bytes())) {
            break names;
        }
    };
    let mut rewritten = Vec::with_capacity(bytes.len());
    let mut previous_end = 0;
    let mut markers = BTreeMap::new();
    let mut names_by_value = BTreeMap::new();
    for ((range, decoded), name) in replacements.into_iter().zip(names) {
        let name = names_by_value.entry(decoded.clone()).or_insert(name);
        rewritten.extend_from_slice(&bytes[previous_end..range.start]);
        rewritten.extend_from_slice(&json_string_token(name));
        previous_end = range.end;
        markers.insert(name.clone(), decoded);
    }
    rewritten.extend_from_slice(&bytes[previous_end..]);
    *bytes = rewritten;
    Ok(markers)
}

fn decode_json_string_bytes(token: &[u8]) -> Result<Vec<u8>, ()> {
    struct JsonStringBytes;
    impl Visitor<'_> for JsonStringBytes {
        type Value = Vec<u8>;

        fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            formatter.write_str("a JSON string")
        }

        fn visit_bytes<E>(self, value: &[u8]) -> Result<Self::Value, E> {
            Ok(value.to_vec())
        }
    }

    let mut deserializer = Deserializer::from_slice(token);
    let value = deserializer
        .deserialize_bytes(JsonStringBytes)
        .map_err(|_| ())?;
    deserializer.end().map_err(|_| ())?;
    Ok(value)
}

fn cache_json_within_depth(bytes: &[u8]) -> bool {
    json_within_depth(bytes, MAX_CACHE_JSON_DEPTH)
}

fn json_within_depth(bytes: &[u8], maximum: usize) -> bool {
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    for byte in bytes {
        if in_string {
            match (*byte, escaped) {
                (b'\\', false) => escaped = true,
                (b'"', false) => in_string = false,
                (_, _) => escaped = false,
            }
            continue;
        }
        match *byte {
            b'"' => in_string = true,
            b'[' | b'{' => {
                depth = depth.saturating_add(1);
                if depth > maximum {
                    return false;
                }
            }
            b']' | b'}' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    true
}

fn find_special_numbers(bytes: &[u8]) -> Vec<SpecialNumberToken> {
    let special_numbers = [
        (b"-Infinity".as_slice(), NonFiniteFloat::NegativeInfinity),
        (b"Infinity".as_slice(), NonFiniteFloat::PositiveInfinity),
        (b"NaN".as_slice(), NonFiniteFloat::NaN),
    ];
    let mut tokens = Vec::new();
    let mut containers = Vec::new();
    let mut in_string = false;
    let mut escaped = false;
    let mut last_significant = None;
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if in_string {
            match (byte, escaped) {
                (b'\\', false) => escaped = true,
                (b'"', false) => {
                    in_string = false;
                    last_significant = Some(b'"');
                }
                (_, _) => escaped = false,
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            in_string = true;
            index += 1;
            continue;
        }

        let in_value_position = match last_significant {
            None => containers.is_empty(),
            Some(b':') => containers.last() == Some(&false),
            Some(b'[' | b',') => containers.last() == Some(&true),
            _ => false,
        };
        let found = special_numbers.iter().find_map(|(token, number)| {
            let end = index.checked_add(token.len())?;
            let preceding = index
                .checked_sub(1)
                .and_then(|previous| bytes.get(previous))
                .copied();
            let following = bytes.get(end).copied();
            (in_value_position
                && bytes.get(index..end) == Some(*token)
                && preceding.is_none_or(is_json_value_boundary)
                && following.is_none_or(is_json_value_end))
            .then_some((end, *number))
        });
        if let Some((end, number)) = found {
            tokens.push(SpecialNumberToken {
                range: index..end,
                number,
            });
            last_significant = bytes.get(end - 1).copied();
            index = end;
        } else {
            match byte {
                b'[' => containers.push(true),
                b'{' => containers.push(false),
                b']' | b'}' => {
                    containers.pop();
                }
                _ => {}
            }
            if !byte.is_ascii_whitespace() {
                last_significant = Some(byte);
            }
            index += 1;
        }
    }
    tokens
}

fn is_json_value_boundary(byte: u8) -> bool {
    byte.is_ascii_whitespace() || matches!(byte, b'[' | b'{' | b',' | b':')
}

fn is_json_value_end(byte: u8) -> bool {
    byte.is_ascii_whitespace() || matches!(byte, b',' | b']' | b'}')
}

fn replace_special_numbers(
    bytes: &mut Vec<u8>,
    tokens: Vec<SpecialNumberToken>,
) -> Result<CacheMarkers, ()> {
    let markers = loop {
        let nonce = next_cache_marker_nonce().ok_or(())?;
        let markers = CacheMarkers::new(nonce);
        if !json_strings_contain_markers(bytes, &markers) {
            break markers;
        }
    };
    let mut replaced =
        Vec::with_capacity(bytes.len().saturating_add(tokens.len().saturating_mul(16)));
    let mut previous_end = 0;
    for token in tokens {
        replaced.extend_from_slice(&bytes[previous_end..token.range.start]);
        replaced.extend_from_slice(&json_string_token(markers.value(token.number)));
        previous_end = token.range.end;
    }
    replaced.extend_from_slice(&bytes[previous_end..]);
    *bytes = replaced;
    Ok(markers)
}

fn json_strings_contain_markers(bytes: &[u8], markers: &CacheMarkers) -> bool {
    json_string_ranges(bytes).any(|range| {
        serde_json::from_slice::<String>(&bytes[range])
            .is_ok_and(|value| markers.matches(&value).is_some())
    })
}

fn json_string_ranges(bytes: &[u8]) -> impl Iterator<Item = std::ops::Range<usize>> + '_ {
    let mut index = 0;
    std::iter::from_fn(move || {
        while index < bytes.len() {
            if bytes[index] != b'"' {
                index += 1;
                continue;
            }
            let start = index;
            index += 1;
            let mut escaped = false;
            while index < bytes.len() {
                match (bytes[index], escaped) {
                    (b'\\', false) => escaped = true,
                    (b'"', false) => {
                        index += 1;
                        return Some(start..index);
                    }
                    (_, _) => escaped = false,
                }
                index += 1;
            }
        }
        None
    })
}

fn ensure_cache_directory(target: &Path) -> io::Result<()> {
    if target.is_dir() {
        return Ok(());
    }
    if target.exists() {
        return Err(io::Error::new(
            io::ErrorKind::NotADirectory,
            format!("cache path is not a directory: {}", target.display()),
        ));
    }
    let parent = target
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let temporary = Builder::new()
        .prefix("pytest-cache-files-")
        .permissions(fs::Permissions::from_mode(0o777))
        .tempdir_in(parent)?;
    for (name, contents) in CACHE_DIR_FILES {
        let path = temporary.path().join(name);
        #[expect(
            clippy::disallowed_methods,
            reason = "the file is inside a private directory that is renamed into place"
        )]
        let mut file = File::create(path)?;
        file.write_all(contents)?;
        file.sync_all()?;
    }
    match renameat_with(CWD, temporary.path(), CWD, target, RenameFlags::NOREPLACE) {
        Ok(()) => Ok(()),
        Err(_error) if target.is_dir() => Ok(()),
        Err(error) => Err(io::Error::from_raw_os_error(error.raw_os_error())),
    }
}

fn write_cache_file(
    path: &Path,
    write: impl FnOnce(&mut File) -> io::Result<()>,
) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing cache value parent"))?;
    let existing = match fs::metadata(path) {
        Ok(metadata) => Some(metadata.permissions()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    // Let the OS apply the umask at creation, as it does for pytest's open("w").
    // Keep replacements private while writing, then restore the original mode.
    let mut builder = Builder::new();
    if existing.is_none() {
        builder.permissions(fs::Permissions::from_mode(0o666));
    }
    let mut temporary = builder.tempfile_in(parent)?;
    write(temporary.as_file_mut())?;
    if let Some(permissions) = existing {
        temporary.as_file().set_permissions(permissions)?;
    }
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}

fn create_safe_parent(root: &Path, relative: &Path) -> io::Result<PathBuf> {
    let canonical_root = root.canonicalize()?;
    let mut current = canonical_root.clone();
    for component in relative.components() {
        let Component::Normal(part) = component else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "cache path escapes the cache directory",
            ));
        };
        current.push(part);
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("cache path is not a directory: {}", current.display()),
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                match fs::create_dir(&current) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
                    Err(error) => return Err(error),
                }
                let metadata = fs::symlink_metadata(&current)?;
                if metadata.file_type().is_symlink() || !metadata.is_dir() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("cache path is not a directory: {}", current.display()),
                    ));
                }
            }
            Err(error) => return Err(error),
        }
    }
    let canonical = current.canonicalize()?;
    if !canonical.starts_with(canonical_root) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "cache path escapes the cache directory",
        ));
    }
    Ok(canonical)
}

fn create_directory_without_symlinks(root: &Path, path: &Path) -> io::Result<()> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("cache path is not a directory: {}", path.display()),
            ));
        }
        return Ok(());
    }
    fs::create_dir(path)?;
    let canonical_root = root.canonicalize()?;
    let canonical_path = path.canonicalize()?;
    if !canonical_path.starts_with(canonical_root) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "cache path escapes the cache directory",
        ));
    }
    Ok(())
}

#[expect(
    clippy::needless_pass_by_value,
    reason = "map_err transfers ownership of I/O errors into the Python exception adapter"
)]
fn io_error(error: io::Error) -> PyErr {
    PyOSError::new_err(error.to_string())
}

fn json_to_python(
    py: Python<'_>,
    value: &Value,
    markers: &CacheReadMarkers,
) -> PyResult<Py<PyAny>> {
    match value {
        Value::Null => Ok(py.None()),
        Value::Bool(value) => Ok(PyBool::new(py, *value).to_owned().unbind().into_any()),
        Value::Number(value) => {
            if let Some(integer) = value.as_i64() {
                return Ok(PyInt::new(py, integer).unbind().into_any());
            }
            if let Some(integer) = value.as_u64() {
                return Ok(PyInt::new(py, integer).unbind().into_any());
            }
            let text = value.to_string();
            if !text
                .chars()
                .any(|character| matches!(character, '.' | 'e' | 'E'))
            {
                let integer = py.get_type::<PyInt>().call1((text,))?;
                return Ok(integer.unbind());
            }
            // Unlike Number::as_f64, Python's JSON decoder accepts overflow as infinity.
            let number = value
                .as_f64()
                .or_else(|| text.parse::<f64>().ok())
                .ok_or_else(|| PyValueError::new_err("cache contains an invalid number"))?;
            Ok(PyFloat::new(py, number).unbind().into_any())
        }
        Value::String(value) => match markers
            .numbers
            .as_ref()
            .and_then(|markers| markers.matches(value))
        {
            Some(NonFiniteFloat::NaN) => Ok(PyFloat::new(py, f64::NAN).unbind().into_any()),
            Some(NonFiniteFloat::PositiveInfinity) => {
                Ok(PyFloat::new(py, f64::INFINITY).unbind().into_any())
            }
            Some(NonFiniteFloat::NegativeInfinity) => {
                Ok(PyFloat::new(py, f64::NEG_INFINITY).unbind().into_any())
            }
            None => cache_string_to_python(py, value, markers),
        },
        Value::Array(values) => {
            let list = PyList::empty(py);
            for value in values {
                list.append(json_to_python(py, value, markers)?.bind(py))?;
            }
            Ok(list.unbind().into_any())
        }
        Value::Object(values) => {
            let dictionary = PyDict::new(py);
            for (key, value) in values {
                dictionary.set_item(
                    cache_string_to_python(py, key, markers)?.bind(py),
                    json_to_python(py, value, markers)?.bind(py),
                )?;
            }
            Ok(dictionary.unbind().into_any())
        }
    }
}

fn cache_string_to_python(
    py: Python<'_>,
    value: &str,
    markers: &CacheReadMarkers,
) -> PyResult<Py<PyAny>> {
    match markers.strings.get(value) {
        Some(bytes) => PyBytes::new(py, bytes)
            .call_method1("decode", ("utf-8", "surrogatepass"))
            .map(Bound::unbind),
        None => Ok(PyString::new(py, value).unbind().into_any()),
    }
}

#[cfg(test)]
mod tests;
