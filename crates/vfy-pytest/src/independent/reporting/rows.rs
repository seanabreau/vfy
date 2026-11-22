//! One owner retains each raw report until its public Python identity is requested.
use super::{
    NativeIndependentReport, NativeSessionReports, PendingAttributes, ReportSnapshot, origin,
    terminal,
};
use pyo3::exceptions::{PyAttributeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyString;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering, fence};
use std::sync::{Mutex, PoisonError};
use vfy_core::test_execution::{ExecutionOutcome, ExecutionPhase, PhaseResult};

const COUNTERS_PER_CHUNK: usize = 256;
type CounterChunk = [AtomicUsize; COUNTERS_PER_CHUNK];

/// Stable logical-owner cells survive growth of the history's row vectors.
#[derive(Default)]
pub(super) struct ReportOwners {
    chunks: Vec<Arc<CounterChunk>>,
}

impl ReportOwners {
    fn allocate(&mut self, id: ReportId, initial: usize) {
        if id.0.is_multiple_of(COUNTERS_PER_CHUNK) {
            self.chunks
                .push(Arc::new(std::array::from_fn(|_| AtomicUsize::new(0))));
        }
        self.counter(id).store(initial, Ordering::Relaxed);
    }

    fn counter(&self, id: ReportId) -> &AtomicUsize {
        &self.chunks[id.0 / COUNTERS_PER_CHUNK][id.0 % COUNTERS_PER_CHUNK]
    }

    fn chunk(&self, id: ReportId) -> Arc<CounterChunk> {
        Arc::clone(&self.chunks[id.0 / COUNTERS_PER_CHUNK])
    }
}

pub(super) type RetiredRows = Arc<Mutex<Vec<ReportId>>>;

fn release_owner(owners: &AtomicUsize) -> bool {
    let previous = owners.fetch_sub(1, Ordering::Release);
    debug_assert!(previous > 0, "report ownership must remain balanced");
    if previous == 1 {
        fence(Ordering::Acquire);
        true
    } else {
        false
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct ReportId(pub(super) usize);

#[expect(
    clippy::large_enum_variant,
    reason = "inline native rows avoid a heap allocation for every ordinary phase"
)]
pub(super) enum ReportEntry {
    Native(NativeIndependentReport),
    Published(Py<NativeIndependentReport>),
}

pub(super) struct ReportSlot {
    pub(super) entry: Option<ReportEntry>,
    pub(super) retained: bool,
    /// Whether `NativeSessionReports::reports` already lists this row.
    pub(super) listed: bool,
}

pub(crate) struct ReportFacts {
    pub(crate) nodeid: Arc<str>,
    pub(crate) phase: ExecutionPhase,
    pub(crate) outcome: ExecutionOutcome,
}

pub(crate) enum ReportExtras {
    Native(Option<PendingAttributes>),
    Python(Py<PyAny>),
}

pub(crate) struct ReportHandle {
    history: Py<NativeSessionReports>,
    id: ReportId,
    retired_rows: RetiredRows,
    owners: Arc<CounterChunk>,
}

impl ReportHandle {
    pub(crate) const fn id(&self) -> ReportId {
        self.id
    }
    pub(crate) fn history<'py>(&self, py: Python<'py>) -> &Bound<'py, NativeSessionReports> {
        self.history.bind(py)
    }
    pub(crate) fn clone_ref(&self, py: Python<'_>) -> Self {
        self.owners[self.id.0 % COUNTERS_PER_CHUNK].fetch_add(1, Ordering::Relaxed);
        Self {
            history: self.history.clone_ref(py),
            id: self.id,
            retired_rows: Arc::clone(&self.retired_rows),
            owners: Arc::clone(&self.owners),
        }
    }
    pub(crate) fn is_native(&self, py: Python<'_>) -> bool {
        matches!(
            self.history
                .bind(py)
                .borrow()
                .rows
                .get(self.id.0)
                .and_then(|slot| slot.entry.as_ref()),
            Some(ReportEntry::Native(_))
        )
    }
    pub(crate) fn facts(&self, py: Python<'_>) -> PyResult<ReportFacts> {
        self.history
            .bind(py)
            .borrow()
            .with_row(py, self.id, |report| ReportFacts {
                nodeid: report.nodeid.clone(),
                phase: report.phase.phase,
                outcome: report.phase.outcome,
            })
    }
    pub(crate) fn to_python(&self, py: Python<'_>) -> PyResult<Py<NativeIndependentReport>> {
        NativeSessionReports::materialize(self.history.bind(py), self.id)
    }
    pub(crate) fn into_python(self, py: Python<'_>) -> PyResult<Py<NativeIndependentReport>> {
        self.to_python(py)
    }
    pub(crate) fn subtest_context(
        &self,
        py: Python<'_>,
    ) -> PyResult<Option<Py<crate::independent::subtests::NativeSubtestContext>>> {
        self.history
            .bind(py)
            .borrow()
            .with_row(py, self.id, |report| report.subtest_context(py))
    }
    pub(crate) fn checkpoint(&self, py: Python<'_>) -> PyResult<PhaseResult> {
        self.history
            .bind(py)
            .borrow()
            .with_row(py, self.id, |report| report.phase.clone())
    }
    pub(crate) fn retain_only(&self, py: Python<'_>) {
        let mut history = self.history.bind(py).borrow_mut();
        history.retain_session(self.id);
        history.list_once(self.id);
    }
    pub(crate) fn snapshot(&self, py: Python<'_>) -> PyResult<PhaseResult> {
        match NativeSessionReports::snapshot_row(self.history.bind(py), self.id)? {
            ReportSnapshot::Phase(phase) => Ok(phase),
            ReportSnapshot::Subtest(_) => Err(PyValueError::new_err(
                "an execution phase cannot be a subtest report",
            )),
        }
    }
    pub(crate) fn getattr(&self, py: Python<'_>, name: &str) -> PyResult<Py<PyAny>> {
        enum Value {
            Text(String),
            Bool(bool),
            Float(f64),
            Object(Option<Py<PyAny>>),
            Wasxfail(Option<Py<PyAny>>),
            Publish,
        }
        if !self.is_native(py) {
            return Ok(self.to_python(py)?.bind(py).getattr(name)?.unbind());
        }
        let value = self
            .history
            .bind(py)
            .borrow()
            .with_row(py, self.id, |report| match name {
                "nodeid" => Value::Text(report.nodeid.to_string()),
                "when" => Value::Text(report.phase.phase.as_str().to_owned()),
                "outcome" => Value::Text(report.phase.outcome.report_outcome().as_str().to_owned()),
                "passed" | "failed" | "skipped" => {
                    Value::Bool(report.phase.outcome.report_outcome().as_str() == name)
                }
                "duration" => Value::Float(report.timing.duration),
                "start" => Value::Float(report.timing.start),
                "stop" => Value::Float(report.timing.stop),
                "longrepr" => {
                    Value::Object(report.longrepr.as_ref().map(|value| value.clone_ref(py)))
                }
                "wasxfail" => Value::Wasxfail(
                    report
                        .pending_attributes
                        .as_ref()
                        .and_then(|attributes| attributes.wasxfail.as_ref())
                        .map(|value| value.clone_ref(py)),
                ),
                _ => Value::Publish,
            })?;
        match value {
            Value::Text(value) => Ok(PyString::new(py, &value).into_any().unbind()),
            Value::Bool(value) => Ok(value.into_pyobject(py)?.to_owned().into_any().unbind()),
            Value::Float(value) => Ok(value.into_pyobject(py)?.into_any().unbind()),
            Value::Object(value) => Ok(value.unwrap_or_else(|| py.None())),
            Value::Wasxfail(value) => {
                value.ok_or_else(|| PyAttributeError::new_err(name.to_owned()))
            }
            Value::Publish => Ok(self.to_python(py)?.bind(py).getattr(name)?.unbind()),
        }
    }
    pub(crate) fn extras(&self, py: Python<'_>) -> PyResult<ReportExtras> {
        enum Extras {
            Native(Option<PendingAttributes>),
            Published(Py<NativeIndependentReport>),
        }
        let extras = {
            let history = self.history.bind(py).borrow();
            match history.rows[self.id.0].entry.as_ref() {
                Some(ReportEntry::Native(report)) => Extras::Native(
                    report
                        .pending_attributes
                        .as_ref()
                        .map(|extras| extras.clone_ref(py)),
                ),
                Some(ReportEntry::Published(report)) => Extras::Published(report.clone_ref(py)),
                None => return Err(PyValueError::new_err("native report row has been released")),
            }
        };
        match extras {
            Extras::Native(extras) => Ok(ReportExtras::Native(extras)),
            Extras::Published(report) => Ok(ReportExtras::Python(
                report.bind(py).getattr("__dict__")?.unbind(),
            )),
        }
    }
    pub(crate) fn hasattr(&self, py: Python<'_>, name: &str) -> PyResult<bool> {
        if name == "wasxfail" && self.is_native(py) {
            return self
                .history
                .bind(py)
                .borrow()
                .with_row(py, self.id, |report| {
                    report
                        .pending_attributes
                        .as_ref()
                        .is_some_and(|attributes| attributes.wasxfail.is_some())
                });
        }
        match self.getattr(py, name) {
            Ok(_) => Ok(true),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => Ok(false),
            Err(error) => Err(error),
        }
    }
    pub(crate) fn setattr(
        &self,
        py: Python<'_>,
        name: &str,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if !self.is_native(py) {
            return self.to_python(py)?.bind(py).setattr(name, value);
        }
        if name == "wasxfail" {
            let previous = {
                let mut history = self.history.bind(py).borrow_mut();
                match history.rows[self.id.0].entry.as_mut() {
                    Some(ReportEntry::Native(report)) => report
                        .pending_attributes
                        .as_mut()
                        .map(|attributes| attributes.wasxfail.replace(value.clone().unbind())),
                    _ => None,
                }
            };
            if let Some(previous) = previous {
                drop(previous);
                return Ok(());
            }
            return self.to_python(py)?.bind(py).setattr(name, value);
        }
        if name == "outcome" {
            let text = value.extract::<String>()?;
            let mut history = self.history.bind(py).borrow_mut();
            if let Some(ReportEntry::Native(report)) = history.rows[self.id.0].entry.as_mut() {
                return report.set_outcome(&text);
            }
            drop(history);
        } else if name == "longrepr" {
            let previous = {
                let mut history = self.history.bind(py).borrow_mut();
                match history.rows[self.id.0].entry.as_mut() {
                    Some(ReportEntry::Native(report)) => {
                        report.representation_changed = true;
                        Some(report.longrepr.replace(value.clone().unbind()))
                    }
                    _ => None,
                }
            };
            if let Some(previous) = previous {
                drop(previous);
                return Ok(());
            }
        }
        self.to_python(py)?.bind(py).setattr(name, value)
    }
}

impl Drop for ReportHandle {
    fn drop(&mut self) {
        if !release_owner(&self.owners[self.id.0 % COUNTERS_PER_CHUNK]) {
            return;
        }
        self.retired_rows
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(self.id);
        Python::attach(|py| {
            NativeSessionReports::apply_retired_rows(self.history.bind(py), &self.retired_rows);
        });
    }
}

impl NativeSessionReports {
    /// Append `id` to the session's report history.
    pub(super) fn list(&mut self, id: ReportId) {
        self.rows[id.0].listed = true;
        self.reports.push(id);
    }

    /// Append `id` unless the history already lists it.
    pub(super) fn list_once(&mut self, id: ReportId) {
        if !self.rows[id.0].listed {
            self.list(id);
        }
    }

    pub(super) fn retain_session(&mut self, id: ReportId) {
        if !self.rows[id.0].retained {
            self.owners.counter(id).fetch_add(1, Ordering::Relaxed);
            self.rows[id.0].retained = true;
        }
    }

    pub(super) fn release_delivery_owners(&mut self) {
        for id in self.terminal.owned_rows() {
            release_owner(self.owners.counter(id));
        }
        for (index, row) in self.rows.iter_mut().enumerate() {
            if row.retained {
                release_owner(self.owners.counter(ReportId(index)));
                row.retained = false;
            }
        }
    }

    fn apply_retired_rows(slf: &Bound<'_, Self>, retired_rows: &RetiredRows) {
        let removed = {
            let Ok(mut history) = slf.try_borrow_mut() else {
                return;
            };
            let pending =
                std::mem::take(&mut *retired_rows.lock().unwrap_or_else(PoisonError::into_inner));
            let mut removed = Vec::new();
            for id in pending {
                if id.0 < history.rows.len()
                    && history.owners.counter(id).load(Ordering::Acquire) == 0
                {
                    removed.extend(history.rows[id.0].entry.take());
                }
            }
            removed
        };
        drop(removed);
    }
    fn flush_retired_rows(slf: &Bound<'_, Self>) {
        let retired_rows = Arc::clone(&slf.borrow().retired_rows);
        Self::apply_retired_rows(slf, &retired_rows);
    }

    /// Snapshot identities own their rows across Python allocation and reentry.
    pub(crate) fn pin_rows(slf: &Bound<'_, Self>, ids: &[ReportId]) -> Vec<ReportHandle> {
        let history = slf.borrow();
        ids.iter()
            .map(|id| {
                history.owners.counter(*id).fetch_add(1, Ordering::Relaxed);
                history.handle(slf, *id)
            })
            .collect()
    }

    fn handle(&self, owner: &Bound<'_, Self>, id: ReportId) -> ReportHandle {
        ReportHandle {
            history: owner.clone().unbind(),
            id,
            retired_rows: Arc::clone(&self.retired_rows),
            owners: self.owners.chunk(id),
        }
    }

    pub(crate) fn prepare_native(
        slf: &Bound<'_, Self>,
        mut report: NativeIndependentReport,
    ) -> PyResult<ReportHandle> {
        let py = slf.py();
        // These normally live only in the instance dictionary. A currently
        // installed class operation must receive the original public setattr.
        let namespace = py
            .get_type::<NativeIndependentReport>()
            .getattr("__dict__")?;
        if namespace.contains("keywords")? || namespace.contains("wasxfail")? {
            let report = NativeIndependentReport::publish(py, report)?;
            return Ok(Self::from_published(slf, report.bind(py)));
        }
        Self::flush_retired_rows(slf);
        let mut history = slf.borrow_mut();
        let id = ReportId(history.rows.len());
        report.row_id = Some(id);
        history.owners.allocate(id, 1);
        history.rows.push(ReportSlot {
            entry: Some(ReportEntry::Native(report)),
            retained: false,
            listed: false,
        });
        Ok(history.handle(slf, id))
    }
    pub(crate) fn from_published(
        slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
    ) -> ReportHandle {
        Self::flush_retired_rows(slf);
        let id = Self::adopt_published(slf, report);
        let history = slf.borrow();
        history.owners.counter(id).fetch_add(1, Ordering::Relaxed);
        history.handle(slf, id)
    }
    pub(super) fn adopt_published(
        slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
    ) -> ReportId {
        let existing = report.borrow().row_id;
        if let Some(id)=existing && slf.borrow().rows.get(id.0).is_some_and(|slot|matches!(&slot.entry,Some(ReportEntry::Published(previous)) if previous.is(report))) {return id;}
        let id = ReportId(slf.borrow().rows.len());
        report.borrow_mut().row_id = Some(id);
        let mut history = slf.borrow_mut();
        history.owners.allocate(id, 0);
        history.rows.push(ReportSlot {
            entry: Some(ReportEntry::Published(report.clone().unbind())),
            retained: false,
            listed: false,
        });
        id
    }
    pub(super) fn with_row<T>(
        &self,
        py: Python<'_>,
        id: ReportId,
        read: impl FnOnce(&NativeIndependentReport) -> T,
    ) -> PyResult<T> {
        match self.rows.get(id.0).and_then(|slot| slot.entry.as_ref()) {
            Some(ReportEntry::Native(report)) => Ok(read(report)),
            Some(ReportEntry::Published(report)) => Ok(read(&report.borrow(py))),
            None => Err(PyValueError::new_err("native report row has been released")),
        }
    }
    pub(crate) fn retain_row(slf: &Bound<'_, Self>, id: ReportId) -> PyResult<()> {
        let history = slf.borrow();
        if id.0 >= history.rows.len() {
            return Err(PyValueError::new_err("unknown native report row"));
        }
        history.owners.counter(id).fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
    pub(crate) fn release_rows(slf: &Bound<'_, Self>, ids: impl IntoIterator<Item = ReportId>) {
        let removed = {
            let mut history = slf.borrow_mut();
            let mut removed = Vec::new();
            for id in ids {
                if id.0 < history.rows.len() && release_owner(history.owners.counter(id)) {
                    removed.extend(history.rows[id.0].entry.take());
                }
            }
            removed
        };
        drop(removed);
    }
    pub(crate) fn record_native(handle: &ReportHandle, py: Python<'_>) -> PyResult<()> {
        let history = handle.history(py);
        let facts = handle.facts(py)?;
        let wasxfail = handle.hasattr(py, "wasxfail")?;
        {
            let mut owner = history.borrow_mut();
            owner.retain_session(handle.id);
            owner.list(handle.id);
        }
        if facts.outcome.report_outcome().as_str() == "failed" && !wasxfail {
            Self::count_failure(history, py)?;
        }
        Ok(())
    }
    fn count_failure(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<()> {
        let session = slf
            .borrow()
            .session
            .as_ref()
            .map(|value| value.clone_ref(py));
        let Some(session) = session else {
            return Ok(());
        };
        let session = session.bind(py);
        let failures = session.getattr("testsfailed")?.extract::<usize>()? + 1;
        session.setattr("testsfailed", failures)?;
        let maxfail = session
            .getattr("config")?
            .getattr("option")?
            .getattr("maxfail")?;
        if maxfail.is_truthy()? && failures.into_pyobject(py)?.ge(&maxfail)? {
            session.setattr("shouldfail", format!("stopping after {failures} failures"))?;
        }
        Ok(())
    }
    pub(crate) fn materialize(
        slf: &Bound<'_, Self>,
        id: ReportId,
    ) -> PyResult<Py<NativeIndependentReport>> {
        let py = slf.py();
        Self::flush_retired_rows(slf);
        let extras = {
            let history = slf.borrow();
            match history.rows.get(id.0).and_then(|slot| slot.entry.as_ref()) {
                Some(ReportEntry::Published(report)) => return Ok(report.clone_ref(py)),
                Some(ReportEntry::Native(report)) => report
                    .pending_attributes
                    .as_ref()
                    .map(|value| value.clone_ref(py)),
                None => return Err(PyValueError::new_err("native report row has been released")),
            }
        };
        // Prepare the complete dictionary before allocating the public shell.
        let dictionary = extras
            .as_ref()
            .map(|attributes| attributes.dictionary(py))
            .transpose()?;
        let dict_name = pyo3::intern!(py, "__dict__");
        let shell = Py::new(py, NativeIndependentReport::empty_shell())?;
        if let Some(dictionary) = &dictionary {
            shell.bind(py).setattr(dict_name, dictionary)?;
        }
        let (result, removed) = {
            let mut history = slf.borrow_mut();
            let slot = history
                .rows
                .get_mut(id.0)
                .ok_or_else(|| PyValueError::new_err("unknown native report row"))?;
            match slot.entry.as_ref() {
                Some(ReportEntry::Published(report)) => (report.clone_ref(py), None),
                Some(ReportEntry::Native(_)) => {
                    let entry = slot.entry.take();
                    let Some(ReportEntry::Native(mut report)) = entry else {
                        return Err(PyValueError::new_err(
                            "native report row changed during publication",
                        ));
                    };
                    let extras = report.pending_attributes.take();
                    let old = std::mem::replace(&mut *shell.bind(py).borrow_mut(), report);
                    slot.entry = Some(ReportEntry::Published(shell.clone_ref(py)));
                    (shell.clone_ref(py), Some((old, extras)))
                }
                None => return Err(PyValueError::new_err("native report row has been released")),
            }
        };
        drop(removed);
        drop(shell);
        drop(extras);
        Ok(result)
    }
    pub(super) fn published_reports(
        slf: &Bound<'_, Self>,
    ) -> PyResult<Vec<Py<NativeIndependentReport>>> {
        let ids = slf.borrow().reports.clone();
        ids.into_iter()
            .map(|id| Self::materialize(slf, id))
            .collect()
    }
    pub(super) fn snapshot_row(slf: &Bound<'_, Self>, id: ReportId) -> PyResult<ReportSnapshot> {
        let py = slf.py();
        let input = {
            let history = slf.borrow();
            match history.rows.get(id.0).and_then(|slot| slot.entry.as_ref()) {
                Some(ReportEntry::Native(report))
                    if !report.representation_changed
                        && !report.refresh_representation
                        && !report.is_subtest() =>
                {
                    Some((
                        report.phase.clone(),
                        report.sections.clone_ref(py),
                        report.phase.report_summary.is_some()
                            || report
                                .origin
                                .as_ref()
                                .is_some_and(origin::ReportOrigin::transports_summary),
                        report.longrepr.as_ref().map(|value| value.clone_ref(py)),
                        report
                            .pending_attributes
                            .as_ref()
                            .map(|value| value.clone_ref(py)),
                    ))
                }
                _ => None,
            }
        };
        if let Some((mut phase, sections, transport_summary, representation, extras)) = input {
            let plain = extras
                .as_ref()
                .is_none_or(|attributes| attributes.wasxfail.is_none());
            if plain {
                let sections = sections.snapshot(py)?;
                phase.set_report_sections(sections);
                if transport_summary {
                    phase.set_report_summary(Some(terminal::summary::native_metadata(
                        py,
                        representation.as_ref(),
                        extras.as_ref(),
                        || {
                            let history = slf.borrow();
                            match history.rows[id.0].entry.as_ref() {
                                Some(ReportEntry::Published(report)) => Some(report.clone_ref(py)),
                                _ => None,
                            }
                        },
                    )?));
                }
                // Match the public snapshot's final scalar read without repeating callbacks.
                let current = slf
                    .borrow()
                    .with_row(py, id, |report| report.phase.clone())?;
                phase.phase = current.phase;
                phase.duration_ms = current.duration_ms;
                phase.outcome = match current.outcome.report_outcome().as_str() {
                    "passed" => ExecutionOutcome::Passed,
                    "skipped" => ExecutionOutcome::Skipped,
                    _ if current.phase == ExecutionPhase::Call => ExecutionOutcome::Failed,
                    _ => ExecutionOutcome::Error,
                };
                return Ok(ReportSnapshot::Phase(phase));
            }
        }
        NativeIndependentReport::snapshot_event(Self::materialize(slf, id)?.bind(py))
    }
}

#[cfg(test)]
mod tests;
