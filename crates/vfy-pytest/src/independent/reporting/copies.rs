//! Wire dictionaries keep the payloads they carry alive until their owner releases them.
//!
//! A report retains each dictionary its `_to_json` issued. Logged reports release
//! their dictionaries when the configuration closes; other issuers release them when
//! they are freed. A decoded copy binds to the test whose makereport returned it and
//! releases the dictionary it was decoded from.

use super::{NativeIndependentReport, NativeSessionReports, origin::ReportOrigin};
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(super) fn issue(source: &Bound<'_, NativeIndependentReport>, data: &Bound<'_, PyDict>) {
    source.borrow_mut().issued_dicts.push(data.clone().unbind());
}

/// Binds a report that a makereport callback returned for `origin`'s test.
pub(super) fn attach(
    report: &Bound<'_, NativeIndependentReport>,
    origin: ReportOrigin,
) -> PyResult<()> {
    let pending = {
        let mut target = report.borrow_mut();
        if target
            .origin
            .as_ref()
            .is_some_and(|previous| previous != &origin)
        {
            return Err(crate::UnsupportedFeatureError::new_err(
                "makereport returned a report for a different native case or session",
            ));
        }
        target.origin = Some(origin);
        target.pending_copy.take()
    };
    // Releasing wire data may re-enter the report.
    drop(pending);
    Ok(())
}

/// Drops issued dictionaries of logged reports outside every borrow, because
/// payload finalizers may call back into the report or the session.
pub(super) fn release(history: &Bound<'_, NativeSessionReports>) {
    let py = history.py();
    let mut released = Vec::new();
    {
        let mut owner = history.borrow_mut();
        for slot in &mut owner.rows {
            match slot.entry.as_mut() {
                Some(super::ReportEntry::Native(report)) => {
                    released.extend(std::mem::take(&mut report.issued_dicts));
                }
                Some(super::ReportEntry::Published(report)) => released.extend(std::mem::take(
                    &mut report.bind(py).borrow_mut().issued_dicts,
                )),
                None => {}
            }
        }
    }
    drop(released);
}

pub(in crate::independent) fn install_cleanup(
    py: Python<'_>,
    history: &Py<NativeSessionReports>,
    config: &Bound<'_, PyAny>,
) -> PyResult<()> {
    config.call_method1(
        "add_cleanup",
        (history.bind(py).getattr("_release_copies")?,),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests;
