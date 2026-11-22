//! Serialized control, collection, progress, and result records exchanged with workers.

use super::{MAX_PLAN_BYTES, ParallelSignalGuard, independent, io_error, write_json_atomic};
use crate::progress::Progress;
use crate::reporting::WorkerJUnitProperties;
use crate::results::NativeRunResultTransport;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyAny;
use serde::{Deserialize, Serialize};
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use vfy_core::test_execution::{CaseResult, WarningRecord};

#[derive(Serialize)]
pub(super) struct ParallelControl<'a> {
    pub(super) state: &'a str,
    pub(super) detail: &'a str,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct WorkerExit {
    pub(super) index: usize,
    pub(super) returncode: i32,
    pub(super) reason: String,
}

pub(super) struct CollectedBootstrap<'a, 'py> {
    pub(super) callback: &'a Bound<'py, PyAny>,
    pub(super) output: &'a Bound<'py, PyAny>,
    pub(super) signals: &'a ParallelSignalGuard,
    #[cfg(target_os = "linux")]
    pub(super) controller_pid: String,
}

pub(super) struct JsonSizeCounter {
    pub(super) bytes: u64,
}

impl Write for JsonSizeCounter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(buffer.len() as u64)
            .ok_or_else(|| io::Error::other("native parallel plan size overflowed"))?;
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Clone, Copy, Default)]
pub(super) enum CollectionRoute {
    #[default]
    Controller,
    Workers,
}

#[derive(Clone, Copy)]
pub(super) enum CollectionErrorPolicy {
    Stop,
    Continue,
}

impl CollectionErrorPolicy {
    pub(super) fn allows_execution(self, errors: usize) -> bool {
        errors == 0 || matches!(self, Self::Continue)
    }
}

#[derive(Clone, Copy)]
pub(super) enum WorkerCompletionRoute {
    Unrecorded,
    PostUnconfigure,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct WorkerCollection {
    pub(super) index: usize,
    pub(super) nodeids: Vec<String>,
    pub(super) reports: Vec<String>,
    pub(super) warnings: Vec<(String, String, usize, String)>,
    pub(super) errors: usize,
    pub(super) cache_snapshot: Option<String>,
    pub(super) benchmark_fixture: bool,
}

pub(super) struct SurplusWarnings {
    pub(super) collection: Vec<WarningRecord>,
    pub(super) finish: Vec<WarningRecord>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct WorkerCollectionCompletion {
    pub(super) index: usize,
    pub(super) returncode: i64,
    pub(super) infrastructure_failed: bool,
    pub(super) warnings: Vec<(String, String, usize, String)>,
    #[serde(default)]
    pub(super) exit_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) invocation: Option<independent::InvocationCompletion>,
}

impl WorkerCollectionCompletion {
    pub(super) fn publish(&self, directory: &Path) -> PyResult<()> {
        let mut counter = JsonSizeCounter { bytes: 0 };
        serde_json::to_writer(&mut counter, self)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        if counter.bytes > MAX_PLAN_BYTES {
            return Err(PyValueError::new_err(
                "native worker completion exceeds the supported metadata size",
            ));
        }
        write_json_atomic(
            &directory.join(format!("collection-finish.{}.json", self.index)),
            self,
        )
        .map_err(io_error)
    }
}

pub(super) fn progress_path(directory: &Path, index: usize) -> PathBuf {
    directory.join(format!("progress.{index}"))
}

/// The outcome fields that terminal progress needs from a finished case.
#[derive(Deserialize, Serialize)]
#[serde(transparent)]
pub(super) struct ProgressRecord {
    pub(super) case: CaseResult,
}

impl ProgressRecord {
    pub(super) fn into_case(self) -> CaseResult {
        self.case
    }
}

/// Terminal progress fed from the workers' progress files.
pub(super) struct WorkerProgress {
    pub(super) display: Progress,
    pub(super) offsets: Vec<u64>,
}

impl WorkerProgress {
    /// Report every complete record appended since the previous read.
    pub(super) fn drain(&mut self, py: Python<'_>, directory: &Path) -> PyResult<()> {
        for (index, offset) in self.offsets.iter_mut().enumerate() {
            let mut file = match File::open(progress_path(directory, index)) {
                Ok(file) => file,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(io_error(error)),
            };
            file.seek(SeekFrom::Start(*offset)).map_err(io_error)?;
            let mut appended = Vec::new();
            file.read_to_end(&mut appended).map_err(io_error)?;
            let Some(end) = appended.iter().rposition(|byte| *byte == b'\n') else {
                continue;
            };
            for line in appended[..end].split(|byte| *byte == b'\n') {
                let record: ProgressRecord = serde_json::from_slice(line)
                    .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
                let case = record.into_case();
                self.display.start(py, &case.nodeid)?;
                self.display.case(py, &case)?;
            }
            *offset += end as u64 + 1;
        }
        Ok(())
    }
}

#[derive(Deserialize)]
pub(super) struct ParallelControlOwned {
    pub(super) state: String,
    #[serde(default)]
    pub(super) detail: String,
}

#[derive(Serialize)]
pub(super) struct WorkerResultBundle<'a> {
    pub(super) result: NativeRunResultTransport<'a>,
    pub(super) junit_properties: WorkerJUnitProperties,
    pub(super) metadata: Vec<(String, String)>,
    pub(super) collection_warnings: Vec<WarningRecord>,
    pub(super) finish_warnings: Vec<WarningRecord>,
}

pub(super) const SURPLUS_DETAIL: &str = "native worker has no assignment for the collected tests";

pub(super) fn surplus_path(directory: &Path, index: usize) -> PathBuf {
    directory.join(format!("surplus.{index}"))
}
