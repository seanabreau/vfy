//! Pluggy's `_multicall` over one ordered method list, including wrapper teardown.

use super::invocation::{CallArguments, HookReturn};
use super::{
    HookArguments, HookBody, Implementation, NativeHookRegistry, NativeHookResult,
    PluggyTeardownRaisedWarning, Style,
};
use crate::independent::reporting::ReportHandle;
use crate::independent::reporting::terminal::NativeStatusCallbacks;
use crate::independent::skipping;
use crate::module_cache::Module;
use pyo3::exceptions::{PyBaseException, PyRuntimeError, PyStopIteration};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyCFunction, PyDict, PyIterator, PyList, PySendResult, PyType};

static GENERATOR_TYPE: PyOnceLock<Py<PyType>> = PyOnceLock::new();

pub(super) fn initialize_generator_type(py: Python<'_>) -> PyResult<()> {
    if GENERATOR_TYPE.get(py).is_none() {
        // Do not hold the once lock across an import or a Python callback.
        let generator_type = py
            .import("vfy._hook_callbacks")?
            .call_method0("generator_type")?
            .cast_into::<PyType>()?
            .unbind();
        let _ = GENERATOR_TYPE.set(py, generator_type);
    }
    Ok(())
}

/// A native status hook that answers from a native report without a Python call.
#[derive(Clone, Copy)]
enum NativeStatusHook {
    Runner,
    Terminal,
    Skipping,
}

impl NativeStatusHook {
    fn of(body: &HookBody) -> Option<Self> {
        match body {
            HookBody::RunnerStatus => Some(Self::Runner),
            HookBody::TerminalStatus => Some(Self::Terminal),
            HookBody::SkippingStatus => Some(Self::Skipping),
            _ => None,
        }
    }

    fn status(
        self,
        py: Python<'_>,
        report: &ReportHandle,
    ) -> PyResult<Option<(&'static str, &'static str, &'static str)>> {
        match self {
            Self::Runner => NativeStatusCallbacks::runner_status_handle(py, report),
            Self::Terminal => NativeStatusCallbacks::terminal_status_handle(py, report).map(Some),
            Self::Skipping => skipping::status_shared(py, skipping::SkipReport::Native(report)),
        }
    }
}

/// A wrapper suspended at its first yield, resumed in reverse order of entry.
enum Teardown {
    SkippingCall(Py<PyAny>),
    SkippingReportPhase(Py<PyAny>),
    HypothesisReport {
        item: Py<PyAny>,
        function: Py<PyAny>,
        owner: Py<crate::independent::hypothesis_report::NativeHypothesisReport>,
        implementation: Py<Implementation>,
    },
    SkippingReport {
        item: Py<PyAny>,
        call: Py<PyAny>,
    },
    /// A new-style wrapper generator, resumed with the call's result or exception.
    Wrapper(Py<PyAny>),
    /// An old-style hookwrapper generator, which receives a `NativeHookResult`.
    Legacy {
        generator: Py<PyAny>,
        plugin_name: String,
    },
}

/// What one teardown step produced.
enum Settled {
    /// The wrapper returned, so its value becomes the call result.
    Value(HookReturn),
    /// The wrapper re-raised a thrown `StopIteration` through PEP 479; the original exception stays.
    Keep,
    /// The wrapper raised; this becomes the pending exception.
    Raised(PyErr),
    /// A new-style wrapper yielded again. Its error ends the call immediately.
    SecondYield(PyErr),
}

/// Monitors can mutate their Python list during dispatch; ordinary calls retain
/// the already selected Rust snapshot in call order.
#[derive(Clone, Copy)]
pub(super) enum Methods<'a, 'py> {
    Selected(&'a [Py<Implementation>]),
    Rows(&'a [super::RegisteredImplementation]),
    RowsWithNative {
        methods: &'a [super::RegisteredImplementation],
        position: usize,
        default: &'a dyn Fn() -> PyResult<Py<PyAny>>,
    },
    SelectedWithNative {
        methods: &'a [Py<Implementation>],
        position: usize,
        default: &'a dyn Fn() -> PyResult<Py<PyAny>>,
    },
    Monitored(&'a Bound<'py, PyList>),
}

impl Methods<'_, '_> {
    fn len(&self) -> usize {
        match self {
            Self::Selected(methods) => methods.len(),
            Self::Rows(methods) => methods.len(),
            Self::RowsWithNative { methods, .. } => methods.len() + 1,
            Self::SelectedWithNative { methods, .. } => methods.len() + 1,
            Self::Monitored(methods) => methods.len(),
        }
    }

    fn at(&self, py: Python<'_>, index: usize) -> PyResult<Method<'_>> {
        match self {
            Self::Selected(methods) => Ok(Method::Registered(ImplementationHandle::Owned(
                methods[methods.len() - index - 1].clone_ref(py),
            ))),
            Self::Rows(methods) => Ok(Method::Registered(ImplementationHandle::Borrowed(
                &methods[methods.len() - index - 1].implementation,
            ))),
            Self::RowsWithNative {
                methods,
                position,
                default,
            } => {
                let storage_index = methods.len() - index;
                if storage_index == *position {
                    Ok(Method::Native(*default))
                } else {
                    Ok(Method::Registered(ImplementationHandle::Borrowed(
                        &methods[storage_index - usize::from(storage_index > *position)]
                            .implementation,
                    )))
                }
            }
            Self::SelectedWithNative {
                methods,
                position,
                default,
            } => {
                let storage_index = methods.len() - index;
                if storage_index == *position {
                    Ok(Method::Native(*default))
                } else {
                    Ok(Method::Registered(ImplementationHandle::Owned(
                        methods[storage_index - usize::from(storage_index > *position)]
                            .clone_ref(py),
                    )))
                }
            }
            Self::Monitored(methods) => Ok(Method::Registered(ImplementationHandle::Owned(
                methods.get_item(index)?.extract()?,
            ))),
        }
    }
}

enum ImplementationHandle<'a> {
    Borrowed(&'a Py<Implementation>),
    Owned(Py<Implementation>),
}

impl ImplementationHandle<'_> {
    fn get(&self) -> &Py<Implementation> {
        match self {
            Self::Borrowed(value) => value,
            Self::Owned(value) => value,
        }
    }
}

enum Method<'a> {
    Registered(ImplementationHandle<'a>),
    Native(&'a dyn Fn() -> PyResult<Py<PyAny>>),
}

/// Original values stay alive through teardown even when wrappers replace them.
enum HookResults {
    First(Option<HookReturn>),
    All(Option<Py<PyList>>),
}

/// Publish one empty all-results list and retain its original identity throughout teardown.
fn exposed_result(
    py: Python<'_>,
    results: &mut HookResults,
    result: &mut HookReturn,
) -> PyResult<Py<PyAny>> {
    if matches!(result, HookReturn::EmptyList) {
        let list = PyList::empty(py).unbind();
        if let HookResults::All(original) = results {
            *original = Some(list.clone_ref(py));
        }
        *result = HookReturn::Python(list.into_any());
    }
    result.to_python(py)
}

/// Runs the selected methods in call order, including wrapper teardown.
pub(super) fn run<'py>(
    registry: &Bound<'py, NativeHookRegistry>,
    name: &str,
    methods: Methods<'_, 'py>,
    kwargs: &Bound<'py, PyDict>,
    firstresult: bool,
) -> PyResult<Py<PyAny>> {
    let mut arguments = CallArguments::Borrowed(kwargs);
    run_arguments(registry, name, methods, &mut arguments, firstresult)?.into_python(registry.py())
}

/// Dispatch a call whose arguments and result may still be native-owned.
#[expect(
    clippy::too_many_lines,
    reason = "ordered teardown preserves shared result, exception, and wrapper lifetimes"
)]
pub(super) fn run_arguments<'py>(
    registry: &Bound<'py, NativeHookRegistry>,
    name: &str,
    methods: Methods<'_, 'py>,
    arguments: &mut CallArguments<'_, 'py>,
    firstresult: bool,
) -> PyResult<HookReturn> {
    let py = registry.py();
    let mut results = if firstresult {
        HookResults::First(None)
    } else {
        HookResults::All(None)
    };
    let mut teardowns = Vec::new();
    let mut exception = call_each(registry, &methods, arguments, &mut results, &mut teardowns)
        .err()
        .map(|error| error.into_value(py));
    let mut result = match &results {
        HookResults::First(value) => value.as_ref().map_or_else(
            || HookReturn::Python(py.None()),
            |value| value.clone_ref(py),
        ),
        HookResults::All(Some(values)) => HookReturn::Python(values.clone_ref(py).into_any()),
        HookResults::All(None) => HookReturn::EmptyList,
    };
    while let Some(teardown) = teardowns.pop() {
        let thrown = exception.as_ref().map(|error| error.clone_ref(py));
        let settled = match teardown {
            Teardown::SkippingCall(item) => {
                let finish = (|| -> PyResult<()> {
                    if let Some(error) = &thrown {
                        let callback = PyCFunction::new_closure(py, None, None, |args, _| {
                            crate::independent::skipping::NativeSkippingHooks::call_finish(
                                &args.get_item(0)?,
                            )?;
                            Ok::<Py<PyAny>, PyErr>(args.py().None())
                        })?;
                        py.import("vfy._hook_callbacks")?.call_method1(
                            "call_while_handling",
                            (callback, error, item.bind(py)),
                        )?;
                        Ok(())
                    } else {
                        crate::independent::skipping::NativeSkippingHooks::call_finish(
                            item.bind(py),
                        )
                    }
                })();
                match finish {
                    Ok(()) => Settled::Keep,
                    Err(error) => Settled::Raised(error),
                }
            }
            Teardown::HypothesisReport {
                item,
                function,
                owner,
                implementation,
            } => {
                if let Some(error) = &thrown {
                    let legacy = (|| -> PyResult<Py<PyAny>> {
                        let _ = arguments.python_kwargs(py)?;
                        let Some(super::invocation::OriginalPhaseCall::Python(call)) =
                            arguments.original_phase_call(py)
                        else {
                            return Err(PyRuntimeError::new_err(
                                "original phase call is unavailable",
                            ));
                        };
                        let generator = function.bind(py).call1((item.bind(py), call))?;
                        start(&generator)?;
                        let plugin_name = implementation.borrow(py).plugin_name.clone();
                        legacy_step(&generator, name, &plugin_name, Err(error.clone_ref(py)))
                    })();
                    match legacy {
                        Ok(value) => Settled::Value(HookReturn::Python(value)),
                        Err(error) => match passes_stop_iteration(py, &error, thrown.as_ref()) {
                            Ok(true) => Settled::Keep,
                            Ok(false) => Settled::Raised(error),
                            Err(error) => Settled::Raised(error),
                        },
                    }
                } else {
                    let finish = match &result {
                        HookReturn::NativeReport(report) => {
                            crate::independent::hypothesis_report::finish_handle(
                                owner.bind(py),
                                item.bind(py),
                                report,
                            )
                        }
                        _ => exposed_result(py, &mut results, &mut result).and_then(|report| {
                            crate::independent::hypothesis_report::finish_python(
                                owner.bind(py),
                                item.bind(py),
                                report.bind(py),
                            )
                        }),
                    };
                    match finish {
                        Ok(()) => Settled::Keep,
                        Err(error) => {
                            let error = if error.is_instance_of::<PyStopIteration>(py) {
                                let runtime =
                                    PyRuntimeError::new_err("generator raised StopIteration");
                                runtime.set_cause(py, Some(error.clone_ref(py)));
                                runtime.set_context(py, Some(error));
                                runtime
                            } else {
                                error
                            };
                            let plugin_name = implementation.borrow(py).plugin_name.clone();
                            match warn_teardown(py, name, &plugin_name, &error) {
                                Ok(()) => Settled::Raised(error),
                                Err(warning) => Settled::Raised(warning),
                            }
                        }
                    }
                }
            }
            Teardown::SkippingReportPhase(item) => {
                if thrown.is_some() {
                    Settled::Keep
                } else {
                    let operation = (|| -> PyResult<()> {
                        let call = arguments.original_phase_call(py).ok_or_else(|| {
                            PyRuntimeError::new_err("original phase call is unavailable")
                        })?;
                        if let HookReturn::NativeReport(report) = &result {
                            crate::independent::skipping::report_shared(
                                item.bind(py),
                                call,
                                crate::independent::skipping::SkipReport::Native(report),
                            )
                        } else {
                            let report = exposed_result(py, &mut results, &mut result)?;
                            crate::independent::skipping::report_shared(
                                item.bind(py),
                                call,
                                crate::independent::skipping::SkipReport::Python(report.bind(py)),
                            )
                        }
                    })();
                    match operation {
                        Ok(()) => Settled::Keep,
                        Err(error) => Settled::Raised(error),
                    }
                }
            }
            Teardown::SkippingReport { item, call } => {
                if thrown.is_some() {
                    Settled::Keep
                } else {
                    match exposed_result(py, &mut results, &mut result).and_then(|report| {
                        crate::independent::skipping::NativeSkippingHooks::report_untyped(
                            item.bind(py),
                            call.bind(py),
                            report.bind(py),
                        )
                    }) {
                        Ok(()) => Settled::Keep,
                        Err(error) => Settled::Raised(error),
                    }
                }
            }
            Teardown::Wrapper(generator) => {
                let input = if thrown.is_some() {
                    Ok(py.None())
                } else {
                    exposed_result(py, &mut results, &mut result)
                };
                match input {
                    Ok(input) => resume_wrapper(generator.bind(py), thrown.as_ref(), &input),
                    Err(error) => Settled::Raised(error),
                }
            }
            Teardown::Legacy {
                generator,
                plugin_name,
            } => {
                let input = match &thrown {
                    Some(error) => Err(error.clone_ref(py)),
                    None => match exposed_result(py, &mut results, &mut result) {
                        Ok(value) => Ok(value),
                        Err(error) => Err(error.into_value(py)),
                    },
                };
                match legacy_step(generator.bind(py), name, &plugin_name, input) {
                    Ok(value) => Settled::Value(HookReturn::Python(value)),
                    Err(error) => match passes_stop_iteration(py, &error, thrown.as_ref()) {
                        Ok(true) => Settled::Keep,
                        Ok(false) => Settled::Raised(error),
                        Err(error) => Settled::Raised(error),
                    },
                }
            }
        };
        match settled {
            Settled::Value(value) => {
                result = value;
                exception = None;
            }
            Settled::Keep => {}
            Settled::Raised(error) => exception = Some(error.into_value(py)),
            Settled::SecondYield(error) => return Err(error),
        }
    }
    match exception {
        Some(error) => Err(PyErr::from_value(error.into_bound(py).into_any())),
        None => Ok(result),
    }
}

/// Calls each implementation in order. Errors stop the walk and become the pending exception.
#[expect(
    clippy::too_many_lines,
    reason = "native and dynamic implementations share one ordered hook dispatch"
)]
fn call_each<'py>(
    registry: &Bound<'py, NativeHookRegistry>,
    methods: &Methods<'_, 'py>,
    arguments: &mut CallArguments<'_, 'py>,
    results: &mut HookResults,
    teardowns: &mut Vec<Teardown>,
) -> PyResult<()> {
    let py = registry.py();
    let mut index = methods.len();
    while index > 0 {
        index -= 1;
        if index >= methods.len() {
            break;
        }
        let implementation = match methods.at(py, index)? {
            Method::Registered(implementation) => implementation,
            Method::Native(default) => {
                let returned = default()?.into_bound(py);
                if !returned.is_none() {
                    match results {
                        HookResults::First(value) => {
                            *value = Some(HookReturn::Python(returned.unbind()));
                            break;
                        }
                        HookResults::All(values) => values
                            .get_or_insert_with(|| PyList::empty(py).unbind())
                            .bind(py)
                            .append(returned)?,
                    }
                }
                continue;
            }
        };
        let body = implementation.get().borrow(py).body.clone_ref(py);
        if let (
            HookBody::SkippingCall,
            CallArguments::Item(item) | CallArguments::ItemNext { item, .. },
        ) = (&body, &*arguments)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            crate::independent::skipping::NativeSkippingHooks::call_start(item)?;
            teardowns.push(Teardown::SkippingCall((*item).clone().unbind()));
            continue;
        }
        if let (HookBody::HypothesisReport(owner), CallArguments::Phase { item, .. }) =
            (&body, &*arguments)
        {
            let function = {
                let implementation = implementation.get().borrow(py);
                if let Some(count) = &implementation.invocation {
                    count.increment()?;
                }
                implementation.function.clone_ref(py)
            };
            teardowns.push(Teardown::HypothesisReport {
                item: (*item).clone().unbind(),
                function,
                owner: owner.clone_ref(py),
                implementation: implementation.get().clone_ref(py),
            });
            continue;
        }
        if let (HookBody::SkippingReport, CallArguments::Phase { item, .. }) = (&body, &*arguments)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            teardowns.push(Teardown::SkippingReportPhase((*item).clone().unbind()));
            continue;
        }
        if let (
            HookBody::RunnerMakeReport,
            CallArguments::Phase {
                item,
                call: Some(call),
                history,
            },
        ) = (&body, &mut *arguments)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            let report = crate::independent::reporting::make::from_call_state(item, call)?;
            let report = crate::independent::reporting::NativeSessionReports::prepare_native(
                history, report,
            )?;
            let returned = HookReturn::NativeReport(report);
            match results {
                HookResults::First(value) => {
                    *value = Some(returned);
                    break;
                }
                HookResults::All(values) => values
                    .get_or_insert_with(|| PyList::empty(py).unbind())
                    .bind(py)
                    .append(returned.into_python(py)?)?,
            }
            continue;
        }
        if let (HookBody::TerminalLogStart(owner), CallArguments::Boundary { nodeid, location }) =
            (&body, &*arguments)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            owner.borrow(py).start_handle(py, nodeid, location)?;
            continue;
        }
        if let (HookBody::SessionLogReport(owner), CallArguments::RawLogReport(report)) =
            (&body, &*arguments)
            && report.is_native(py)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            owner.borrow(py).record_handle(report, py)?;
            continue;
        }
        if let (HookBody::TerminalLogReport(owner), CallArguments::RawLogReport(report)) =
            (&body, &*arguments)
            && report.is_native(py)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            crate::independent::reporting::terminal::consume_handle(&owner.borrow(py), report, py)?;
            continue;
        }
        if let (HookBody::SubtestsStatus, CallArguments::RawStatus { report, config }) =
            (&body, &*arguments)
            && report.is_native(py)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            if let Some(status) = crate::independent::subtests::NativeSubtestsStatus::status_handle(
                py, report, config,
            )? {
                let returned = HookReturn::Python(status.into_pyobject(py)?.into_any().unbind());
                match results {
                    HookResults::First(value) => {
                        *value = Some(returned);
                        break;
                    }
                    HookResults::All(values) => {
                        values
                            .get_or_insert_with(|| PyList::empty(py).unbind())
                            .bind(py)
                            .append(returned.into_python(py)?)?;
                    }
                }
            }
            continue;
        }
        if let CallArguments::RawStatus { report, .. } = &*arguments
            && let Some(hook) = NativeStatusHook::of(&body)
            && report.is_native(py)
        {
            if let Some(count) = &implementation.get().borrow(py).invocation {
                count.increment()?;
            }
            if let Some(status) = hook.status(py, report)? {
                let returned = HookReturn::NativeStatus(status);
                match results {
                    HookResults::First(value) => {
                        *value = Some(returned);
                        break;
                    }
                    HookResults::All(values) => {
                        values
                            .get_or_insert_with(|| PyList::empty(py).unbind())
                            .bind(py)
                            .append(returned.into_python(py)?)?;
                    }
                }
            }
            continue;
        }
        let names = implementation.get().borrow(py).arguments.clone_ref(py);
        let arguments = Implementation::arguments(py, names.bind(py), arguments)?;
        if let Some(count) = &implementation.get().borrow(py).invocation {
            count.increment()?;
        }
        let (body, style, function, plugin_name) = {
            let implementation = implementation.get().borrow(py);
            (
                implementation.body.clone_ref(py),
                implementation.style,
                implementation.function.clone_ref(py),
                if implementation.style == Style::Legacy {
                    implementation.plugin_name.clone()
                } else {
                    String::new()
                },
            )
        };
        match (body, &arguments) {
            (HookBody::SkippingCall, HookArguments::One(item)) => {
                crate::independent::skipping::NativeSkippingHooks::call_start(item)?;
                teardowns.push(Teardown::SkippingCall(item.clone().unbind()));
                continue;
            }
            (
                HookBody::SkippingReport,
                HookArguments::Two {
                    first: item,
                    second: call,
                },
            ) => {
                teardowns.push(Teardown::SkippingReport {
                    item: item.clone().unbind(),
                    call: call.clone().unbind(),
                });
                continue;
            }
            _ => {}
        }
        let returned = arguments.call(function.bind(py))?;
        match style {
            Style::Plain => {
                if !returned.is_none() {
                    match results {
                        HookResults::First(value) => {
                            *value = Some(HookReturn::Python(returned.unbind()));
                            break;
                        }
                        HookResults::All(values) => values
                            .get_or_insert_with(|| PyList::empty(py).unbind())
                            .bind(py)
                            .append(returned)?,
                    }
                }
            }
            Style::Wrapper => {
                start(&returned)?;
                teardowns.push(Teardown::Wrapper(returned.unbind()));
            }
            Style::Legacy => {
                start(&returned)?;
                teardowns.push(Teardown::Legacy {
                    generator: returned.unbind(),
                    plugin_name,
                });
            }
        }
    }
    Ok(())
}

/// Advances a wrapper to its first yield.
fn start(generator: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = generator.py();
    match Module::Builtins
        .get(py)?
        .getattr(pyo3::intern!(py, "next"))?
        .call1((generator,))
    {
        Ok(_) => Ok(()),
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
            Err(wrap_failure(generator, "did not yield"))
        }
        Err(error) => Err(error),
    }
}

/// Resumes a new-style wrapper with the thrown exception, or with the result.
fn resume_wrapper(
    generator: &Bound<'_, PyAny>,
    thrown: Option<&Py<PyBaseException>>,
    result: &Py<PyAny>,
) -> Settled {
    let py = generator.py();
    if thrown.is_none()
        && GENERATOR_TYPE
            .get(py)
            .is_some_and(|kind| generator.is_exact_instance(kind.bind(py).as_any()))
        && let Ok(iterator) = generator.cast::<PyIterator>()
    {
        return match iterator.send(result.bind(py)) {
            Ok(PySendResult::Return(value)) => Settled::Value(HookReturn::Python(value.unbind())),
            Ok(PySendResult::Next(_)) => match generator.call_method0(pyo3::intern!(py, "close")) {
                Ok(_) => Settled::SecondYield(wrap_failure(generator, "has second yield")),
                Err(error) => Settled::Raised(error),
            },
            Err(error) => Settled::Raised(error),
        };
    }
    let sent = match thrown {
        Some(error) => generator.call_method1(pyo3::intern!(py, "throw"), (error.bind(py),)),
        None => generator.call_method1(pyo3::intern!(py, "send"), (result.bind(py),)),
    };
    match sent {
        Ok(_) => match generator.call_method0(pyo3::intern!(py, "close")) {
            Ok(_) => Settled::SecondYield(wrap_failure(generator, "has second yield")),
            Err(error) => Settled::Raised(error),
        },
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
            match error.value(py).getattr(pyo3::intern!(py, "value")) {
                Ok(value) => Settled::Value(HookReturn::Python(value.unbind())),
                Err(error) => Settled::Raised(error),
            }
        }
        Err(error) => match passes_stop_iteration(py, &error, thrown) {
            Ok(true) => match generator.call_method0(pyo3::intern!(py, "close")) {
                Ok(_) => Settled::Keep,
                Err(error) => Settled::Raised(error),
            },
            Ok(false) => Settled::Raised(error),
            Err(error) => Settled::Raised(error),
        },
    }
}

/// True when a `StopIteration` thrown into a teardown came back as the
/// `RuntimeError` that PEP 479 raises. Pluggy keeps the original exception pending.
fn passes_stop_iteration(
    py: Python<'_>,
    error: &PyErr,
    thrown: Option<&Py<PyBaseException>>,
) -> PyResult<bool> {
    let Some(thrown) = thrown else {
        return Ok(false);
    };
    if !thrown.bind(py).is_instance_of::<PyStopIteration>()
        || !error.is_instance_of::<PyRuntimeError>(py)
    {
        return Ok(false);
    }
    let cause = PyCFunction::new_closure(py, None, None, |arguments, _| {
        arguments
            .get_item(0)?
            .getattr("__cause__")
            .map(Bound::unbind)
    })?;
    // A user descriptor can raise: preserve its exception and automatic context
    // before the ordinary Rust teardown loop selects the next outer wrapper.
    let cause = py.import("vfy._hook_callbacks")?.call_method1(
        "call_while_handling",
        (cause, error.value(py), error.value(py)),
    )?;
    Ok(cause.is(thrown.bind(py)))
}

/// Runs the post-yield half of an old-style hookwrapper, as pluggy's
/// `run_old_style_hookwrapper` does. Its `Ok` value is the wrapper's return.
fn legacy_step(
    generator: &Bound<'_, PyAny>,
    name: &str,
    plugin_name: &str,
    input: Result<Py<PyAny>, Py<PyBaseException>>,
) -> PyResult<Py<PyAny>> {
    let py = generator.py();
    let outcome = Py::new(
        py,
        match input {
            Ok(value) => NativeHookResult::from_outcome(py, Ok(value)),
            Err(error) => NativeHookResult::new(py, py.None(), error.bind(py).as_any())?,
        },
    )?;
    let reported = match generator.call_method1(pyo3::intern!(py, "send"), (outcome.bind(py),)) {
        Ok(_) => Err(wrap_failure(generator, "has second yield")),
        Err(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(()),
        Err(error) => warn_teardown(py, name, plugin_name, &error).and(Err(error)),
    };
    // The generator is closed before the outcome is reported, as in a `finally` block.
    generator.call_method0(pyo3::intern!(py, "close"))?;
    reported?;
    NativeHookResult::outcome(&outcome, py).map_err(|error| {
        if error.is_instance_of::<PyStopIteration>(py) {
            let runtime = PyRuntimeError::new_err("generator raised StopIteration");
            runtime.set_cause(py, Some(error));
            runtime
        } else {
            error
        }
    })
}

fn wrap_failure(generator: &Bound<'_, PyAny>, message: &str) -> PyErr {
    match wrap_failure_message(generator, message) {
        Ok(text) => PyRuntimeError::new_err(text),
        Err(error) => error,
    }
}

fn wrap_failure_message(generator: &Bound<'_, PyAny>, message: &str) -> PyResult<String> {
    let code = generator.getattr("gi_code")?;
    Ok(format!(
        "wrap_controller at '{}' {}:{} {}",
        code.getattr("co_name")?.extract::<String>()?,
        code.getattr("co_filename")?.extract::<String>()?,
        code.getattr("co_firstlineno")?.extract::<usize>()?,
        message
    ))
}

fn warn_teardown(py: Python<'_>, name: &str, plugin_name: &str, error: &PyErr) -> PyResult<()> {
    let name = name.to_owned();
    let plugin_name = plugin_name.to_owned();
    let warning = PyCFunction::new_closure(py, None, None, move |arguments, _| {
        emit_teardown_warning(
            arguments.py(),
            &name,
            &plugin_name,
            &arguments.get_item(0)?.cast_into::<PyBaseException>()?,
        )
    })?;
    // Formatting and warning filters run user code inside pluggy's except block.
    py.import("vfy._hook_callbacks")?.call_method1(
        "call_while_handling",
        (warning, error.value(py), error.value(py)),
    )?;
    Ok(())
}

fn emit_teardown_warning(
    py: Python<'_>,
    name: &str,
    plugin_name: &str,
    value: &Bound<'_, PyBaseException>,
) -> PyResult<()> {
    let kind = value.get_type().getattr("__name__")?.extract::<String>()?;
    // pluggy formats the exception with an f-string, which calls `__format__`.
    let text = value
        .call_method1("__format__", ("",))?
        .extract::<String>()?;
    let message = format!(
        "A plugin raised an exception during an old-style hookwrapper teardown.\nPlugin: {plugin_name}, Hook: {name}\n{kind}: {text}\nFor more information see https://pluggy.readthedocs.io/en/stable/api_reference.html#pluggy.PluggyTeardownRaisedWarning"
    );
    let category = teardown_warning_category(py);
    let warnings = Module::Warnings.get(py)?;
    if let Some(origin) = TeardownOrigin::of(value)? {
        // pytest attributes the warning to its own runner frame; vfy has no such frame, so the
        // raising hook implementation is the closest user-owned location.
        let keywords = PyDict::new(py);
        keywords.set_item("module", origin.module)?;
        keywords.set_item("registry", origin.registry)?;
        keywords.set_item("module_globals", origin.globals)?;
        warnings.call_method(
            "warn_explicit",
            (message, category, origin.filename, origin.line),
            Some(&keywords),
        )?;
    } else {
        let keywords = PyDict::new(py);
        keywords.set_item("stacklevel", 2)?;
        warnings.call_method("warn", (message, category), Some(&keywords))?;
    }
    Ok(())
}

/// The warning class pluggy defines when it is importable, otherwise vfy's equivalent.
fn teardown_warning_category(py: Python<'_>) -> Bound<'_, PyAny> {
    py.import(pyo3::intern!(py, "pluggy"))
        .and_then(|module| module.getattr(pyo3::intern!(py, "PluggyTeardownRaisedWarning")))
        .unwrap_or_else(|_| py.get_type::<PluggyTeardownRaisedWarning>().into_any())
}

/// The innermost traceback frame of the exception a hookwrapper raised during teardown.
struct TeardownOrigin<'py> {
    filename: Bound<'py, PyAny>,
    line: Bound<'py, PyAny>,
    module: Bound<'py, PyAny>,
    registry: Bound<'py, PyAny>,
    globals: Bound<'py, PyAny>,
}

impl<'py> TeardownOrigin<'py> {
    fn of(value: &Bound<'py, PyBaseException>) -> PyResult<Option<Self>> {
        let py = value.py();
        let mut traceback = value.getattr("__traceback__")?;
        if traceback.is_none() {
            return Ok(None);
        }
        loop {
            let next = traceback.getattr("tb_next")?;
            if next.is_none() {
                break;
            }
            traceback = next;
        }
        let frame = traceback.getattr("tb_frame")?;
        let globals = frame.getattr("f_globals")?;
        let registry =
            globals.call_method1("setdefault", ("__warningregistry__", PyDict::new(py)))?;
        let module = globals.call_method1("get", ("__name__", "<string>"))?;
        Ok(Some(Self {
            filename: frame.getattr("f_code")?.getattr("co_filename")?,
            line: traceback.getattr("tb_lineno")?,
            module,
            registry,
            globals,
        }))
    }
}
