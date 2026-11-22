//! Phase publication and report snapshot materialization.

use super::call::{NativeIndependentCallInfo, PhaseCompletion};
use super::reportinfo::location;
use super::rows::ReportEntry;
use super::{
    NativeIndependentReport, NativeSessionReports, PhaseProjection, PhasePublication,
    ReportSnapshot, Sections, copies, interaction, make, origin,
};
use crate::hooks::NativeHookRegistry;
use crate::independent::lifecycle::Context;
use crate::results::ReportLocation;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::borrow::Cow;
use std::sync::Arc;
use vfy_core::test_execution::{CaseResult, PhaseResult};

pub(in crate::independent) fn publish_phase(
    py: Python<'_>,
    context: &Context,
    item: &Bound<'_, PyAny>,
    location: &Arc<ReportLocation>,
    completion: PhaseCompletion,
    sections: Sections,
    publication: PhasePublication<'_, '_>,
) -> PyResult<PhaseResult> {
    let completion = crate::independent::unittest::completion(py, item, completion)?;
    let nodeid = item
        .getattr(pyo3::intern!(py, "nodeid"))?
        .extract::<String>()?;
    let which = completion.phase();
    let origin = publication.boundary.origin;
    let (mut call, capture) = completion.into_call_state(py)?;
    call.pending_report = Some(make::PendingReport {
        location: location.clone(),
        shared_nodeid: publication.shared_nodeid.cloned(),
        sections,
        capture,
        origin: Some(origin.clone()),
    });
    let mut arguments =
        crate::hooks::invocation::CallArguments::phase(item, call, publication.history);
    let result = crate::hooks::NativeHookRegistry::call_subset_arguments(
        context.registry.bind(py),
        "pytest_runtest_makereport",
        &mut arguments,
        &NativeHookRegistry::item_exclusions(context.registry.bind(py), item)?,
        true,
    );
    arguments.release_phase_kwargs();
    let result = result.and_then(|report| match report {
        crate::hooks::invocation::HookReturn::NativeReport(report) => Ok(report),
        other => {
            let report = other
                .into_python(py)?
                .into_bound(py)
                .cast_into::<NativeIndependentReport>()?;
            copies::attach(&report, origin)?;
            Ok(NativeSessionReports::from_published(
                publication.history,
                &report,
            ))
        }
    });
    arguments.finish_phase(py);
    let report = result?;
    let facts = report.facts(py)?;
    if facts.nodeid.as_ref() != nodeid.as_str() || facts.phase != which {
        return Err(crate::UnsupportedFeatureError::new_err(
            "pytest_runtest_makereport returned a report for a different item or phase",
        ));
    }
    let mut log_arguments = crate::hooks::invocation::CallArguments::RawLogReport(&report);
    let removed = NativeHookRegistry::item_exclusions(context.registry.bind(py), item)?;
    let logged = NativeHookRegistry::call_subset_arguments(
        context.registry.bind(py),
        "pytest_runtest_logreport",
        &mut log_arguments,
        &removed,
        false,
    );
    drop(removed);
    log_arguments.release_log_kwargs();
    if let Err(error) = logged {
        let history = context
            .session
            .bind(py)
            .getattr("_vfy_report")?
            .cast_into::<NativeSessionReports>()?;
        if history.is(report.history(py)) {
            report.retain_only(py);
        } else {
            let published = report.to_python(py)?;
            NativeSessionReports::retain_report(&history, published.bind(py));
        }
        return Err(error);
    }
    if arguments.original_has_exception(py)? {
        let call = arguments.original_call_for_interaction(py)?;
        let published = report.to_python(py)?;
        interaction::dispatch(
            py,
            context.registry.bind(py),
            item,
            call.bind(py),
            published.bind(py).as_any(),
        )?;
    }
    if matches!(publication.projection, PhaseProjection::Full)
        || publication.history.borrow().report_count() == publication.report_start
    {
        report.snapshot(py)
    } else {
        // Emitted reports replace checkpoints after protocol wrappers and at final publication.
        report.checkpoint(py)
    }
}

/// Resolve completed deliveries after protocol wrappers have returned.
pub(in crate::independent) fn case_snapshots_since(
    history: &Bound<'_, NativeSessionReports>,
    start: usize,
    nodeid: &str,
    completed: Option<&CaseResult>,
    origin: &origin::ReportOrigin,
) -> PyResult<Vec<ReportSnapshot>> {
    let py = history.py();
    let reports = history.borrow().since(start)?.to_vec();
    let mut snapshots = Vec::new();
    for id in reports {
        let native = history.borrow().with_row(py, id, |report| {
            (
                report.origin.clone(),
                report.nodeid.clone(),
                report.phase.phase,
                report.is_subtest(),
            )
        })?;
        let published = {
            let owner = history.borrow();
            match owner.rows[id.0].entry.as_ref() {
                Some(ReportEntry::Published(report)) => Some(report.clone_ref(py)),
                _ => None,
            }
        };
        let Some(report) = published else {
            if native.0.as_ref() != Some(origin) {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "test protocol report belongs to another native case",
                ));
            }
            if native.1.as_ref() != nodeid {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "independent test protocol reports must belong to the active item",
                ));
            }
            if !native.3
                && completed.is_some_and(|completed| {
                    !completed.phases.iter().any(|phase| phase.phase == native.2)
                })
            {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "independent test protocol reports must preserve completed native phases",
                ));
            }
            snapshots.push(NativeSessionReports::snapshot_row(history, id)?);
            continue;
        };
        let report = report.bind(py);
        if !report.borrow().bound_origin(py)?.same(py, origin) {
            return Err(crate::UnsupportedFeatureError::new_err(
                "test protocol report belongs to another native case",
            ));
        }
        let same_nodeid = {
            let value = report.getattr(pyo3::intern!(py, "nodeid"))?;
            value.extract::<Cow<'_, str>>()? == nodeid
        };
        if !same_nodeid {
            return Err(crate::UnsupportedFeatureError::new_err(
                "independent test protocol reports must belong to the active item",
            ));
        }
        if !report.borrow().is_subtest()
            && let Some(completed) = completed
        {
            let has_phase = {
                let value = report.getattr(pyo3::intern!(py, "when"))?;
                let phase = value.extract::<Cow<'_, str>>()?;
                completed
                    .phases
                    .iter()
                    .any(|completed| completed.phase.as_str() == phase)
            };
            if !has_phase {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "independent test protocol reports must preserve completed native phases",
                ));
            }
        }
        snapshots.push(NativeIndependentReport::snapshot_event(report)?);
    }
    Ok(snapshots)
}

struct CaseReportGroup {
    first: usize,
    last: usize,
    // With duplicate case identities, the first case receives the deliveries and
    // validation uses the last case.
    first_case: Option<usize>,
    last_case: Option<usize>,
}

struct ReportDelivery {
    group: usize,
    nodeid: Arc<str>,
    snapshot: Option<ReportSnapshot>,
    next: Option<usize>,
}

#[derive(Default)]
pub(super) struct ReportBuffers {
    by_test: std::collections::HashMap<usize, usize>,
    groups: Vec<CaseReportGroup>,
    deliveries: Vec<ReportDelivery>,
}

impl ReportBuffers {
    pub(super) fn push(
        &mut self,
        test: vfy_core::test_plan::TestId,
        nodeid: Arc<str>,
        report: ReportSnapshot,
    ) {
        let delivery = self.deliveries.len();
        let group = *self.by_test.entry(test.index()).or_insert_with(|| {
            let group = self.groups.len();
            self.groups.push(CaseReportGroup {
                first: delivery,
                last: delivery,
                first_case: None,
                last_case: None,
            });
            group
        });
        let owner = &mut self.groups[group];
        if owner.last != delivery {
            self.deliveries[owner.last].next = Some(delivery);
            owner.last = delivery;
        }
        self.deliveries.push(ReportDelivery {
            group,
            nodeid,
            snapshot: Some(report),
            next: None,
        });
    }

    pub(super) fn bind_cases(&mut self, cases: &[CaseResult]) {
        for (index, case) in cases.iter().enumerate() {
            if let Some(group) = case
                .test_id()
                .and_then(|test| self.by_test.get(&test.index()))
            {
                let group = &mut self.groups[*group];
                group.first_case.get_or_insert(index);
                group.last_case = Some(index);
            }
        }
    }

    pub(super) fn validate(&self, cases: &[CaseResult]) -> PyResult<()> {
        let mismatch = self.deliveries.iter().find(|delivery| {
            self.groups[delivery.group]
                .last_case
                .is_some_and(|index| delivery.nodeid.as_ref() != cases[index].nodeid)
        });
        if let Some(delivery) = mismatch {
            let nodeid = &delivery.nodeid;
            return Err(crate::UnsupportedFeatureError::new_err(format!(
                "retained report belongs to another native case: {nodeid}"
            )));
        }
        Ok(())
    }

    pub(super) fn apply(mut self, cases: &mut [CaseResult]) {
        for group in self.groups {
            if let Some(index) = group.first_case {
                let mut next = Some(group.first);
                let snapshots = std::iter::from_fn(|| {
                    let delivery = &mut self.deliveries[next?];
                    next = delivery.next;
                    delivery.snapshot.take()
                });
                replace_case_snapshots(&mut cases[index], snapshots);
            }
        }
    }
}

/// Snapshot each delivery in chronological order before borrowing the result.
/// One contiguous delivery store owns snapshots until all validation succeeds;
/// case groups link into it without allocating a separate vector per case.
fn report_snapshots(py: Python<'_>, session: &Bound<'_, PyAny>) -> PyResult<ReportBuffers> {
    let history = session
        .getattr("_vfy_report")?
        .cast_into::<NativeSessionReports>()?;
    let (generation, reports) = {
        let owner = history.borrow();
        (owner.generation(), owner.reports.clone())
    };
    let mut buffers = ReportBuffers::default();
    for id in reports {
        let (origin, nodeid) = history.borrow().with_row(py, id, |report| {
            (report.origin.clone(), report.nodeid.clone())
        })?;
        let origin = origin.ok_or_else(|| {
            PyValueError::new_err("retained report has no bound native case provenance")
        })?;
        if origin.generation() != generation {
            return Err(crate::UnsupportedFeatureError::new_err(
                "retained report belongs to another native session",
            ));
        }
        buffers.push(
            origin.observed_test(),
            nodeid,
            NativeSessionReports::snapshot_row(&history, id)?,
        );
    }
    Ok(buffers)
}

/// Worker transport uses report sources, joined by authenticated case identity.
pub(in crate::independent) fn worker_report_locations(
    py: Python<'_>,
    session: &Bound<'_, PyAny>,
    cases: &[CaseResult],
) -> PyResult<Vec<(String, ReportLocation)>> {
    let history = session
        .getattr("_vfy_report")?
        .cast_into::<NativeSessionReports>()?;
    let reports = history.borrow().reports.clone();
    let mut by_test = std::collections::BTreeMap::new();
    for id in reports {
        let (origin, location, nodeid) = history.borrow().with_row(py, id, |report| {
            (
                report.origin.clone(),
                report.location.clone(),
                report.nodeid.clone(),
            )
        })?;
        let test = origin
            .ok_or_else(|| {
                PyValueError::new_err("retained report has no bound native case provenance")
            })?
            .observed_test();
        if let Some(previous) = by_test.get(&test.index()) {
            if previous != &location {
                return Err(crate::UnsupportedFeatureError::new_err(format!(
                    "independent worker report locations differ between phases for {nodeid}"
                )));
            }
        } else {
            by_test.insert(test.index(), location);
        }
    }
    cases
        .iter()
        .map(|case| {
            let test = case.test_id().ok_or_else(|| {
                PyValueError::new_err("completed worker case has no authenticated test identity")
            })?;
            let location = by_test.get(&test.index()).cloned().ok_or_else(|| {
                PyValueError::new_err(format!(
                    "completed worker case has no retained report source: {}",
                    case.nodeid
                ))
            })?;
            Ok((case.nodeid.clone(), location.as_ref().clone()))
        })
        .collect()
}

pub(in crate::independent) fn case_from_snapshots(
    nodeid: String,
    duration_ms: f64,
    reports: Vec<ReportSnapshot>,
) -> CaseResult {
    let mut case = CaseResult::new(nodeid, duration_ms, Vec::new());
    replace_case_snapshots(&mut case, reports);
    case
}

pub(in crate::independent) fn replace_case_snapshots(
    case: &mut CaseResult,
    reports: impl IntoIterator<Item = ReportSnapshot>,
) {
    use vfy_core::test_execution::CaseReportEvent;
    let mut phases = std::mem::take(&mut case.phases);
    phases.clear();
    let mut subtests = Vec::new();
    let mut events: Option<Vec<CaseReportEvent>> = None;
    for report in reports {
        match report {
            ReportSnapshot::Phase(phase) => {
                if let Some(previous) = phases
                    .iter_mut()
                    .find(|previous| previous.phase == phase.phase)
                {
                    // Deliveries remain in NativeSessionReports. A case retains one
                    // current snapshot per executed phase, at its first event slot.
                    *previous = phase;
                } else {
                    if let Some(events) = &mut events {
                        events.push(CaseReportEvent::Phase(phases.len()));
                    }
                    phases.push(phase);
                }
            }
            ReportSnapshot::Subtest(subtest) => {
                let events = events
                    .get_or_insert_with(|| (0..phases.len()).map(CaseReportEvent::Phase).collect());
                events.push(CaseReportEvent::Subtest(subtests.len()));
                subtests.push(subtest);
            }
        }
    }
    case.set_subtests(subtests.into_boxed_slice());
    if let Some(events) = events {
        case.set_report_events(events.into_boxed_slice());
    } else {
        case.set_implicit_phase_events(phases.len());
    }
    case.phases = phases;
}

pub(in crate::independent) fn item_sections(item: &Bound<'_, PyAny>) -> PyResult<Sections> {
    item.getattr(pyo3::intern!(item.py(), "_report_sections"))?
        .try_iter()?
        .map(|entry| {
            let (when, key, content) = entry?.extract::<(String, String, String)>()?;
            Ok((format!("Captured {key} {when}"), content))
        })
        .collect()
}

pub(in crate::independent) fn make_subtest_report(
    py: Python<'_>,
    ihook: &Bound<'_, PyAny>,
    item: &Bound<'_, PyAny>,
    completion: PhaseCompletion,
    origin: origin::ReportOrigin,
) -> PyResult<(Py<NativeIndependentReport>, Py<NativeIndependentCallInfo>)> {
    let completion = crate::independent::unittest::completion(py, item, completion)?;
    let (call, capture) = completion.into_subtest_call(py)?;
    call.borrow_mut(py).pending_report = Some(make::PendingReport {
        location: Arc::new(location(item)?),
        shared_nodeid: None,
        sections: item_sections(item)?,
        capture,
        origin: Some(origin.clone()),
    });
    let kwargs = PyDict::new(py);
    kwargs.set_item("item", item)?;
    kwargs.set_item("call", &call)?;
    let result = if ihook.hasattr("_registry")? {
        ihook.getattr("_registry")?.call_method1(
            "call_subset",
            (
                "pytest_runtest_makereport",
                kwargs,
                ihook.getattr("_remove_plugins")?,
                true,
            ),
        )
    } else {
        ihook
            .getattr("pytest_runtest_makereport")?
            .call((), Some(&kwargs))
    };
    let result = result.and_then(|report| {
        let report = report.cast_into::<NativeIndependentReport>()?;
        copies::attach(&report, origin)?;
        Ok(report)
    });
    drop(call.borrow_mut(py).pending_report.take());
    let report = result?;
    if report.getattr("nodeid")?.ne(item.getattr("nodeid")?)?
        || report.getattr("when")?.ne("call")?
    {
        return Err(crate::UnsupportedFeatureError::new_err(
            "subtest makereport returned a report for a different item or phase",
        ));
    }
    Ok((report.unbind(), call))
}

pub(in crate::independent) fn boundary(
    py: Python<'_>,
    context: &Context,
    item: &Bound<'_, PyAny>,
    name: &str,
    location: &ReportLocation,
) -> PyResult<()> {
    let nodeid = item.getattr(pyo3::intern!(py, "nodeid"))?;
    let location = location.into_pyobject(py)?.into_any();
    let mut arguments = crate::hooks::invocation::CallArguments::Boundary {
        nodeid: &nodeid,
        location: &location,
    };
    let removed = NativeHookRegistry::item_exclusions(context.registry.bind(py), item)?;
    let result = NativeHookRegistry::call_subset_arguments(
        context.registry.bind(py),
        name,
        &mut arguments,
        &removed,
        false,
    );
    drop(removed);
    // Release the hook operands before propagating an error so their
    // destructors run before the caller observes the failure.
    drop(arguments);
    drop(nodeid);
    drop(location);
    result?;
    Ok(())
}

pub(in crate::independent) fn materialize_reports(
    py: Python<'_>,
    session: &Bound<'_, PyAny>,
    result: &Py<crate::results::NativeRunResult>,
) -> PyResult<()> {
    let mut reports = report_snapshots(py, session)?;
    let mut result = result.borrow_mut(py);
    let record = result.record_mut();
    reports.bind_cases(&record.cases);
    reports.validate(&record.cases)?;
    reports.apply(&mut record.cases);
    record.counts =
        vfy_core::test_execution::compute_run_counts(&record.cases, &record.collection_reports);
    Ok(())
}
