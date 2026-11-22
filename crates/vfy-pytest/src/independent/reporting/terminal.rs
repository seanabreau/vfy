//! Terminal status buckets retain the report handles consumed by the hook.

pub(crate) use super::rows::ReportExtras;
use super::{NativeIndependentReport, NativeSessionReports, ReportId};
use crate::module_cache::Module;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::Mutex;

pub(in crate::independent) mod collection;
pub(super) mod live;
pub(super) mod progress;
pub(super) mod projection;
pub(super) mod reason;
pub(in crate::independent) mod session;
pub(super) mod summary;
pub(super) mod verbose;
pub(super) mod wrap;

pub(in crate::independent) fn write_report_lines(
    reporter: &Bound<'_, PyAny>,
    results: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let reversed = reporter
        .py()
        .import("builtins")?
        .call_method1("reversed", (results,))?;
    for result in reversed.try_iter()? {
        let result = result?;
        if result.is_instance_of::<PyString>() {
            reporter.call_method1("write_line", (result,))?;
        } else {
            for line in result.try_iter()? {
                reporter.call_method1("write_line", (line?,))?;
            }
        }
    }
    Ok(())
}

/// Terminal buckets retain report identities until a caller asks for the mapping.
enum TerminalBuckets {
    Native {
        buckets: Vec<(String, Vec<ReportId>)>,
        revision: u64,
    },
    Public(Py<PyAny>),
}

/// The terminal index owns either native rows or the public mapping.
#[derive(Default)]
pub(super) struct TerminalStatusIndex {
    buckets: Option<TerminalBuckets>,
    parallel_progress: bool,
}

impl TerminalStatusIndex {
    pub(super) fn new(_py: Python<'_>) -> Self {
        Self {
            buckets: Some(TerminalBuckets::Native {
                buckets: Vec::new(),
                revision: 0,
            }),
            parallel_progress: false,
        }
    }

    fn public(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        match &self.buckets {
            Some(TerminalBuckets::Public(stats)) => Some(stats.clone_ref(py)),
            _ => None,
        }
    }

    pub(super) fn append_report(
        &mut self,
        category: &str,
        report: ReportId,
    ) -> PyResult<Option<bool>> {
        let Some(TerminalBuckets::Native { buckets, revision }) = &mut self.buckets else {
            return Ok(None);
        };
        *revision = revision.checked_add(1).ok_or_else(|| {
            pyo3::exceptions::PyOverflowError::new_err("terminal report count exceeds u64")
        })?;
        let is_new =
            if let Some((_, reports)) = buckets.iter_mut().find(|(name, _)| name == category) {
                reports.push(report);
                false
            } else {
                buckets.push((category.to_owned(), vec![report]));
                true
            };
        Ok(Some(is_new))
    }

    pub(super) fn owned_rows(&self) -> impl Iterator<Item = ReportId> + '_ {
        let buckets = match &self.buckets {
            Some(TerminalBuckets::Native { buckets, .. }) => Some(buckets),
            _ => None,
        };
        buckets
            .into_iter()
            .flatten()
            .flat_map(|(_, rows)| rows.iter().copied())
    }

    pub(super) fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(TerminalBuckets::Public(stats)) = &self.buckets {
            visit.call(stats)?;
        }
        Ok(())
    }

    pub(super) fn take(&mut self) -> Self {
        std::mem::take(self)
    }

    pub(super) fn parallel_progress(&self) -> bool {
        self.parallel_progress
    }

    pub(super) fn mark_parallel_progress(&mut self) {
        self.parallel_progress = true;
    }
}

pub(super) fn append_handle(
    py: Python<'_>,
    report: &super::ReportHandle,
    category: &str,
) -> PyResult<Option<bool>> {
    let history = report.history(py);
    let appended = history
        .borrow_mut()
        .terminal
        .append_report(category, report.id())?;
    if appended.is_some() {
        NativeSessionReports::retain_row(history, report.id())?;
    }
    Ok(appended)
}

/// Build a private complete mapping before atomically making it the live owner.
/// Reentrant publication wins; subsequent callers always receive that mapping.
pub(super) fn public_stats(history: &Bound<'_, NativeSessionReports>) -> PyResult<Py<PyAny>> {
    let py = history.py();
    loop {
        let (revision, buckets) = {
            let state = history.borrow();
            if let Some(stats) = state.terminal.public(py) {
                return Ok(stats);
            }
            match &state.terminal.buckets {
                Some(TerminalBuckets::Native { buckets, revision }) => (*revision, buckets.clone()),
                _ => {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "terminal statistics are no longer available",
                    ));
                }
            }
        };
        let ids = buckets
            .iter()
            .flat_map(|(_, reports)| reports.iter().copied())
            .collect::<Vec<_>>();
        let _pins = NativeSessionReports::pin_rows(history, &ids);
        let candidate = PyDict::new(py);
        for (category, reports) in buckets {
            let values = PyList::empty(py);
            for report in reports {
                if let Some(selected) = history.borrow().terminal.public(py) {
                    return Ok(selected);
                }
                let object = match NativeSessionReports::materialize(history, report) {
                    Ok(object) => object,
                    Err(error) => {
                        if let Some(selected) = history.borrow().terminal.public(py) {
                            return Ok(selected);
                        }
                        return Err(error);
                    }
                };
                values.append(object)?;
            }
            candidate.set_item(category, values)?;
        }
        let (selected, old) = {
            let mut state = history.borrow_mut();
            if let Some(existing) = state.terminal.public(py) {
                (Some(existing), None)
            } else if matches!(
                &state.terminal.buckets,
                Some(TerminalBuckets::Native { revision: current, .. }) if *current == revision
            ) {
                let selected = candidate.clone().into_any().unbind();
                let old = state
                    .terminal
                    .buckets
                    .replace(TerminalBuckets::Public(selected.clone_ref(py)));
                (Some(selected), old)
            } else {
                (None, None)
            }
        };
        release_buckets(history, old);
        if let Some(selected) = selected {
            return Ok(selected);
        }
    }
}

pub(super) fn replace_stats(history: &Bound<'_, NativeSessionReports>, stats: &Bound<'_, PyAny>) {
    let old = history
        .borrow_mut()
        .terminal
        .buckets
        .replace(TerminalBuckets::Public(stats.clone().unbind()));
    release_buckets(history, old);
}

fn release_buckets(history: &Bound<'_, NativeSessionReports>, old: Option<TerminalBuckets>) {
    match old {
        Some(TerminalBuckets::Native { buckets, .. }) => {
            NativeSessionReports::release_rows(
                history,
                buckets.into_iter().flat_map(|(_, reports)| reports),
            );
        }
        Some(TerminalBuckets::Public(stats)) => drop(stats),
        None => (),
    }
}

/// The public terminal statistics mapping is owned by the session report index.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeTerminalStats;

#[pymethods]
impl NativeTerminalStats {
    #[new]
    fn new() -> Self {
        Self
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        if instance.is_none() {
            return Ok(slf.clone().into_any().unbind());
        }
        Ok(instance
            .getattr("_status_index")?
            .call_method0("terminal_live_stats")?
            .unbind())
    }

    #[expect(clippy::unused_self, reason = "the descriptor owns the public setter")]
    fn __set__(&self, instance: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        instance
            .getattr("_status_index")?
            .call_method1("terminal_replace_stats", (value,))?;
        Ok(())
    }
}

fn native_stats_owner<'py>(
    reporter: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, NativeSessionReports>>> {
    let Some(descriptor) =
        crate::hooks::native_caller::type_slot(reporter, pyo3::intern!(reporter.py(), "stats"))?
    else {
        return Ok(None);
    };
    if !descriptor.is_exact_instance_of::<NativeTerminalStats>() {
        return Ok(None);
    }
    let Some(getattribute) = crate::hooks::native_caller::type_slot(
        reporter,
        pyo3::intern!(reporter.py(), "__getattribute__"),
    )?
    else {
        return Ok(None);
    };
    let standard = reporter
        .py()
        .get_type::<PyDict>()
        .getattr("__base__")?
        .getattr("__getattribute__")?;
    if !getattribute.is(&standard) {
        return Ok(None);
    }
    let index = reporter.getattr("_status_index")?;
    if !index.is_exact_instance_of::<NativeSessionReports>() {
        return Ok(None);
    }
    Ok(Some(index.cast_into::<NativeSessionReports>()?))
}

/// Read native buckets only when the installed descriptor owns them.
pub(super) fn native_bucket_counts(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<Option<Vec<(String, usize)>>> {
    let Some(index) = native_stats_owner(reporter)? else {
        return Ok(None);
    };
    let index = index.borrow();
    Ok(match &index.terminal.buckets {
        Some(TerminalBuckets::Native { buckets, .. }) => Some(
            buckets
                .iter()
                .map(|(name, rows)| (name.clone(), rows.len()))
                .collect(),
        ),
        _ => None,
    })
}

pub(super) fn native_category_reports<'py>(
    reporter: &Bound<'py, PyAny>,
    category: &str,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let Some(index) = native_stats_owner(reporter)? else {
        return Ok(None);
    };
    let rows = {
        let index = index.borrow();
        match &index.terminal.buckets {
            Some(TerminalBuckets::Native { buckets, .. }) => buckets
                .iter()
                .find_map(|(name, rows)| (name == category).then_some(rows.clone()))
                .unwrap_or_default(),
            _ => return Ok(None),
        }
    };
    let _pins = NativeSessionReports::pin_rows(&index, &rows);
    let reports = PyList::empty(reporter.py());
    for row in rows {
        let object = match NativeSessionReports::materialize(&index, row) {
            Ok(object) => object,
            Err(error) => {
                if index.borrow().terminal.public(reporter.py()).is_some() {
                    return Ok(None);
                }
                return Err(error);
            }
        };
        reports.append(object)?;
    }
    if index.borrow().terminal.public(reporter.py()).is_some() {
        return Ok(None);
    }
    Ok(Some(reports))
}

/// The terminal's default `_add_stats` operation, exposed as a descriptor.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeTerminalAddStats {
    program: Mutex<Option<crate::hooks::native_caller::BoundProgram>>,
}

impl NativeTerminalAddStats {
    fn program(&self, py: Python<'_>) -> PyResult<crate::hooks::native_caller::BoundProgram> {
        self.program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|program| program.clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("terminal operation is closed")
            })
    }
}

#[pymethods]
impl NativeTerminalAddStats {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Py<Self>> {
        let owner = Py::new(
            py,
            Self {
                program: Mutex::new(None),
            },
        )?;
        let program = crate::hooks::native_caller::BoundProgram::create(
            owner.bind(py).as_any(),
            "add_stats",
        )?;
        *owner
            .borrow(py)
            .program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(program);
        Ok(owner)
    }

    #[expect(clippy::unused_self, reason = "the descriptor owns the bound callable")]
    fn add_stats(
        &self,
        reporter: &Bound<'_, PyAny>,
        category: &Bound<'_, PyAny>,
        items: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        reporter
            .getattr("_status_index")?
            .call_method1("terminal_add_stats", (reporter, category, items))?;
        Ok(())
    }

    #[pyo3(signature = (*args, **kwargs))]
    fn __call__(
        slf: &Bound<'_, Self>,
        args: &Bound<'_, PyTuple>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<PyAny>> {
        let function = slf.borrow().program(slf.py())?.function(slf.py());
        Ok(function.bind(slf.py()).call(args, kwargs)?.unbind())
    }

    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let function = slf.borrow().program(slf.py())?.function(slf.py());
        Ok(slf
            .py()
            .import("inspect")?
            .call_method1("signature", (function,))?
            .unbind())
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        if instance.is_none() {
            return Ok(slf.clone().into_any().unbind());
        }
        let program = slf.borrow().program(slf.py())?;
        program.bind(slf.py(), instance)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 requires its traversal visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(program) = self.program.try_lock()
            && let Some(program) = &*program
        {
            program.traverse(&visit)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let program = self
            .program
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        drop(program);
    }
}

/// Resolve the currently installed callback; replacements remain ordinary Python calls.
fn resolve_native_add_stats(
    reporter: &Bound<'_, PyAny>,
    callback: &Bound<'_, PyAny>,
) -> PyResult<Option<Py<NativeTerminalAddStats>>> {
    let py = reporter.py();
    let method_type = crate::hooks::nativeop::method_type(py)?;
    if !callback.get_type().is(method_type.bind(py))
        || !callback
            .getattr(pyo3::intern!(py, "__self__"))?
            .is(reporter)
    {
        return Ok(None);
    }
    let function = callback.getattr(pyo3::intern!(py, "__func__"))?;
    if !function.is_instance_of::<PyCFunction>()
        || !function.getattr("__name__")?.eq("add_stats")?
    {
        return Ok(None);
    }
    let owner = function.getattr("__self__")?;
    if !owner.is_exact_instance_of::<NativeTerminalAddStats>() {
        return Ok(None);
    }
    Ok(Some(owner.cast_into::<NativeTerminalAddStats>()?.unbind()))
}

/// The terminal reporter's registered logreport hook owns its live reporter.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeTerminalLogReport {
    reporter: Mutex<Option<Py<PyAny>>>,
}

/// Bind the terminal hook without retaining a self-cycle on the reporter.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeTerminalLogReportDescriptor;

/// Bind the terminal start hook to its registered reporter.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeTerminalLogStartDescriptor;

/// Bind one hook method of a fresh owner that holds only the reporter.
fn bind_log_hook(
    descriptor: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
    method: &str,
) -> PyResult<Py<PyAny>> {
    if instance.is_none() {
        return Ok(descriptor.clone().unbind());
    }
    let py = descriptor.py();
    let owner = Py::new(
        py,
        NativeTerminalLogReport {
            reporter: Mutex::new(Some(instance.clone().unbind())),
        },
    )?;
    Ok(owner.bind(py).getattr(method)?.unbind())
}

#[pymethods]
impl NativeTerminalLogStartDescriptor {
    #[new]
    fn new() -> Self {
        Self
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        bind_log_hook(slf.as_any(), instance, "pytest_runtest_logstart")
    }
}

#[pymethods]
impl NativeTerminalLogReportDescriptor {
    #[new]
    fn new() -> Self {
        Self
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        bind_log_hook(slf.as_any(), instance, "pytest_runtest_logreport")
    }
}

impl NativeTerminalLogReport {
    pub(in crate::independent) fn reporter(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.reporter
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|reporter| reporter.clone_ref(py))
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("terminal reporter is closed"))
    }

    pub(crate) fn start_handle(
        &self,
        py: Python<'_>,
        nodeid: &Bound<'_, PyAny>,
        location: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let location = location.cast::<pyo3::types::PyTuple>()?;
        let fspath = location.get_item(0)?;
        let lineno = location.get_item(1)?;
        let domain = location.get_item(2)?;
        let reporter = self.reporter(py)?;
        let reporter = reporter.bind(py);
        let index = reporter.getattr("_status_index")?;
        let callback = index.getattr("terminal_logstart")?;
        let location = pyo3::types::PyTuple::new(py, [fspath, lineno, domain])?;
        if index.is_exact_instance_of::<NativeSessionReports>()
            && callback.is_exact_instance_of::<PyCFunction>()
            && callback.getattr("__self__")?.is(&index)
            && callback.getattr("__name__")?.eq("terminal_logstart")?
        {
            return live::logstart(reporter, nodeid, location.as_any());
        }
        callback.call1((reporter, nodeid, location))?;
        Ok(())
    }
}

#[pymethods]
impl NativeTerminalLogReport {
    #[new]
    fn new(reporter: Py<PyAny>) -> Self {
        Self {
            reporter: Mutex::new(Some(reporter)),
        }
    }

    fn pytest_runtest_logreport(
        &self,
        py: Python<'_>,
        report: &Bound<'_, NativeIndependentReport>,
    ) -> PyResult<()> {
        let reporter = self.reporter(py)?;
        let index = reporter
            .bind(py)
            .getattr("_status_index")?
            .cast_into::<NativeSessionReports>()?;
        record(&index, report, reporter.bind(py))
    }

    fn pytest_runtest_logstart(
        &self,
        py: Python<'_>,
        nodeid: &Bound<'_, PyAny>,
        location: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let reporter = self.reporter(py)?;
        Module::TerminalResources
            .get(py)?
            .call_method1("logstart", (reporter, nodeid, location))?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 requires its traversal visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(reporter) = self.reporter.try_lock() {
            visit.call(&*reporter)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let reporter = self
            .reporter
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        drop(reporter);
    }
}

/// Snapshot resources only after the owner borrow has ended.
pub(super) type BucketSnapshot = Vec<(Py<PyAny>, Vec<Py<PyAny>>)>;

pub(super) fn snapshot_handles(stats: &Bound<'_, PyAny>) -> PyResult<BucketSnapshot> {
    let mut buckets = Vec::new();
    for pair in stats.call_method0("items")?.try_iter()? {
        let pair = pair?;
        let (category, reports) = pair.extract::<(Py<PyAny>, Bound<'_, PyAny>)>()?;
        let reports = reports
            .try_iter()?
            .map(|report| report.map(Bound::unbind))
            .collect::<PyResult<Vec<_>>>()?;
        buckets.push((category, reports));
    }
    Ok(buckets)
}

pub(super) fn snapshot(py: Python<'_>, handles: BucketSnapshot) -> PyResult<Py<PyDict>> {
    let buckets = PyDict::new(py);
    for (category, reports) in handles {
        buckets.set_item(category, PyList::new(py, reports)?)?;
    }
    Ok(buckets.unbind())
}

pub(super) fn reports(stats: &Bound<'_, PyAny>) -> PyResult<Vec<Py<PyAny>>> {
    let mut reports = Vec::new();
    for bucket in stats.call_method0("values")?.try_iter()? {
        for report in bucket?.try_iter()? {
            reports.push(report?.unbind());
        }
    }
    Ok(reports)
}

/// Ordered terminal operands retain the same snapshot through callbacks.
pub(crate) enum ReportSource {
    Native(super::ReportHandle),
    Python(Py<PyAny>),
}

impl ReportSource {
    pub(crate) fn extras(&self, py: Python<'_>) -> PyResult<ReportExtras> {
        match self {
            Self::Native(report) => report.extras(py),
            Self::Python(report) => Ok(ReportExtras::Python(
                report.bind(py).getattr("__dict__")?.unbind(),
            )),
        }
    }

    pub(crate) fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::Native(report) => Ok(report.to_python(py)?.into_any()),
            Self::Python(report) => Ok(report.clone_ref(py)),
        }
    }
}

pub(crate) fn report_sources(
    history: &Bound<'_, NativeSessionReports>,
) -> PyResult<Vec<ReportSource>> {
    let (ids, public) = {
        let owner = history.borrow();
        match &owner.terminal.buckets {
            Some(TerminalBuckets::Native { buckets, .. }) => (
                buckets
                    .iter()
                    .flat_map(|(_, rows)| rows.iter().copied())
                    .collect::<Vec<_>>(),
                None,
            ),
            Some(TerminalBuckets::Public(stats)) => {
                (Vec::new(), Some(stats.clone_ref(history.py())))
            }
            None => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "terminal statistics are no longer available",
                ));
            }
        }
    };
    if let Some(public) = public {
        return Ok(reports(public.bind(history.py()))?
            .into_iter()
            .map(ReportSource::Python)
            .collect());
    }
    Ok(NativeSessionReports::pin_rows(history, &ids)
        .into_iter()
        .map(ReportSource::Native)
        .collect())
}

/// Custom status operands retain their actual `CPython` consumer frame.
pub(super) fn record(
    index: &Bound<'_, super::NativeSessionReports>,
    report: &Bound<'_, NativeIndependentReport>,
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let result = live::status(report.as_any(), reporter)?;
    consume_result(index, report, reporter, result)
}

fn consume_result(
    index: &Bound<'_, super::NativeSessionReports>,
    report: &Bound<'_, NativeIndependentReport>,
    reporter: &Bound<'_, PyAny>,
    result: Py<PyAny>,
) -> PyResult<()> {
    let py = index.py();
    if live::consume_plain(report.as_any(), reporter, result.bind(py))? {
        return Ok(());
    }
    // Transfer the original result into the Python expression without an extra
    // retained tuple reference: tuple subclasses can observe their destruction.
    let pending = Mutex::new(Some(result));
    let status = PyCFunction::new_closure(py, None, None, move |_, _| {
        let result = pending
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        result.ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("terminal status result was already consumed")
        })
    })?;
    Module::TerminalResources.get(py)?.call_method1(
        pyo3::intern!(py, "consume"),
        (index, report, reporter, status),
    )?;
    Ok(())
}

fn status_handle(
    report: &super::ReportHandle,
    reporter: &Bound<'_, PyAny>,
) -> PyResult<crate::hooks::invocation::HookReturn> {
    use crate::hooks::invocation::{CallArguments, HookReturn};
    use crate::hooks::native_caller::{ResolvedHookCall, resolve};

    let py = reporter.py();
    reporter.setattr("_tests_ran", true)?;
    let callback = reporter
        .getattr("config")?
        .getattr("hook")?
        .getattr("pytest_report_teststatus")?;
    let config = reporter.getattr("config")?;
    match resolve(&callback)? {
        ResolvedHookCall::Native {
            registry,
            name,
            removed,
        } => {
            let mut args = CallArguments::RawStatus {
                report,
                config: &config,
            };
            crate::hooks::NativeHookRegistry::call_subset_arguments(
                registry.bind(py),
                &name,
                &mut args,
                removed.bind(py),
                false,
            )
        }
        foreign => {
            let kwargs = PyDict::new(py);
            kwargs.set_item("report", report.to_python(py)?)?;
            kwargs.set_item("config", &config)?;
            let result = match foreign {
                ResolvedHookCall::ForeignCaller(callback) => {
                    callback.bind(py).call((), Some(&kwargs))?
                }
                ResolvedHookCall::ForeignRegistry {
                    call,
                    name,
                    removed,
                } => call.bind(py).call1((name, kwargs, removed))?,
                ResolvedHookCall::Native { .. } => {
                    return Err(pyo3::exceptions::PyRuntimeError::new_err(
                        "unexpected native hook resolution",
                    ));
                }
            };
            Ok(HookReturn::Python(result.unbind()))
        }
    }
}

/// Execute the terminal hook against a report row until a callback needs it.
pub(crate) fn consume_handle(
    owner: &NativeTerminalLogReport,
    report: &super::ReportHandle,
    py: Python<'_>,
) -> PyResult<()> {
    use crate::hooks::invocation::HookReturn;

    let reporter = owner.reporter(py)?;
    let reporter = reporter.bind(py);
    let result = status_handle(report, reporter)?;
    let (category, letter, word) = match result {
        HookReturn::NativeStatus(status) if report.is_native(py) => status,
        result => {
            let result = result.into_python(py)?;
            let object = report.to_python(py)?;
            return consume_result(report.history(py), object.bind(py), reporter, result);
        }
    };
    let callback = reporter.getattr("_add_stats")?;
    let added = if resolve_native_add_stats(reporter, &callback)?.is_some() {
        if reporter.getattr("_status_index")?.is(report.history(py)) {
            append_handle(py, report, category)?
        } else {
            None
        }
    } else {
        None
    };
    match added {
        Some(true) => {
            reporter.call_method0("_set_main_color")?;
        }
        Some(false) => {}
        None => {
            let items = PyList::new(py, [report.to_python(py)?])?;
            callback.call1((category, items))?;
        }
    }
    if letter.is_empty() && word.is_empty() {
        return Ok(());
    }
    let was_xfail = report.hasattr(py, "wasxfail")?;
    let passed = report.getattr(py, "passed")?.bind(py).is_truthy()?;
    let color = if passed {
        Some(if was_xfail { "yellow" } else { "green" })
    } else if report.getattr(py, "failed")?.bind(py).is_truthy()? {
        Some("red")
    } else if report.getattr(py, "skipped")?.bind(py).is_truthy()? {
        Some("yellow")
    } else {
        None
    };
    let markup = PyDict::new(py);
    if let Some(color) = color {
        markup.set_item(color, true)?;
    }
    let letter = pyo3::types::PyString::new(py, letter);
    let word = pyo3::types::PyString::new(py, word);
    live::emit_handle(
        report,
        reporter,
        letter.as_any(),
        word.as_any(),
        markup.as_any(),
    )
}

/// Stateless callbacks for the two separately registered core status providers.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeStatusCallbacks;

fn runner_status_values(failed: bool, skipped: bool) -> (&'static str, &'static str, &'static str) {
    if failed {
        ("error", "E", "ERROR")
    } else if skipped {
        ("skipped", "s", "SKIPPED")
    } else {
        ("", "", "")
    }
}

fn terminal_status_values(
    failed: bool,
    skipped: bool,
    is_call: bool,
) -> (&'static str, &'static str, &'static str) {
    if failed {
        if is_call {
            ("failed", "F", "FAILED")
        } else {
            ("error", "E", "ERROR")
        }
    } else if skipped {
        ("skipped", "s", "SKIPPED")
    } else {
        ("passed", ".", "PASSED")
    }
}

fn runner_status_public(
    report: &Bound<'_, PyAny>,
) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
    let py = report.py();
    let is_fixture_phase = {
        let when = report.getattr(pyo3::intern!(py, "when"))?;
        matches!(when.extract::<&str>()?, "setup" | "teardown")
    };
    if !is_fixture_phase {
        return Ok(None);
    }
    let failed = report.getattr(pyo3::intern!(py, "failed"))?.is_truthy()?;
    let skipped = !failed && report.getattr(pyo3::intern!(py, "skipped"))?.is_truthy()?;
    Ok(Some(runner_status_values(failed, skipped)))
}

fn terminal_status_public(
    report: &Bound<'_, PyAny>,
) -> PyResult<(&'static str, &'static str, &'static str)> {
    let py = report.py();
    let failed = report.getattr(pyo3::intern!(py, "failed"))?.is_truthy()?;
    let is_call = failed && report.getattr(pyo3::intern!(py, "when"))?.eq("call")?;
    let skipped = !failed && report.getattr(pyo3::intern!(py, "skipped"))?.is_truthy()?;
    Ok(terminal_status_values(failed, skipped, is_call))
}

impl NativeStatusCallbacks {
    pub(crate) fn runner_status_handle(
        py: Python<'_>,
        report: &super::ReportHandle,
    ) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
        if !report.is_native(py) {
            return runner_status_public(report.to_python(py)?.bind(py).as_any());
        }
        let facts = report.facts(py)?;
        if facts.phase == vfy_core::test_execution::ExecutionPhase::Call {
            return Ok(None);
        }
        let outcome = facts.outcome.report_outcome();
        Ok(Some(runner_status_values(
            outcome == vfy_core::selection::TestOutcome::Failed,
            outcome == vfy_core::selection::TestOutcome::Skipped,
        )))
    }

    pub(crate) fn terminal_status_handle(
        py: Python<'_>,
        report: &super::ReportHandle,
    ) -> PyResult<(&'static str, &'static str, &'static str)> {
        if !report.is_native(py) {
            return terminal_status_public(report.to_python(py)?.bind(py).as_any());
        }
        let facts = report.facts(py)?;
        let outcome = facts.outcome.report_outcome();
        Ok(terminal_status_values(
            outcome == vfy_core::selection::TestOutcome::Failed,
            outcome == vfy_core::selection::TestOutcome::Skipped,
            facts.phase == vfy_core::test_execution::ExecutionPhase::Call,
        ))
    }
}

#[pymethods]
impl NativeStatusCallbacks {
    #[expect(
        clippy::unused_self,
        reason = "the registered runner hook uses a bound native callback"
    )]
    fn runner_makereport(
        &self,
        item: &Bound<'_, PyAny>,
        call: &Bound<'_, super::call::NativeIndependentCallInfo>,
    ) -> PyResult<Py<super::NativeIndependentReport>> {
        super::make::from_call(item, call)
    }

    #[expect(
        clippy::unused_self,
        reason = "a bound callback lets the protocol shell preserve hook signatures"
    )]
    fn runner_status(
        &self,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
        runner_status_public(report)
    }

    #[expect(
        clippy::unused_self,
        reason = "a bound callback lets the protocol shell preserve hook signatures"
    )]
    fn terminal_status(
        &self,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<(&'static str, &'static str, &'static str)> {
        terminal_status_public(report)
    }
}

pub(crate) fn callbacks(py: Python<'_>) -> PyResult<Py<NativeStatusCallbacks>> {
    Py::new(py, NativeStatusCallbacks)
}
