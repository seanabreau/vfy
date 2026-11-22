//! The command hook owns entry into the single native session lifecycle.
use super::{Context, cleanup::CleanupGuard, state::LifecycleStage};
use crate::hooks::NativeHookRegistry;
use crate::independent::collection_state::OwnedCollector;
use crate::independent::{PreparedCollector, coordinator, invocation, start_prepared};
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyWeakrefMethods, PyWeakrefReference};
use std::cell::RefCell;
use std::path::PathBuf;
use std::time::Instant;

pub(in crate::independent) struct Completion {
    pub(in crate::independent) result: Option<Py<NativeRunResult>>,
    pub(in crate::independent) status: i32,
}

struct Input {
    collector: OwnedCollector,
    root: PathBuf,
    invocation_dir: String,
    options: Option<Py<PyDict>>,
    parallel: Option<Py<PyAny>>,
    started: Instant,
}

enum State {
    Pending(Box<Input>),
    Running,
    Complete(Py<NativeRunResult>),
    Failed,
    Closed,
}

// Only the Rust dispatch scope strongly owns interpreter-local collector state.
// A plugin may retain or transfer the outward callback, but only its exact
// no-callback weakref and originating thread identity survive that operation.
#[pyclass(frozen, unsendable, weakref, module = "vfy._native._pytest")]
struct CommandOwner {
    state: RefCell<State>,
}

#[pyclass(frozen, module = "vfy._native._pytest")]
struct CommandCallback {
    owner: Py<PyWeakrefReference>,
    thread: std::thread::ThreadId,
}

#[pymethods]
impl CommandCallback {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "PyO3 exposes the bound callback signature"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if self.thread != std::thread::current().id() {
            return Err(PyRuntimeError::new_err(
                "native command callback belongs to another thread",
            ));
        }
        let owner = self
            .owner
            .bind(py)
            .upgrade()
            .ok_or_else(|| {
                PyAssertionError::new_err("native command callback is no longer available")
            })?
            .cast_into::<CommandOwner>()?;
        let input = match owner.borrow().state.replace(State::Running) {
            State::Pending(input) => *input,
            previous => {
                owner.borrow().state.replace(previous);
                return Err(PyAssertionError::new_err(
                    "native command callback is no longer available",
                ));
            }
        };
        let result = execute(py, input);
        match result {
            Ok(result) => {
                let status = result.borrow(py).record().exitstatus.ok_or_else(|| {
                    PyRuntimeError::new_err("native command result has no session exitstatus")
                })?;
                owner.borrow().state.replace(State::Complete(result));
                Ok(super::exit_status_object(py, status)?.unbind())
            }
            Err(error) => {
                owner.borrow().state.replace(State::Failed);
                Err(error)
            }
        }
    }
}

fn execute(py: Python<'_>, input: Input) -> PyResult<Py<NativeRunResult>> {
    let mut prepared = PreparedCollector {
        collector: input.collector.bind(py),
        root: input.root,
    };
    let context = prepared
        .collector
        .context
        .as_ref()
        .expect("prepared context")
        .clone_ref(py);
    let cleanup = CleanupGuard::new(py, &context);
    let result = (|| {
        start_prepared(
            py,
            &mut prepared,
            &input.invocation_dir,
            input.options.as_ref().map(|options| options.bind(py)),
        )?;
        let workers: usize = context
            .config
            .bind(py)
            .call_method1("getoption", ("workers", 1))?
            .extract()?;
        if workers == 0 {
            return Err(crate::IndependentUsageError::new_err(
                "independent execution requires a positive worker count",
            ));
        }
        if workers > 1 && !context.invocation.is_worker() {
            coordinator::run_context(
                py,
                &context,
                input.parallel.ok_or_else(|| {
                    PyRuntimeError::new_err(
                        "native parallel command requires its coordinator callback",
                    )
                })?,
            )
        } else {
            invocation::execute_serial(
                py,
                prepared,
                &input.invocation_dir,
                input.options.as_ref().map(|options| options.bind(py)),
                input.started,
            )
        }
    })();
    cleanup.complete(result)
}

pub(in crate::independent) fn run(
    py: Python<'_>,
    prepared: PreparedCollector<'_>,
    root: &str,
    options: Option<&Bound<'_, PyDict>>,
    parallel: Option<Py<PyAny>>,
    started: Instant,
) -> PyResult<Completion> {
    let context = prepared
        .collector
        .context
        .as_ref()
        .expect("prepared context")
        .clone_ref(py);
    context.stage.set(LifecycleStage::CommandLine);
    validate_xdist_mode(py, &context)?;
    let owner = Py::new(
        py,
        CommandOwner {
            state: RefCell::new(State::Pending(Box::new(Input {
                collector: OwnedCollector::new(prepared.collector),
                root: prepared.root,
                invocation_dir: root.to_owned(),
                options: options.map(|options| options.clone().unbind()),
                parallel,
                started,
            }))),
        },
    )?;
    let callback = Py::new(
        py,
        CommandCallback {
            owner: PyWeakrefReference::new(owner.bind(py))?.unbind(),
            thread: std::thread::current().id(),
        },
    )?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("config", &context.config)?;
    let outcome = NativeHookRegistry::call_with_default(
        context.registry.bind(py),
        py,
        "pytest_cmdline_main",
        &kwargs,
        callback.into_any(),
        true,
        "main",
    );
    let state = owner.borrow(py).state.replace(State::Closed);
    // A suppressed default or callback failure owns no completed suite. Preserve
    // that absence; command success cannot manufacture test evidence.
    let result = if let State::Complete(result) = state {
        Some(result)
    } else {
        None
    };
    let closed = match &outcome {
        Err(error) => {
            context.note_error(py, error)?;
            context.close_while_handling(py, error)
        }
        Ok(_) => context.close(py),
    };
    closed?;
    let status = outcome?;
    Ok(Completion {
        result,
        status: if status.bind(py).is_none() {
            0
        } else {
            status.extract(py)?
        },
    })
}

pub(in crate::independent) fn validate_xdist_mode(
    py: Python<'_>,
    context: &Context,
) -> PyResult<()> {
    let config = context.config.bind(py);
    let manager = config.getattr("pluginmanager")?;
    let xdist = manager
        .call_method1("has_plugin", ("xdist",))?
        .is_truthy()?
        || manager
            .call_method1("has_plugin", ("xdist.plugin",))?
            .is_truthy()?;
    let looponfail = manager
        .call_method1("has_plugin", ("xdist.looponfail",))?
        .is_truthy()?;
    if !(xdist || looponfail) {
        return Ok(());
    }
    for name in [
        "looponfail",
        "distload",
        "numprocesses",
        "tx",
        "px",
        "rsyncdir",
        "rsyncignore",
    ] {
        if config
            .call_method1("getoption", (name, py.None()))?
            .is_truthy()?
        {
            return Err(crate::UnsupportedFeatureError::new_err(format!(
                "independent pytest supports the xdist serial plugin configuration; option '{name}' requires unsupported xdist execution; use `vfy test --workers=2`, or --engine=pytest-native with vfy[pytest] and pytest-xdist installed"
            )));
        }
    }
    if xdist {
        if config
            .call_method1("getoption", ("dist", "no"))?
            .extract::<String>()?
            != "no"
        {
            return Err(crate::UnsupportedFeatureError::new_err(
                "independent pytest supports xdist --dist=no; use `vfy test --workers=2`, or --engine=pytest-native with vfy[pytest] and pytest-xdist installed",
            ));
        }
        for name in ["rsyncdirs", "rsyncignore", "looponfailroots"] {
            if config.call_method1("getini", (name,))?.is_truthy()? {
                return Err(crate::UnsupportedFeatureError::new_err(format!(
                    "independent pytest does not support xdist configuration '{name}'; use `vfy test --engine=pytest-native` with vfy[pytest] and pytest-xdist installed"
                )));
            }
        }
    }
    Ok(())
}
