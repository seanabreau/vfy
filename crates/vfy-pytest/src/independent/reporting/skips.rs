//! Represent skipped outcomes in reports.

use pyo3::exceptions::{PyAssertionError, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PySlice, PyString};
use vfy_core::test_execution::{PhasePayload, SkipLocation};

pub(super) enum SkipFormatting {
    Formatted,
    OtherException,
}

pub(super) fn format_exception(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
    payload: &mut PhasePayload,
) -> PyResult<SkipFormatting> {
    let skipped = py.import("vfy.independent.facade")?.getattr("Skipped")?;
    if !info.getattr("value")?.is_instance(&skipped)? {
        // unittest.SkipTest conversion belongs to its earlier makereport hook.
        return Ok(SkipFormatting::OtherException);
    }
    let crash = super::diagnostics::exception_crash(py, info)?.ok_or_else(|| {
        PyAssertionError::new_err("There should always be a traceback entry for skipping a test.")
    })?;
    // ExceptionInfo.exconly() removes formatter suffix whitespace before location selection.
    let message = PyString::new(py, &crash.text)
        .call_method0("rstrip")?
        .extract::<String>()?;
    let value = info.getattr("value")?;
    let location = if value.getattr("_use_item_location")?.is_truthy()? {
        let reported = item
            .call_method0("reportinfo")?
            .get_item(py.get_type::<PySlice>().call1((py.None(), 2, py.None()))?)?;
        let mut reported = reported.try_iter()?;
        let path = reported.next().transpose()?.ok_or_else(|| {
            PyValueError::new_err("not enough values to unpack (expected 2, got 0)")
        })?;
        let line = reported.next().transpose()?.ok_or_else(|| {
            PyValueError::new_err("not enough values to unpack (expected 2, got 1)")
        })?;
        if reported.next().transpose()?.is_some() {
            return Err(PyValueError::new_err(
                "too many values to unpack (expected 2)",
            ));
        }
        if line.is_none() {
            return Err(PyAssertionError::new_err(()));
        }
        let path = py
            .import("os")?
            .call_method1("fspath", (path,))?
            .extract::<String>()?;
        let line = line.add(1)?.extract::<usize>()?;
        SkipLocation { path, line }
    } else {
        SkipLocation {
            path: crash.location.path,
            line: usize::try_from(crash.location.line)
                .map_err(|error| PyOverflowError::new_err(error.to_string()))?,
        }
    };
    payload.exception = Some(message.into());
    payload.skip_location = Some(location);
    payload.detail.clear();
    payload.terminal_lead.clear();
    Ok(SkipFormatting::Formatted)
}
