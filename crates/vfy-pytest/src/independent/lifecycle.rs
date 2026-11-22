//! Context shared by the lifecycle hook dispatchers for one invocation.

use super::CollectedSuite;
use crate::hooks::NativeHookRegistry;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyString, PyTuple};
use std::cell::{Cell, RefCell};
use std::path::PathBuf;
use std::rc::Rc;
use vfy_core::independent_collection::{
    CollectionArgumentPolicy, CollectionSettings, CollectionSpecification,
};
use vfy_core::test_execution::WarningRecord;
mod admission;
pub(super) mod cleanup;
pub(super) mod collection;
pub(super) mod commandline;
mod configure;
mod diagnostics;
mod directory;
mod header;
mod internalerror;
pub(super) mod nodes;
mod pythonpath;
mod selection;
pub(super) mod state;
mod warnings;
use state::{InvocationState, LifecycleStage};

pub(super) struct Context {
    pub(super) config: Py<PyAny>,
    pub(super) session: Py<PyAny>,
    pub(super) items: Vec<Py<PyAny>>,
    pub(super) registry: Py<NativeHookRegistry>,
    pub(super) warnings: Rc<RefCell<Vec<WarningRecord>>>,
    pub(super) parser: Py<PyAny>,
    pub(super) raw_plugin_prefix: Cell<Option<usize>>,
    terminal_status_provider: Rc<RefCell<Option<Py<PyAny>>>>,
    pub(super) terminal_summary: Rc<RefCell<Option<Py<pyo3::types::PyWeakrefReference>>>>,
    pub(super) stage: Rc<Cell<LifecycleStage>>,
    pub(super) invocation: Rc<InvocationState>,
}

impl Context {
    #[expect(
        clippy::too_many_lines,
        reason = "the constructor passes the original Python configuration operands through ordered provider registration and historic hooks"
    )]
    pub(super) fn new_owned(
        py: Python<'_>,
        root: &str,
        paths: &[String],
        options: Option<&Bound<'_, PyDict>>,
        invocation: Rc<InvocationState>,
    ) -> PyResult<Self> {
        let raw_options = match options {
            Some(options) if !options.contains("_arguments")? => Some(options.copy()?),
            _ => None,
        };
        let protocol = py.import("vfy.independent.protocol")?;
        let registry = Py::new(py, NativeHookRegistry::new())?;
        let manager = protocol.getattr("PluginManager")?.call1((&registry,))?;
        admission::install(py, registry.bind(py), &manager)?;
        let supplied = options
            .map(|options| options.get_item("_configuration"))
            .transpose()?
            .flatten();
        let constructed = supplied.is_none();
        let configuration = if let Some(configuration) = supplied {
            configuration
        } else {
            let arguments = options
                .map(|options| options.get_item("_arguments"))
                .transpose()?
                .flatten()
                .unwrap_or(PyList::new(py, paths)?.into_any());
            let environment = py
                .import("os")?
                .getattr("environ")?
                .call_method1("get", ("PYTEST_ADDOPTS",))?;
            let result = py
                .import("vfy.native")?
                .getattr("NativePytestConfiguration")?
                .call1((arguments, root, environment));
            match result {
                Ok(configuration) => configuration,
                Err(error) => {
                    record_preparation_usage(py, &invocation, &error);
                    return Err(error);
                }
            }
        };
        let projection = configuration.call_method0("projection")?;
        let (cli_args, _) = configuration
            .call_method0("plugin_preparse_args")?
            .extract::<(Vec<String>, Vec<String>)>()?;
        if let Err(error) = super::plugins::exclude_cli_plugins(&manager, &cli_args) {
            record_preparation_usage(py, &invocation, &error);
            return Err(error);
        }
        if constructed && let Some(options) = options {
            options.set_item("_arguments", projection.get_item("effective_args")?)?;
        }
        let parser = protocol.getattr("Parser")?.call1((options
            .map(|options| options.get_item("_parser"))
            .transpose()?
            .flatten(),))?;
        let ini = parser.getattr("_ini")?;
        let values = Self::parser_option_values(&parser, options)?;
        let config = protocol.getattr("Config")?.call1((
            &manager,
            projection.get_item("rootdir")?,
            values,
            configuration,
            ini,
            projection.get_item("config_file")?,
        ))?;
        config.setattr("_parser", &parser)?;
        config.setattr("_vfy_raw_options", raw_options)?;
        parser.setattr("_processopt", config.getattr("_processopt")?)?;
        py.import("vfy.independent.imports")?
            .call_method1("install_assertion_config", (&config,))?;
        let session = protocol.getattr("Session")?.call1((&config,))?;
        super::execution::install_node_finalizers(py, &session)?;
        super::collection::arena::install(
            py,
            &session,
            PathBuf::from(config.getattr("rootpath")?.str()?.to_str()?),
        )?;
        if invocation.retain_session.get() {
            *invocation.session.borrow_mut() = Some(session.clone().unbind());
        }
        let context = Self {
            config: config.unbind(),
            session: session.unbind(),
            items: Vec::new(),
            registry,
            warnings: Rc::default(),
            parser: parser.unbind(),
            raw_plugin_prefix: Cell::new(None),
            terminal_status_provider: Rc::default(),
            terminal_summary: Rc::default(),
            stage: Rc::clone(&invocation.stage),
            invocation,
        };
        context.register(py, context.config.clone_ref(py), "pytestconfig")?;
        let manager = context.config.bind(py).getattr("pluginmanager")?;
        context.register_status_hooks(py, &protocol)?;
        context.call(py, "pytest_addhooks", &[("pluginmanager", &manager)])?;
        context.call(
            py,
            "pytest_addoption",
            &[
                ("parser", context.parser.bind(py)),
                ("pluginmanager", &manager),
            ],
        )?;
        context.install_configure(py)?;
        context.install_junit_lifecycle(py)?;
        super::skipping::install(py, &context)?;
        Ok(context)
    }

    /// Visit the Python objects this context keeps alive for the cyclic collector.
    pub(super) fn traverse(&self, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.config)?;
        visit.call(&self.session)?;
        visit.call(&self.registry)?;
        visit.call(&self.parser)?;
        for item in &self.items {
            visit.call(item)?;
        }
        Ok(())
    }

    fn parser_option_values<'py>(
        parser: &Bound<'py, PyAny>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let values = parser.call_method0("defaults")?.cast::<PyDict>()?.copy()?;
        if let Some(options) = options {
            for (name, value) in options.iter() {
                values.set_item(name, value)?;
            }
        }
        for (name, value) in [("verbose", 0), ("quiet", 0), ("maxfail", 0)] {
            if !values.contains(name)? {
                values.set_item(name, value)?;
            }
        }
        Ok(values)
    }

    fn register_status_hooks(
        &self,
        py: Python<'_>,
        protocol: &Bound<'_, PyModule>,
    ) -> PyResult<()> {
        let statuses = super::reporting::terminal::callbacks(py)?;
        let manager = self.config.bind(py).getattr("pluginmanager")?;
        for (name, class, callback) in [
            ("runner", "BuiltinRunnerStatusHooks", "runner_status"),
            ("terminal", "BuiltinTerminalStatusHooks", "terminal_status"),
        ] {
            if manager.call_method1("is_blocked", (name,))?.is_truthy()? {
                continue;
            }
            let plugin = if name == "terminal" {
                self.terminal_status_hooks(py)?
            } else {
                protocol
                    .getattr(class)?
                    .call1((
                        statuses.bind(py).getattr(callback)?,
                        statuses.bind(py).getattr("runner_makereport")?,
                    ))?
                    .unbind()
            };
            self.register(py, plugin, name)?;
        }
        Ok(())
    }

    pub(super) fn terminal_status_hooks(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let retained = self
            .terminal_status_provider
            .borrow()
            .as_ref()
            .map(|plugin| plugin.clone_ref(py));
        if let Some(plugin) = retained {
            return Ok(plugin);
        }
        let statuses = super::reporting::terminal::callbacks(py)?;
        let plugin = py
            .import("vfy.independent.protocol")?
            .getattr("BuiltinTerminalStatusHooks")?
            .call1((statuses.bind(py).getattr("terminal_status")?,))?
            .unbind();
        // Keep the actual provider through unregistration, as pytest retains its
        // imported builtin module. Public registry replacements cannot mint it.
        let previous = self
            .terminal_status_provider
            .replace(Some(plugin.clone_ref(py)));
        drop(previous);
        Ok(plugin)
    }

    pub(super) fn parse_options(
        &self,
        py: Python<'_>,
        paths: &[String],
        options: Option<&Bound<'_, PyDict>>,
        final_parse: bool,
    ) -> PyResult<Vec<String>> {
        let Some(options) = options else {
            return Ok(paths.to_vec());
        };
        let Some(arguments) = options.get_item("_arguments")? else {
            return Ok(paths.to_vec());
        };
        let capture = final_parse;
        let consumed = capture
            .then(|| Self::builtin_arguments(&arguments))
            .flatten();
        if capture {
            *self.invocation.consumed_arguments.borrow_mut() =
                Some(crate::parallel::independent::ConsumedArguments::Unavailable);
        }
        let parsed = self
            .parser
            .bind(py)
            .call_method1("parse", (&arguments, final_parse))
            .map_err(|error| {
                if error.is_instance_of::<pyo3::exceptions::PySystemExit>(py) {
                    crate::IndependentUsageError::new_err("invalid command line arguments")
                } else {
                    error
                }
            })?;
        if let Some(consumed) = consumed
            && Self::builtin_arguments(&arguments).as_ref() == Some(&consumed)
        {
            *self.invocation.consumed_arguments.borrow_mut() =
                Some(crate::parallel::independent::ConsumedArguments::Stable {
                    arguments: consumed,
                    parser: {
                        let version = py.version_info();
                        vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                            version.major,
                            version.minor,
                            version.patch,
                        )
                    },
                });
        }
        let values = parsed.cast::<PyDict>()?;
        let raw_options = self.config.bind(py).getattr("_vfy_raw_options")?;
        if !raw_options.is_none() {
            for (name, value) in raw_options.cast::<PyDict>()?.iter() {
                if name.extract::<String>()? == "plugins" {
                    let configured = values.get_item("plugins")?;
                    let prefix = configured.as_ref().map_or(Ok(0), PyAnyMethods::len)?;
                    self.raw_plugin_prefix.set(Some(prefix));
                    if let Some(configured) = configured.filter(|_| prefix != 0) {
                        let plugins = configured.call_method0("copy")?;
                        plugins.call_method1("extend", (&value,))?;
                        values.set_item(name, plugins)?;
                    } else {
                        values.set_item(name, value)?;
                    }
                } else {
                    values.set_item(name, value)?;
                }
            }
        }
        if let Some(value) = values
            .get_item("basetemp")?
            .filter(|value| !value.is_none())
        {
            let root = self.config.bind(py).getattr("_invocation_dir")?.str()?;
            super::validate_basetemp(
                std::path::Path::new(root.to_str()?),
                value.extract::<String>()?.as_str(),
            )
            .map_err(crate::IndependentUsageError::new_err)?;
        }
        let verbose = values
            .get_item("verbose")?
            .map_or(Ok(0), |value| value.extract::<i64>())?;
        let quiet = values
            .get_item("quiet")?
            .map_or(Ok(0), |value| value.extract::<i64>())?;
        values.set_item("verbose", verbose - quiet)?;
        if !final_parse {
            let namespace = py
                .import("argparse")?
                .getattr("Namespace")?
                .call((), Some(values))?;
            self.config
                .bind(py)
                .setattr("known_args_namespace", namespace)?;
        }
        let destination = self.config.bind(py).getattr("option")?;
        for (name, value) in values.iter() {
            options.set_item(&name, &value)?;
            destination.setattr(name.extract::<String>()?, value)?;
        }
        values
            .get_item("paths")?
            .map_or_else(|| Ok(paths.to_vec()), |value| value.extract())
    }

    fn builtin_arguments(arguments: &Bound<'_, PyAny>) -> Option<Vec<String>> {
        let py = arguments.py();
        if !arguments.get_type().is(py.get_type::<PyList>()) {
            return None;
        }
        let arguments = arguments.cast::<PyList>().ok()?;
        if arguments.len() > 65_536 {
            return None;
        }
        arguments
            .iter()
            .map(|value| {
                value
                    .get_type()
                    .is(py.get_type::<PyString>())
                    .then(|| value.extract::<String>().ok())
                    .flatten()
            })
            .collect()
    }

    pub(super) fn specification(
        &self,
        py: Python<'_>,
        invocation_dir: &str,
        paths: &[String],
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<CollectionSpecification> {
        let config = self.config.bind(py);
        let setting = |name| {
            config
                .call_method1("getini", (name,))?
                .extract::<Vec<String>>()
        };
        let option = |name| -> PyResult<Vec<String>> {
            options
                .map(|options| options.get_item(name))
                .transpose()?
                .flatten()
                .map_or_else(|| Ok(Vec::new()), |value| value.extract::<Vec<String>>())
        };
        let rootdir = PathBuf::from(config.getattr("rootpath")?.str()?.to_str()?);
        let argument_policy = if config
            .call_method1("getoption", ("keepduplicates", false))?
            .is_truthy()?
        {
            CollectionArgumentPolicy::Keep
        } else {
            CollectionArgumentPolicy::Normalize
        };
        CollectionSpecification::from_resolved(
            CollectionSettings {
                argument_policy,
                rootdir,
                invocation_dir: PathBuf::from(invocation_dir),
                testpaths: setting("testpaths")?,
                python_files: setting("python_files")?,
                python_classes: setting("python_classes")?,
                python_functions: setting("python_functions")?,
                norecursedirs: setting("norecursedirs")?,
            },
            paths,
            &option("ignore")?,
            &option("ignore_glob")?,
        )
        .map_err(crate::IndependentUsageError::new_err)
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            config: self.config.clone_ref(py),
            session: self.session.clone_ref(py),
            items: self.items.iter().map(|item| item.clone_ref(py)).collect(),
            registry: self.registry.clone_ref(py),
            warnings: Rc::clone(&self.warnings),
            parser: self.parser.clone_ref(py),
            raw_plugin_prefix: Cell::new(self.raw_plugin_prefix.get()),
            terminal_status_provider: Rc::clone(&self.terminal_status_provider),
            terminal_summary: Rc::clone(&self.terminal_summary),
            stage: Rc::clone(&self.stage),
            invocation: Rc::clone(&self.invocation),
        }
    }

    pub(super) fn call(
        &self,
        py: Python<'_>,
        name: &str,
        extra: &[(&str, &Bound<'_, PyAny>)],
    ) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("config", &self.config)?;
        kwargs.set_item("session", &self.session)?;
        for (name, value) in extra {
            kwargs.set_item(name, value)?;
        }
        if matches!(
            name,
            "pytest_addhooks"
                | "pytest_addoption"
                | "pytest_configure"
                | "pytest_plugin_registered"
                | "pytest_warning_recorded"
        ) {
            self.registry
                .bind(py)
                .call_method1("call_historic", (name, kwargs))?;
            Ok(py.None())
        } else {
            NativeHookRegistry::call(self.registry.bind(py), py, name, &kwargs, false)
        }
    }

    pub(super) fn removed_plugins(
        &self,
        py: Python<'_>,
        path: &std::path::Path,
    ) -> PyResult<Py<PyAny>> {
        let removed = self
            .registry
            .bind(py)
            .borrow()
            .conftest_plugins_outside(py, Some(path));
        if removed.is_empty() {
            Ok(PyTuple::empty(py).into_any().unbind())
        } else {
            Ok(PyList::new(py, removed)?.into_any().unbind())
        }
    }

    pub(super) fn register(&self, py: Python<'_>, plugin: Py<PyAny>, name: &str) -> PyResult<()> {
        let manager = self.config.bind(py).getattr("pluginmanager")?;
        manager.call_method1("register", (plugin, name))?;
        Ok(())
    }

    pub(super) fn start(&self, py: Python<'_>) -> PyResult<()> {
        let warnings = py
            .import("vfy.independent.protocol")?
            .getattr("BuiltinWarnings")?
            .call0()?;
        self.register(py, warnings.unbind(), "warnings")?;
        self.capture_warnings(py, "config", || self.start_session(py))
    }

    fn start_session(&self, py: Python<'_>) -> PyResult<()> {
        self.enter_session_directory();
        let reports = Py::new(py, super::reporting::NativeSessionReports::new(py, self))?;
        super::reporting::install_copy_cleanup(py, &reports, self.config.bind(py))?;
        self.session.bind(py).setattr("_vfy_report", reports)?;
        self.register(py, self.session.clone_ref(py), "session")?;
        let values = self.parser.bind(py).call_method0("defaults")?;
        let options = self.config.bind(py).getattr("option")?;
        for (name, value) in values.cast::<PyDict>()?.iter() {
            let name = name.extract::<String>()?;
            if !options.hasattr(&name)? {
                options.setattr(&name, value)?;
            }
        }
        self.registry.bind(py).call_method0("check_pending")?;
        self.stage.set(LifecycleStage::Configuring);
        self.config.bind(py).setattr("_configured", true)?;
        self.call(py, "pytest_configure", &[])?;
        self.stage.set(LifecycleStage::SessionStarting);
        super::parameter_order::session_start(py, self)?;
        self.stage.set(LifecycleStage::Collecting);
        Ok(())
    }

    pub(super) fn finish(&self, py: Python<'_>, exitstatus: i32) -> PyResult<i32> {
        // pytest's successful _main result is normalized with `or 0`.
        let status = exit_status_object(py, exitstatus)?;
        self.finish_session(py, &status)
    }

    pub(super) fn finish_current(&self, py: Python<'_>) -> PyResult<i32> {
        let status = self.session.bind(py).getattr("exitstatus")?;
        self.finish_session(py, &status)
    }

    fn finish_session(&self, py: Python<'_>, status: &Bound<'_, PyAny>) -> PyResult<i32> {
        self.invocation.begin_worker_shutdown(py)?;
        self.session.bind(py).setattr("exitstatus", status)?;
        self.stage.set(LifecycleStage::Finishing);
        self.restore_session_directory(py)?;
        let finished = if let Some(cleanup) = self.deferred_cleanup(py)? {
            let kwargs = PyDict::new(py);
            kwargs.set_item("config", &self.config)?;
            kwargs.set_item("session", &self.session)?;
            kwargs.set_item("exitstatus", status)?;
            NativeHookRegistry::call_with_default(
                self.registry.bind(py),
                py,
                "pytest_sessionfinish",
                &kwargs,
                cleanup.into_any(),
                false,
                "runner",
            )
        } else {
            self.call(py, "pytest_sessionfinish", &[("exitstatus", status)])
        };
        let cleanup_error = self.invocation.fixture_cleanup_error.borrow_mut().take();
        if let Err(error) = &finished
            && cleanup_error
                .as_ref()
                .is_some_and(|actual| actual.bind(py).is(error.value(py)))
            && !super::controls::is_control_error(py, error)?
        {
            // Only the exact error from native fixture unwinding owns pytest's
            // cleanup-failure status. Wrappers can replace or suppress it.
            self.session.bind(py).setattr("exitstatus", 1)?;
            self.invocation.reported(
                py,
                error,
                LifecycleStage::Finishing,
                state::Disposition::Return(1),
                state::FailureOrigin::FixtureCleanup,
            );
        }
        let finished = match finished {
            Err(error)
                if matches!(
                    super::controls::kind(py, &error)?,
                    Some(super::controls::ControlKind::Exit)
                ) =>
            {
                let code = error.value(py).getattr("returncode")?;
                if !code.is_none() {
                    self.session.bind(py).setattr("exitstatus", &code)?;
                }
                diagnostics::write_exit_message(py, &error)?;
                Ok(py.None())
            }
            result => result,
        };
        let closed = if let Err(error) = &finished {
            self.note_error(py, error)?;
            self.close_while_handling(py, error)
        } else {
            self.close(py)
        };
        closed?;
        finished?;
        self.session.bind(py).getattr("exitstatus")?.extract()
    }
}

/// The status object pytest receives: `ExitCode` for the codes it names, `int` otherwise.
pub(super) fn exit_status_object(py: Python<'_>, status: i32) -> PyResult<Bound<'_, PyAny>> {
    if (1..=6).contains(&status) {
        py.import("vfy.independent.facade")?
            .getattr("ExitCode")?
            .call1((status,))
    } else {
        Ok(status.into_pyobject(py)?.into_any())
    }
}

/// Record a usage rejection made while the command line is still being prepared.
fn record_preparation_usage(py: Python<'_>, invocation: &InvocationState, error: &PyErr) {
    if error.is_instance_of::<crate::IndependentUsageError>(py) {
        invocation.record(
            py,
            error,
            LifecycleStage::Preparing,
            state::Disposition::Return(4),
            state::FailureOrigin::Usage,
        );
    }
}

pub(super) fn prepare_items(
    py: Python<'_>,
    suite: &mut CollectedSuite,
    keyword: Option<&str>,
) -> PyResult<()> {
    let context = &mut suite.context;
    let arena = super::collection::arena::for_context(py, context)?;
    let items = super::collection::arena::NativeCollectionArena::items(&arena)?;
    selection::modify_items(py, context, &items, keyword)?;
    super::selection::bind(py, context, &items)
}

#[derive(Clone, Copy)]
enum DraftSelection {
    Missing,
    Unselected(usize),
    Selected(usize),
}

pub(super) fn synchronize_items(py: Python<'_>, suite: &mut CollectedSuite) -> PyResult<()> {
    let context = &mut suite.context;
    let arena = super::collection::arena::for_context(py, context)?;
    let items = super::collection::arena::NativeCollectionArena::items(&arena)?;
    // Dense arena IDs locate the first draft and its selection state directly.
    let mut state = vec![DraftSelection::Missing; arena.borrow().node_count()];
    for (index, binding) in suite.tests.iter().enumerate() {
        let slot = state
            .get_mut(binding.collection_node.index())
            .ok_or_else(|| PyValueError::new_err("unknown collection node"))?;
        if matches!(slot, DraftSelection::Missing) {
            *slot = DraftSelection::Unselected(index);
        }
    }
    let mut selected = Vec::new();
    for item in items.iter() {
        let id = super::collection::arena::NativeCollectionArena::resolve_item(&arena, &item)?;
        let slot = state.get_mut(id.index()).ok_or_else(|| {
            PyValueError::new_err("collection hook selected an item without a native draft")
        })?;
        let index = match *slot {
            DraftSelection::Missing => {
                return Err(PyValueError::new_err(
                    "collection hook selected an item without a native draft",
                ));
            }
            DraftSelection::Unselected(index) | DraftSelection::Selected(index) => index,
        };
        *slot = DraftSelection::Selected(index);
        selected.push((index, item.unbind()));
    }
    for (index, test) in suite.plan.tests().iter().enumerate() {
        if !matches!(
            state[suite.tests[index].collection_node.index()],
            DraftSelection::Selected(_)
        ) {
            suite.deselected_nodeids.push(test.nodeid.clone());
        }
    }
    let order = selected.iter().map(|(index, _)| *index).collect::<Vec<_>>();
    let mut remaining = vec![0usize; suite.tests.len()];
    for &index in &order {
        remaining[index] += 1;
    }
    let mut bindings = std::mem::take(&mut suite.tests)
        .into_iter()
        .map(Some)
        .collect::<Vec<_>>();
    context.items.clear();
    for (index, item) in selected {
        remaining[index] -= 1;
        let binding = if remaining[index] == 0 {
            bindings[index].take()
        } else {
            bindings[index]
                .as_ref()
                .map(|binding| binding.clone_ref(py))
        };
        let Some(binding) = binding else {
            return Err(PyValueError::new_err("selected binding is unavailable"));
        };
        suite.tests.push(binding);
        context.items.push(item);
    }
    suite
        .plan
        .select_tests(&order)
        .map_err(PyValueError::new_err)?;
    Ok(())
}
