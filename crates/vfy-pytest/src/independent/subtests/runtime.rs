//! `CPython` context objects and builtin provider callbacks.

use super::{SubtestClock, SubtestFailures, must_reraise, saferepr};
use crate::independent::capture::SubtestLogs;
use crate::independent::global_capture::SubtestOutput;
use crate::independent::reporting::{self, NativeIndependentReport, ReportHandle};
use crate::independent::{Collector, NativeIndependentRequest};
use crate::module_cache::Module;
use pyo3::exceptions::{PyAssertionError, PyAttributeError, PyBaseException};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;
use vfy_core::test_execution::{ExecutionOutcome, PhasePayload};

#[pyclass(
    frozen,
    unsendable,
    module = "_pytest.subtests",
    name = "SubtestContext"
)]
pub(crate) struct NativeSubtestContext {
    values: RefCell<Option<(Py<PyAny>, Py<PyDict>)>>,
}

impl NativeSubtestContext {
    pub(in crate::independent) fn handles(
        &self,
        py: Python<'_>,
    ) -> PyResult<(Py<PyAny>, Py<PyDict>)> {
        self.values
            .borrow()
            .as_ref()
            .map(|(msg, kwargs)| (msg.clone_ref(py), kwargs.clone_ref(py)))
            .ok_or_else(|| PyAttributeError::new_err("subtest context has been cleared"))
    }

    pub(in crate::independent) fn describe(&self, py: Python<'_>) -> PyResult<String> {
        let (msg, kwargs) = self.handles(py)?;
        let mut parts = Vec::new();
        if !msg.bind(py).is_none() {
            parts.push(format!("[{}]", msg.bind(py).str()?.to_str()?));
        }
        let mut parameters = Vec::new();
        for entry in kwargs.bind(py).as_any().call_method0("items")?.try_iter()? {
            let (key, value) = entry?.extract::<(String, Py<PyAny>)>()?;
            parameters.push(format!("{key}={}", value.bind(py).str()?.to_str()?));
        }
        if !parameters.is_empty() {
            parts.push(format!("({})", parameters.join(", ")));
        }
        Ok(if parts.is_empty() {
            "(<subtest>)".to_owned()
        } else {
            parts.join(" ")
        })
    }
}

fn frozen_error(py: Python<'_>, template: &str, name: &str) -> PyResult<()> {
    Err(PyErr::from_value(
        py.import("dataclasses")?
            .getattr("FrozenInstanceError")?
            .call1((PyString::new(py, template).call_method1("format", (name,))?,))?,
    ))
}

#[pymethods]
impl NativeSubtestContext {
    #[new]
    #[pyo3(signature = (*, msg, kwargs))]
    pub(in crate::independent) fn new(
        py: Python<'_>,
        msg: Py<PyAny>,
        kwargs: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        let values = PyDict::new(py);
        for entry in kwargs.call_method0("items")?.try_iter()? {
            let (key, value) = entry?.extract::<(Py<PyAny>, Py<PyAny>)>()?;
            values.set_item(key, saferepr::render(value.bind(py))?)?;
        }
        Ok(Self {
            values: RefCell::new(Some((msg, values.unbind()))),
        })
    }

    #[getter]
    fn msg(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.handles(py)?.0)
    }
    #[getter]
    fn kwargs(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        Ok(self.handles(py)?.1)
    }

    fn _to_json(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let (msg, kwargs) = self.handles(py)?;
        let data = PyDict::new(py);
        data.set_item("msg", msg)?;
        data.set_item("kwargs", kwargs)?;
        Ok(py
            .import("copy")?
            .call_method1("deepcopy", (data,))?
            .cast_into::<PyDict>()?
            .unbind())
    }

    #[classmethod]
    fn _from_json(
        class: &Bound<'_, pyo3::types::PyType>,
        data: &Bound<'_, PyDict>,
    ) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(class.py());
        kwargs.set_item("msg", data.as_any().get_item("msg")?)?;
        kwargs.set_item("kwargs", data.as_any().get_item("kwargs")?)?;
        Ok(class.call((), Some(&kwargs))?.unbind())
    }

    #[expect(
        clippy::unused_self,
        reason = "PyO3 requires an instance descriptor for this frozen attribute protocol"
    )]
    fn __setattr__(&self, py: Python<'_>, name: &str, _value: &Bound<'_, PyAny>) -> PyResult<()> {
        frozen_error(py, "cannot assign to field {!r}", name)
    }
    #[expect(
        clippy::unused_self,
        reason = "PyO3 requires an instance descriptor for this frozen attribute protocol"
    )]
    fn __delattr__(&self, py: Python<'_>, name: &str) -> PyResult<()> {
        frozen_error(py, "cannot delete field {!r}", name)
    }
    fn __hash__(&self, py: Python<'_>) -> PyResult<isize> {
        let (msg, kwargs) = self.handles(py)?;
        Module::Builtins
            .get(py)?
            .call_method1("hash", ((msg, kwargs),))?
            .extract()
    }

    fn __richcmp__(
        &self,
        py: Python<'_>,
        other: &Bound<'_, PyAny>,
        operation: pyo3::basic::CompareOp,
    ) -> PyResult<Py<PyAny>> {
        let Ok(other) = other.cast::<Self>() else {
            return Ok(py.NotImplemented());
        };
        if !matches!(
            operation,
            pyo3::basic::CompareOp::Eq | pyo3::basic::CompareOp::Ne
        ) {
            return Ok(py.NotImplemented());
        }
        let (msg, kwargs) = self.handles(py)?;
        let (other_msg, other_kwargs) = other.borrow().handles(py)?;
        let equal =
            msg.bind(py).eq(other_msg.bind(py))? && kwargs.bind(py).eq(other_kwargs.bind(py))?;
        Ok((equal == matches!(operation, pyo3::basic::CompareOp::Eq))
            .into_pyobject(py)?
            .to_owned()
            .into_any()
            .unbind())
    }

    fn __repr__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (msg, kwargs) = self.handles(py)?;
        Ok(PyString::new(py, "SubtestContext(msg={!r}, kwargs={!r})")
            .call_method1("format", (msg, kwargs))?
            .unbind())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(values) = self.values.try_borrow()
            && let Some((msg, kwargs)) = &*values
        {
            visit.call(msg)?;
            visit.call(kwargs)?;
        }
        Ok(())
    }
    fn __clear__(&self) {
        let values = self.values.borrow_mut().take();
        drop(values);
    }
}

#[pyclass(frozen, unsendable, module = "vfy._native._pytest")]
struct NativeSubtestFailures {
    state: RefCell<SubtestFailures>,
}

#[pymethods]
impl NativeSubtestFailures {
    fn __getitem__(&self, nodeid: String) -> usize {
        *self.state.borrow_mut().by_nodeid.entry(nodeid).or_default()
    }
    fn __setitem__(&self, nodeid: String, value: usize) {
        self.state.borrow_mut().by_nodeid.insert(nodeid, value);
    }
    fn __len__(&self) -> usize {
        self.state.borrow().by_nodeid.len()
    }
    fn __contains__(&self, nodeid: &str) -> bool {
        self.state.borrow().by_nodeid.contains_key(nodeid)
    }
    fn items(&self) -> Vec<(String, usize)> {
        self.state
            .borrow()
            .by_nodeid
            .iter()
            .map(|(key, value)| (key.clone(), *value))
            .collect()
    }
}

fn failures(config: &Bound<'_, PyAny>) -> PyResult<Py<NativeSubtestFailures>> {
    let key = config
        .py()
        .import("vfy.independent.subtests_provider")?
        .getattr("failed_subtests_key")?;
    config
        .getattr("stash")?
        .get_item(key)?
        .extract()
        .map_err(Into::into)
}

/// The owner objects that `Subtests` and its context managers share.
struct SubtestsObjects {
    ihook: Py<PyAny>,
    suspend: Py<PyAny>,
    request: Py<NativeIndependentRequest>,
}

impl SubtestsObjects {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            ihook: self.ihook.clone_ref(py),
            suspend: self.suspend.clone_ref(py),
            request: self.request.clone_ref(py),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.ihook)?;
        visit.call(&self.suspend)?;
        visit.call(&self.request)
    }
}

/// The owner objects plus the per-subtest values of the stock `_SubTestContextManager`.
struct ManagerObjects {
    subtests: SubtestsObjects,
    config: Py<PyAny>,
    msg: Py<PyAny>,
    kwargs: Py<PyDict>,
}

impl ManagerObjects {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            subtests: self.subtests.clone_ref(py),
            config: self.config.clone_ref(py),
            msg: self.msg.clone_ref(py),
            kwargs: self.kwargs.clone_ref(py),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.subtests.traverse(visit)?;
        visit.call(&self.config)?;
        visit.call(&self.msg)?;
        visit.call(&self.kwargs)
    }
}

#[pyclass(
    frozen,
    unsendable,
    dict,
    weakref,
    module = "_pytest.subtests",
    name = "Subtests"
)]
pub(crate) struct NativeSubtests {
    objects: RefCell<Option<SubtestsObjects>>,
}

impl NativeSubtests {
    fn objects(&self, py: Python<'_>) -> PyResult<SubtestsObjects> {
        self.objects
            .borrow()
            .as_ref()
            .map(|objects| objects.clone_ref(py))
            .ok_or_else(|| PyAssertionError::new_err("subtests owner has been cleared"))
    }
}

#[pymethods]
impl NativeSubtests {
    #[new]
    #[expect(
        clippy::used_underscore_binding,
        reason = "The pinned public constructor names this keyword _ispytest"
    )]
    #[pyo3(signature = (ihook, suspend_capture_ctx, request, *, _ispytest=false))]
    fn new(
        py: Python<'_>,
        ihook: Py<PyAny>,
        suspend_capture_ctx: Py<PyAny>,
        request: Py<NativeIndependentRequest>,
        _ispytest: bool,
    ) -> PyResult<Self> {
        py.import("vfy.independent.fixtures")?
            .call_method1("_private_api", (_ispytest,))?;
        Ok(Self {
            objects: RefCell::new(Some(SubtestsObjects {
                ihook,
                suspend: suspend_capture_ctx,
                request,
            })),
        })
    }
    #[getter]
    fn _request(&self, py: Python<'_>) -> PyResult<Py<NativeIndependentRequest>> {
        Ok(self.objects(py)?.request)
    }
    #[getter]
    fn _ihook(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.objects(py)?.ihook)
    }
    #[getter]
    fn _suspend_capture_ctx(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Ok(self.objects(py)?.suspend)
    }
    #[pyo3(signature = (msg=None, **kwargs))]
    fn test(
        slf: &Bound<'_, Self>,
        msg: Option<Py<PyAny>>,
        kwargs: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<NativeSubtestContextManager> {
        let py = slf.py();
        let subtests = slf.borrow().objects(py)?;
        let config = subtests.request.bind(py).getattr("config")?.unbind();
        Ok(NativeSubtestContextManager {
            objects: RefCell::new(Some(ManagerObjects {
                subtests,
                config,
                msg: msg.unwrap_or_else(|| py.None()),
                kwargs: kwargs
                    .map_or_else(|| PyDict::new(py), Clone::clone)
                    .unbind(),
            })),
            entered: RefCell::new(None),
        })
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_borrow()
            && let Some(objects) = &*objects
        {
            objects.traverse(&visit)?;
        }
        Ok(())
    }
    fn __clear__(&self) {
        let objects = self.objects.borrow_mut().take();
        drop(objects);
    }
}

struct EnteredSubtest {
    clock: SubtestClock,
    output: SubtestOutput,
    logs: SubtestLogs,
}

#[pyclass(
    frozen,
    unsendable,
    dict,
    weakref,
    module = "_pytest.subtests",
    name = "_SubTestContextManager"
)]
pub(crate) struct NativeSubtestContextManager {
    objects: RefCell<Option<ManagerObjects>>,
    entered: RefCell<Option<EnteredSubtest>>,
}

impl NativeSubtestContextManager {
    fn objects(&self, py: Python<'_>) -> PyResult<ManagerObjects> {
        self.objects
            .borrow()
            .as_ref()
            .map(|objects| objects.clone_ref(py))
            .ok_or_else(|| PyAttributeError::new_err("subtest context manager has been cleared"))
    }

    fn field<T>(
        &self,
        py: Python<'_>,
        select: impl FnOnce(&ManagerObjects) -> &Py<T>,
    ) -> PyResult<Py<T>> {
        self.objects
            .borrow()
            .as_ref()
            .map(|objects| select(objects).clone_ref(py))
            .ok_or_else(|| PyAttributeError::new_err("subtest context manager has been cleared"))
    }

    /// Dispatch the logreport with pytest's capture suspended, then restore capture.
    fn dispatch_logreport(&self, py: Python<'_>, report: &Bound<'_, PyAny>) -> PyResult<()> {
        let objects = self.objects(py)?;
        let suspension = objects.subtests.suspend.bind(py).call0()?;
        suspension.call_method0("__enter__")?;
        let dispatched = (|| {
            let kwargs = PyDict::new(py);
            kwargs.set_item("report", report)?;
            objects
                .subtests
                .ihook
                .bind(py)
                .getattr("pytest_runtest_logreport")?
                .call((), Some(&kwargs))
        })();
        let restored = match &dispatched {
            Ok(_) => suspension.call_method1("__exit__", (py.None(), py.None(), py.None())),
            Err(error) => suspension.call_method1(
                "__exit__",
                (error.get_type(py), error.value(py), error.traceback(py)),
            ),
        };
        let suppressed = restored?.is_truthy()?;
        if !suppressed {
            dispatched?;
        }
        Ok(())
    }
}

#[pymethods]
impl NativeSubtestContextManager {
    #[getter]
    fn msg(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.field(py, |objects| &objects.msg)
    }
    #[getter]
    fn kwargs(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        self.field(py, |objects| &objects.kwargs)
    }
    #[getter]
    fn request(&self, py: Python<'_>) -> PyResult<Py<NativeIndependentRequest>> {
        self.field(py, |objects| &objects.subtests.request)
    }
    #[getter]
    fn config(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.field(py, |objects| &objects.config)
    }
    #[getter]
    fn ihook(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.field(py, |objects| &objects.subtests.ihook)
    }
    #[getter]
    fn suspend_capture_ctx(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.field(py, |objects| &objects.subtests.suspend)
    }

    #[new]
    fn new(
        ihook: Py<PyAny>,
        msg: Py<PyAny>,
        kwargs: Py<PyDict>,
        suspend_capture_ctx: Py<PyAny>,
        request: Py<NativeIndependentRequest>,
        config: Py<PyAny>,
    ) -> Self {
        Self {
            objects: RefCell::new(Some(ManagerObjects {
                subtests: SubtestsObjects {
                    ihook,
                    suspend: suspend_capture_ctx,
                    request,
                },
                config,
                msg,
                kwargs,
            })),
            entered: RefCell::new(None),
        }
    }

    fn __enter__(&self, py: Python<'_>) -> PyResult<()> {
        let previous = self.entered.borrow_mut().take();
        drop(previous);
        let clock = SubtestClock::start();
        let config = self
            .objects(py)?
            .subtests
            .request
            .bind(py)
            .getattr("config")?;
        let output = SubtestOutput::start(&config)?;
        let logs = SubtestLogs::start(&config)?;
        *self.entered.borrow_mut() = Some(EnteredSubtest {
            clock,
            output,
            logs,
        });
        Ok(())
    }
    fn __exit__(
        &self,
        py: Python<'_>,
        _kind: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let entered = self.entered.borrow_mut().take().ok_or_else(|| {
            PyAttributeError::new_err("subtest context manager has not been entered")
        })?;
        let error = if value.is_none() {
            None
        } else {
            Some(PyErr::from_value(
                value.cast::<PyBaseException>()?.as_any().clone(),
            ))
        };
        let logs = entered.logs.complete(py);
        let mut capture = entered.output.complete(py)?;
        capture.log = logs?;
        let completion = entered.clock.complete(
            error
                .as_ref()
                .map_or(Ok(()), |error| Err(error.clone_ref(py))),
            PhasePayload::default(),
        );
        let objects = self.objects(py)?;
        let request = objects.subtests.request.bind(py);
        let item = request.getattr("node")?;
        let origin = request.borrow().report_source(py)?;
        let ihook = objects.subtests.ihook.bind(py);
        let (base, call) = reporting::make_subtest_report(py, ihook, &item, completion, origin)?;
        // The pinned builtin evaluates kwargs repr only after makereport returns.
        let data = NativeSubtestContext::new(
            py,
            objects.msg.clone_ref(py),
            objects.kwargs.bind(py).as_any(),
        )?;
        let report = reporting::subtest_report::clone_report(
            py,
            base.bind(py),
            Py::new(py, data)?,
            &capture,
        )?;
        if report.bind(py).getattr("failed")?.is_truthy()? {
            let index = failures(objects.config.bind(py))?;
            let nodeid = item.getattr("nodeid")?.extract::<String>()?;
            index
                .borrow(py)
                .state
                .borrow_mut()
                .record(&nodeid, ExecutionOutcome::Failed)?;
        }
        self.dispatch_logreport(py, report.bind(py).as_any())?;
        if reporting::interaction::eligible(py, call.bind(py).as_any(), report.bind(py).as_any())? {
            reporting::interaction::dispatch_subtest(
                py,
                ihook,
                &item,
                call.bind(py).as_any(),
                report.bind(py).as_any(),
            )?;
        }
        let Some(error) = error else {
            return Ok(true);
        };
        if must_reraise(py, objects.config.bind(py), &error)? {
            return Ok(false);
        }
        let session = request.getattr("session")?;
        Ok(!session.getattr("shouldfail")?.is_truthy()?)
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(objects) = self.objects.try_borrow()
            && let Some(objects) = &*objects
        {
            objects.traverse(&visit)?;
        }
        if let Ok(entered) = self.entered.try_borrow()
            && let Some(entered) = &*entered
        {
            entered.output.traverse(&visit)?;
            entered.logs.traverse(&visit)?;
        }
        Ok(())
    }
    fn __clear__(&self) {
        let entered = self.entered.borrow_mut().take();
        let objects = self.objects.borrow_mut().take();
        drop((entered, objects));
    }
}

#[pyfunction]
fn configure_subtests(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = config.py();
    let key = Module::Subtests.get(py)?.getattr("failed_subtests_key")?;
    config.getattr("stash")?.set_item(
        key,
        Py::new(
            py,
            NativeSubtestFailures {
                state: RefCell::new(SubtestFailures::default()),
            },
        )?,
    )
}

#[pyfunction(name = "create_independent_subtests")]
fn create_subtests(
    py: Python<'_>,
    request: Py<NativeIndependentRequest>,
) -> PyResult<NativeSubtests> {
    let item = request.bind(py).getattr("node")?;
    let manager = item
        .getattr("config")?
        .getattr("pluginmanager")?
        .call_method1("get_plugin", ("capturemanager",))?;
    let suspend = if manager.is_none() {
        py.import("contextlib")?.getattr("nullcontext")?
    } else {
        manager.getattr("suspended")?
    };
    NativeSubtests::new(
        py,
        item.getattr("ihook")?.unbind(),
        suspend.unbind(),
        request,
        true,
    )
}

#[pyfunction]
fn report_subtest_status(
    report: &Bound<'_, PyAny>,
    config: &Bound<'_, PyAny>,
) -> PyResult<Option<(String, String, String)>> {
    status(StatusReport::Python(report), config)
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeSubtestsStatus;

#[pymethods]
impl NativeSubtestsStatus {
    #[expect(
        clippy::unused_self,
        reason = "hook admission identifies the bound native callback through its owner"
    )]
    fn pytest_report_teststatus(
        &self,
        report: &Bound<'_, PyAny>,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(String, String, String)>> {
        status(StatusReport::Python(report), config)
    }
}

impl NativeSubtestsStatus {
    pub(crate) fn status_handle(
        py: Python<'_>,
        report: &ReportHandle,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<Option<(String, String, String)>> {
        status(StatusReport::Native(py, report), config)
    }
}

#[derive(Clone, Copy)]
enum StatusReport<'a, 'py> {
    Python(&'a Bound<'py, PyAny>),
    Native(Python<'py>, &'a ReportHandle),
}

impl<'py> StatusReport<'_, 'py> {
    fn py(&self) -> Python<'py> {
        match self {
            Self::Python(report) => report.py(),
            Self::Native(py, _) => *py,
        }
    }

    fn getattr(&self, name: &str) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Python(report) => report.getattr(name),
            Self::Native(py, report) => Ok(report.getattr(*py, name)?.into_bound(*py)),
        }
    }

    fn hasattr(&self, name: &str) -> PyResult<bool> {
        match self {
            Self::Python(report) => report.hasattr(name),
            Self::Native(py, report) => report.hasattr(*py, name),
        }
    }

    fn setattr(&self, name: &str, value: &Bound<'py, PyAny>) -> PyResult<()> {
        match self {
            Self::Python(report) => report.setattr(name, value),
            Self::Native(py, report) => report.setattr(*py, name, value),
        }
    }

    fn context(&self) -> PyResult<Option<Py<NativeSubtestContext>>> {
        match self {
            Self::Python(report) => Ok(report
                .cast::<NativeIndependentReport>()
                .ok()
                .and_then(|report| report.borrow().subtest_context(report.py()))),
            Self::Native(py, report) => report.subtest_context(*py),
        }
    }
}

fn status(
    report: StatusReport<'_, '_>,
    config: &Bound<'_, PyAny>,
) -> PyResult<Option<(String, String, String)>> {
    let py = report.py();
    if report.getattr("when")?.ne("call")? {
        return Ok(None);
    }
    let quiet = config.call_method1("get_verbosity", ("subtests",))?.eq(0)?;
    let context = report.context()?;
    if let Some(context) = context {
        let outcome = report.getattr("outcome")?.extract::<String>()?;
        let description = context.borrow(py).describe(py)?;
        let outcome = if report.hasattr("wasxfail")? {
            // Non-quiet output reports only skipped expected failures.
            if !quiet && outcome != "skipped" {
                return Ok(None);
            }
            ExecutionOutcome::XFailed
        } else if report.getattr("failed")?.is_truthy()? {
            let status = super::status(ExecutionOutcome::Failed, &description, quiet)
                .expect("failed subtests always have a status");
            return Ok(Some((outcome, status.letter.to_owned(), status.word)));
        } else if report.getattr("passed")?.is_truthy()? {
            ExecutionOutcome::Passed
        } else if report.getattr("skipped")?.is_truthy()? {
            ExecutionOutcome::Skipped
        } else {
            return Ok(None);
        };
        return Ok(super::status(outcome, &description, quiet).map(|status| {
            (
                status.category.to_owned(),
                status.letter.to_owned(),
                status.word,
            )
        }));
    }
    let index = failures(config)?;
    let nodeid = report.getattr("nodeid")?.extract::<String>()?;
    index
        .borrow(py)
        .state
        .borrow_mut()
        .by_nodeid
        .entry(nodeid.clone())
        .or_default();
    if report.getattr("passed")?.is_truthy()? {
        let detail = index
            .borrow(py)
            .state
            .borrow()
            .parent_detail(&nodeid, ExecutionOutcome::Passed);
        if let Some(detail) = detail {
            report.setattr("outcome", &PyString::new(py, "failed").into_any())?;
            report.setattr("longrepr", &PyString::new(py, &detail).into_any())?;
        }
    }
    Ok(None)
}

impl Collector<'_> {
    pub(in crate::independent) fn install_subtests_provider(
        &mut self,
        raw_plugins: &[String],
    ) -> PyResult<()> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyAssertionError::new_err("subtests require an active configuration"))?;
        let config = context.config.bind(self.py);
        let restore = self
            .py
            .import("vfy.independent.outcomes")?
            .call_method0("install_outcome_modules")?;
        if let Err(error) = config.call_method1("add_cleanup", (&restore,)) {
            let _ = restore.call0();
            return Err(error);
        }
        self.exclude_raw_provider(raw_plugins, "subtests")?;
        self.activate_subtests_provider()
    }
    pub(in crate::independent) fn activate_subtests_provider(&mut self) -> PyResult<()> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyAssertionError::new_err("subtests require an active configuration"))?;
        let manager = context.config.bind(self.py).getattr("pluginmanager")?;
        if manager
            .call_method1("is_blocked", ("subtests",))?
            .is_truthy()?
        {
            return Ok(());
        }
        let status = Py::new(self.py, NativeSubtestsStatus)?;
        let provider = Module::Subtests
            .get(self.py)?
            .getattr("SubtestsProvider")?
            .call1((
                wrap_pyfunction!(configure_subtests, self.py)?,
                status.bind(self.py).getattr("pytest_report_teststatus")?,
            ))?;
        if !manager
            .call_method1("register", (&provider, "subtests"))?
            .is_none()
        {
            self.plugins
                .insert("subtests".to_owned(), provider.unbind());
        }
        Ok(())
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(create_subtests, module)?)?;
    module.add_function(wrap_pyfunction!(configure_subtests, module)?)?;
    module.add_function(wrap_pyfunction!(report_subtest_status, module)?)?;
    module.add_class::<NativeSubtestContext>()?;
    module.add_class::<NativeSubtests>()?;
    module.add_class::<NativeSubtestContextManager>()?;
    module.add_class::<NativeSubtestFailures>()?;
    module.add_class::<NativeSubtestsStatus>()?;
    Ok(())
}
