//! Worker-side session object that claims tests and publishes results.

use super::{
    DemandCaseCursor, JsonSizeCounter, MAX_PARALLEL_WORKERS, MAX_PLAN_BYTES, POLL_INTERVAL,
    ParallelControlOwned, ParallelPlan, ParallelScheduling, WorkerCollection,
    WorkerCollectionCompletion, WorkerExit, WorkerResultBundle, collection_exit_code, demand,
    first_mismatch, independent, io_error, parse_worker_junit_properties, progress_path, read_json,
    surplus_path, validate_plan, validate_worker_collection, wait_for_worker_plan,
    write_json_atomic,
};
use crate::NativeParallelError;
use crate::results::{NativeRunResult, warning_records};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyAny;
use rustix::fs::{FlockOperation, flock};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use vfy_core::test_execution::{CaseResult, CollectionOutcome};

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeParallelWorker {
    pub(super) directory: PathBuf,
    pub(super) index: usize,
    pub(super) indices: Vec<usize>,
    pub(super) maxfail: usize,
    pub(super) demand: Option<Arc<demand::DemandQueue>>,
    pub(super) coverage_shard: Option<PathBuf>,
    pub(super) progress: Mutex<Option<File>>,
}

impl NativeParallelWorker {
    pub(super) fn publish_collection_snapshot(
        directory: &Path,
        collection: &WorkerCollection,
        invocation: Option<&independent::InvocationBinding>,
    ) -> PyResult<()> {
        let width: usize = read_json(&directory.join("bootstrap.json")).map_err(io_error)?;
        match invocation {
            Some(binding) => {
                binding.validate_collection_binding(directory, collection.index, width)?;
            }
            None if !(2..=MAX_PARALLEL_WORKERS).contains(&width) || collection.index >= width => {
                return Err(PyValueError::new_err(
                    "invalid worker collection index or width",
                ));
            }
            None => (),
        }
        let path = directory.join(format!("collection.{}.json", collection.index));
        if path.is_file() {
            return Ok(());
        }
        validate_worker_collection(collection)?;
        let mut counter = JsonSizeCounter { bytes: 0 };
        serde_json::to_writer(&mut counter, collection)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        if counter.bytes > MAX_PLAN_BYTES {
            return Err(PyValueError::new_err(
                "native worker collection exceeds the supported manifest size",
            ));
        }
        write_json_atomic(&path, collection).map_err(io_error)
    }
}

#[pymethods]
impl NativeParallelWorker {
    /// Publish the independent driver's authoritative collection snapshot.
    #[staticmethod]
    #[pyo3(signature = (directory, index, nodeids, result, *, invocation=None))]
    fn publish_independent_collection(
        directory: String,
        index: usize,
        nodeids: Vec<String>,
        result: &NativeRunResult,
        invocation: Option<&Bound<'_, crate::independent::NativeIndependentInvocation>>,
    ) -> PyResult<()> {
        let binding = invocation
            .map(crate::independent::NativeIndependentInvocation::collection_binding)
            .transpose()?;
        let record = result.record();
        crate::results::validate_worker_observations(record.observations.as_ref())?;
        if record.engine != "vfy-independent-v1"
            || record.collected != nodeids.len()
            || !record.cases.is_empty()
        {
            return Err(PyValueError::new_err(
                "invalid independent worker collection snapshot",
            ));
        }
        let reports = record
            .collection_reports
            .iter()
            .map(|report| {
                serde_json::to_string(report)
                    .map_err(|error| PyValueError::new_err(error.to_string()))
            })
            .collect::<PyResult<Vec<_>>>()?;
        let errors = record
            .collection_reports
            .iter()
            .filter(|report| report.outcome == CollectionOutcome::Failed)
            .count();
        Self::publish_collection_snapshot(
            &PathBuf::from(directory),
            &WorkerCollection {
                index,
                nodeids,
                reports,
                warnings: Vec::new(),
                errors,
                cache_snapshot: Some(result.transport_json()?),
                benchmark_fixture: false,
            },
            binding.as_ref(),
        )
    }

    /// Validate and return the Rust assignment in order.
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the full collection manifest once at the worker boundary"
    )]
    fn prepare_independent(&self, nodeids: Vec<String>) -> PyResult<Vec<String>> {
        let plan: ParallelPlan = read_json(&self.directory.join("plan.json")).map_err(io_error)?;
        if nodeids != plan.nodeids
            && !independent::read_identity(&self.directory)?.is_some_and(|identity| {
                identity.permits_selected_collection(&nodeids, &plan.nodeids)
            })
        {
            return Err(PyValueError::new_err(
                "independent worker assignment has a different collection manifest",
            ));
        }
        let assigned = self
            .indices
            .iter()
            .map(|index| {
                plan.nodeids.get(*index).cloned().ok_or_else(|| {
                    PyValueError::new_err("independent worker assignment index is out of range")
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        Ok(if self.demand.is_some() {
            plan.nodeids
        } else {
            assigned
        })
    }

    #[staticmethod]
    fn independent(
        py: Python<'_>,
        directory: String,
        index: usize,
        nodeids: Vec<String>,
    ) -> PyResult<Option<Self>> {
        if let Err(error) = wait_for_worker_plan(py, Path::new(&directory), index) {
            if error.is_instance_of::<NativeParallelError>(py) {
                return Ok(None);
            }
            return Err(error);
        }
        Self::new(py, directory, index, nodeids).map(Some)
    }

    /// Whether the supervisor released worker `index` because collection needs fewer workers.
    #[staticmethod]
    fn surplus(directory: &str, index: usize) -> bool {
        surplus_path(Path::new(directory), index).is_file()
    }

    /// The coverage shard this worker writes, when the supervisor registered one.
    #[getter]
    fn coverage_shard(&self) -> Option<String> {
        self.coverage_shard
            .as_ref()
            .map(|path| path.to_string_lossy().into_owned())
    }

    #[new]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the collected node IDs as an owned Python list"
    )]
    fn new(
        py: Python<'_>,
        directory: String,
        index: usize,
        nodeids: Vec<String>,
    ) -> PyResult<Self> {
        let directory = PathBuf::from(directory);
        if directory.join("bootstrap.json").is_file() {
            wait_for_worker_plan(py, &directory, index)?;
        }
        let plan = read_json::<ParallelPlan>(&directory.join("plan.json")).map_err(|error| {
            PyRuntimeError::new_err(format!("invalid native worker plan: {error}"))
        })?;
        validate_plan(&plan).map_err(PyValueError::new_err)?;
        let independent_demand = plan.scheduling == ParallelScheduling::Demand
            && independent::read_identity(&directory)?
                .is_some_and(|identity| identity.is_parallel());
        if plan.scheduling == ParallelScheduling::Demand && !independent_demand {
            return Err(PyValueError::new_err(
                "demand workers require an inherited precompiled session",
            ));
        }
        if index >= plan.assignments.len() {
            return Err(PyValueError::new_err(format!(
                "native parallel worker index {index} is out of range"
            )));
        }
        if nodeids != plan.nodeids
            && !independent::read_identity(&directory)?.is_some_and(|identity| {
                identity.permits_selected_collection(&nodeids, &plan.nodeids)
            })
        {
            let mismatch = first_mismatch(&plan.nodeids, &nodeids);
            return Err(PyValueError::new_err(format!(
                "native parallel worker {index} collected a different test manifest at index {mismatch}"
            )));
        }
        let demand = independent_demand
            .then(|| {
                demand::DemandQueue::open_independent(
                    &directory,
                    plan.nodeids.len(),
                    plan.assignments.len(),
                    index,
                )
                .map(Arc::new)
            })
            .transpose()?;
        Ok(Self {
            directory,
            index,
            indices: plan.assignments.into_iter().nth(index).ok_or_else(|| {
                PyValueError::new_err("native parallel worker assignment is missing")
            })?,
            maxfail: plan.maxfail,
            demand,
            coverage_shard: None,
            progress: Mutex::new(None),
        })
    }

    #[staticmethod]
    #[pyo3(signature = (directory, index, nodeids, reports, warnings, errors, cache_snapshot = None, benchmark_fixture = false))]
    #[expect(
        clippy::too_many_arguments,
        reason = "the Python-visible signature carries one argument per published snapshot field"
    )]
    fn publish_collection(
        directory: String,
        index: usize,
        nodeids: Vec<String>,
        reports: Vec<String>,
        warnings: Vec<(String, String, usize, String)>,
        errors: usize,
        cache_snapshot: Option<String>,
        benchmark_fixture: bool,
    ) -> PyResult<()> {
        Self::publish_collection_snapshot(
            &PathBuf::from(directory),
            &WorkerCollection {
                index,
                nodeids,
                reports,
                warnings,
                errors,
                cache_snapshot,
                benchmark_fixture,
            },
            None,
        )
    }

    #[staticmethod]
    #[pyo3(signature = (directory, index, returncode, infrastructure_failed, warnings, exit_reason = None))]
    fn complete_collection(
        directory: String,
        index: usize,
        returncode: &Bound<'_, PyAny>,
        infrastructure_failed: bool,
        warnings: Vec<(String, String, usize, String)>,
        exit_reason: Option<String>,
    ) -> PyResult<()> {
        let directory = PathBuf::from(directory);
        let width: usize = read_json(&directory.join("bootstrap.json")).map_err(io_error)?;
        if !(2..=MAX_PARALLEL_WORKERS).contains(&width) || index >= width {
            return Err(PyValueError::new_err(
                "invalid worker completion index or width",
            ));
        }
        let completion = WorkerCollectionCompletion {
            index,
            returncode: collection_exit_code(returncode)?,
            infrastructure_failed,
            warnings,
            exit_reason,
            invocation: None,
        };
        completion.publish(&directory)
    }

    fn complete_shutdown(&self, returncode: &Bound<'_, PyAny>) -> PyResult<i64> {
        let completion = WorkerCollectionCompletion {
            index: self.index,
            returncode: collection_exit_code(returncode)?,
            infrastructure_failed: false,
            warnings: Vec::new(),
            exit_reason: None,
            invocation: None,
        };
        completion.publish(&self.directory)?;
        Ok(completion.returncode.rem_euclid(256))
    }

    #[getter]
    fn indices(&self) -> Vec<usize> {
        self.indices.clone()
    }

    fn complete_exit(&self, reason: String, returncode: i32) -> PyResult<()> {
        let exit = WorkerExit {
            index: self.index,
            returncode,
            reason,
        };
        write_json_atomic(
            &self.directory.join(format!("exit.{}.json", self.index)),
            &exit,
        )
        .map_err(io_error)
    }

    #[pyo3(signature = (result, junit_options = None, metadata = None, collection_warnings = None, finish_warnings = None))]
    fn complete(
        &self,
        result: &NativeRunResult,
        junit_options: Option<&Bound<'_, PyAny>>,
        metadata: Option<Vec<(String, String)>>,
        collection_warnings: Option<Vec<(String, String, usize, String)>>,
        finish_warnings: Option<Vec<(String, String, usize, String)>>,
    ) -> PyResult<()> {
        let worker_record = self.result_bundle(
            result,
            junit_options,
            metadata,
            collection_warnings,
            finish_warnings,
        )?;
        write_json_atomic(&self.result_path(), &worker_record).map_err(io_error)
    }
}

impl NativeParallelWorker {
    pub(super) fn result_bundle<'a>(
        &self,
        result: &'a NativeRunResult,
        junit_options: Option<&Bound<'_, PyAny>>,
        metadata: Option<Vec<(String, String)>>,
        collection_warnings: Option<Vec<(String, String, usize, String)>>,
        finish_warnings: Option<Vec<(String, String, usize, String)>>,
    ) -> PyResult<WorkerResultBundle<'a>> {
        let expected = self.assigned_count();
        if result.collected_count() != expected {
            return Err(PyValueError::new_err(format!(
                "native worker {} ran {} collected tests; its assigned batch has {expected}",
                self.index,
                result.collected_count()
            )));
        }
        let junit_properties = junit_options
            .map(parse_worker_junit_properties)
            .transpose()?
            .unwrap_or_else(|| result.frozen_junit_properties());
        let (collection, finish) = result.worker_session_warning_records();
        let worker_record = WorkerResultBundle {
            result: result.transport(),
            junit_properties,
            metadata: metadata.unwrap_or_default(),
            collection_warnings: collection_warnings.map_or(collection, warning_records),
            finish_warnings: finish_warnings.map_or(finish, warning_records),
        };
        Ok(worker_record)
    }

    pub(crate) fn demand_cursor(&self) -> Option<DemandCaseCursor<'_>> {
        self.demand
            .as_deref()
            .map(|queue| DemandCaseCursor::new(self, queue))
    }

    pub(crate) fn assigned_count(&self) -> usize {
        self.demand
            .as_deref()
            .map_or(self.indices.len(), demand::DemandQueue::claimed_count)
    }

    pub(crate) fn ready_and_wait(&self, py: Python<'_>) -> PyResult<()> {
        vfy_core::atomic_write(&self.ready_path(), b"ready\n").map_err(io_error)?;
        loop {
            py.check_signals()?;
            match fs::read_to_string(self.directory.join("control.json")) {
                Ok(control) => {
                    let control: ParallelControlOwned = serde_json::from_str(&control)
                        .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
                    return match control.state.as_str() {
                        "go" => Ok(()),
                        "abort" => Err(PyRuntimeError::new_err(if control.detail.is_empty() {
                            "native parallel session aborted before test execution".to_owned()
                        } else {
                            control.detail
                        })),
                        state => Err(PyRuntimeError::new_err(format!(
                            "unknown native parallel control state {state:?}"
                        ))),
                    };
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(io_error(error)),
            }
            py.detach(|| thread::sleep(POLL_INTERVAL));
        }
    }

    pub(crate) fn claim_test(&self) -> PyResult<bool> {
        if self.maxfail == 0 {
            return Ok(true);
        }
        let mut failures = self.open_failure_budget()?;
        let current = read_failure_count(&mut failures).map_err(io_error)?;
        Ok(current < self.maxfail)
    }

    pub(crate) fn record_failures(&self, count: usize) -> PyResult<()> {
        if count == 0 || self.maxfail == 0 {
            return Ok(());
        }
        let mut failures = self.open_failure_budget()?;
        let current = read_failure_count(&mut failures).map_err(io_error)?;
        let updated = current.saturating_add(count);
        failures
            .set_len(0)
            .and_then(|()| failures.seek(SeekFrom::Start(0)).map(|_| ()))
            .and_then(|()| writeln!(failures, "{updated}"))
            .and_then(|()| failures.flush())
            .map_err(io_error)?;
        Ok(())
    }

    fn open_failure_budget(&self) -> PyResult<File> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(self.directory.join("failure-budget"))
            .map_err(io_error)?;
        flock(&file, FlockOperation::LockExclusive).map_err(io_error)?;
        Ok(file)
    }

    /// Append a finished case so the supervisor can report progress before
    /// the worker publishes its result.
    pub(crate) fn record_progress(&self, case: &CaseResult) -> PyResult<()> {
        let mut line =
            serde_json::to_vec(case).map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        line.push(b'\n');
        let mut progress = self
            .progress
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let file = match progress.as_mut() {
            Some(file) => file,
            None => progress.insert(
                OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(progress_path(&self.directory, self.index))
                    .map_err(io_error)?,
            ),
        };
        // One append per record keeps lines whole for the concurrent reader.
        file.write_all(&line).map_err(io_error)
    }

    fn ready_path(&self) -> PathBuf {
        self.directory.join(format!("ready.{}", self.index))
    }

    pub(super) fn result_path(&self) -> PathBuf {
        self.directory.join(format!("result.{}.json", self.index))
    }
}

fn read_failure_count(file: &mut File) -> std::io::Result<usize> {
    file.seek(SeekFrom::Start(0))?;
    let mut value = String::new();
    file.read_to_string(&mut value)?;
    value
        .trim()
        .parse()
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
}
