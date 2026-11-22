//! Worker result transport documents and atomic run-record JSON output.

use super::ReportLocation;
use super::html::merge_html_records;
use crate::reporting;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::{BufWriter, ErrorKind, Write};
use std::path::Path;
use vfy_core::test_execution::{
    CaseResult, CollectionReport, Counts, ExecutionRecordIdentity, PhasePayload, RunObservations,
    RunRecord, WarningRecord,
};

#[derive(Serialize)]
pub(crate) struct NativeRunResultTransport<'a> {
    #[serde(flatten)]
    pub(super) record: &'a RunRecord,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(super) report_locations: &'a Option<Vec<(String, ReportLocation)>>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub(super) html_extras: &'a BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub(super) html_errors: &'a BTreeMap<String, String>,
}

#[derive(Deserialize)]
pub(super) struct NativeRunResultTransportOwned {
    #[serde(default)]
    internal_errors: Vec<PhasePayload>,
    #[serde(default)]
    pub(super) exitstatus: Option<i32>,
    #[serde(flatten)]
    pub(super) observations: Option<RunObservations>,
    // Nested flattening of RunRecord fails on f64 with this crate's serde_json features.
    pub(super) engine: String,
    #[serde(flatten)]
    pub(super) execution_identity: Option<ExecutionRecordIdentity>,
    pub(super) collected: usize,
    pub(super) counts: Counts,
    pub(super) duration_ms: f64,
    pub(super) warnings: Vec<WarningRecord>,
    pub(super) cases: Vec<CaseResult>,
    #[serde(default)]
    pub(super) collection_reports: Vec<CollectionReport>,
    #[serde(default)]
    pub(super) deselected_nodeids: Vec<String>,
    #[serde(default)]
    pub(super) report_locations: Option<Vec<(String, ReportLocation)>>,
    #[serde(default)]
    pub(super) html_extras: BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    #[serde(default)]
    pub(super) html_errors: BTreeMap<String, String>,
}

#[derive(Deserialize)]
pub(super) struct NativeParallelWorkerResult<Header = serde::de::IgnoredAny> {
    independent: Option<Header>,
    pub(super) result: NativeRunResultTransportOwned,
    #[serde(default)]
    pub(super) junit_properties: reporting::WorkerJUnitProperties,
    #[serde(default)]
    pub(super) metadata: Vec<(String, String)>,
    #[serde(default)]
    pub(super) collection_warnings: Vec<WarningRecord>,
    #[serde(default)]
    pub(super) finish_warnings: Vec<WarningRecord>,
}

impl NativeRunResultTransportOwned {
    pub(super) fn merge_html_records(
        &mut self,
        extras: &mut BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
        errors: &mut BTreeMap<String, String>,
    ) -> PyResult<()> {
        if self.html_extras.is_empty() && self.html_errors.is_empty() {
            return Ok(());
        }
        let nodeids = self.cases.iter().map(|case| case.nodeid.as_str()).collect();
        merge_html_records(&nodeids, std::mem::take(&mut self.html_extras), extras)?;
        merge_html_records(&nodeids, std::mem::take(&mut self.html_errors), errors)
    }

    pub(super) fn into_record(self) -> RunRecord {
        RunRecord {
            internal_errors: self.internal_errors,
            collection_reports: self.collection_reports,
            deselected_nodeids: self.deselected_nodeids,
            engine: self.engine,
            execution_identity: self.execution_identity,
            observations: self.observations,
            exitstatus: self.exitstatus,
            collected: self.collected,
            counts: self.counts,
            duration_ms: self.duration_ms,
            warnings: self.warnings,
            cases: self.cases,
        }
    }
}

#[derive(Debug)]
pub(super) enum RunRecordWriteError {
    Io(std::io::Error),
    Serialization(serde_json::Error),
}

impl From<std::io::Error> for RunRecordWriteError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl std::fmt::Display for RunRecordWriteError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => error.fmt(formatter),
            Self::Serialization(error) => error.fmt(formatter),
        }
    }
}

impl RunRecordWriteError {
    pub(super) fn into_pyerr(self) -> PyErr {
        match self {
            Self::Io(error) => PyRuntimeError::new_err(error.to_string()),
            Self::Serialization(error) if error.classify() == serde_json::error::Category::Io => {
                let kind = error.io_error_kind().unwrap_or(ErrorKind::Other);
                PyRuntimeError::new_err(std::io::Error::new(kind, error).to_string())
            }
            Self::Serialization(error) => PyRuntimeError::new_err(error.to_string()),
        }
    }
}

pub(super) fn write_run_record_json(
    destination: &Path,
    record: &RunRecord,
) -> Result<(), RunRecordWriteError> {
    let parent = destination
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    {
        let mut writer = BufWriter::new(&mut temporary);
        serde_json::to_writer_pretty(&mut writer, record)
            .map_err(RunRecordWriteError::Serialization)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    temporary
        .persist(destination)
        .map_err(|error| RunRecordWriteError::Io(error.error))?;
    Ok(())
}

pub(crate) fn validate_worker_observations(observed: Option<&RunObservations>) -> PyResult<()> {
    if observed.is_some_and(|observed| observed.coordinator_configuration.is_some()) {
        return Err(PyValueError::new_err(
            "worker result contains coordinator configuration",
        ));
    }
    Ok(())
}

pub(crate) fn worker_declares_exitstatus(json: &str, status: i32) -> PyResult<bool> {
    let worker: NativeParallelWorkerResult =
        serde_json::from_str(json).map_err(|error| PyValueError::new_err(error.to_string()))?;
    let record = worker.result.into_record();
    vfy_core::test_execution::validate_run_record(&record)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(record.engine == "vfy-independent-v1"
        && record
            .exitstatus
            .is_some_and(|declared| matches_process_status(declared, status)))
}

pub(super) fn matches_process_status(declared: i32, actual: i32) -> bool {
    #[cfg(unix)]
    {
        declared.rem_euclid(256) == actual
    }
    #[cfg(not(unix))]
    {
        declared == actual
    }
}

pub(crate) struct WorkerResultSummary {
    pub(crate) completed: usize,
    pub(crate) exitstatus: Option<i32>,
    pub(crate) engine: String,
    pub(crate) independent: Option<crate::parallel::independent::RecordHeader>,
}

/// Decode the existing worker document without granting live-report capabilities.
pub(crate) fn worker_result_summary(json: &str) -> PyResult<WorkerResultSummary> {
    let worker: NativeParallelWorkerResult<crate::parallel::independent::RecordHeader> =
        serde_json::from_str(json).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(WorkerResultSummary {
        completed: worker.result.cases.len(),
        exitstatus: worker.result.exitstatus,
        engine: worker.result.engine,
        independent: worker.independent,
    })
}
