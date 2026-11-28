//! Worker collection files and their validation.

use super::{
    COLLECTION_ABORT_DETAIL, MAX_NODEID_BYTES, MAX_PARALLEL_NODEIDS, MAX_PLAN_BYTES, POLL_INTERVAL,
    ParallelControlOwned, SURPLUS_DETAIL, WorkerCollection, io_error, native_parallel_error,
    read_json, surplus_path,
};
use pyo3::exceptions::{PyOverflowError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyInt};
use std::path::Path;
use std::thread;

pub(super) fn wait_for_worker_plan(py: Python<'_>, directory: &Path, index: usize) -> PyResult<()> {
    loop {
        py.check_signals()?;
        // The supervisor releases surplus workers before publishing the plan.
        // Once we see the plan, the following lookup sees our final role.
        let plan_ready = directory.join("plan.json").is_file();
        if surplus_path(directory, index).is_file() {
            return Err(native_parallel_error(
                py,
                SURPLUS_DETAIL.to_owned(),
                Some(2),
            ));
        }
        let control_path = directory.join("control.json");
        if control_path.is_file() {
            let control: ParallelControlOwned = read_json(&control_path).map_err(io_error)?;
            if control.state == "abort" {
                if control.detail == COLLECTION_ABORT_DETAIL
                    && directory.join("bootstrap.json").is_file()
                    && !directory.join("plan.json").is_file()
                {
                    return Err(native_parallel_error(py, control.detail, Some(2)));
                }
                return Err(PyRuntimeError::new_err(control.detail));
            }
        }
        if plan_ready {
            return Ok(());
        }
        py.detach(|| thread::sleep(POLL_INTERVAL));
    }
}

pub(super) fn collection_report_shapes(reports: &[String]) -> PyResult<Vec<(String, String)>> {
    reports
        .iter()
        .map(|report| {
            let value: serde_json::Value = serde_json::from_str(report).map_err(|error| {
                PyValueError::new_err(format!("invalid native collection report: {error}"))
            })?;
            let nodeid = value
                .get("nodeid")
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| PyValueError::new_err("native collection report has no node ID"))?;
            let outcome = value
                .get("outcome")
                .and_then(serde_json::Value::as_str)
                .filter(|outcome| matches!(*outcome, "passed" | "failed" | "skipped"))
                .ok_or_else(|| {
                    PyValueError::new_err("native collection report has an invalid outcome")
                })?;
            Ok((nodeid.to_owned(), outcome.to_owned()))
        })
        .collect()
}

pub(super) fn validate_worker_collection(collection: &WorkerCollection) -> PyResult<()> {
    if collection.nodeids.len() > MAX_PARALLEL_NODEIDS
        || collection
            .nodeids
            .iter()
            .any(|nodeid| nodeid.is_empty() || nodeid.len() > MAX_NODEID_BYTES)
    {
        return Err(PyValueError::new_err(
            "native worker collection exceeds the supported manifest size",
        ));
    }
    collection_report_shapes(&collection.reports)?;
    Ok(())
}

pub(super) fn read_worker_collection(path: &Path) -> PyResult<WorkerCollection> {
    if path.metadata().map_err(io_error)?.len() > MAX_PLAN_BYTES {
        return Err(PyValueError::new_err(
            "native worker collection exceeds the supported manifest size",
        ));
    }
    let collection: WorkerCollection = read_json(path).map_err(io_error)?;
    validate_worker_collection(&collection)?;
    Ok(collection)
}

// Match CPython's SystemExit handling without calling an integer subclass's __int__.
// PyLong_AsLong overflow becomes -1, which is process status 255 on this platform.
pub(super) fn collection_exit_code(value: &Bound<'_, PyAny>) -> PyResult<i64> {
    if value.is_none() {
        return Ok(0);
    }
    let Ok(integer) = value.cast::<PyInt>() else {
        return Ok(1);
    };
    match integer.extract::<std::os::raw::c_long>() {
        Ok(code) => Ok(i64::from(code.to_le_bytes()[0])),
        Err(error) if error.is_instance_of::<PyOverflowError>(value.py()) => Ok(255),
        Err(error) => Err(error),
    }
}
