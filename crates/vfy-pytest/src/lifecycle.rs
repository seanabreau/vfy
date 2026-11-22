//! Built-in pytest protocol scopes and deferred phase exceptions.

use crate::UnsupportedFeatureError;
use crate::warnings::WarningService;
use pyo3::exceptions::{PyNameError, PyRuntimeError, PyStopIteration, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyFunction, PyString, PyTuple};
use vfy_core::test_execution::ExecutionPhase;

#[derive(Clone, Copy)]
enum PhaseHookArguments {
    None,
    Item,
    NextItem,
    ItemNextItem,
    NextItemItem,
}

/// How pluggy runs a hook implementation.
#[derive(Clone, Copy, PartialEq, Eq)]
enum HookStyle {
    Plain,
    /// `wrapper=True`: the exception is thrown into the generator, which may handle it.
    Wrapper,
    /// `hookwrapper=True`: receives a `pluggy.Result` after yielding.
    LegacyWrapper,
}

impl HookStyle {
    fn of(implementation: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = implementation.py();
        Ok(
            if implementation
                .getattr(intern!(py, "wrapper"))?
                .is_truthy()?
            {
                Self::Wrapper
            } else if implementation
                .getattr(intern!(py, "hookwrapper"))?
                .is_truthy()?
            {
                Self::LegacyWrapper
            } else {
                Self::Plain
            },
        )
    }

    fn wraps(self) -> bool {
        self != Self::Plain
    }
}

/// pytest-timeout registers under its entry-point name or its module name.
const TIMEOUT_PLUGIN_NAMES: [&str; 2] = ["timeout", "pytest_timeout"];
const BENCHMARK_PLUGIN_NAMES: [&str; 2] = ["benchmark", "pytest_benchmark.plugin"];

struct PhaseHook {
    function: Py<PyAny>,
    style: HookStyle,
    arguments: PhaseHookArguments,
}

impl PhaseHookArguments {
    /// Map pluggy's declared argument names onto the hook's call shape.
    fn from_names(names: &[String]) -> Option<Self> {
        match names {
            [] => Some(Self::None),
            [item] if item == "item" => Some(Self::Item),
            [next] if next == "nextitem" => Some(Self::NextItem),
            [item, next] if item == "item" && next == "nextitem" => Some(Self::ItemNextItem),
            [next, item] if item == "item" && next == "nextitem" => Some(Self::NextItemItem),
            _ => None,
        }
    }
}

impl PhaseHook {
    fn arguments<'py>(
        &self,
        item: &Bound<'py, PyAny>,
        nextitem: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let py = item.py();
        let nextitem = || {
            nextitem
                .cloned()
                .unwrap_or_else(|| py.None().into_bound(py))
        };
        match self.arguments {
            PhaseHookArguments::None => Ok(PyTuple::empty(py)),
            PhaseHookArguments::Item => PyTuple::new(py, [item.clone()]),
            PhaseHookArguments::NextItem => PyTuple::new(py, [nextitem()]),
            PhaseHookArguments::ItemNextItem => PyTuple::new(py, [item.clone(), nextitem()]),
            PhaseHookArguments::NextItemItem => PyTuple::new(py, [nextitem(), item.clone()]),
        }
    }

    fn call<'py>(
        &self,
        item: &Bound<'py, PyAny>,
        nextitem: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.function
            .bind(item.py())
            .call1(self.arguments(item, nextitem)?)
    }
}

#[derive(Default)]
struct ProtocolHooks {
    /// Protocol wrappers in pluggy's call order, outermost first.
    protocols: Vec<(PhaseHook, Py<PyAny>)>,
    /// Plain protocol hooks pluggy calls before the runner; a result from one
    /// would replace the test protocol.
    protocol_overrides: Vec<(PhaseHook, String)>,
    warning_position: Option<usize>,
    legacy_adapter: Option<LegacyAdapter>,
}

/// Pluggy's adapter that runs a legacy hookwrapper as a new-style wrapper.
struct LegacyAdapter {
    function: Py<PyAny>,
    keywords: AdapterKeywords,
}

/// Whether the adapter takes the hook's keyword arguments after its positional
/// arguments, as pluggy 1.7 does; pluggy 1.6 accepts positional arguments only.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AdapterKeywords {
    Absent,
    Accepted,
}

impl ProtocolHooks {
    /// Collect `pytest_runtest_protocol` implementations around the native protocol.
    ///
    /// pytest's runner, unittest, and warnings implementations are native services.
    /// Wrappers run in pluggy's call order; plain implementations pluggy calls
    /// after the runner never run, because the runner returns a result.
    fn new(config: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = config.py();
        let callers = py.import(intern!(py, "pluggy._callers"))?;
        let mut hooks = Self::default();
        if callers.hasattr(intern!(py, "run_old_style_hookwrapper"))? {
            let function = callers.getattr(intern!(py, "run_old_style_hookwrapper"))?;
            let parameters: usize = function
                .getattr(intern!(py, "__code__"))?
                .getattr(intern!(py, "co_argcount"))?
                .extract()?;
            hooks.legacy_adapter = Some(LegacyAdapter {
                function: function.unbind(),
                keywords: if parameters > 3 {
                    AdapterKeywords::Accepted
                } else {
                    AdapterKeywords::Absent
                },
            });
        }
        let implementations = config
            .getattr(intern!(py, "hook"))?
            .getattr(intern!(py, "pytest_runtest_protocol"))?
            .call_method0(intern!(py, "get_hookimpls"))?
            .extract::<Vec<Bound<'_, PyAny>>>()?;
        let mut before_runner = true;
        for implementation in implementations.iter().rev() {
            let plugin: String = implementation
                .getattr(intern!(py, "plugin_name"))?
                .extract()?;
            if plugin == "runner" {
                before_runner = false;
                continue;
            }
            if plugin == "warnings" {
                hooks.warning_position = Some(hooks.protocols.len());
                continue;
            }
            if plugin == "unittest" {
                continue;
            }
            let names: Vec<String> = implementation.getattr(intern!(py, "argnames"))?.extract()?;
            let arguments = PhaseHookArguments::from_names(&names).ok_or_else(|| {
                PyTypeError::new_err(format!(
                    "unsupported pytest_runtest_protocol arguments from {plugin}: {names:?}"
                ))
            })?;
            let hook = PhaseHook {
                function: implementation.getattr(intern!(py, "function"))?.unbind(),
                style: HookStyle::of(implementation)?,
                arguments,
            };
            if hook.style.wraps() {
                hooks
                    .protocols
                    .push((hook, implementation.clone().unbind()));
            } else if before_runner {
                hooks.protocol_overrides.push((hook, plugin));
            }
        }
        Ok(hooks)
    }
    fn enter_wrapper<'py>(
        &self,
        context: &mut ProtocolContext,
        hook: &PhaseHook,
        implementation: &Py<PyAny>,
        item: &Bound<'py, PyAny>,
        nextitem: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<()> {
        let py = item.py();
        let (protocol, style) = if hook.style == HookStyle::LegacyWrapper {
            if let Some(adapter) = &self.legacy_adapter {
                // Pluggy 1.6 adapts legacy wrappers into new-style wrappers.
                let function = adapter.function.bind(py);
                let arguments = hook.arguments(item, nextitem)?;
                let wrapper = match adapter.keywords {
                    AdapterKeywords::Absent => {
                        function.call1((implementation, "pytest_runtest_protocol", arguments))?
                    }
                    AdapterKeywords::Accepted => function.call1((
                        implementation,
                        "pytest_runtest_protocol",
                        arguments,
                        py.None(),
                    ))?,
                };
                (wrapper, HookStyle::Wrapper)
            } else {
                // Pluggy 1.5 shares one Result across its mixed wrapper stack,
                // including when a legacy wrapper fails before yielding.
                context.legacy_result = Some(
                    py.import(intern!(py, "pluggy"))?
                        .getattr(intern!(py, "Result"))?
                        .unbind(),
                );
                (hook.call(item, nextitem)?, HookStyle::LegacyWrapper)
            }
        } else {
            (hook.call(item, nextitem)?, HookStyle::Wrapper)
        };
        if let Err(error) = py
            .import(intern!(py, "builtins"))?
            .getattr(intern!(py, "next"))?
            .call1((&protocol,))
        {
            if error.is_instance_of::<PyStopIteration>(py) {
                py.import(intern!(py, "pluggy._callers"))?
                    .call_method1(intern!(py, "_raise_wrapfail"), (&protocol, "did not yield"))?;
            }
            return Err(error);
        }
        context.push(protocol.unbind(), style);
        if style == HookStyle::LegacyWrapper {
            context
                .legacy_implementations
                .push(implementation.clone_ref(py));
        }
        Ok(())
    }
}

pub(crate) struct HookGlobals {
    globals: Py<PyDict>,
    builtins: Py<PyDict>,
}

impl HookGlobals {
    pub(crate) fn new(function: &Bound<'_, PyAny>, module: &str) -> PyResult<Option<Self>> {
        let py = function.py();
        if !function.is_exact_instance_of::<PyFunction>()
            || !function
                .getattr(intern!(py, "__module__"))?
                .extract::<String>()
                .is_ok_and(|actual| actual == module)
        {
            return Ok(None);
        }
        let globals = function.getattr(intern!(py, "__globals__"))?;
        let builtins = function.getattr(intern!(py, "__builtins__"))?;
        if !globals.is_exact_instance_of::<PyDict>() || !builtins.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        Ok(Some(Self {
            globals: globals.cast_into()?.unbind(),
            builtins: builtins.cast_into()?.unbind(),
        }))
    }

    pub(crate) fn lookup<'py>(
        &self,
        py: Python<'py>,
        name: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let function = match self.globals.bind(py).get_item(name)? {
            Some(function) => Some(function),
            None => self.builtins.bind(py).get_item(name)?,
        };
        if let Some(function) = function {
            return Ok(function);
        }
        let error = PyNameError::new_err(format!("name '{}' is not defined", name.to_str()?));
        error.value(py).setattr(intern!(py, "name"), name)?;
        Err(error)
    }
}

/// Coordinates built-in lifecycle services and project or reviewed plug-in phase hooks.
pub(crate) struct LifecycleService {
    protocols: ProtocolHooks,
    phases: [Vec<PhaseHook>; 3],
    project_phases: [bool; 3],
    current_test_globals: [Option<HookGlobals>; 3],
    xfail_globals: [Option<HookGlobals>; 2],
    // pytest-timeout's `--session-timeout` sets `session.shouldfail`.
    session_stops: bool,
}

impl LifecycleService {
    pub(crate) fn new(items: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = items.py();
        let mut service = Self {
            protocols: ProtocolHooks::default(),
            phases: std::array::from_fn(|_| Vec::new()),
            project_phases: [false; 3],
            current_test_globals: std::array::from_fn(|_| None),
            xfail_globals: std::array::from_fn(|_| None),
            session_stops: false,
        };
        let Some(first) = items.try_iter()?.next() else {
            return Ok(service);
        };
        let config = first?.getattr(intern!(py, "config"))?;
        let manager = config.getattr(intern!(py, "pluginmanager"))?;
        service.protocols = ProtocolHooks::new(&config)?;
        let timeout = TIMEOUT_PLUGIN_NAMES
            .iter()
            .map(|name| manager.call_method1(intern!(py, "getplugin"), (name,)))
            .find(|plugin| plugin.as_ref().map_or(true, |plugin| !plugin.is_none()))
            .transpose()?;
        service.session_stops = !service.protocols.protocols.is_empty()
            || !service.protocols.protocol_overrides.is_empty();
        if let Some(timeout) = timeout {
            service.session_stops = true;
            // `func_only` timeouts cover only the call phase.
            service.phases[1].push(PhaseHook {
                function: timeout
                    .getattr(intern!(py, "pytest_runtest_call"))?
                    .unbind(),
                style: HookStyle::LegacyWrapper,
                arguments: PhaseHookArguments::Item,
            });
        }
        if BENCHMARK_PLUGIN_NAMES
            .iter()
            .map(|name| manager.call_method1(intern!(py, "getplugin"), (name,)))
            .collect::<PyResult<Vec<_>>>()?
            .iter()
            .any(|plugin| !plugin.is_none())
        {
            // Upstream marks its fixture skipped from `pytest_runtest_makereport`,
            // which native execution does not call; vfy's wrapper sets the flag.
            service.phases[1].push(PhaseHook {
                function: py
                    .import(intern!(py, "vfy._pytest.benchmark_support"))?
                    .getattr(intern!(py, "pytest_runtest_call"))?
                    .unbind(),
                style: HookStyle::Wrapper,
                arguments: PhaseHookArguments::Item,
            });
        }
        for (index, name) in ["setup", "call", "teardown"].iter().enumerate() {
            let implementations = config
                .getattr(intern!(py, "hook"))?
                .getattr(format!("pytest_runtest_{name}"))?
                .call_method0(intern!(py, "get_hookimpls"))?;
            for implementation in implementations.try_iter()? {
                let implementation = implementation?;
                let plugin: String = implementation
                    .getattr(intern!(py, "plugin_name"))?
                    .extract()?;
                if plugin == "runner" {
                    let function = implementation.getattr(intern!(py, "function"))?;
                    service.current_test_globals[index] =
                        HookGlobals::new(&function, "_pytest.runner")?;
                }
                if plugin == "skipping" && index < 2 {
                    let function = implementation.getattr(intern!(py, "function"))?;
                    service.xfail_globals[index] = HookGlobals::new(&function, "_pytest.skipping")?;
                }
                if is_conftest(&implementation)?
                    || matches!(plugin.as_str(), "randomly" | "pytest_randomly")
                {
                    service.project_phases[index] = true;
                }
                if matches!(plugin.as_str(), "threadexception" | "unraisableexception") {
                    let names: Vec<String> =
                        implementation.getattr(intern!(py, "argnames"))?.extract()?;
                    let arguments = match names.as_slice() {
                        [] => PhaseHookArguments::None,
                        [name] if name == "item" => PhaseHookArguments::Item,
                        _ => {
                            return Err(PyTypeError::new_err(format!(
                                "unsupported {plugin} phase hook arguments: {names:?}"
                            )));
                        }
                    };
                    service.phases[index].push(PhaseHook {
                        function: implementation.getattr(intern!(py, "function"))?.unbind(),
                        style: HookStyle::of(&implementation)?,
                        arguments,
                    });
                }
            }
        }
        Ok(service)
    }

    pub(crate) fn evaluate_xfail_marks<'py>(
        &self,
        item: &Bound<'py, PyAny>,
        phase: ExecutionPhase,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = item.py();
        let index = match phase {
            ExecutionPhase::Setup => Some(0),
            ExecutionPhase::Call => Some(1),
            ExecutionPhase::Teardown => None,
        };
        let name = intern!(py, "evaluate_xfail_marks");
        if let Some(globals) = index.and_then(|index| self.xfail_globals[index].as_ref()) {
            globals.lookup(py, name)?.call1((item,))
        } else {
            py.import(intern!(py, "_pytest.skipping"))?
                .call_method1(name, (item,))
        }
    }

    pub(crate) fn update_current_test(
        &self,
        item: &Bound<'_, PyAny>,
        phase: Option<&str>,
    ) -> PyResult<()> {
        let py = item.py();
        let index = match phase {
            Some("setup") => Some(0),
            Some("call") => Some(1),
            Some("teardown") | None => Some(2),
            Some(_) => None,
        };
        let phase = phase.map(|phase| match phase {
            "setup" => intern!(py, "setup").clone(),
            "call" => intern!(py, "call").clone(),
            "teardown" => intern!(py, "teardown").clone(),
            phase => PyString::new(py, phase),
        });
        let name = intern!(py, "_update_current_test_var");
        if let Some(globals) = index.and_then(|index| self.current_test_globals[index].as_ref()) {
            // Registered runner hooks resolve live globals, independent of sys.modules aliases.
            globals.lookup(py, name)?.call1((item, phase))?;
        } else {
            py.import(intern!(py, "_pytest.runner"))?
                .call_method1(name, (item, phase))?;
        }
        Ok(())
    }

    pub(crate) fn has_project_phases(&self) -> bool {
        self.project_phases.iter().any(|present| *present)
    }

    /// Whether a hook can stop the session between tests, so the loop must
    /// check `session.shouldfail` and `session.shouldstop` after each one.
    pub(crate) fn can_stop_session(&self) -> bool {
        self.has_project_phases() || self.session_stops
    }

    /// Enter protocol wrappers and call the plain hooks pluggy runs before the runner.
    ///
    /// # Errors
    /// Returns hook errors, or an unsupported-feature error when a plain hook
    /// returns a result and so would replace pytest's test protocol.
    pub(crate) fn enter(
        &self,
        item: &Bound<'_, PyAny>,
        nextitem: Option<&Bound<'_, PyAny>>,
        warnings: &mut WarningService,
        profile: &mut usize,
    ) -> PyResult<Option<ProtocolContext>> {
        let py = item.py();
        // pytest dispatches protocol hooks through config.hook, including hooks
        // from conftests outside this item's directory.
        let hooks = &self.protocols;
        let mut context = ProtocolContext::default();
        context.preserve_stop_iteration = hooks.legacy_adapter.is_some();
        let entered = (|| {
            for index in 0..=hooks.protocols.len() {
                if hooks.warning_position == Some(index) {
                    // Item filters belong at pytest's warnings-wrapper slot;
                    // outer tryfirst wrappers execute outside that scope.
                    *profile = warnings.refresh_profile_for_case(py, item, *profile, None)?;
                    warnings.enter_profile(
                        py,
                        &item.getattr(intern!(py, "nodeid"))?.extract::<String>()?,
                        *profile,
                    )?;
                    context.warning_depth = Some(context.depth());
                }
                let Some((hook, implementation)) = hooks.protocols.get(index) else {
                    break;
                };
                hooks.enter_wrapper(&mut context, hook, implementation, item, nextitem)?;
            }
            for (hook, plugin) in &hooks.protocol_overrides {
                if !hook.call(item, nextitem)?.is_none() {
                    return Err(UnsupportedFeatureError::new_err(format!(
                        "native pytest cannot run the test protocol from {plugin}: its \
                         pytest_runtest_protocol returned a result for {}",
                        item.getattr(intern!(py, "nodeid"))?
                    )));
                }
            }
            Ok(())
        })();
        if let Err(error) = entered {
            // A replacement protocol cannot be honored even if a wrapper handles
            // the rejection; do not report a successful run of omitted tests.
            let rejection = error
                .is_instance_of::<UnsupportedFeatureError>(py)
                .then(|| error.clone_ref(py));
            let outcome = context.finish(py, Err(error), warnings);
            if let Some(rejection) = rejection {
                return Err(rejection);
            }
            outcome?;
            return Ok(None);
        }
        Ok(Some(context))
    }

    pub(crate) fn with_phase<T>(
        &self,
        item: &Bound<'_, PyAny>,
        phase: ExecutionPhase,
        run: impl FnOnce(&mut PhaseContext<'_>) -> PyResult<T>,
    ) -> PyResult<T> {
        let mut scope = self.enter_phase(item, phase)?;
        let result = run(&mut scope);
        scope.finish(item, result.as_ref().err())?;
        result
    }

    pub(crate) fn enter_phase(
        &self,
        item: &Bound<'_, PyAny>,
        phase: ExecutionPhase,
    ) -> PyResult<PhaseContext<'_>> {
        let py = item.py();
        let index = match phase {
            ExecutionPhase::Setup => 0,
            ExecutionPhase::Call => 1,
            ExecutionPhase::Teardown => 2,
        };
        let mut context = PhaseContext {
            wrappers: ProtocolContext::default(),
            hooks: &self.phases[index],
            ordered: if self.project_phases[index] {
                Some(ordered_phase(item, phase)?)
            } else {
                None
            },
            cursor: 0,
            nextitem: None,
        };
        // Pluggy invokes implementations in reverse registration order. Older
        // pytest versions use wrappers; newer versions drain configured queues.
        for hook in self.phases[index].iter().rev() {
            if hook.style.wraps() {
                let protocol = hook.call(item, None)?;
                protocol.call_method0(intern!(py, "__next__"))?;
                context.wrappers.push(protocol.unbind(), hook.style);
            }
        }
        Ok(context)
    }
}

#[derive(Default)]
pub(crate) struct ProtocolContext {
    inline: [Option<(Py<PyAny>, HookStyle)>; 2],
    protocols: Vec<(Py<PyAny>, HookStyle)>,
    warning_depth: Option<usize>,
    legacy_result: Option<Py<PyAny>>,
    legacy_implementations: Vec<Py<PyAny>>,
    preserve_stop_iteration: bool,
}

impl ProtocolContext {
    fn push(&mut self, protocol: Py<PyAny>, style: HookStyle) {
        if let Some(slot) = self.inline.iter_mut().find(|slot| slot.is_none()) {
            *slot = Some((protocol, style));
        } else {
            self.protocols.push((protocol, style));
        }
    }

    fn pop(&mut self) -> Option<(Py<PyAny>, HookStyle)> {
        self.protocols
            .pop()
            .or_else(|| self.inline.iter_mut().rev().find_map(Option::take))
    }

    fn is_empty(&self) -> bool {
        self.protocols.is_empty() && self.inline.iter().all(Option::is_none)
    }

    fn depth(&self) -> usize {
        self.protocols.len() + self.inline.iter().filter(|entry| entry.is_some()).count()
    }

    fn initial_legacy_outcome<'py>(
        &self,
        py: Python<'py>,
        outcome: &PyResult<Py<PyAny>>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        match &self.legacy_result {
            Some(class) => {
                let (result, error) = match outcome {
                    Ok(value) => (value.clone_ref(py), py.None()),
                    Err(error) => (py.None(), error.clone_ref(py).into_value(py).into_any()),
                };
                Ok(Some(class.bind(py).call1((result, error))?))
            }
            None => Ok(None),
        }
    }

    fn update_legacy_outcome(
        legacy: &Bound<'_, PyAny>,
        outcome: &PyResult<Py<PyAny>>,
    ) -> PyResult<()> {
        let py = legacy.py();
        match outcome {
            Ok(value) => legacy.call_method1(intern!(py, "force_result"), (value,))?,
            Err(error) => legacy.call_method1(
                intern!(py, "force_exception"),
                (error.clone_ref(py).into_value(py),),
            )?,
        };
        Ok(())
    }

    fn finish_legacy_wrapper(
        &mut self,
        py: Python<'_>,
        protocol: &Bound<'_, PyAny>,
        legacy: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let implementation = self.legacy_implementations.pop().ok_or_else(|| {
            PyRuntimeError::new_err("legacy protocol wrapper has no hook implementation")
        })?;
        match protocol.call_method1(intern!(py, "send"), (legacy,)) {
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => Ok(()),
            Err(error) => {
                py.import(intern!(py, "pluggy._callers"))?.call_method1(
                    intern!(py, "_warn_teardown_exception"),
                    ("pytest_runtest_protocol", implementation, error.value(py)),
                )?;
                Err(error)
            }
            Ok(_) => py
                .import(intern!(py, "pluggy._callers"))?
                .call_method1(
                    intern!(py, "_raise_wrapfail"),
                    (protocol, "has second yield"),
                )
                .map(drop),
        }
    }

    /// Carry each wrapper's result or exception to the next enclosing wrapper.
    pub(crate) fn finish(
        &mut self,
        py: Python<'_>,
        mut outcome: PyResult<Py<PyAny>>,
        warnings: &mut WarningService,
    ) -> PyResult<Py<PyAny>> {
        let legacy_outcome = self.initial_legacy_outcome(py, &outcome)?;
        loop {
            if self.warning_depth == Some(self.depth()) {
                self.warning_depth = None;
                if let Err(error) = warnings.exit_item(py) {
                    outcome = Err(error);
                }
                if let Some(legacy) = &legacy_outcome {
                    Self::update_legacy_outcome(legacy, &outcome)?;
                }
            }
            let Some((protocol, style)) = self.pop() else {
                break;
            };
            let protocol = protocol.bind(py);
            if let Some(legacy) = &legacy_outcome
                && style == HookStyle::LegacyWrapper
            {
                if let Err(error) = self.finish_legacy_wrapper(py, protocol, legacy) {
                    let mut teardowns = Vec::new();
                    while let Some((protocol, _)) = self.pop() {
                        teardowns.push(protocol);
                    }
                    // Pluggy stores the stack outermost first; Python's
                    // eventual list cleanup releases it in reverse order.
                    teardowns.reverse();
                    // Pluggy 1.5 aborts its mixed-stack unwind here. Its
                    // traceback retains the remaining generators until the
                    // error is released, rather than closing them now.
                    return py
                        .import(intern!(py, "vfy._pytest.native_pytest"))?
                        .call_method1(
                            intern!(py, "raise_protocol_teardown_error"),
                            (error.value(py), teardowns),
                        )
                        .map(Bound::unbind);
                }
                let error = legacy.getattr(intern!(py, "_exception"))?;
                // Reading the retained exception must not raise it: outer
                // legacy wrappers observe its original traceback in 1.5.
                outcome = if error.is_none() {
                    legacy.getattr(intern!(py, "_result")).map(Bound::unbind)
                } else {
                    Err(PyErr::from_value(error))
                };
                continue;
            }
            let resumed = match &outcome {
                Err(error) => protocol
                    .call_method1(intern!(py, "throw"), (error.clone_ref(py).into_value(py),)),
                Ok(value) => protocol.call_method1(intern!(py, "send"), (value.bind(py),)),
            };
            outcome = match resumed {
                Err(error) if error.is_instance_of::<PyStopIteration>(py) => error
                    .value(py)
                    .getattr(intern!(py, "value"))
                    .map(Bound::unbind),
                Err(error)
                    if self.preserve_stop_iteration
                        && error.is_instance_of::<PyRuntimeError>(py)
                        && outcome.as_ref().err().is_some_and(|original| {
                            original.is_instance_of::<PyStopIteration>(py)
                                && error
                                    .cause(py)
                                    .is_some_and(|cause| cause.value(py).is(original.value(py)))
                        }) =>
                {
                    // PEP 479 wraps a thrown StopIteration in RuntimeError;
                    // pluggy preserves the original hook exception in this case.
                    match protocol.call_method0(intern!(py, "close")) {
                        Ok(_) => outcome,
                        Err(error) => Err(error),
                    }
                }
                Err(error) => Err(error),
                Ok(_) => match protocol.call_method0(intern!(py, "close")) {
                    Err(error) => Err(error),
                    Ok(_) => {
                        // Pluggy rejects a second yield outside its exception
                        // propagation loop, so outer wrappers cannot recover it.
                        return py
                            .import(intern!(py, "pluggy._callers"))?
                            .call_method1(
                                intern!(py, "_raise_wrapfail"),
                                (protocol, "has second yield"),
                            )
                            .map(Bound::unbind);
                    }
                },
            };
            if let Some(legacy) = &legacy_outcome {
                Self::update_legacy_outcome(legacy, &outcome)?;
            }
        }
        if let Some(legacy) = legacy_outcome {
            legacy
                .call_method0(intern!(py, "get_result"))
                .map(Bound::unbind)
        } else {
            outcome
        }
    }
}

impl Drop for ProtocolContext {
    fn drop(&mut self) {
        if self.is_empty() {
            return;
        }
        // An abandoned context has no result to report; closing runs each
        // wrapper's cleanup without claiming that the protocol completed.
        Python::attach(|py| {
            while let Some((protocol, _)) = self.pop() {
                let _ = protocol.bind(py).call_method0(intern!(py, "close"));
            }
        });
    }
}

pub(crate) struct PhaseContext<'a> {
    wrappers: ProtocolContext,
    hooks: &'a [PhaseHook],
    ordered: Option<Vec<PhaseOperation>>,
    cursor: usize,
    nextitem: Option<Py<PyAny>>,
}

impl PhaseContext<'_> {
    pub(crate) fn builtin_cleanup(&mut self) {
        self.ordered = None;
    }

    pub(crate) fn needs_nextitem(&self) -> bool {
        self.ordered.as_ref().is_some_and(|operations| {
            operations.iter().any(|operation| {
                matches!(
                    operation,
                    PhaseOperation::Hook(PhaseHook {
                        arguments: PhaseHookArguments::NextItem
                            | PhaseHookArguments::ItemNextItem
                            | PhaseHookArguments::NextItemItem,
                        ..
                    })
                )
            })
        })
    }

    pub(crate) fn set_nextitem(&mut self, nextitem: Option<Py<PyAny>>) {
        self.nextitem = nextitem;
    }

    /// Invoke project and built-in callbacks up to the native operation's slot.
    pub(crate) fn before(&mut self, item: &Bound<'_, PyAny>, anchor: PhaseAnchor) -> PyResult<()> {
        let Some(operations) = &self.ordered else {
            return Ok(());
        };
        if anchor != PhaseAnchor::End
            && !operations[self.cursor..].iter().any(
                |operation| matches!(operation, PhaseOperation::Anchor(name) if *name == anchor),
            )
        {
            // Disabled built-in hooks have no slot; do not consume later callbacks.
            return Ok(());
        }
        while let Some(operation) = operations.get(self.cursor) {
            self.cursor += 1;
            match operation {
                PhaseOperation::Anchor(name) if *name == anchor => return Ok(()),
                PhaseOperation::Anchor(_) => {}
                PhaseOperation::Hook(hook) => {
                    hook.call(
                        item,
                        self.nextitem.as_ref().map(|next| next.bind(item.py())),
                    )?;
                }
            }
        }
        Ok(())
    }

    pub(crate) fn finish(
        mut self,
        item: &Bound<'_, PyAny>,
        original: Option<&PyErr>,
    ) -> PyResult<()> {
        let py = item.py();
        let mut failure = original.map(|error| error.clone_ref(py));
        if failure.is_none() && self.ordered.is_some() {
            failure = self.before(item, PhaseAnchor::End).err();
        } else if failure.is_none() {
            for hook in self.hooks.iter().rev().filter(|hook| !hook.style.wraps()) {
                if let Err(error) = hook.call(item, None) {
                    failure = Some(error);
                    break;
                }
            }
        }
        while let Some((protocol, style)) = self.wrappers.pop() {
            let result = match &failure {
                Some(error) if style == HookStyle::Wrapper => protocol
                    .bind(py)
                    .call_method1(intern!(py, "throw"), (error.clone_ref(py).into_value(py),)),
                _ => protocol
                    .bind(py)
                    .call_method1(intern!(py, "send"), (py.None(),)),
            };
            match result {
                Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                    if style == HookStyle::Wrapper {
                        failure = None;
                    }
                }
                Err(error) => failure = Some(error),
                Ok(_) => {
                    failure = Some(PyRuntimeError::new_err(
                        "pytest phase lifecycle wrapper yielded more than once",
                    ));
                    let _ = protocol.bind(py).call_method0(intern!(py, "close"));
                }
            }
        }
        match (failure, original) {
            (Some(error), Some(original)) if error.value(py).is(original.value(py)) => Ok(()),
            (Some(error), _) => Err(error),
            (None, _) => Ok(()),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum PhaseAnchor {
    Skipping,
    Runner,
    End,
}

enum PhaseOperation {
    Anchor(PhaseAnchor),
    Hook(PhaseHook),
}

enum PhasePlugin {
    Runner,
    Skipping,
    Exception,
    Randomly,
    Other,
}

fn phase_plugin(name: &Bound<'_, PyAny>) -> PyResult<PhasePlugin> {
    fn classify(name: &str) -> PhasePlugin {
        match name {
            "runner" => PhasePlugin::Runner,
            "skipping" => PhasePlugin::Skipping,
            "threadexception" | "unraisableexception" => PhasePlugin::Exception,
            "randomly" | "pytest_randomly" => PhasePlugin::Randomly,
            _ => PhasePlugin::Other,
        }
    }

    if let Ok(name) = name.cast_exact::<PyString>() {
        return Ok(classify(name.to_str()?));
    }
    Ok(classify(&name.extract::<String>()?))
}

fn tuple_arguments(
    names: &Bound<'_, PyAny>,
    phase: ExecutionPhase,
) -> PyResult<Option<PhaseHookArguments>> {
    let Ok(names) = names.cast_exact::<PyTuple>() else {
        return Ok(None);
    };
    if names.is_empty() {
        return Ok(Some(PhaseHookArguments::None));
    }
    if names.len() > 2 {
        return Ok(None);
    }
    let first = names.get_item(0)?;
    let Ok(first) = first.cast_exact::<PyString>() else {
        return Ok(None);
    };
    let first = first.to_str()?;
    if names.len() == 1 {
        return Ok(decode_arguments(&[first], phase));
    }
    let second = names.get_item(1)?;
    let Ok(second) = second.cast_exact::<PyString>() else {
        return Ok(None);
    };
    Ok(decode_arguments(&[first, second.to_str()?], phase))
}

fn phase_arguments(
    names: &Bound<'_, PyAny>,
    phase: ExecutionPhase,
) -> PyResult<PhaseHookArguments> {
    // Exact immutable inputs can be decoded without allocating owned strings.
    // Other sequences retain extraction behavior, including custom iteration.
    if let Some(arguments) = tuple_arguments(names, phase)? {
        return Ok(arguments);
    }
    let names: Vec<String> = names.extract()?;
    decode_arguments(&names, phase).ok_or_else(|| {
        PyTypeError::new_err(format!(
            "unsupported conftest phase hook arguments: {names:?}"
        ))
    })
}

fn decode_arguments(
    names: &[impl AsRef<str>],
    phase: ExecutionPhase,
) -> Option<PhaseHookArguments> {
    match names {
        [] => Some(PhaseHookArguments::None),
        [name] if name.as_ref() == "item" => Some(PhaseHookArguments::Item),
        [name] if name.as_ref() == "nextitem" && phase == ExecutionPhase::Teardown => {
            Some(PhaseHookArguments::NextItem)
        }
        [first, second]
            if first.as_ref() == "item"
                && second.as_ref() == "nextitem"
                && phase == ExecutionPhase::Teardown =>
        {
            Some(PhaseHookArguments::ItemNextItem)
        }
        [first, second]
            if first.as_ref() == "nextitem"
                && second.as_ref() == "item"
                && phase == ExecutionPhase::Teardown =>
        {
            Some(PhaseHookArguments::NextItemItem)
        }
        _ => None,
    }
}

fn is_conftest(implementation: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = implementation.py();
    let plugin = implementation.getattr(intern!(py, "plugin"))?;
    let Some(path) = plugin.getattr_opt(intern!(py, "__file__"))? else {
        return Ok(false);
    };
    let Ok(path) = path.cast::<PyString>() else {
        return Ok(false);
    };
    Ok(std::path::Path::new(path.to_str()?)
        .file_name()
        .is_some_and(|name| name == "conftest.py"))
}

fn ordered_phase(item: &Bound<'_, PyAny>, phase: ExecutionPhase) -> PyResult<Vec<PhaseOperation>> {
    let py = item.py();
    let hook_name = match phase {
        ExecutionPhase::Setup => intern!(py, "pytest_runtest_setup"),
        ExecutionPhase::Call => intern!(py, "pytest_runtest_call"),
        ExecutionPhase::Teardown => intern!(py, "pytest_runtest_teardown"),
    };
    let implementations = item
        .getattr(intern!(py, "ihook"))?
        .getattr(hook_name)?
        .call_method0(intern!(py, "get_hookimpls"))?;
    let mut ordered = Vec::new();
    for implementation in implementations.try_iter()? {
        let implementation = implementation?;
        let plugin = phase_plugin(&implementation.getattr(intern!(py, "plugin_name"))?)?;
        if matches!(plugin, PhasePlugin::Runner) {
            ordered.push(PhaseOperation::Anchor(PhaseAnchor::Runner));
        } else if matches!(plugin, PhasePlugin::Skipping) && phase == ExecutionPhase::Setup {
            ordered.push(PhaseOperation::Anchor(PhaseAnchor::Skipping));
        } else if is_conftest(&implementation)?
            || matches!(plugin, PhasePlugin::Exception | PhasePlugin::Randomly)
        {
            if implementation
                .getattr(intern!(py, "wrapper"))?
                .is_truthy()?
                || implementation
                    .getattr(intern!(py, "hookwrapper"))?
                    .is_truthy()?
            {
                // Built-in wrappers are entered and unwound by the existing scope.
                if is_conftest(&implementation)? {
                    return Err(PyTypeError::new_err(
                        "native pytest does not support conftest phase hook wrappers",
                    ));
                }
                if matches!(plugin, PhasePlugin::Randomly) {
                    return Err(PyTypeError::new_err(
                        "native pytest does not support pytest-randomly phase hook wrappers",
                    ));
                }
                continue;
            }
            let arguments =
                phase_arguments(&implementation.getattr(intern!(py, "argnames"))?, phase)?;
            ordered.push(PhaseOperation::Hook(PhaseHook {
                function: implementation.getattr(intern!(py, "function"))?.unbind(),
                style: HookStyle::Plain,
                arguments,
            }));
        }
    }
    ordered.reverse();
    Ok(ordered)
}

#[cfg(test)]
mod tests;
