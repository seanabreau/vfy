//! Bind builtin collectors to the plan and fixture runtime.

use super::arena;
use crate::hooks::NativeHookRegistry;
use crate::independent::plugin_fixtures::{ConftestDirectory, FixtureOrigin};
use crate::independent::{
    ArgumentBinding, Collector, TestBinding, arguments, asyncio, collection, configuration,
    fixtures,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{BTreeMap, HashSet};
use std::path::Path;
use std::sync::Arc;
use vfy_core::FixtureId;
use vfy_core::independent_collection::{CollectionFile, conftests};
use vfy_core::test_plan::{FixtureChains, FixturePlan, ScopeOwners, TestBody, TestId, TestPlan};

pub(in crate::independent) struct FixtureRegistration<'a, 'py> {
    pub(in crate::independent) attribute_name: Bound<'py, PyAny>,
    pub(in crate::independent) original: &'a Bound<'py, PyAny>,
    pub(in crate::independent) declaration: fixtures::Declaration<'py>,
    pub(in crate::independent) arguments: ArgumentBinding,
    pub(in crate::independent) origin: FixtureOrigin,
}

/// The constructor that stops pytest from collecting a test class, checked in
/// the order of `_pytest.python.Class.collect`.
fn blocking_constructor(class: &Bound<'_, PyAny>) -> PyResult<Option<&'static str>> {
    let object = class.py().import("builtins")?.getattr("object")?;
    for name in ["__init__", "__new__"] {
        if class.getattr(name)?.ne(object.getattr(name)?)? {
            return Ok(Some(name));
        }
    }
    Ok(None)
}

/// Warn at the class definition, as pytest's `Class.collect` does.
fn warn_blocked_class(
    class_node: &Bound<'_, PyAny>,
    class: &Bound<'_, PyAny>,
    constructor: &str,
) -> PyResult<()> {
    let message = format!(
        "cannot collect test class {} because it has a {constructor} constructor (from: {})",
        class.getattr("__name__")?.repr()?,
        class_node.getattr("parent")?.getattr("nodeid")?,
    );
    let warning = class_node
        .py()
        .import("vfy.independent.facade")?
        .getattr("PytestCollectionWarning")?
        .call1((message,))?;
    class_node.call_method1("warn", (warning,))?;
    Ok(())
}

impl<'py> Collector<'py> {
    pub(in crate::independent) fn add_fixture_declaration(
        &mut self,
        registration: FixtureRegistration<'_, 'py>,
        chains: &mut FixtureChains,
        baseid: &str,
    ) -> PyResult<()> {
        registration.origin.validate(self)?;
        let declared = registration.declaration;
        let definition = &declared.metadata;
        let parameters = definition
            .get_item("params")?
            .filter(|value| !value.is_none())
            .map(|values| {
                values
                    .try_iter()?
                    .map(|value| value.map(Bound::unbind))
                    .collect::<PyResult<Vec<_>>>()
            })
            .transpose()?;
        let name = definition
            .get_item("name")?
            .filter(|value| !value.is_none())
            .map_or_else(
                || registration.attribute_name.extract::<String>(),
                |value| value.extract::<String>(),
            )?;
        if registration.origin.reusable()
            && let Some(index) =
                self.fixture_bindings
                    .iter()
                    .enumerate()
                    .find_map(|(index, binding)| {
                        (binding.definition.bind(self.py).is(registration.original)
                            && binding.origin == registration.origin
                            && self.fixtures.get(index).is_some_and(|fixture| {
                                fixture.name == name && fixture.package_baseid == baseid
                            }))
                        .then_some(index)
                    })
        {
            if !chains.by_name.contains_key(&name) {
                chains.ordered_names.push(name.clone());
            }
            chains
                .by_name
                .entry(name)
                .or_default()
                .push(FixtureId::from_index(index));
            return Ok(());
        }
        let scope = fixtures::scope(self.py, definition, &name, self.context.as_ref())?;
        let callable = &declared.function;
        let id = FixtureId::from_index(self.fixtures.len());
        let (execution, async_loop_scope) =
            asyncio::fixture_plan(self.context.as_ref(), callable, scope)?;
        self.fixtures.push(FixturePlan {
            id,
            name: name.clone(),
            package_baseid: baseid.to_owned(),
            dependencies: arguments(callable, registration.arguments)?,
            declared_scope: scope,
            parameterized: parameters.is_some(),
            async_loop_scope,
            execution,
        });
        self.add_fixture_binding(
            id,
            registration.original.clone(),
            declared,
            parameters,
            registration.origin,
        )?;
        if !chains.by_name.contains_key(&name) {
            chains.ordered_names.push(name.clone());
        }
        chains.by_name.entry(name).or_default().push(id);
        Ok(())
    }

    pub(in crate::independent) fn collect_module(
        &mut self,
        root: &Path,
        file: &CollectionFile,
        owner: arena::CollectionNodeId,
    ) -> PyResult<Vec<arena::CollectionNodeId>> {
        let mut chains = self.plugin_and_conftest_chains(root, &file.path)?;
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        self.current_node = Some(owner);
        let arena = arena::for_context(self.py, context)?;
        let module_node = arena.borrow().object(self.py, owner)?;
        let module = module_node.bind(self.py).getattr("obj")?.unbind();
        self.modules
            .insert(file.path.clone(), module.clone_ref(self.py));
        collection::refresh_naming(self)?;
        self.add_fixtures(
            module.bind(self.py),
            &mut chains,
            &file.nodeid,
            FixtureOrigin::Module(owner),
        )?;
        let lifecycle = self
            .py
            .import("vfy.independent.xunit")?
            .call_method1("module_fixtures", (module.bind(self.py),))?;
        self.add_lifecycle_fixtures(
            &lifecycle,
            &mut chains,
            &file.nodeid,
            FixtureOrigin::Module(owner),
        )?;
        self.load_module_marks(module_node.bind(self.py))?;
        let chains = Arc::new(chains);
        arena
            .borrow_mut()
            .set_fixtures(owner, Arc::clone(&chains))?;
        self.module_children(file, owner, module.bind(self.py), &chains)
    }

    /// Fixtures from registered plugins and from the conftests that apply to ``path``.
    fn plugin_and_conftest_chains(&mut self, root: &Path, path: &Path) -> PyResult<FixtureChains> {
        let mut chains = FixtureChains::default();
        for (registration, plugin) in self.registered_fixture_plugins()? {
            self.add_plugin_fixtures(plugin.bind(self.py), &mut chains, registration)?;
        }
        for conftest in conftests(&self.conftest_cutoff()?, path) {
            let module = self.import_file(&conftest)?;
            let relative = conftest
                .parent()
                .and_then(|directory| directory.strip_prefix(root).ok());
            let directory = if relative.is_some_and(|path| path.as_os_str().is_empty()) {
                ConftestDirectory::Rootdir
            } else {
                ConftestDirectory::Other
            };
            let baseid = relative
                .unwrap_or(Path::new(""))
                .to_string_lossy()
                .into_owned();
            self.add_fixtures(
                module.bind(self.py),
                &mut chains,
                &baseid,
                FixtureOrigin::Conftest(directory),
            )?;
        }
        Ok(chains)
    }

    /// Fixtures visible to a plugin item: plugins and directory conftests.
    fn custom_fixture_chains(
        &mut self,
        owner: arena::CollectionNodeId,
        path: &Path,
    ) -> PyResult<Arc<FixtureChains>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let arena = arena::for_context(self.py, context)?;
        if let Ok(chains) = arena.borrow().fixtures(owner) {
            return Ok(chains);
        }
        let root = self.root.clone();
        let chains = Arc::new(self.plugin_and_conftest_chains(&root, path)?);
        arena
            .borrow_mut()
            .set_fixtures(owner, Arc::clone(&chains))?;
        Ok(chains)
    }

    /// Replace the module marks that the items of the next collected module inherit.
    fn load_module_marks(&mut self, node: &Bound<'py, PyAny>) -> PyResult<()> {
        self.module_marks.clear();
        for mark in node.getattr("own_markers")?.try_iter()? {
            self.module_marks.push(mark?.unbind());
        }
        Ok(())
    }

    /// Give a plugin item its executable test entry; its body is `item.runtest()`.
    pub(in crate::independent) fn add_custom_item(
        &mut self,
        id: arena::CollectionNodeId,
        parent: arena::CollectionNodeId,
        source_file: Option<&Path>,
    ) -> PyResult<()> {
        let arena = arena::for_context(
            self.py,
            self.context
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("item collection requires a session"))?,
        )?;
        let (item, path) = {
            let arena = arena.borrow();
            (arena.object(self.py, id)?, arena.path(id)?.to_path_buf())
        };
        let item = item.bind(self.py);
        // A file hook can return an item whose path is inherited from its directory.
        // Use the hook's file for conftest visibility and the item for plan ownership.
        let fixture_owner = if source_file.is_some() { id } else { parent };
        let owner_nodeid = arena
            .borrow()
            .object(self.py, fixture_owner)?
            .bind(self.py)
            .getattr("nodeid")?
            .extract::<String>()?;
        let chains = self.custom_fixture_chains(fixture_owner, source_file.unwrap_or(&path))?;
        let marks = PyList::empty(self.py);
        for mark in item.call_method0("iter_markers")?.try_iter()? {
            marks.append(mark?)?;
        }
        if let Some(context) = &self.context {
            configuration::markers(self.py, context, &marks)?;
        }
        // Pytest leaves fixture activation to items returned directly by a file hook.
        let mut eager = if source_file.is_some() {
            Vec::new()
        } else {
            self.eager_fixtures(&chains, &marks)?
        };
        if let Some(declared) = item.getattr_opt("fixturenames")? {
            eager.extend(declared.extract::<Vec<String>>()?);
        }
        let test = TestPlan {
            id: TestId::from_index(self.tests.len()),
            collection_node: Some(id),
            nodeid: item.getattr("nodeid")?.extract()?,
            body: TestBody::Item,
            async_loop_scope: None,
            argument_names: Vec::new().into(),
            eager_fixtures: eager.into(),
            fixture_chains: chains,
            fixture_state: Some(Arc::new(vfy_core::test_plan::TestFixtureState::default())),
            owners: ScopeOwners {
                module: Arc::from(owner_nodeid.as_str()),
                class: None,
            },
        };
        item.setattr(
            "fixturenames",
            crate::independent::lifecycle::nodes::fixture_names(&test, &self.fixtures),
        )?;
        let binding = TestBinding {
            collection_node: id,
            provider_scope: 0,
            callable: item.getattr("runtest")?.unbind(),
            class: None,
            argument_keys: pyo3::types::PyTuple::empty(self.py).unbind(),
        };
        self.push_test(test, binding);
        Ok(())
    }

    /// Let plug-ins such as anyio mark a test candidate through
    /// `pytest_pycollect_makeitem` before the native collector builds its items.
    fn pycollect_makeitem(
        &self,
        collector: &Bound<'py, PyAny>,
        path: &Path,
        name: &str,
        object: &Bound<'py, PyAny>,
    ) -> PyResult<()> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let registry = context.registry.bind(self.py);
        if registry
            .call_method1("get_hookimpls", ("pytest_pycollect_makeitem",))?
            .is_empty()?
        {
            return Ok(());
        }
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("collector", collector)?;
        kwargs.set_item("name", name)?;
        kwargs.set_item("obj", object)?;
        let removed = context.removed_plugins(self.py, path.parent().unwrap_or(path))?;
        let result = registry.call_method1(
            "call_subset",
            ("pytest_pycollect_makeitem", kwargs, removed, true),
        )?;
        if result.is_none() {
            return Ok(());
        }
        Err(crate::UnsupportedFeatureError::new_err(format!(
            "pytest_pycollect_makeitem returned a collection result for {name:?}; \
             the independent engine collects Python functions natively"
        )))
    }

    pub(in crate::independent) fn collect_file_hooked(&self) -> PyResult<bool> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let registry = context.registry.bind(self.py);
        for hook in ["pytest_collect_file", "pytest_pycollect_makemodule"] {
            if !registry
                .call_method1("get_hookimpls", (hook,))?
                .is_empty()?
            {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// Apply the live `python_files` naming policy to one path.
    pub(in crate::independent) fn matches_python_files(&self, path: &Path) -> PyResult<bool> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let patterns = context
            .config
            .bind(self.py)
            .call_method1("getini", ("python_files",))?
            .extract()?;
        Ok(vfy_core::independent_collection::matches_python_file(
            path, patterns,
        ))
    }

    pub(in crate::independent) fn collect_class_node(
        &mut self,
        file: &CollectionFile,
        owner: arena::CollectionNodeId,
    ) -> PyResult<Vec<arena::CollectionNodeId>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("class collection requires a session"))?;
        let arena = arena::for_context(self.py, context)?;
        let (module, object, chains) = {
            let arena = arena.borrow();
            let module = arena.scope_owner(owner, vfy_core::FixtureScope::Module, "")?;
            (
                arena.object(self.py, module)?,
                arena.object(self.py, owner)?,
                arena.fixtures(module)?,
            )
        };
        self.load_module_marks(module.bind(self.py))?;
        self.current_node = Some(owner);
        let name = object.bind(self.py).getattr("name")?.extract::<String>()?;
        self.collect_class(file, &name, object.bind(self.py), &chains)
    }

    fn module_children(
        &mut self,
        file: &CollectionFile,
        owner: arena::CollectionNodeId,
        module: &Bound<'py, PyAny>,
        chains: &Arc<FixtureChains>,
    ) -> PyResult<Vec<arena::CollectionNodeId>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("module discovery requires a session"))?;
        let arena = arena::for_context(self.py, context)?;
        let mut children = Vec::new();
        let namespace = module.getattr("__dict__")?;
        let mut entries = Vec::new();
        for (name, value) in namespace.cast::<PyDict>()?.iter() {
            entries.push((name.extract::<String>()?, value.unbind()));
        }
        for (name, callable) in entries {
            let callable = callable.bind(self.py);
            if self
                .specification
                .as_ref()
                .is_some_and(|spec| spec.matches_function(&name))
                && self
                    .inspect
                    .call_method1("isfunction", (callable,))?
                    .is_truthy()?
                && !fixtures::is_declaration(callable)?
            {
                let module_node = arena.borrow().object(self.py, owner)?;
                self.pycollect_makeitem(module_node.bind(self.py), &file.path, &name, callable)?;
                children.extend(self.add_test(file, &name, callable, None, chains)?);
            } else if self
                .inspect
                .call_method1("isclass", (callable,))?
                .is_truthy()?
            {
                let unittest = crate::independent::unittest::is_testcase(callable)?;
                if !unittest
                    && !self
                        .specification
                        .as_ref()
                        .is_some_and(|spec| spec.matches_class(&name))
                {
                    continue;
                }
                if unittest
                    && self
                        .inspect
                        .call_method1("isabstract", (callable,))?
                        .is_truthy()?
                {
                    continue;
                }
                if callable.hasattr("__test__")? && !callable.getattr("__test__")?.is_truthy()? {
                    continue;
                }
                let context = self.context.as_ref().ok_or_else(|| {
                    PyRuntimeError::new_err("class collection requires a session")
                })?;
                let nodeid = format!("{}::{name}", file.nodeid);
                let marks = self.class_marks(callable)?;
                let id = if unittest {
                    arena::unittest_collector(self.py, context, owner, &nodeid, &file.path, &marks)?
                } else {
                    arena::collector(
                        self.py,
                        context,
                        arena::CollectionNodeKind::Class,
                        owner,
                        &nodeid,
                        &file.path,
                        &marks,
                    )?
                };
                let object = arena.borrow().object(self.py, id)?;
                object.bind(self.py).setattr("obj", callable)?;
                children.push(id);
            }
        }
        Ok(children)
    }

    pub(in crate::independent) fn collection_items(&self) -> PyResult<Bound<'py, PyList>> {
        let items = self.collection_items_with_length()?;
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let arena = arena::for_context(self.py, context)?;
        for (item, id) in items.iter().zip(&self.collected_order) {
            let expected = arena.borrow().object(self.py, *id)?;
            if !item.is(expected.bind(self.py)) {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "mutating Session.items during native collection is unsupported",
                ));
            }
        }
        Ok(items)
    }

    fn collection_items_with_length(&self) -> PyResult<Bound<'py, PyList>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("collection requires a live session"))?;
        let arena = arena::for_context(self.py, context)?;
        let items = arena::NativeCollectionArena::items(&arena)?;
        if items.len() != self.collected_order.len() {
            return Err(crate::UnsupportedFeatureError::new_err(
                "mutating Session.items during native collection is unsupported",
            ));
        }
        Ok(items)
    }

    pub(in crate::independent) fn item_collected(
        &mut self,
        id: arena::CollectionNodeId,
    ) -> PyResult<()> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("item collection requires a session"))?;
        let arena = arena::for_context(self.py, context)?;
        arena::NativeCollectionArena::validate_parent(&arena, id)?;
        // Empty hook dispatch cannot mutate the existing items. Validate the full
        // prefix around callbacks, and once after traversal for other collectors.
        let callbacks = context
            .registry
            .bind(self.py)
            .borrow()
            .invokes_callbacks(self.py, "pytest_itemcollected");
        let items = if callbacks {
            self.collection_items()?
        } else {
            self.collection_items_with_length()?
        };
        let item = arena.borrow().object(self.py, id)?;
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("item", &item)?;
        let removed = NativeHookRegistry::item_exclusions(
            context.registry.bind(self.py),
            item.bind(self.py),
        )?;
        let result = NativeHookRegistry::call_subset(
            context.registry.bind(self.py),
            "pytest_itemcollected",
            &kwargs,
            &removed,
            false,
        );
        // Release temporary hook arguments before inspecting collection mutations.
        drop(removed);
        drop(kwargs);
        result?;
        arena::NativeCollectionArena::validate_parent(&arena, id)?;
        if callbacks {
            self.collection_items()?;
        } else {
            self.collection_items_with_length()?;
        }
        items.append(item)?;
        self.collected_order.push(id);
        Ok(())
    }

    fn collect_class(
        &mut self,
        file: &CollectionFile,
        name: &str,
        class_node: &Bound<'py, PyAny>,
        module_chains: &Arc<FixtureChains>,
    ) -> PyResult<Vec<arena::CollectionNodeId>> {
        {
            let class = class_node.getattr("obj")?;
            if class.hasattr("__test__")? && !class.getattr("__test__")?.is_truthy()? {
                return Ok(Vec::new());
            }
        }
        let class = class_node.getattr("obj")?;
        let owner = self
            .current_node
            .ok_or_else(|| PyRuntimeError::new_err("class collection has no owning node"))?;
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("class collection requires a session"))?;
        let testcase = arena::for_context(self.py, context)?
            .borrow()
            .is_unittest_class(owner)?;
        if !testcase && let Some(constructor) = blocking_constructor(&class)? {
            warn_blocked_class(class_node, &class, constructor)?;
            return Ok(Vec::new());
        }
        let chains = self.class_fixture_chains(file, name, module_chains)?;
        if testcase {
            let mut functions = Vec::new();
            for method in crate::independent::unittest::test_names(&class)? {
                let callable = class.getattr(method.as_str())?;
                functions.push(self.add_unittest(
                    file,
                    &format!("{name}::{method}"),
                    &callable,
                    &class,
                    &chains,
                )?);
            }
            return Ok(functions);
        }
        let mro = class_node
            .getattr("obj")?
            .getattr("__mro__")?
            .try_iter()?
            .collect::<PyResult<Vec<_>>>()?;
        let mut seen = HashSet::new();
        let mut groups = Vec::new();
        for owner in &mro {
            let mut methods = Vec::new();
            for item in owner
                .getattr("__dict__")?
                .call_method0("items")?
                .try_iter()?
            {
                let (method, value) = item?.extract::<(String, Py<PyAny>)>()?;
                if !seen.insert(method.clone()) {
                    continue;
                }
                if self
                    .specification
                    .as_ref()
                    .is_some_and(|spec| spec.matches_function(&method))
                    && value.bind(self.py).is_callable()
                    && !fixtures::is_declaration(value.bind(self.py))?
                {
                    methods.push((method, value));
                }
            }
            groups.push(methods);
        }
        let mut functions = Vec::new();
        for methods in groups.into_iter().rev() {
            for (method, function) in methods {
                self.pycollect_makeitem(class_node, &file.path, &method, function.bind(self.py))?;
                let class = class_node.getattr("obj")?;
                functions.extend(self.add_test(
                    file,
                    &format!("{name}::{method}"),
                    function.bind(self.py),
                    Some(&class),
                    &chains,
                )?);
            }
        }
        Ok(functions)
    }

    /// Register the autouse fixtures that run xunit or unittest lifecycle methods.
    fn add_lifecycle_fixtures(
        &mut self,
        declarations: &Bound<'py, PyAny>,
        chains: &mut FixtureChains,
        baseid: &str,
        origin: FixtureOrigin,
    ) -> PyResult<()> {
        for declaration in declarations.try_iter()? {
            let original = declaration?;
            let declared = fixtures::declaration(&original, false)?.ok_or_else(|| {
                PyRuntimeError::new_err("lifecycle function has no fixture declaration")
            })?;
            self.add_fixture_declaration(
                FixtureRegistration {
                    attribute_name: declared.metadata.get_item("name")?.ok_or_else(|| {
                        PyRuntimeError::new_err("lifecycle function has no fixture name")
                    })?,
                    original: &original,
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

    fn class_fixture_chains(
        &mut self,
        file: &CollectionFile,
        name: &str,
        module_chains: &Arc<FixtureChains>,
    ) -> PyResult<Arc<FixtureChains>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("class collection requires a session"))?;
        let arena = arena::for_context(self.py, context)?;
        let owner = self
            .current_node
            .ok_or_else(|| PyRuntimeError::new_err("class collection has no owning node"))?;
        let mut chains = module_chains.as_ref().clone();
        let object = arena.borrow().object(self.py, owner)?;
        let class = object.bind(self.py).getattr("obj")?;
        let unittest = arena.borrow().is_unittest_class(owner)?;
        let lifecycle = if unittest {
            self.py
                .import("vfy.independent.unittest_support")?
                .call_method1("lifecycle_fixtures", (&class,))?
        } else {
            self.py
                .import("vfy.independent.xunit")?
                .call_method1("class_fixtures", (&class,))?
        };
        self.add_lifecycle_fixtures(
            &lifecycle,
            &mut chains,
            &format!("{}::{name}", file.nodeid),
            FixtureOrigin::Class(owner),
        )?;
        arena::NativeCollectionArena::parse_class_holder(&arena, owner, |holder| {
            let mro = holder.get_type().getattr("__mro__")?;
            let mut seen = HashSet::new();
            let mut declarations = BTreeMap::new();
            for owner in mro.try_iter()? {
                for item in owner?
                    .getattr("__dict__")?
                    .call_method0("items")?
                    .try_iter()?
                {
                    let (name, declaration) = item?.extract::<(String, Py<PyAny>)>()?;
                    if seen.insert(name.clone())
                        && fixtures::is_declaration(declaration.bind(self.py))?
                    {
                        declarations.insert(name, declaration);
                    }
                }
            }
            for (attribute_name, original) in declarations {
                let original = original.bind(self.py);
                let Some(declared) =
                    fixtures::declaration_on_holder(holder, &attribute_name, original)?
                else {
                    continue;
                };
                self.add_fixture_declaration(
                    FixtureRegistration {
                        attribute_name: attribute_name.into_pyobject(self.py)?.into_any(),
                        original,
                        declaration: declared.declaration,
                        arguments: declared.arguments,
                        origin: FixtureOrigin::Class(owner),
                    },
                    &mut chains,
                    &format!("{}::{name}", file.nodeid),
                )?;
            }
            Ok(())
        })?;
        Ok(Arc::new(chains))
    }

    pub(in crate::independent) fn class_marks(
        &self,
        class: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        let marks = PyList::empty(self.py);
        let owners = class
            .getattr("__mro__")?
            .try_iter()?
            .collect::<PyResult<Vec<_>>>()?;
        for owner in owners.into_iter().rev() {
            let namespace = owner.getattr("__dict__")?;
            let public_marks = namespace.call_method1("get", ("pytestmark",))?;
            if !public_marks.is_none() {
                if public_marks.hasattr("name")? {
                    marks.append(&public_marks)?;
                } else {
                    for mark in public_marks.try_iter()? {
                        marks.append(mark?)?;
                    }
                }
            }
        }
        Ok(marks)
    }

    pub(in crate::independent) fn order_collected_drafts(&mut self) -> PyResult<()> {
        if self.tests.len() == self.test_bindings.len()
            && self.test_bindings.len() == self.collected_order.len()
            && self
                .test_bindings
                .iter()
                .zip(&self.collected_order)
                .all(|(binding, id)| binding.collection_node == *id)
        {
            return Ok(());
        }
        let indices = self
            .test_bindings
            .iter()
            .enumerate()
            .map(|(index, binding)| (binding.collection_node, index))
            .collect::<BTreeMap<_, _>>();
        let tests = std::mem::take(&mut self.tests);
        let bindings = std::mem::take(&mut self.test_bindings);
        for id in &self.collected_order {
            let index = *indices
                .get(id)
                .ok_or_else(|| PyValueError::new_err("collected item has no native draft"))?;
            let mut test = tests[index].clone();
            test.id = TestId::from_index(self.tests.len());
            self.tests.push(test);
            self.test_bindings.push(bindings[index].clone_ref(self.py));
        }
        Ok(())
    }
}
