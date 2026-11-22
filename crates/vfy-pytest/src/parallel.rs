//! Worker process supervision, scheduling, and result merging for parallel native runs.

use crate::NativeParallelError;
use crate::reporting::WorkerJUnitProperties;
use crate::results::ParallelResultMode;
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::time::Duration;
use tempfile::NamedTempFile;

mod cancellation;
mod collection;
mod demand;
pub(crate) mod independent;
pub(crate) mod mutation;
mod plan;
mod process;
mod protocol;
mod publication;
mod session;
mod streams;
mod supervisor;
mod worker;

pub(crate) use cancellation::retain_parent_context;
use collection::{
    collection_exit_code, collection_report_shapes, read_worker_collection,
    validate_worker_collection, wait_for_worker_plan,
};
pub(crate) use demand::{DemandCaseCursor, DemandQueue};
pub(crate) use mutation::cache::{
    FreshEvidence, fingerprints as fresh_cache_fingerprints, read as read_fresh_cache,
    write as write_fresh_cache,
};
use plan::{
    ParallelPlan, ParallelScheduling, ScopeGrouping, first_mismatch, group_count, partition,
    validate_plan, validate_plan_size,
};
pub(crate) use process::validate_fork_resources;
use process::{
    ParallelChild, collected_fork_timeout, coverage_base, is_missing_process_group,
    stop_prespawned, worker_exit_code,
};
use protocol::{
    CollectedBootstrap, CollectionErrorPolicy, CollectionRoute, JsonSizeCounter, ParallelControl,
    ParallelControlOwned, SURPLUS_DETAIL, SurplusWarnings, WorkerCollection,
    WorkerCollectionCompletion, WorkerCompletionRoute, WorkerExit, WorkerProgress,
    WorkerResultBundle, progress_path, surplus_path,
};
pub(crate) use publication::NativeMutationPublication;
pub(crate) use session::NativeParallelSession;
use session::ParallelSignalGuard;
use streams::{read_worker_tails, replay_worker_stream, replay_worker_stream_range};
use supervisor::ParallelSupervisor;
pub(crate) use worker::NativeParallelWorker;
#[cfg(test)]
use {
    plan::{CaseWeights, partition_by_cases, partition_by_scope, partition_by_stripes},
    process::read_fork_children,
    protocol::ProgressRecord,
};

/// Interval at which the supervisor rechecks worker exits, progress files, and
/// interrupts. One loop covers all three on Linux and macOS; macOS has no pidfd,
/// and every wait keeps its own deadline.
const POLL_INTERVAL: Duration = Duration::from_millis(20);
/// Upper bound on waiting for prespawned workers after worker zero fails during startup.
const PRESPAWN_SETTLE_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_PARALLEL_WORKERS: usize = vfy_core::MAX_WORKERS as usize;
pub(crate) const MAX_PARALLEL_NODEIDS: usize = 1_000_000;
pub(crate) const MAX_NODEID_BYTES: usize = 65_536;
pub(crate) const MAX_PLAN_BYTES: u64 = 128 * 1024 * 1024;
const OUTPUT_CHUNK_BYTES: usize = 64 * 1024;
const DIAGNOSTIC_STREAM_BYTES: usize = 64 * 1024;
const COLLECTION_ABORT_DETAIL: &str = "collection failed before native execution";

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> std::io::Result<T> {
    let size = fs::metadata(path)?.len();
    if size > MAX_PLAN_BYTES {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "native parallel plan exceeds the supported size",
        ));
    }
    let bytes = fs::read(path)?;
    serde_json::from_slice(&bytes)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
}
fn parse_worker_junit_properties(options: &Bound<'_, PyAny>) -> PyResult<WorkerJUnitProperties> {
    let options = options.cast::<PyDict>()?;
    let global_properties = match options.get_item("global_properties")? {
        Some(values) => crate::reporting::parse_properties(&values)?,
        None => Vec::new(),
    };
    Ok(WorkerJUnitProperties { global_properties })
}
fn write_json_atomic(path: &Path, value: &impl Serialize) -> std::io::Result<()> {
    let mut temporary = NamedTempFile::new_in(path.parent().unwrap_or_else(|| Path::new(".")))?;
    {
        let mut writer = BufWriter::new(&mut temporary);
        serde_json::to_writer(&mut writer, value)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    temporary.persist(path).map_err(|error| error.error)?;
    Ok(())
}
fn io_error(error: impl std::fmt::Display) -> PyErr {
    PyOSError::new_err(error.to_string())
}
fn native_parallel_error(py: Python<'_>, message: String, returncode: Option<i32>) -> PyErr {
    let error = NativeParallelError::new_err(message);
    let _ = error
        .value(py)
        .setattr("returncode", returncode.unwrap_or(3));
    error
}

#[cfg(test)]
mod tests;
