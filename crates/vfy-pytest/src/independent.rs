//! Dependency-independent collection and execution through `CPython` objects.

use crate::results::{NativeRunResult, native_run_result};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyModule, PyString};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;
use vfy_core::independent_collection::{
    CollectionFile, CollectionSpecification, validate_basetemp,
};
use vfy_core::selection::TestOutcome;
use vfy_core::test_execution::{
    CollectionOutcome, CollectionReport, ExecutionOutcome, ExecutionPhase, PhasePayload, RunRecord,
    compute_run_counts,
};
use vfy_core::test_plan::{FixtureChains, FixtureExecution, FixturePlan, SuitePlan, TestPlan};
use vfy_core::{FixtureId, FixtureScope};

fn phase_text(py: Python<'_>, phase: ExecutionPhase) -> Bound<'_, PyString> {
    match phase {
        ExecutionPhase::Setup => pyo3::intern!(py, "setup"),
        ExecutionPhase::Call => pyo3::intern!(py, "call"),
        ExecutionPhase::Teardown => pyo3::intern!(py, "teardown"),
    }
    .clone()
}

fn scope_text(py: Python<'_>, scope: FixtureScope) -> Bound<'_, PyString> {
    match scope {
        FixtureScope::Function => pyo3::intern!(py, "function"),
        FixtureScope::Class => pyo3::intern!(py, "class"),
        FixtureScope::Module => pyo3::intern!(py, "module"),
        FixtureScope::Package => pyo3::intern!(py, "package"),
        FixtureScope::Session => pyo3::intern!(py, "session"),
    }
    .clone()
}

fn outcome_text(py: Python<'_>, outcome: ExecutionOutcome) -> Bound<'_, PyString> {
    match outcome.report_outcome() {
        TestOutcome::Passed => pyo3::intern!(py, "passed"),
        TestOutcome::Failed => pyo3::intern!(py, "failed"),
        TestOutcome::Skipped => pyo3::intern!(py, "skipped"),
    }
    .clone()
}

fn collect_text(py: Python<'_>) -> Bound<'_, PyString> {
    pyo3::intern!(py, "collect").clone()
}

/// Initialize intern caches before the corresponding native types are exposed.
pub(crate) fn initialize_protocol_strings(py: Python<'_>) {
    for phase in [
        ExecutionPhase::Setup,
        ExecutionPhase::Call,
        ExecutionPhase::Teardown,
    ] {
        drop(phase_text(py, phase));
    }
    for outcome in [
        ExecutionOutcome::Passed,
        ExecutionOutcome::Failed,
        ExecutionOutcome::Skipped,
    ] {
        drop(outcome_text(py, outcome));
    }
    for scope in [
        FixtureScope::Function,
        FixtureScope::Class,
        FixtureScope::Module,
        FixtureScope::Package,
        FixtureScope::Session,
    ] {
        drop(scope_text(py, scope));
    }
    drop(collect_text(py));
}

mod assignment;
mod asyncio;
pub(crate) mod callspec;
mod capture;
pub(crate) use capture::register_warning_filter;
mod capture_fixture;
mod capture_provider;
mod collection;
mod collection_state;
mod thread_bound;
pub(crate) use collection::arena::NativeCollectionArena;
mod configuration;
pub(crate) mod controls;
mod coordinator;
mod invocation;
pub(crate) use invocation::NativeIndependentInvocation;
mod imports;
mod plugin_fixtures;
mod plugins;
use imports::ImportMode;
use plugin_fixtures::FixtureOrigin;
mod builtin_providers;
pub(crate) use builtin_providers::register_temporary_paths;
mod cache_provider;
mod doctest_provider;
mod execution;
mod property_provider;
mod unraisable;
pub(crate) use unraisable::register as register_unraisable;
mod runtime_providers;
pub(crate) use runtime_providers::register as register_runtime_providers;
pub(crate) mod subtests;
pub(crate) use capture_fixture::{
    independent_capture_fixture, register as register_capture_fixture,
};
pub(crate) use execution::{
    NativeIndependentRequest, NativeResolvedFixtureDefinitions, independent_fixture_resource,
};
mod fixture_binding;
mod fixture_lookup;
mod fixtures;
pub(crate) mod hypothesis_report;
pub(crate) use fixture_lookup::register as register_fixture_lookup;
mod global_capture;
mod initial_conftest;
mod lifecycle;
pub(crate) mod monkeypatch;
pub(crate) use lifecycle::state::{Disposition, FailureOrigin, LifecycleStage};
mod node;
mod observations;
pub(crate) use node::register as register_node_protocol;
mod parameter_order;
mod parameters;
pub(crate) use parameters::independent_parameter_iterator_warning;
pub(crate) mod reporting;
mod selection;
pub(crate) mod skipping;
pub(crate) use imports::{independent_import_path, independent_matches_python_name};
pub(crate) use reporting::terminal::{
    NativeTerminalAddStats, NativeTerminalLogReport, NativeTerminalLogReportDescriptor,
    NativeTerminalLogStartDescriptor, NativeTerminalStats,
};
pub(crate) use reporting::{
    NativeIndependentCallInfo, NativeIndependentCollectReport, NativeIndependentReport,
    NativeSessionLogReport, NativeSessionLogReportDescriptor, NativeSessionReports,
    independent_collector_repr_failure, independent_item_reportinfo, independent_item_repr_failure,
    register_value_representations,
};
mod session;
mod unittest;
pub(crate) use session::NativeIndependentSession;
pub(crate) use session::{
    fresh_mutation_test_plans, has_collectable_mutations, install_mutation_code,
};
pub(crate) mod session_hooks;
use parameters::IdEscaping;

struct FixtureBinding {
    runtime: Py<fixture_binding::NativeFixtureBinding>,
    definition: Py<PyAny>,
    autouse: bool,
    origin: FixtureOrigin,
    parameters: Option<Vec<Py<PyAny>>>,
    ids: Option<Py<PyAny>>,
}

impl TestBinding {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            collection_node: self.collection_node,
            provider_scope: self.provider_scope,
            callable: self.callable.clone_ref(py),
            class: self.class.as_ref().map(|class| class.clone_ref(py)),
            argument_keys: self.argument_keys.clone_ref(py),
        }
    }
}

struct FixtureParameter {
    index: usize,
    value: Py<PyAny>,
}

struct TestBinding {
    collection_node: collection::arena::CollectionNodeId,
    provider_scope: usize,
    callable: Py<PyAny>,
    class: Option<Py<PyAny>>,
    argument_keys: Py<pyo3::types::PyTuple>,
}

struct CollectedSuite {
    plan: SuitePlan,
    fixtures: Vec<FixtureBinding>,
    tests: Vec<TestBinding>,
    basetemp: Option<String>,
    context: lifecycle::Context,
    collection_reports: Vec<CollectionReport>,
    deselected_nodeids: Vec<String>,
}

struct Collector<'py> {
    py: Python<'py>,
    inspect: Bound<'py, PyModule>,
    fixtures: Vec<FixturePlan>,
    fixture_bindings: Vec<FixtureBinding>,
    direct_fixtures: BTreeMap<(vfy_core::CollectionNodeId, String), FixtureId>,
    tests: Vec<TestPlan>,
    test_bindings: Vec<TestBinding>,
    modules: BTreeMap<PathBuf, Py<PyAny>>,
    module_marks: Vec<Py<PyAny>>,
    context: Option<lifecycle::Context>,
    collection_reports: Vec<CollectionReport>,
    deselected_nodeids: Vec<String>,
    plugins: BTreeMap<String, Py<PyAny>>,
    current_node: Option<collection::arena::CollectionNodeId>,
    collected_order: Vec<collection::arena::CollectionNodeId>,
    specification: Option<CollectionSpecification>,
    root: PathBuf,
    import_mode: ImportMode,
    /// The selected run's collection scope, read at the first collected file.
    collection_scope: selection::CollectionScopeState,
}

#[derive(Clone, Copy)]
enum ArgumentBinding {
    Function,
    Method,
}

fn arguments(callable: &Bound<'_, PyAny>, binding: ArgumentBinding) -> PyResult<Vec<String>> {
    arguments_impl(callable, binding, None)
}

fn arguments_with_keys(
    callable: &Bound<'_, PyAny>,
    binding: ArgumentBinding,
) -> PyResult<(Vec<String>, Py<pyo3::types::PyTuple>)> {
    let mut keys = Vec::new();
    let names = arguments_impl(callable, binding, Some(&mut keys))?;
    Ok((
        names,
        pyo3::types::PyTuple::new(callable.py(), keys)?.unbind(),
    ))
}

fn arguments_impl(
    callable: &Bound<'_, PyAny>,
    binding: ArgumentBinding,
    mut keys: Option<&mut Vec<Py<PyAny>>>,
) -> PyResult<Vec<String>> {
    let signature = callable
        .py()
        .import("inspect")?
        .call_method1("signature", (callable,))?;
    let parameters = signature.getattr("parameters")?;
    let empty = callable
        .py()
        .import("inspect")?
        .getattr("Parameter")?
        .getattr("empty")?;
    let mut names = Vec::new();
    for parameter in parameters.call_method0("values")?.try_iter()? {
        let parameter = parameter?;
        let kind = parameter.getattr("kind")?.extract::<u8>()?;
        if kind == 0 {
            return Err(PyValueError::new_err(
                "positional-only test and fixture arguments are unsupported",
            ));
        }
        if (kind == 1 || kind == 3) && parameter.getattr("default")?.is(&empty) {
            let name = parameter.getattr("name")?;
            names.push(name.extract::<String>()?);
            if let Some(keys) = &mut keys {
                keys.push(name.unbind());
            }
        }
    }
    if matches!(binding, ArgumentBinding::Method) && !names.is_empty() {
        names.remove(0);
        if let Some(keys) = &mut keys {
            keys.remove(0);
        }
    }
    if callable.hasattr("__wrapped__")? {
        let supplied = mock_patch_arguments(callable)?.min(names.len());
        names.drain(..supplied);
        if let Some(keys) = &mut keys {
            keys.drain(..supplied);
        }
    }
    Ok(names)
}

/// The leading arguments that `mock.patch` decorators supply, which pytest
/// does not request as fixtures.
fn mock_patch_arguments(callable: &Bound<'_, PyAny>) -> PyResult<usize> {
    let Some(patchings) = callable.getattr_opt("patchings")? else {
        return Ok(0);
    };
    if !patchings.is_truthy()? {
        return Ok(0);
    }
    let py = callable.py();
    let modules = py.import("sys")?.getattr("modules")?;
    let mut sentinels = Vec::new();
    for name in ["mock", "unittest.mock"] {
        if let Some(module) = modules
            .call_method1("get", (name,))?
            .extract::<Option<Bound<'_, PyAny>>>()?
            && let Some(sentinel) = module.getattr_opt("DEFAULT")?
        {
            sentinels.push(sentinel);
        }
    }
    let mut count = 0;
    for patching in patchings.try_iter()? {
        let patching = patching?;
        let new = patching.getattr("new")?;
        if !patching.getattr("attribute_name")?.is_truthy()?
            && sentinels.iter().any(|sentinel| new.is(sentinel))
        {
            count += 1;
        }
    }
    Ok(count)
}

/// The empty signature that stock pytest reports for a native callback.
fn callable_signature(py: Python<'_>) -> PyResult<Py<PyAny>> {
    Ok(py
        .import("inspect")?
        .getattr("Signature")?
        .call0()?
        .unbind())
}

fn metadata<'py>(object: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    if object.hasattr(name)? {
        Ok(Some(object.getattr(name)?))
    } else {
        Ok(None)
    }
}

impl<'py> Collector<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(Self {
            py,
            inspect: py.import("inspect")?,
            fixtures: Vec::new(),
            fixture_bindings: Vec::new(),
            direct_fixtures: BTreeMap::new(),
            tests: Vec::new(),
            test_bindings: Vec::new(),
            modules: BTreeMap::new(),
            module_marks: Vec::new(),
            context: None,
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
            plugins: BTreeMap::new(),
            current_node: None,
            collected_order: Vec::new(),
            specification: None,
            root: PathBuf::new(),
            import_mode: ImportMode::Prepend,
            collection_scope: selection::CollectionScopeState::Unread,
        })
    }

    fn load_declared_plugins(&mut self, module: &Bound<'py, PyAny>) -> PyResult<()> {
        let Some(plugins) = metadata(module, "pytest_plugins")? else {
            return Ok(());
        };
        let names = if let Ok(name) = plugins.extract::<String>() {
            vec![name]
        } else {
            plugins.extract::<Vec<String>>()?
        };
        for name in names {
            self.load_plugin(&name)?;
        }
        Ok(())
    }

    fn initialize(
        &mut self,
        root: &Path,
        invocation_dir: &Path,
        paths: &[String],
        options: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<()> {
        root.clone_into(&mut self.root);
        if let Some(context) = &self.context {
            configuration::minimum_version(self.py, context)?;
            context.configure_pythonpath(self.py)?;
            context.parse_options(self.py, paths, options, false)?;
        }
        let plugins = self.configured_plugins(options)?;
        self.install_capture_provider(&plugins.raw)?;
        self.install_builtin_providers(&plugins.raw)?;
        self.install_cache_provider(&plugins.raw)?;
        self.install_subtests_provider(&plugins.raw)?;
        self.install_doctest_provider(&plugins.raw)?;
        self.install_property_provider(&plugins.raw)?;
        self.install_unraisable_provider(&plugins.raw)?;
        self.install_runtime_providers(&plugins.raw)?;
        self.refresh_import_mode()?;
        if let Some(context) = &self.context {
            context
                .stage
                .set(lifecycle::state::LifecycleStage::PluginLoading);
        }
        self.initialize_asyncio_imports()?;
        for plugin in plugins.configured.into_iter().chain(plugins.raw) {
            self.configure_plugin(&plugin)?;
        }
        self.autoload_plugins()?;
        self.load_environment_plugins()?;
        self.load_replacements()?;
        self.load_controller_defaults()?;
        if let Some(context) = &self.context {
            context
                .parser
                .bind(self.py)
                .call_method0("install_native_worker_alias")?;
        }
        if let Some(context) = &self.context {
            configuration::required_plugins(self.py, context)?;
            let initial_paths = context.parse_options(self.py, paths, options, false)?;
            let args = options
                .map(|options| options.get_item("_arguments"))
                .transpose()?
                .flatten()
                .unwrap_or(PyList::new(self.py, paths)?.into_any());
            initial_conftest::load(self, &args, invocation_dir, initial_paths)?;
        }
        Ok(())
    }

    fn import_file(&mut self, path: &Path) -> PyResult<Py<PyAny>> {
        if let Some(module) = self.modules.get(path) {
            return Ok(module.clone_ref(self.py));
        }
        self.refresh_import_mode()?;
        let purpose = if path.file_name().is_some_and(|name| name == "conftest.py") {
            imports::ImportPurpose::Conftest
        } else {
            imports::ImportPurpose::TestModule
        };
        let loaded = imports::load_module(self.py, path, &self.root, self.import_mode, purpose);
        let module = match loaded {
            Ok(module) => module,
            Err(error) => {
                if path.file_name().is_none_or(|name| name != "conftest.py")
                    || !error.is_instance_of::<pyo3::exceptions::PyException>(self.py)
                    || controls::is_control_error(self.py, &error)?
                {
                    return Err(error);
                }
                let path = self
                    .py
                    .import("pathlib")?
                    .getattr("Path")?
                    .call1((path.to_string_lossy().as_ref(),))?;
                let kwargs = PyDict::new(self.py);
                kwargs.set_item("cause", error.value(self.py))?;
                let value = self
                    .py
                    .import("vfy.independent.errors")?
                    .getattr("ConftestImportFailure")?
                    .call((path,), Some(&kwargs))?;
                let wrapped = PyErr::from_value(value);
                wrapped.set_cause(self.py, Some(error));
                return Err(wrapped);
            }
        };
        if path.file_name().is_some_and(|name| name == "conftest.py") {
            if let Some(context) = &self.context {
                context.register(self.py, module.clone_ref(self.py), &path.to_string_lossy())?;
            }
            self.load_declared_plugins(module.bind(self.py))?;
        }
        self.modules
            .insert(path.to_owned(), module.clone_ref(self.py));
        Ok(module)
    }

    fn add_fixture_binding(
        &mut self,
        id: FixtureId,
        declaration: Bound<'py, PyAny>,
        declared: fixtures::Declaration<'py>,
        parameters: Option<Vec<Py<PyAny>>>,
        origin: FixtureOrigin,
    ) -> PyResult<()> {
        let fixture = &self.fixtures[id.index()];
        let definition = &declared.metadata;
        self.fixture_bindings.push(FixtureBinding {
            runtime: fixture_binding::NativeFixtureBinding::new(
                self.py,
                id,
                fixture.name.clone(),
                fixture.declared_scope,
                declared.callable.unbind(),
            )?,
            definition: declaration.unbind(),
            parameters,
            origin,
            ids: definition
                .get_item("ids")?
                .filter(|value| !value.is_none())
                .map(Bound::unbind),
            autouse: definition
                .get_item("autouse")?
                .map_or(Ok(false), |value| value.is_truthy())?,
        });
        Ok(())
    }

    fn add_fixtures(
        &mut self,
        module: &Bound<'py, PyAny>,
        chains: &mut FixtureChains,
        baseid: &str,
        origin: FixtureOrigin,
    ) -> PyResult<()> {
        let namespace = module.getattr("__dict__")?;
        for entry in namespace.call_method0("items")?.try_iter()? {
            let (name, declaration) = entry?.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
            let Some(declared) = fixtures::declaration(&declaration, origin.is_class())? else {
                continue;
            };
            self.add_fixture_declaration(
                collection::objects::FixtureRegistration {
                    attribute_name: name,
                    original: &declaration,
                    declaration: declared,
                    arguments: ArgumentBinding::Function,
                    origin,
                },
                chains,
                baseid,
            )?;
        }
        Ok(())
    }

    fn validate_test_callable(
        &self,
        callable: &Bound<'py, PyAny>,
        file: &CollectionFile,
        name: &str,
    ) -> PyResult<()> {
        if self
            .inspect
            .call_method1("isgeneratorfunction", (callable,))?
            .is_truthy()?
        {
            return Err(PyValueError::new_err(format!(
                "generator test functions are unsupported: {}::{name}",
                file.nodeid
            )));
        }
        Ok(())
    }

    fn test_marks(
        &self,
        callable: &Bound<'py, PyAny>,
        class: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let marks = PyList::empty(self.py);
        for mark in function_marks(callable)? {
            marks.append(mark)?;
        }
        if let Some(class) = class {
            for mark in self.class_marks(class)?.iter() {
                marks.append(mark)?;
            }
        }
        for mark in &self.module_marks {
            marks.append(mark)?;
        }
        if let Some(context) = &self.context {
            configuration::markers(self.py, context, &marks)?;
        }
        Ok(marks)
    }

    fn eager_fixtures(
        &self,
        chains: &FixtureChains,
        marks: &Bound<'py, PyList>,
    ) -> PyResult<Vec<String>> {
        let mut eager = chains
            .ordered_names
            .iter()
            .filter(|name| {
                chains.by_name[*name]
                    .last()
                    .is_some_and(|id| self.fixture_bindings[id.index()].autouse)
            })
            .cloned()
            .collect::<Vec<_>>();
        for mark in marks.iter() {
            if mark.getattr("name")?.extract::<String>()? == "usefixtures" {
                eager.extend(mark.getattr("args")?.extract::<Vec<String>>()?);
            }
        }

        if let Some(context) = &self.context {
            eager.extend(
                context
                    .config
                    .bind(self.py)
                    .call_method1("getini", ("usefixtures",))?
                    .extract::<Vec<String>>()?,
            );
        }
        Ok(eager)
    }

    fn ini_string(&self, name: &str) -> PyResult<String> {
        self.context.as_ref().map_or_else(
            || Ok(String::new()),
            |context| {
                context
                    .config
                    .bind(self.py)
                    .call_method1("getini", (name,))?
                    .extract()
            },
        )
    }

    fn test_argument_binding(
        &self,
        class: Option<&Bound<'py, PyAny>>,
        name: &str,
    ) -> PyResult<ArgumentBinding> {
        if let Some(class) = class {
            let descriptor = self.inspect.call_method1(
                "getattr_static",
                (class, name.rsplit("::").next().unwrap_or(name)),
            )?;
            if descriptor.is_instance(&self.py.import("builtins")?.getattr("staticmethod")?)? {
                Ok(ArgumentBinding::Function)
            } else {
                Ok(ArgumentBinding::Method)
            }
        } else {
            Ok(ArgumentBinding::Function)
        }
    }

    fn test_id_escaping(&self) -> PyResult<IdEscaping> {
        let escaping_setting =
            "disable_test_id_escaping_and_forfeit_all_rights_to_community_support";
        let disable_escaping = self
            .context
            .as_ref()
            .map(|context| {
                context
                    .config
                    .bind(self.py)
                    .call_method1("getini", (escaping_setting,))?
                    .is_truthy()
            })
            .transpose()?
            .unwrap_or(false);
        Ok(if disable_escaping {
            IdEscaping::Disabled
        } else {
            IdEscaping::Ascii
        })
    }

    fn direct_parameter_chains(
        &mut self,
        definition: vfy_core::CollectionNodeId,
        row: &parameters::ParameterRow,
        chains: &Arc<FixtureChains>,
    ) -> PyResult<Arc<FixtureChains>> {
        if row.parameters.is_empty() {
            return Ok(Arc::clone(chains));
        }
        let mut chains = chains.as_ref().clone();
        for parameter in row.parameters.keys() {
            let scope = row.scopes[parameter];
            let context = self.context.as_ref().ok_or_else(|| {
                PyRuntimeError::new_err("parameter definition requires a collection session")
            })?;
            let arena = collection::arena::for_context(self.py, context)?;
            let scope_node = arena.borrow().parameter_owner(definition, scope)?;
            let key = (scope_node, parameter.clone());
            let id = if let Some(id) = self.direct_fixtures.get(&key) {
                *id
            } else {
                let id = FixtureId::from_index(self.fixtures.len());
                self.fixtures.push(FixturePlan {
                    id,
                    name: parameter.clone(),
                    package_baseid: String::new(),
                    dependencies: vec!["request".to_owned()],
                    declared_scope: scope,
                    parameterized: false,
                    async_loop_scope: None,
                    execution: FixtureExecution::DirectParameter,
                });
                self.fixture_bindings.push(callspec::direct_binding(
                    self.py,
                    &self.fixtures[id.index()],
                )?);
                self.direct_fixtures.insert(key, id);
                id
            };
            if !chains.by_name.contains_key(parameter) {
                chains.ordered_names.push(parameter.clone());
            }
            chains.by_name.insert(parameter.clone(), vec![id]);
        }
        Ok(Arc::new(chains))
    }

    fn finish(mut self) -> PyResult<CollectedSuite> {
        if let Some(spec) = &self.specification {
            let unmatched =
                spec.unmatched_selectors(&self.tests.iter().map(|test| test.nodeid.as_str()));
            if !unmatched.is_empty()
                && self
                    .collection_reports
                    .iter()
                    .all(|report| report.outcome == CollectionOutcome::Passed)
            {
                return Err(crate::IndependentUsageError::new_err(format!(
                    "not found: {}",
                    unmatched.join(", ")
                )));
            }
        }
        self.order_collected_drafts()?;
        let plan =
            SuitePlan::new(self.tests, self.fixtures, None).map_err(PyValueError::new_err)?;
        Ok(CollectedSuite {
            plan,
            fixtures: self.fixture_bindings,
            tests: self.test_bindings,
            basetemp: None,
            context: self.context.ok_or_else(|| {
                PyRuntimeError::new_err("collection requires a configured session")
            })?,
            collection_reports: self.collection_reports,
            deselected_nodeids: self.deselected_nodeids,
        })
    }
}

/// A test function's own marks. pytest stores them in `pytestmark`, which
/// wrapping decorators such as Hypothesis's `given` copy to the wrapper.
fn function_marks<'py>(callable: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let Some(public) = metadata(callable, "pytestmark")?.filter(|marks| !marks.is_none()) else {
        return Ok(Vec::new());
    };
    if public.hasattr("name")? {
        return Ok(vec![public]);
    }
    public.try_iter()?.collect()
}

fn module_mark_objects<'py>(module: &Bound<'py, PyAny>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let Some(marks) = metadata(module, "pytestmark")? else {
        return Ok(Vec::new());
    };
    if marks.hasattr("name")? {
        Ok(vec![marks])
    } else {
        marks.try_iter()?.collect()
    }
}

struct PreparedCollector<'py> {
    collector: Collector<'py>,
    root: PathBuf,
}

fn prepare_collector_owned<'py>(
    py: Python<'py>,
    root: &str,
    paths: &[String],
    options: Option<&Bound<'py, PyDict>>,
    explicit_basetemp: Option<&str>,
    invocation: std::rc::Rc<lifecycle::state::InvocationState>,
) -> PyResult<PreparedCollector<'py>> {
    let mut prepared =
        prepare_commandline_owned(py, root, paths, options, explicit_basetemp, invocation)?;
    let context = prepared
        .collector
        .context
        .as_ref()
        .expect("initialized context")
        .clone_ref(py);
    let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &context);
    let result = start_prepared(py, &mut prepared, root, options).map(|()| prepared);
    cleanup.complete(result)
}

fn start_prepared(
    py: Python<'_>,
    prepared: &mut PreparedCollector<'_>,
    root: &str,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<()> {
    let context = prepared
        .collector
        .context
        .as_ref()
        .expect("initialized context");
    lifecycle::commandline::validate_xdist_mode(py, context)?;
    let paths = context
        .config
        .bind(py)
        .getattr("args")?
        .extract::<Vec<String>>()?;
    // Targets are fixed before configure hooks run, but pytest reports a missing
    // target only after the session starts.
    let specification = context.specification(py, root, &paths, options);
    context.start(py)?;
    prepared.collector.specification = Some(specification?);
    Ok(())
}

fn prepare_commandline_owned<'py>(
    py: Python<'py>,
    root: &str,
    paths: &[String],
    options: Option<&Bound<'py, PyDict>>,
    explicit_basetemp: Option<&str>,
    invocation: std::rc::Rc<lifecycle::state::InvocationState>,
) -> PyResult<PreparedCollector<'py>> {
    let mut collector = Collector::new(py)?;
    let context = lifecycle::Context::new_owned(py, root, paths, options, invocation)?;
    let root_path = PathBuf::from(
        context
            .config
            .bind(py)
            .getattr("rootpath")?
            .str()?
            .to_str()?,
    );
    let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &context);
    let diagnostics = context.clone_ref(py);
    let result = (|| {
        collector.context = Some(context);
        collector.initialize(&root_path, Path::new(root), paths, options)?;
        let context = collector.context.as_ref().expect("initialized context");
        let initial_paths = context.parse_options(py, paths, options, false)?;
        context
            .stage
            .set(lifecycle::state::LifecycleStage::ConftestLoading);
        collection::preload(&mut collector, Path::new(root), &initial_paths)?;
        let context = collector.context.as_ref().expect("initialized context");
        let final_paths = context.parse_options(py, paths, options, true)?;
        context.config.bind(py).setattr("args", final_paths)?;
        if let Some(basetemp) = explicit_basetemp {
            context
                .config
                .bind(py)
                .getattr("option")?
                .setattr("basetemp", basetemp)?;
        }
        Ok(PreparedCollector {
            collector,
            root: root_path,
        })
    })();
    diagnostics.report_error(py, &result)?;
    cleanup.complete(result)
}

fn error_payload(py: Python<'_>, error: &PyErr) -> PyResult<PhasePayload> {
    let detail = py
        .import("traceback")?
        .call_method1("format_exception", (error.value(py),))?
        .extract::<Vec<String>>()?
        .concat();
    Ok(PhasePayload {
        exception: Some(format!("{}: {}", error.get_type(py).name()?, error.value(py)).into()),
        detail,
        ..PhasePayload::default()
    })
}

/// Prepare the plan and protocol context without executing tests.
fn prepare_suite(
    py: Python<'_>,
    paths: &[String],
    root: &str,
    keyword: Option<&str>,
    basetemp: Option<&str>,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<CollectedSuite> {
    prepare_suite_owned(
        py,
        paths,
        root,
        keyword,
        basetemp,
        options,
        std::rc::Rc::new(lifecycle::state::InvocationState::new()),
    )
}

fn prepare_suite_owned(
    py: Python<'_>,
    paths: &[String],
    root: &str,
    keyword: Option<&str>,
    basetemp: Option<&str>,
    options: Option<&Bound<'_, PyDict>>,
    invocation: std::rc::Rc<lifecycle::state::InvocationState>,
) -> PyResult<CollectedSuite> {
    let root_path =
        std::fs::canonicalize(root).map_err(|error| PyValueError::new_err(error.to_string()))?;
    let basetemp = basetemp
        .map(|value| validate_basetemp(&root_path, value))
        .transpose()
        .map_err(crate::IndependentUsageError::new_err)?;
    let basetemp = basetemp.map(|path| path.to_string_lossy().into_owned());
    let prepared =
        prepare_collector_owned(py, root, paths, options, basetemp.as_deref(), invocation)?;
    finish_collection(py, prepared, keyword, basetemp)
}

fn finish_collection(
    py: Python<'_>,
    prepared: PreparedCollector<'_>,
    keyword: Option<&str>,
    basetemp: Option<String>,
) -> PyResult<CollectedSuite> {
    let PreparedCollector {
        collector,
        root: root_path,
    } = prepared;
    let context = collector
        .context
        .as_ref()
        .expect("initialized context")
        .clone_ref(py);
    let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &context);
    let result = context.capture_warnings(py, "collect", || {
        if let Some(specification) = &collector.specification {
            for warning in specification.warnings() {
                configuration::diagnostic(
                    py,
                    vfy_core::pytest_config::ConfigurationDiagnostic::ConfigWarning(
                        warning.clone(),
                    ),
                )?;
            }
        }
        let keyword = match keyword {
            Some(keyword) => Some(keyword.to_owned()),
            None => context
                .config
                .bind(py)
                .getattr("option")?
                .getattr("keyword")?
                .extract()?,
        };
        let mut suite = lifecycle::collection::run(py, collector, root_path, keyword)?;
        suite.basetemp = basetemp;
        Ok(suite)
    });
    cleanup.complete(result)
}

/// Execute a prepared suite through the same lifecycle used by the public frontend.
/// Stock pytest runs the collected items and reports `testsfailed` when this is set.
fn continue_on_collection_errors(py: Python<'_>, context: &lifecycle::Context) -> PyResult<bool> {
    context
        .config
        .bind(py)
        .call_method1("getoption", ("continue_on_collection_errors", false))?
        .is_truthy()
}

fn option_flag(options: Option<&Bound<'_, PyDict>>, name: &str) -> PyResult<bool> {
    options
        .map(|options| options.get_item(name))
        .transpose()?
        .flatten()
        .map_or(Ok(false), |flag| flag.is_truthy())
}

fn execute_prepared(
    py: Python<'_>,
    mut suite: CollectedSuite,
    root: &str,
    collect_only: bool,
    options: Option<&Bound<'_, PyDict>>,
    started: Instant,
) -> PyResult<Py<NativeRunResult>> {
    let guard_context = suite.context.clone_ref(py);
    let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &guard_context);
    let result = (|| {
        suite
            .context
            .invocation
            .validate_execution_mode(collect_only)?;
        assignment::publish(py, &mut suite, options)?;
        let collected = suite.plan.tests().len();
        let collection_reports = std::mem::take(&mut suite.collection_reports);
        let deselected_nodeids = std::mem::take(&mut suite.deselected_nodeids);
        let context = suite.context.clone_ref(py);
        let collection_aborted = option_flag(options, "_collection_aborted")?;
        let collection_surplus = context.invocation.surplus.get();
        let errors = collection_reports
            .iter()
            .filter(|report| report.outcome == CollectionOutcome::Failed)
            .count();
        context.session.bind(py).setattr("testsfailed", errors)?;
        let mode = if errors > 0 && !continue_on_collection_errors(py, &context)? {
            session_hooks::LoopMode::CollectionFailed(errors)
        } else if collect_only || collection_aborted || collection_surplus {
            session_hooks::LoopMode::CollectOnly
        } else {
            session_hooks::LoopMode::Run
        };
        let collection_warnings = context.warnings.borrow().len();
        let mut outcome = session_hooks::execute_loop(py, suite, root, mode)?;
        let interrupted = outcome.interrupted || collection_aborted;
        let control = outcome
            .error
            .as_ref()
            .map(|error| controls::is_control_error(py, error))
            .transpose()?
            .unwrap_or(false);
        if let Some(error) = &outcome.error {
            let diagnostic: PyResult<()> = Err(error.clone_ref(py));
            context.report_error(py, &diagnostic)?;
        }
        let internal_errors = outcome
            .error
            .as_ref()
            .filter(|_| !control)
            .map(|error| error_payload(py, error))
            .transpose()?
            .into_iter()
            .collect::<Vec<_>>();
        // Stock exits its handled internal-error scope before session finish.
        // Keep the original traceback and any plug-in-held aliases untouched.
        if let Some(error) = &outcome.error
            && context
                .invocation
                .release_converted_internal_error(py, error)
        {
            drop(outcome.error.take());
        }
        let counts = compute_run_counts(&outcome.cases, &collection_reports);
        let record = RunRecord {
            collection_reports,
            deselected_nodeids,
            internal_errors,
            engine: "vfy-independent-v1".to_owned(),
            observations: None,
            exitstatus: None,
            execution_identity: None,
            collected,
            counts,
            duration_ms: started.elapsed().as_secs_f64() * 1000.0,
            warnings: Vec::new(),
            cases: outcome.cases,
        };
        let result = Py::new(py, native_run_result(record, None))?;
        if let Some(failure) = &outcome.session_failure {
            failure.retain_worker_stop(py, &context);
        }
        let completion = if outcome.session_failure.is_some() {
            controls::LoopCompletion::SessionFailed
        } else if control {
            controls::LoopCompletion::Control
        } else if interrupted {
            controls::LoopCompletion::Interrupted
        } else {
            controls::LoopCompletion::Completed
        };
        finish_result(
            py,
            &context,
            &result,
            completion,
            started,
            collection_warnings,
            options,
        )?;
        vfy_core::test_execution::validate_run_record(result.borrow(py).record())
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        reporting::html::complete(py, &context, &result)?;
        Ok(result)
    })();
    cleanup.complete(result)
}

fn finish_result(
    py: Python<'_>,
    context: &lifecycle::Context,
    result: &Py<NativeRunResult>,
    completion: controls::LoopCompletion,
    started: Instant,
    collection_warnings: usize,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<()> {
    context.bind_junit_result(py, result)?;
    context.bind_terminal_result(
        py,
        result,
        reporting::terminal::session::SummaryOrigin::Serial,
    )?;
    let has_internal_errors = !result.borrow(py).record().internal_errors.is_empty();
    let collected = context
        .session
        .bind(py)
        .getattr("testscollected")?
        .extract()?;
    let finish_warnings = context.warnings.borrow().len();
    let status = context.finish_execution(py, has_internal_errors, collected, completion)?;
    context
        .stage
        .set(lifecycle::state::LifecycleStage::Materializing);
    reporting::materialize_reports(py, context.session.bind(py), result)?;
    // CLI-only runs need observations when they publish the native result or
    // expose it through the HTML reporter. Workers always transport them.
    let skip_observations = !context.invocation.is_worker()
        && options
            .map(|options| options.get_item("_cli_skip_observations"))
            .transpose()?
            .flatten()
            .map_or(Ok(false), |flag| flag.is_truthy())?;
    let observations = if skip_observations && {
        let options = context.config.bind(py).getattr("option")?;
        !options.getattr("native_result_json")?.is_truthy()?
            && !options
                .getattr_opt("htmlpath")?
                .map_or(Ok(false), |path| path.is_truthy())?
    } {
        None
    } else {
        Some(observations::snapshot(py, context)?)
    };
    let locations = if context.invocation.is_worker() {
        let cases = result.borrow(py).record().cases.clone();
        Some(reporting::worker_report_locations(
            py,
            context.session.bind(py),
            &cases,
        )?)
    } else {
        None
    };
    let mut result = result.borrow_mut(py);
    let record = result.record_mut();
    record.exitstatus = Some(status);
    record.duration_ms = started.elapsed().as_secs_f64() * 1000.0;
    record.observations = observations;
    record.warnings.clone_from(&context.warnings.borrow());
    if context.invocation.worker.borrow().is_some() || context.invocation.surplus.get() {
        result.partition_worker_warnings(collection_warnings, finish_warnings)?;
    }
    if let Some(locations) = locations {
        result.set_report_locations(locations)?;
    }
    Ok(())
}

/// Collect and execute a suite without importing a pytest distribution.
#[pyfunction]
#[pyo3(signature = (paths, root, keyword=None, collect_only=false, basetemp=None, options=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts Python sequences into an owned Vec at this boundary"
)]
pub(crate) fn run_independent_suite(
    py: Python<'_>,
    paths: Vec<String>,
    root: &str,
    keyword: Option<&str>,
    collect_only: bool,
    basetemp: Option<&str>,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Py<NativeRunResult>> {
    let started = Instant::now();
    let suite = prepare_suite(py, &paths, root, keyword, basetemp, options)?;
    execute_prepared(py, suite, root, collect_only, options, started)
}

/// Return collection identities without executing test bodies.
#[pyfunction]
#[pyo3(signature = (paths, root, keyword=None, basetemp=None, options=None))]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts Python sequences into an owned Vec at this boundary"
)]
pub(crate) fn collect_independent_suite(
    py: Python<'_>,
    paths: Vec<String>,
    root: &str,
    keyword: Option<&str>,
    basetemp: Option<&str>,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<String>> {
    let started = Instant::now();
    let suite = prepare_suite(py, &paths, root, keyword, basetemp, options)?;
    let nodeids = suite
        .plan
        .tests()
        .iter()
        .map(|test| test.nodeid.clone())
        .collect();
    let result = execute_prepared(py, suite, root, true, options, started)?;
    if result
        .borrow(py)
        .record()
        .collection_reports
        .iter()
        .any(|report| report.outcome == CollectionOutcome::Failed)
    {
        return Err(PyRuntimeError::new_err("errors during collection"));
    }
    Ok(nodeids)
}
