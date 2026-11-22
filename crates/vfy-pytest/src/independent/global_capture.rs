//! Invocation-owned standard IO; native guards delimit collection and test phases.

use super::lifecycle::Context;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Mutex, MutexGuard};
use vfy_core::test_execution::PhasePayload;

#[cfg(unix)]
mod fd;
mod subtest;
mod sys;
pub(super) use subtest::SubtestOutput;

/// Direction of a global-capture state change.
#[derive(Clone, Copy, PartialEq, Eq)]
enum CaptureTransition {
    Resume,
    Suspend,
}

const SUSPENDED: u8 = 0;
const ACTIVE: u8 = 1;
const CHANGING: u8 = 2;
const CLOSED: u8 = 3;

pub(super) fn locked<'a, T>(mutex: &'a Mutex<T>, what: &str) -> PyResult<MutexGuard<'a, T>> {
    mutex
        .lock()
        .map_err(|_| PyRuntimeError::new_err(format!("{what} lock is poisoned")))
}

#[derive(Clone, Copy)]
#[repr(u8)]
enum CaptureMode {
    Fd,
    Sys,
    TeeSys,
    No,
}

impl CaptureMode {
    fn from_namespace(namespace: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = namespace.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item("fd_capture", namespace.getattr("capture")?.eq("fd")?)?;
        py.import("vfy.independent.stdio")?
            .getattr("prepare_stdio")?
            .call((), Some(&kwargs))?;
        match namespace.getattr("capture")?.extract::<String>()?.as_str() {
            "fd" => Ok(Self::Fd),
            "sys" => Ok(Self::Sys),
            "tee-sys" => Ok(Self::TeeSys),
            "no" => Ok(Self::No),
            value => Err(crate::IndependentUsageError::new_err(format!(
                "invalid capture method: {value}"
            ))),
        }
    }
}

enum OutputResource {
    #[cfg(unix)]
    Fd(Py<fd::NativeFdCapture>),
    Sys(Py<sys::NativeSysCapture>),
    #[cfg(not(unix))]
    Python(Py<PyAny>),
}

impl OutputResource {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => Self::Fd(output.clone_ref(py)),
            Self::Sys(output) => Self::Sys(output.clone_ref(py)),
            #[cfg(not(unix))]
            Self::Python(output) => Self::Python(output.clone_ref(py)),
        }
    }

    fn bind(&self, py: Python<'_>, owner: &Bound<'_, NativeGlobalCapture>) -> PyResult<()> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => output.get().bind(py, owner.as_any()),
            Self::Sys(output) => output.get().bind(py, owner.as_any()),
            #[cfg(not(unix))]
            Self::Python(output) => output.bind(py).call_method1("bind", (owner,)).map(drop),
        }
    }

    fn transition(&self, py: Python<'_>, transition: CaptureTransition) -> PyResult<()> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => match transition {
                CaptureTransition::Resume => output.get().resume(py),
                CaptureTransition::Suspend => output.get().suspend(py),
            },
            Self::Sys(output) => output.get().transition(py, transition),
            #[cfg(not(unix))]
            Self::Python(output) => output
                .bind(py)
                .call_method0(match transition {
                    CaptureTransition::Resume => "resume",
                    CaptureTransition::Suspend => "suspend",
                })
                .map(drop),
        }
    }

    fn readouterr(&self, py: Python<'_>) -> PyResult<(String, String)> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => output.get().readouterr(py),
            Self::Sys(output) => output.get().readouterr(py),
            #[cfg(not(unix))]
            Self::Python(output) => output.bind(py).call_method0("readouterr")?.extract(),
        }
    }

    fn write_original(&self, py: Python<'_>, out: &str, err: &str) -> PyResult<()> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => output.get().write_original(py, out, err),
            Self::Sys(output) => output.get().write_original(py, out, err),
            #[cfg(not(unix))]
            Self::Python(output) => output
                .bind(py)
                .call_method1("write_original", (out, err))
                .map(drop),
        }
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => output.get().close(py),
            Self::Sys(output) => output.get().close(py),
            #[cfg(not(unix))]
            Self::Python(output) => output.bind(py).call_method0("close").map(drop),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            #[cfg(unix)]
            Self::Fd(output) => visit.call(output),
            Self::Sys(output) => visit.call(output),
            #[cfg(not(unix))]
            Self::Python(output) => visit.call(output),
        }
    }
}

struct Resources {
    output: Option<OutputResource>,
    input: Option<Py<PyAny>>,
}

impl Resources {
    fn create(py: Python<'_>, mode: CaptureMode) -> PyResult<Self> {
        let adapter = py.import("vfy.independent.stdio")?;
        let output = match mode {
            CaptureMode::Fd => {
                #[cfg(unix)]
                {
                    Some(OutputResource::Fd(fd::NativeFdCapture::create(
                        py, &adapter,
                    )?))
                }
                #[cfg(not(unix))]
                {
                    Some(OutputResource::Python(
                        adapter.call_method0("FdCaptureIO")?.unbind(),
                    ))
                }
            }
            CaptureMode::Sys | CaptureMode::TeeSys => Some(OutputResource::Sys(
                sys::NativeSysCapture::create(py, &adapter, matches!(mode, CaptureMode::TeeSys))?,
            )),
            CaptureMode::No => None,
        };
        let input = match mode {
            CaptureMode::Fd | CaptureMode::Sys => {
                let kwargs = PyDict::new(py);
                kwargs.set_item("redirect_fd", matches!(mode, CaptureMode::Fd))?;
                match adapter.getattr("InputCaptureIO")?.call((), Some(&kwargs)) {
                    Ok(input) => Some(input),
                    Err(error) => {
                        if let Some(output) = &output {
                            output.close(py)?;
                        }
                        return Err(error);
                    }
                }
            }
            CaptureMode::TeeSys | CaptureMode::No => None,
        };
        Ok(Self {
            output,
            input: input.map(Bound::unbind),
        })
    }
}

/// The config cleanup stack closes this owner before its retained IO can be released.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeGlobalCapture {
    resources: Mutex<Option<Resources>>,
    fixture: Mutex<Option<Py<PyAny>>>,
    state: AtomicU8,
    prepared: AtomicBool,
    mode: AtomicU8,
}

impl NativeGlobalCapture {
    fn output(&self, py: Python<'_>) -> PyResult<Option<OutputResource>> {
        let resources = locked(&self.resources, "capture resource")?;
        let resources = resources
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("global capture is not initialized"))?;
        Ok(resources.output.as_ref().map(|output| output.clone_ref(py)))
    }

    fn transition(&self, py: Python<'_>, transition: CaptureTransition) -> PyResult<bool> {
        let (from, to) = match transition {
            CaptureTransition::Resume => (SUSPENDED, ACTIVE),
            CaptureTransition::Suspend => (ACTIVE, SUSPENDED),
        };
        match self
            .state
            .compare_exchange(from, CHANGING, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => {}
            Err(state)
                if state == to || (transition == CaptureTransition::Suspend && state == CLOSED) =>
            {
                return Ok(false);
            }
            Err(_) => {
                return Err(PyRuntimeError::new_err(
                    "capture is closed or changing state",
                ));
            }
        }
        let result = self
            .output(py)
            .and_then(|output| output.map_or(Ok(()), |output| output.transition(py, transition)));
        // A callback can close the owner while the transition is in flight.
        // Do not republish an active or suspended state after that close.
        let _ = self.state.compare_exchange(
            CHANGING,
            if result.is_ok() { to } else { from },
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        result.map(|()| true)
    }

    fn take(&self, py: Python<'_>) -> PyResult<(String, String)> {
        self.output(py)?.map_or_else(
            || Ok((String::new(), String::new())),
            |output| output.readouterr(py),
        )
    }

    pub(super) fn fixture(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        Ok(locked(&self.fixture, "capture fixture")?
            .as_ref()
            .map(|fixture| fixture.clone_ref(py)))
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let fixture = locked(&self.fixture, "capture fixture")?.take();
        let fixture_closed =
            fixture.map(|fixture| fixture.bind(py).call_method0("close").map(drop));
        let resources = locked(&self.resources, "capture resource")?.take();
        self.state.store(CLOSED, Ordering::Release);
        let Some(resources) = resources else {
            return Ok(());
        };
        // No resource lock is held while stream protocols or destructors run.
        let output = resources.output.map(|output| {
            let replay = (|| -> PyResult<()> {
                output.transition(py, CaptureTransition::Suspend)?;
                let (stdout, stderr) = output.readouterr(py)?;
                output.write_original(py, &stdout, &stderr)?;
                Ok(())
            })();
            output.close(py)?;
            replay
        });
        let input = resources
            .input
            .map(|input| input.bind(py).call_method0("close").map(drop));
        input.transpose()?;
        output.transpose()?;
        fixture_closed.transpose()?;
        Ok(())
    }
}

#[pymethods]
impl NativeGlobalCapture {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        self.close(py)
    }

    fn stop_global_capturing(&self, py: Python<'_>) -> PyResult<()> {
        self.close(py)
    }

    fn is_globally_capturing(&self) -> bool {
        self.mode.load(Ordering::Acquire) != CaptureMode::No as u8
    }

    fn is_capturing(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.is_globally_capturing() {
            return Ok("global".into_pyobject(py)?.into_any().unbind());
        }
        if let Some(fixture) = self.fixture(py)? {
            let name = fixture
                .bind(py)
                .getattr("_vfy_capture_name")?
                .extract::<String>()?;
            return Ok(format!("fixture {name}")
                .into_pyobject(py)?
                .into_any()
                .unbind());
        }
        Ok(false.into_pyobject(py)?.to_owned().into_any().unbind())
    }

    fn fixture_started(&self, py: Python<'_>, resource: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.state.load(Ordering::Acquire) == CLOSED {
            return Err(PyRuntimeError::new_err("capture is closed"));
        }
        let previous = {
            let mut fixture = locked(&self.fixture, "capture fixture")?;
            match fixture.as_ref() {
                Some(previous) if previous.bind(py).is(resource) => return Ok(()),
                Some(previous) => previous.clone_ref(py),
                None => {
                    *fixture = Some(resource.clone().unbind());
                    return Ok(());
                }
            }
        };
        let previous = previous
            .bind(py)
            .getattr("_vfy_capture_name")?
            .extract::<String>()?;
        let requested = resource.getattr("_vfy_capture_name")?.extract::<String>()?;
        let message = format!("cannot use {requested} and {previous} at the same time");
        match resource.getattr("request") {
            Ok(request)
                if request.is_instance_of::<super::execution::NativeIndependentRequest>() =>
            {
                Err(super::fixture_lookup::raiseerror(
                    py,
                    &request,
                    PyString::new(py, &message).as_any(),
                )?)
            }
            Err(error) if !error.is_instance_of::<PyAttributeError>(py) => Err(error),
            _ => Err(PyValueError::new_err(message)),
        }
    }

    fn fixture_stopped(&self, py: Python<'_>, resource: &Bound<'_, PyAny>) -> PyResult<()> {
        let removed = locked(&self.fixture, "capture fixture")?
            .take_if(|current| current.bind(py).is(resource));
        drop(removed);
        Ok(())
    }

    fn initial(
        slf: PyRef<'_, Self>,
        py: Python<'_>,
        config: &Bound<'_, PyAny>,
    ) -> PyResult<NativeInitialCapture> {
        let owner: Py<Self> = slf.into();
        let namespace = config.getattr("known_args_namespace")?.unbind();
        prepare(py, config, namespace.bind(py), &owner)?;
        Ok(NativeInitialCapture {
            owner,
            namespace: Mutex::new(Some(namespace)),
            complete: AtomicBool::new(false),
        })
    }

    fn suspended(slf: PyRef<'_, Self>, py: Python<'_>) -> PyResult<NativeCaptureSuspension> {
        let owner = slf.into();
        NativeCaptureSuspension::start(py, &owner)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(fixture) = self.fixture.try_lock()
            && let Some(fixture) = fixture.as_ref()
        {
            visit.call(fixture)?;
        }
        if let Ok(resources) = self.resources.try_lock()
            && let Some(resources) = resources.as_ref()
        {
            if let Some(output) = &resources.output {
                output.traverse(&visit)?;
            }
            if let Some(input) = &resources.input {
                visit.call(input)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        let _ = self.close(py);
    }
}

/// Both fixture-disabled IO and live logging use this same native suspension.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeCaptureSuspension {
    owner: Py<NativeGlobalCapture>,
    restore: AtomicBool,
    fixture: Mutex<Option<Py<PyAny>>>,
}

impl NativeCaptureSuspension {
    pub(super) fn start(py: Python<'_>, owner: &Py<NativeGlobalCapture>) -> PyResult<Self> {
        let fixture = match owner.get().fixture(py)? {
            Some(fixture)
                if fixture
                    .bind(py)
                    .call_method0("_is_started")?
                    .extract::<bool>()? =>
            {
                Some(fixture)
            }
            _ => None,
        };
        if let Some(fixture) = &fixture
            && let Err(error) = fixture.bind(py).call_method0("_stop")
        {
            owner.get().fixture_started(py, fixture.bind(py))?;
            return Err(error);
        }
        let restore = match owner.get().transition(py, CaptureTransition::Suspend) {
            Ok(restore) => restore,
            Err(error) => {
                if let Some(fixture) = fixture {
                    fixture.bind(py).call_method0("_resume")?;
                }
                return Err(error);
            }
        };
        Ok(Self {
            owner: owner.clone_ref(py),
            restore: AtomicBool::new(restore),
            fixture: Mutex::new(fixture),
        })
    }

    pub(super) fn finish(&self, py: Python<'_>) -> PyResult<()> {
        let fixture = locked(&self.fixture, "capture fixture")?.take();
        let resumed = if self.restore.swap(false, Ordering::AcqRel) {
            self.owner
                .get()
                .transition(py, CaptureTransition::Resume)
                .map(drop)
        } else {
            Ok(())
        };
        let fixture_resumed =
            fixture.map(|fixture| fixture.bind(py).call_method0("_resume").map(drop));
        fixture_resumed.transpose()?;
        resumed
    }
}

#[pymethods]
impl NativeCaptureSuspension {
    #[expect(
        clippy::unused_self,
        reason = "The context manager protocol requires a bound entry method"
    )]
    fn __enter__(&self) {}

    fn __exit__(
        &self,
        py: Python<'_>,
        _kind: &Bound<'_, PyAny>,
        _value: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        self.finish(py)?;
        Ok(false)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.owner)?;
        if let Ok(fixture) = self.fixture.try_lock()
            && let Some(fixture) = fixture.as_ref()
        {
            visit.call(fixture)?;
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        let _ = self.finish(py);
    }
}

pub(super) fn owner(config: &Bound<'_, PyAny>) -> PyResult<Option<Py<NativeGlobalCapture>>> {
    Ok(retained_owner(config)?.filter(|owner| owner.get().prepared.load(Ordering::Acquire)))
}

pub(super) fn rebind_fork(config: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = config.py();
    let Some(owner) = owner(config)? else {
        return Ok(());
    };
    match owner.get().output(py)? {
        #[cfg(unix)]
        Some(OutputResource::Fd(output)) => output.get().rebind_fork(py),
        Some(OutputResource::Sys(output)) => output.get().rebind_fork(py),
        _ => Ok(()),
    }
}

pub(super) fn fork_descriptors(config: &Bound<'_, PyAny>) -> PyResult<Vec<i32>> {
    let py = config.py();
    let Some(owner) = owner(config)? else {
        return Ok(Vec::new());
    };
    let (output, input) = {
        let resources = locked(&owner.get().resources, "global capture resources")?;
        let Some(resources) = resources.as_ref() else {
            return Ok(Vec::new());
        };
        (
            resources.output.as_ref().map(|output| output.clone_ref(py)),
            resources.input.as_ref().map(|input| input.clone_ref(py)),
        )
    };
    let mut descriptors = Vec::new();
    #[cfg(unix)]
    if let Some(OutputResource::Fd(output)) = output {
        descriptors.extend(output.get().fork_descriptors(py)?);
    }
    if let Some(input) = input {
        let input = input.bind(py);
        if let Some(saved) = input.getattr("_saved")?.extract::<Option<i32>>()? {
            descriptors.push(saved);
        }
        let null = input.getattr("_null")?;
        if !null.is_none() {
            descriptors.push(null.call_method0("fileno")?.extract()?);
        }
    }
    Ok(descriptors)
}

fn retained_owner(config: &Bound<'_, PyAny>) -> PyResult<Option<Py<NativeGlobalCapture>>> {
    match config.getattr("_vfy_global_capture") {
        Ok(owner) => Ok(Some(owner.extract()?)),
        Err(error) if error.is_instance_of::<PyAttributeError>(config.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

pub(super) fn install(py: Python<'_>, context: &Context) -> PyResult<Option<Py<PyAny>>> {
    let config = context.config.bind(py);
    if retained_owner(config)?.is_some() {
        return Ok(None);
    }
    let capture = Py::new(
        py,
        NativeGlobalCapture {
            resources: Mutex::new(None),
            fixture: Mutex::new(None),
            state: AtomicU8::new(SUSPENDED),
            prepared: AtomicBool::new(false),
            mode: AtomicU8::new(CaptureMode::No as u8),
        },
    )?;
    let hooks = py
        .import("vfy.independent.capture_hooks")?
        .call_method1("InitialCaptureHooks", (&capture,))?;
    config.setattr("_vfy_global_capture", &capture)?;
    let hooks = hooks.unbind();
    context.register(py, hooks.clone_ref(py), "capture")?;
    Ok(Some(hooks))
}

fn prepare(
    py: Python<'_>,
    config: &Bound<'_, PyAny>,
    namespace: &Bound<'_, PyAny>,
    capture: &Py<NativeGlobalCapture>,
) -> PyResult<()> {
    if capture.get().prepared.swap(true, Ordering::AcqRel) {
        return Err(PyRuntimeError::new_err(
            "initial capture is already initialized",
        ));
    }
    let mode = CaptureMode::from_namespace(namespace)?;
    capture.get().mode.store(mode as u8, Ordering::Release);
    let resources = Resources::create(py, mode)?;
    let input = resources.input.as_ref().map(|input| input.clone_ref(py));
    let output = resources.output.as_ref().map(|output| output.clone_ref(py));
    *locked(&capture.get().resources, "capture resource")? = Some(resources);
    let installed = (|| -> PyResult<()> {
        if let Some(output) = output {
            output.bind(py, capture.bind(py))?;
        }
        config
            .getattr("pluginmanager")?
            .call_method1("register", (capture, "capturemanager"))?;
        config.call_method1("add_cleanup", (capture,))?;
        if let Some(input) = input {
            input.bind(py).call_method0("start")?;
        }
        capture.get().transition(py, CaptureTransition::Resume)?;
        Ok(())
    })();
    if installed.is_err() {
        capture.get().close(py)?;
    }
    installed
}

#[pyclass(frozen, module = "vfy._native._pytest")]
struct NativeInitialCapture {
    owner: Py<NativeGlobalCapture>,
    namespace: Mutex<Option<Py<PyAny>>>,
    complete: AtomicBool,
}

#[pymethods]
impl NativeInitialCapture {
    #[expect(
        clippy::unused_self,
        reason = "The context manager protocol requires a bound entry method"
    )]
    fn __enter__(&self) {}

    fn __exit__(
        &self,
        py: Python<'_>,
        _kind: &Bound<'_, PyAny>,
        error: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let result = (|| -> PyResult<()> {
            if !self.complete.swap(true, Ordering::AcqRel) {
                self.owner
                    .get()
                    .transition(py, CaptureTransition::Suspend)?;
                if !error.is_none()
                    && let Some(output) = self.owner.get().output(py)?
                {
                    let (stdout, stderr) = self.owner.get().take(py)?;
                    output.write_original(py, &stdout, &stderr)?;
                }
            }
            Ok(())
        })();
        let namespace = locked(&self.namespace, "capture namespace")?.take();
        drop(namespace);
        result?;
        Ok(false)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.owner)?;
        if let Ok(namespace) = self.namespace.try_lock()
            && let Some(namespace) = namespace.as_ref()
        {
            visit.call(namespace)?;
        }
        Ok(())
    }

    fn __clear__(&self, py: Python<'_>) {
        if !self.complete.swap(true, Ordering::AcqRel) {
            let _ = self.owner.get().transition(py, CaptureTransition::Suspend);
        }
        let namespace = self
            .namespace
            .lock()
            .ok()
            .and_then(|mut namespace| namespace.take());
        drop(namespace);
    }
}

pub(super) struct Capture<'py> {
    owner: Option<Py<NativeGlobalCapture>>,
    py: Python<'py>,
}

impl<'py> Capture<'py> {
    pub(super) fn start(py: Python<'py>, context: Option<&Context>) -> PyResult<Self> {
        let context = context.ok_or_else(|| {
            PyValueError::new_err("independent capture configuration is unavailable")
        })?;
        let config = context.config.bind(py);
        let owner = owner(config)?;
        if let Some(owner) = &owner {
            owner.get().transition(py, CaptureTransition::Resume)?;
        } else {
            let enabled = context
                .registry
                .bind(py)
                .try_borrow()?
                .has_plugin("capture");
            if enabled {
                return Err(PyValueError::new_err(
                    "independent global capture has not been initialized",
                ));
            }
        }
        Ok(Self { owner, py })
    }

    fn take(&self) -> PyResult<(String, String)> {
        let Some(owner) = &self.owner else {
            return Ok((String::new(), String::new()));
        };
        let owner = owner.get();
        owner.transition(self.py, CaptureTransition::Suspend)?;
        owner.take(self.py)
    }

    pub(super) fn retain(&self, payload: &mut PhasePayload) -> PyResult<()> {
        let (stdout, stderr) = self.take()?;
        payload.stdout.push_str(&stdout);
        payload.stderr.push_str(&stderr);
        Ok(())
    }

    pub(super) fn complete<T>(
        self,
        result: PyResult<T>,
    ) -> PyResult<(PyResult<T>, String, String)> {
        let (stdout, stderr) = self.take()?;
        Ok((result, stdout, stderr))
    }
}

impl Drop for Capture<'_> {
    fn drop(&mut self) {
        if let Some(owner) = &self.owner {
            let _ = owner.get().transition(self.py, CaptureTransition::Suspend);
        }
    }
}
