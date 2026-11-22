//! Default report construction used by the registered runner hook.

use super::call::{NativeIndependentCallInfo, PhaseTiming};
use super::{NativeIndependentReport, Sections};
use crate::results::ReportLocation;
use pyo3::prelude::*;
use std::sync::Arc;
use vfy_core::test_execution::{ExecutionPhase, PhasePayload};

pub(crate) struct PendingReport {
    pub(super) location: Arc<ReportLocation>,
    pub(super) shared_nodeid: Option<Arc<str>>,
    pub(super) sections: Sections,
    pub(super) capture: PhasePayload,
    pub(super) origin: Option<super::origin::ReportOrigin>,
}

pub(super) fn from_call(
    item: &Bound<'_, PyAny>,
    call: &Bound<'_, NativeIndependentCallInfo>,
) -> PyResult<Py<NativeIndependentReport>> {
    let py = item.py();
    let pending = call.borrow_mut().pending_report.take();
    let state = pending_or_default(item, pending)?;
    let (when, timing, info) = call.borrow().projection(py)?;
    let report = from_parts(item, state, when, timing, info, || call.borrow().timing())?;
    NativeIndependentReport::publish(py, report)
}

pub(crate) fn from_call_state(
    item: &Bound<'_, PyAny>,
    call: &mut NativeIndependentCallInfo,
) -> PyResult<NativeIndependentReport> {
    let state = pending_or_default(item, call.pending_report.take())?;
    let (when, timing, info) = call.projection(item.py())?;
    from_parts(item, state, when, timing, info, || timing)
}

fn pending_or_default(
    item: &Bound<'_, PyAny>,
    pending: Option<PendingReport>,
) -> PyResult<PendingReport> {
    Ok(match pending {
        Some(state) => state,
        None => PendingReport {
            location: Arc::new(super::location(item)?),
            shared_nodeid: None,
            sections: super::item_sections(item)?,
            capture: PhasePayload::default(),
            origin: None,
        },
    })
}

fn from_parts(
    item: &Bound<'_, PyAny>,
    state: PendingReport,
    when: ExecutionPhase,
    timing: PhaseTiming,
    info: Option<Py<PyAny>>,
    final_timing: impl FnOnce() -> PhaseTiming,
) -> PyResult<NativeIndependentReport> {
    let py = item.py();
    let mut representation = None;
    let phase = NativeIndependentCallInfo::phase_result_parts(
        py,
        when,
        timing,
        info,
        state.capture,
        |_error, info, payload| match super::skips::format_exception(py, item, info, payload)? {
            super::skips::SkipFormatting::Formatted => Ok(()),
            super::skips::SkipFormatting::OtherException => {
                let result = super::failure::dispatch(py, item, info, when)?;
                super::failure::preserve_diagnostic(result.bind(py), payload)?;
                representation = Some(result);
                Ok(())
            }
        },
    )?;
    let mut report = NativeIndependentReport::prepare(
        py,
        super::ReportIdentity {
            location: &state.location,
            shared_nodeid: state.shared_nodeid.as_ref(),
        },
        phase,
        state.sections,
        item,
        final_timing(),
        representation.as_ref(),
    )?;
    report.origin = state.origin;
    Ok(report)
}
