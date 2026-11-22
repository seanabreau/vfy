//! Validation of execution-record identity and warnings supplied by Python.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::Path;
use vfy_core::test_execution::{ExecutionRecordIdentity, WarningRecord};

pub(crate) fn warning_records(
    warnings: Vec<(String, String, usize, String)>,
) -> Vec<WarningRecord> {
    warnings
        .into_iter()
        .map(|(message, filename, lineno, nodeid)| WarningRecord {
            message,
            nodeid,
            filename,
            lineno,
            ..WarningRecord::default()
        })
        .collect()
}

pub(super) fn parse_execution_record_identity(
    value: &Bound<'_, PyAny>,
) -> PyResult<ExecutionRecordIdentity> {
    let value = value.cast::<PyDict>()?;
    if value.len() != 3 {
        return Err(PyValueError::new_err(
            "native execution identity must contain session_token, observer_nonce, and rootdir",
        ));
    }
    let session_token = value
        .get_item("session_token")?
        .ok_or_else(|| PyValueError::new_err("native execution identity has no session_token"))?
        .extract::<String>()?;
    let observer_nonce = value
        .get_item("observer_nonce")?
        .ok_or_else(|| PyValueError::new_err("native execution identity has no observer_nonce"))?
        .extract::<String>()?;
    let rootdir = value
        .get_item("rootdir")?
        .ok_or_else(|| PyValueError::new_err("native execution identity has no rootdir"))?
        .extract::<String>()?;
    if !is_hex_token(&session_token) || !is_hex_token(&observer_nonce) {
        return Err(PyValueError::new_err(
            "native execution identity tokens must be 64 lowercase hexadecimal characters",
        ));
    }
    if !Path::new(&rootdir).is_absolute() {
        return Err(PyValueError::new_err(
            "native execution identity rootdir must be absolute",
        ));
    }
    Ok(ExecutionRecordIdentity {
        producer_pid: std::process::id(),
        session_token,
        observer_nonce,
        session_depth: 0,
        rootdir,
    })
}

fn is_hex_token(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn parse_execution_record_warnings(
    value: &Bound<'_, PyAny>,
) -> PyResult<Vec<WarningRecord>> {
    let warnings = value.cast::<PyList>()?;
    warnings
        .iter()
        .map(|warning| {
            let warning = warning.cast::<PyDict>()?;
            if warning.len() != 4 {
                return Err(PyValueError::new_err(
                    "native execution warning must contain nodeid, message, filename, and lineno",
                ));
            }
            let nodeid = warning
                .get_item("nodeid")?
                .ok_or_else(|| PyValueError::new_err("native execution warning has no nodeid"))?
                .extract::<String>()?;
            let message = warning
                .get_item("message")?
                .ok_or_else(|| PyValueError::new_err("native execution warning has no message"))?
                .extract::<String>()?;
            let filename = warning
                .get_item("filename")?
                .ok_or_else(|| PyValueError::new_err("native execution warning has no filename"))?
                .extract::<String>()?;
            let lineno = warning
                .get_item("lineno")?
                .ok_or_else(|| PyValueError::new_err("native execution warning has no lineno"))?
                .extract::<usize>()?;
            if lineno == 0 {
                return Err(PyValueError::new_err(
                    "native execution warning lineno must be positive",
                ));
            }
            Ok(WarningRecord {
                message,
                nodeid,
                filename,
                lineno,
                ..WarningRecord::default()
            })
        })
        .collect()
}
