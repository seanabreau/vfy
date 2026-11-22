//! One collection arena owns the collector and executable item identities.

use crate::independent::imports::{self, ImportMode};
use crate::independent::lifecycle::Context;
use crate::independent::reporting::NativeIndependentCollectReport;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::{PyTraverseError, PyVisit};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use vfy_core::independent_collection::DirectoryLineage;
use vfy_core::test_plan::FixtureChains;

pub(in crate::independent) use vfy_core::CollectionNodeId;
mod factory;
pub(in crate::independent) use factory::{
    FunctionInput, collector, custom, directory, function, unittest_collector,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(in crate::independent) enum CollectionNodeKind {
    Session,
    Directory,
    Package,
    Module,
    Class,
    FunctionDefinition,
    Function,
    /// A collector returned by a `pytest_collect_file` hook or `Collector.collect()`.
    CustomCollector,
    /// An item returned by a custom collector's `collect()`.
    CustomItem,
}

impl CollectionNodeKind {
    /// Whether the node is an executable test item rather than a collector.
    pub(in crate::independent) const fn is_item(self) -> bool {
        matches!(self, Self::Function | Self::CustomItem)
    }
}

enum ClassFactoryState {
    Unprepared,
    Calling,
    Parsing(Py<PyAny>),
    Parsed,
    Failed,
}

enum CollectionState {
    Uncollected,
    Calling,
    Complete(Py<NativeIndependentCollectReport>),
}

#[derive(Default)]
struct DirectoryEntry {
    node: Option<CollectionNodeId>,
    conftests_loaded: bool,
}

struct CollectionNode {
    kind: CollectionNodeKind,
    parent: Option<CollectionNodeId>,
    object: Py<PyAny>,
    path: PathBuf,
    class_factory: ClassFactoryState,
    unittest: bool,
    collection: CollectionState,
    lineage: DirectoryLineage,
    fixtures: Option<Arc<FixtureChains>>,
}

impl CollectionNode {
    fn new(
        kind: CollectionNodeKind,
        parent: Option<CollectionNodeId>,
        object: Py<PyAny>,
        path: PathBuf,
    ) -> Self {
        Self {
            kind,
            parent,
            object,
            path,
            class_factory: ClassFactoryState::Unprepared,
            unittest: false,
            collection: CollectionState::Uncollected,
            lineage: DirectoryLineage::default(),
            fixtures: None,
        }
    }
}

struct IndexedObject {
    first_node: CollectionNodeId,
    // Repeated registrations can have different kinds; any async Function qualifies.
    asynchronous: bool,
}

#[pyclass(module = "vfy._native._pytest", unsendable)]
pub(crate) struct NativeCollectionArena {
    root: PathBuf,
    cutoff: PathBuf,
    initial_paths: Vec<PathBuf>,
    nodes: Vec<CollectionNode>,
    // Nodes retain their Python objects until this index is cleared, preventing
    // an address from being reused for another object while it is indexed.
    objects: BTreeMap<usize, IndexedObject>,
    directories: BTreeMap<PathBuf, DirectoryEntry>,
    items: Option<Py<PyList>>,
}

pub(in crate::independent) fn install(
    py: Python<'_>,
    session: &Bound<'_, PyAny>,
    root: PathBuf,
) -> PyResult<()> {
    let arena = Py::new(
        py,
        NativeCollectionArena {
            cutoff: crate::independent::configuration::conftest_cutoff(
                &session.getattr("config")?,
                &root,
            )?,
            initial_paths: Vec::new(),
            root: root.clone(),
            nodes: vec![CollectionNode::new(
                CollectionNodeKind::Session,
                None,
                session.clone().unbind(),
                root,
            )],
            objects: BTreeMap::from([(
                session.as_ptr() as usize,
                IndexedObject {
                    first_node: CollectionNodeId::from_index(0),
                    asynchronous: false,
                },
            )]),
            directories: BTreeMap::new(),
            items: Some(session.getattr("items")?.cast_into::<PyList>()?.unbind()),
        },
    )?;
    session.setattr("_vfy_collection", arena)
}

pub(in crate::independent) fn for_context<'py>(
    py: Python<'py>,
    context: &Context,
) -> PyResult<Bound<'py, NativeCollectionArena>> {
    let arena = context
        .session
        .bind(py)
        .getattr("_vfy_collection")?
        .cast_into::<NativeCollectionArena>()?;
    let session = arena.borrow().object(py, CollectionNodeId::from_index(0))?;
    if !session.bind(py).is(context.session.bind(py)) {
        return Err(PyValueError::new_err(
            "collection arena belongs to another session",
        ));
    }
    Ok(arena)
}

impl NativeCollectionArena {
    pub(in crate::independent) fn node_count(&self) -> usize {
        self.nodes.len()
    }

    fn module_id(&self, py: Python<'_>, module: &Bound<'_, PyAny>) -> PyResult<CollectionNodeId> {
        if let Some(entry) = self.objects.get(&(module.as_ptr() as usize))
            && self.node(entry.first_node)?.kind == CollectionNodeKind::Module
        {
            return Ok(entry.first_node);
        }
        self.nodes
            .iter()
            .position(|node| {
                node.kind == CollectionNodeKind::Module && node.object.bind(py).is(module)
            })
            .map(CollectionNodeId::from_index)
            .ok_or_else(|| PyValueError::new_err("module is not owned by this collection"))
    }

    fn node(&self, id: CollectionNodeId) -> PyResult<&CollectionNode> {
        self.nodes
            .get(id.index())
            .ok_or_else(|| PyValueError::new_err("unknown collection node"))
    }

    pub(in crate::independent) fn is_unittest_class(&self, id: CollectionNodeId) -> PyResult<bool> {
        Ok(self.node(id)?.unittest)
    }

    pub(in crate::independent) fn is_unittest_item(&self, id: CollectionNodeId) -> PyResult<bool> {
        match self.node(id)?.parent {
            Some(parent) => self.is_unittest_class(parent),
            None => Ok(false),
        }
    }

    fn node_mut(&mut self, id: CollectionNodeId) -> PyResult<&mut CollectionNode> {
        self.nodes
            .get_mut(id.index())
            .ok_or_else(|| PyValueError::new_err("unknown collection node"))
    }

    pub(in crate::independent) fn initial_paths(&mut self, paths: Vec<PathBuf>) {
        self.initial_paths = paths;
    }

    pub(in crate::independent) fn items<'py>(
        slf: &Bound<'py, Self>,
    ) -> PyResult<Bound<'py, PyList>> {
        let py = slf.py();
        let (items, session) = {
            let arena = slf.borrow();
            let items = arena
                .items
                .as_ref()
                .ok_or_else(|| PyValueError::new_err("collection items have been released"))?
                .clone_ref(py);
            (items, arena.object(py, CollectionNodeId::from_index(0))?)
        };
        if !session.bind(py).getattr("items")?.is(items.bind(py)) {
            return Err(crate::UnsupportedFeatureError::new_err(
                "replacing Session.items during native collection is unsupported",
            ));
        }
        Ok(items.into_bound(py))
    }

    pub(in crate::independent) fn object(
        &self,
        py: Python<'_>,
        id: CollectionNodeId,
    ) -> PyResult<Py<PyAny>> {
        Ok(self.node(id)?.object.clone_ref(py))
    }

    pub(in crate::independent) fn parse_class_holder<'py, R>(
        slf: &Bound<'py, Self>,
        id: CollectionNodeId,
        parse: impl FnOnce(&Bound<'py, PyAny>) -> PyResult<R>,
    ) -> PyResult<R> {
        let py = slf.py();
        let collector = {
            let mut arena = slf.borrow_mut();
            let node = arena.node_mut(id)?;
            if node.kind != CollectionNodeKind::Class
                || !matches!(node.class_factory, ClassFactoryState::Unprepared)
            {
                return Err(PyRuntimeError::new_err(
                    "class fixture parsing has already started",
                ));
            }
            node.class_factory = ClassFactoryState::Calling;
            node.object.clone_ref(py)
        };
        let holder = match collector.bind(py).call_method0("newinstance") {
            Ok(holder) => holder,
            Err(error) => {
                slf.borrow_mut().node_mut(id)?.class_factory = ClassFactoryState::Failed;
                return Err(error);
            }
        };
        slf.borrow_mut().node_mut(id)?.class_factory =
            ClassFactoryState::Parsing(holder.clone().unbind());
        let result = Self::validate_parent(slf, id).and_then(|()| parse(&holder));
        let removed = {
            let mut arena = slf.borrow_mut();
            std::mem::replace(
                &mut arena.node_mut(id)?.class_factory,
                if result.is_ok() {
                    ClassFactoryState::Parsed
                } else {
                    ClassFactoryState::Failed
                },
            )
        };
        drop(removed);
        drop(holder);
        result
    }

    pub(in crate::independent) fn validate_parent(
        slf: &Bound<'_, Self>,
        id: CollectionNodeId,
    ) -> PyResult<()> {
        let py = slf.py();
        let chain = {
            let arena = slf.borrow();
            let mut chain = Vec::new();
            let mut current = Some(id);
            while let Some(id) = current {
                let node = arena.node(id)?;
                let parent = node.parent.map(|id| arena.object(py, id)).transpose()?;
                chain.push((node.object.clone_ref(py), parent));
                current = node.parent;
            }
            chain
        };
        for (object, expected) in chain {
            let actual = object.bind(py).getattr("parent")?;
            let matches = expected
                .as_ref()
                .map_or_else(|| actual.is_none(), |parent| actual.is(parent.bind(py)));
            if !matches {
                return Err(crate::UnsupportedFeatureError::new_err(
                    "reparenting native collection nodes is unsupported",
                ));
            }
        }
        Ok(())
    }

    pub(in crate::independent) fn resolve_item(
        slf: &Bound<'_, Self>,
        object: &Bound<'_, PyAny>,
    ) -> PyResult<CollectionNodeId> {
        let id = {
            let arena = slf.borrow();
            match arena
                .objects
                .get(&(object.as_ptr() as usize))
                .map(|entry| entry.first_node)
            {
                Some(id) if arena.node(id)?.kind.is_item() => Some(id),
                // Preserve the first executable registration if a custom
                // collector has also registered this object as a collector.
                _ => arena
                    .nodes
                    .iter()
                    .position(|node| node.kind.is_item() && node.object.bind(slf.py()).is(object))
                    .map(CollectionNodeId::from_index),
            }
        }
        .ok_or_else(|| PyValueError::new_err("item is not owned by this collection"))?;
        Self::validate_parent(slf, id)?;
        Ok(id)
    }

    pub(in crate::independent) fn kind(
        &self,
        id: CollectionNodeId,
    ) -> PyResult<CollectionNodeKind> {
        Ok(self.node(id)?.kind)
    }

    pub(in crate::independent) fn lineage(
        &self,
        id: CollectionNodeId,
    ) -> PyResult<DirectoryLineage> {
        Ok(self.node(id)?.lineage.clone())
    }

    pub(in crate::independent) fn set_lineage(
        &mut self,
        id: CollectionNodeId,
        lineage: DirectoryLineage,
    ) -> PyResult<()> {
        self.node_mut(id)?.lineage = lineage;
        Ok(())
    }

    pub(in crate::independent) fn set_fixtures(
        &mut self,
        id: CollectionNodeId,
        fixtures: Arc<FixtureChains>,
    ) -> PyResult<()> {
        self.node_mut(id)?.fixtures = Some(fixtures);
        Ok(())
    }

    pub(in crate::independent) fn fixtures(
        &self,
        id: CollectionNodeId,
    ) -> PyResult<Arc<FixtureChains>> {
        self.node(id)?
            .fixtures
            .as_ref()
            .map(Arc::clone)
            .ok_or_else(|| PyRuntimeError::new_err("module fixture declarations are not collected"))
    }

    pub(in crate::independent) fn find_object(
        &self,
        object: &Bound<'_, PyAny>,
    ) -> PyResult<CollectionNodeId> {
        self.objects
            .get(&(object.as_ptr() as usize))
            .map(|entry| entry.first_node)
            .ok_or_else(|| PyValueError::new_err("collection result has a foreign node"))
    }

    pub(in crate::independent) fn begin_collection(
        slf: &Bound<'_, Self>,
        id: CollectionNodeId,
        cached: bool,
    ) -> PyResult<Option<Py<NativeIndependentCollectReport>>> {
        let removed = {
            let mut arena = slf.borrow_mut();
            let node = arena.node_mut(id)?;
            match &node.collection {
                CollectionState::Complete(report) if cached => {
                    return Ok(Some(report.clone_ref(slf.py())));
                }
                CollectionState::Calling => {
                    return Err(PyRuntimeError::new_err("collector is already collecting"));
                }
                CollectionState::Uncollected | CollectionState::Complete(_) => (),
            }
            std::mem::replace(&mut node.collection, CollectionState::Calling)
        };
        drop(removed);
        Ok(None)
    }

    pub(in crate::independent) fn conftests_loaded(&self, directory: &Path) -> bool {
        self.directories
            .get(directory)
            .is_some_and(|entry| entry.conftests_loaded)
    }

    pub(in crate::independent) fn complete_conftests(&mut self, directory: &Path) {
        self.directories
            .entry(directory.to_path_buf())
            .or_default()
            .conftests_loaded = true;
    }

    pub(crate) fn conftests_published(slf: &Bound<'_, Self>, path: &Path) -> bool {
        let directory = if path.is_file() {
            path.parent().unwrap_or(path)
        } else {
            path
        };
        slf.borrow().conftests_loaded(directory)
    }

    /// Resolve a function's declared module source after the live path callback.
    pub(crate) fn conftests_published_for_item(
        slf: &Bound<'_, Self>,
        item: &Bound<'_, PyAny>,
        path: &Path,
    ) -> bool {
        let declared = {
            let arena = slf.borrow();
            arena
                .objects
                .get(&(item.as_ptr() as usize))
                .and_then(|entry| {
                    let node = arena.nodes.get(entry.first_node.index())?;
                    // Only factory::function creates these kinds, using the owning
                    // Module's source path. Custom items can point at directories.
                    if matches!(
                        node.kind,
                        CollectionNodeKind::Function | CollectionNodeKind::FunctionDefinition
                    ) && node.path == path
                    {
                        Some(arena.conftests_loaded(path.parent().unwrap_or(path)))
                    } else {
                        None
                    }
                })
        };
        declared.unwrap_or_else(|| Self::conftests_published(slf, path))
    }

    pub(in crate::independent) fn complete_collection(
        &mut self,
        id: CollectionNodeId,
        report: Py<NativeIndependentCollectReport>,
    ) -> PyResult<()> {
        if !matches!(self.node(id)?.collection, CollectionState::Calling) {
            return Err(PyRuntimeError::new_err(
                "collector has no running collection",
            ));
        }
        self.node_mut(id)?.collection = CollectionState::Complete(report);
        Ok(())
    }

    pub(in crate::independent) fn abandon_collection(
        &mut self,
        id: CollectionNodeId,
    ) -> PyResult<()> {
        if matches!(self.node(id)?.collection, CollectionState::Calling) {
            self.node_mut(id)?.collection = CollectionState::Uncollected;
        }
        Ok(())
    }

    pub(in crate::independent) fn clear_reports(slf: &Bound<'_, Self>) {
        let removed = {
            let mut arena = slf.borrow_mut();
            arena
                .nodes
                .iter_mut()
                .filter_map(|node| {
                    match std::mem::replace(&mut node.collection, CollectionState::Uncollected) {
                        CollectionState::Complete(report) => Some(report),
                        CollectionState::Uncollected | CollectionState::Calling => None,
                    }
                })
                .collect::<Vec<_>>()
        };
        drop(removed);
    }

    pub(in crate::independent) fn path(&self, id: CollectionNodeId) -> PyResult<&Path> {
        Ok(&self.node(id)?.path)
    }

    pub(in crate::independent) fn ancestry(
        &self,
        mut id: CollectionNodeId,
    ) -> PyResult<Vec<CollectionNodeId>> {
        let mut result = Vec::new();
        loop {
            result.push(id);
            match self.node(id)?.parent {
                Some(parent) => id = parent,
                None => break,
            }
        }
        result.reverse();
        Ok(result)
    }

    pub(in crate::independent) fn contains(
        &self,
        ancestor: CollectionNodeId,
        mut descendant: CollectionNodeId,
    ) -> PyResult<bool> {
        loop {
            if ancestor == descendant {
                return Ok(true);
            }
            let Some(parent) = self.node(descendant)?.parent else {
                return Ok(false);
            };
            descendant = parent;
        }
    }

    pub(in crate::independent) fn parameter_owner(
        &self,
        definition: CollectionNodeId,
        scope: vfy_core::FixtureScope,
    ) -> PyResult<CollectionNodeId> {
        use vfy_core::FixtureScope;
        if scope == FixtureScope::Function {
            return Ok(definition);
        }
        let mut current = Some(definition);
        let mut module = None;
        while let Some(id) = current {
            let node = self.node(id)?;
            if node.kind == CollectionNodeKind::Module {
                module = Some(id);
            }
            if matches!(
                (scope, node.kind),
                (FixtureScope::Class, CollectionNodeKind::Class)
                    | (FixtureScope::Module, CollectionNodeKind::Module)
                    | (FixtureScope::Package, CollectionNodeKind::Package)
                    | (FixtureScope::Session, CollectionNodeKind::Session)
            ) {
                return Ok(id);
            }
            current = node.parent;
        }
        if scope == FixtureScope::Class {
            module
                .ok_or_else(|| PyRuntimeError::new_err("parameter definition has no module owner"))
        } else {
            Ok(CollectionNodeId::from_index(0))
        }
    }

    pub(in crate::independent) fn scope_owner(
        &self,
        item: CollectionNodeId,
        scope: vfy_core::FixtureScope,
        baseid: &str,
    ) -> PyResult<CollectionNodeId> {
        use vfy_core::FixtureScope;
        if scope == FixtureScope::Function {
            self.node(item)?;
            return Ok(item);
        }
        let mut current = Some(item);
        while let Some(id) = current {
            let node = self.node(id)?;
            let matches = match scope {
                FixtureScope::Session => node.kind == CollectionNodeKind::Session,
                FixtureScope::Class => node.kind == CollectionNodeKind::Class,
                // A custom file collector owns module scope for its items.
                FixtureScope::Module => matches!(
                    node.kind,
                    CollectionNodeKind::Module | CollectionNodeKind::CustomCollector
                ),
                FixtureScope::Package => {
                    node.kind == CollectionNodeKind::Package
                        && node
                            .path
                            .strip_prefix(&self.root)
                            .is_ok_and(|path| path.to_string_lossy() == baseid)
                }
                FixtureScope::Function => unreachable!(),
            };
            if matches {
                return Ok(id);
            }
            current = node.parent;
        }
        match scope {
            FixtureScope::Class => Ok(item),
            FixtureScope::Package | FixtureScope::Session => Ok(CollectionNodeId::from_index(0)),
            FixtureScope::Module | FixtureScope::Function => Err(PyRuntimeError::new_err(
                "collection node has no requested scope owner",
            )),
        }
    }

    pub(in crate::independent) fn add_object(
        &mut self,
        kind: CollectionNodeKind,
        parent: CollectionNodeId,
        object: Py<PyAny>,
        path: PathBuf,
        asynchronous: bool,
    ) -> PyResult<CollectionNodeId> {
        self.node(parent)?;
        let id = CollectionNodeId::from_index(self.nodes.len());
        let asynchronous = kind == CollectionNodeKind::Function && asynchronous;
        self.objects
            .entry(object.as_ptr() as usize)
            .and_modify(|entry| entry.asynchronous |= asynchronous)
            .or_insert(IndexedObject {
                first_node: id,
                asynchronous,
            });
        self.nodes
            .push(CollectionNode::new(kind, Some(parent), object, path));
        Ok(id)
    }
}

#[pymethods]
impl NativeCollectionArena {
    fn set_module_object(
        slf: &Bound<'_, Self>,
        module: &Bound<'_, PyAny>,
        value: Py<PyAny>,
    ) -> PyResult<()> {
        slf.borrow().module_id(slf.py(), module)?;
        module.setattr("_obj", value)
    }

    fn module_object(slf: &Bound<'_, Self>, module: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let id = slf.borrow().module_id(py, module)?;
        match module.getattr("_obj") {
            Ok(cached) if !cached.is_none() => return Ok(cached.unbind()),
            Ok(_) => (),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => {}
            Err(error) => return Err(error),
        }
        let (path, root) = {
            let arena = slf.borrow();
            (arena.node(id)?.path.clone(), arena.root.clone())
        };
        let mode = module
            .getattr("config")?
            .call_method1("getoption", ("import_mode", "prepend"))?
            .extract::<String>()?;
        let mode = ImportMode::parse(&mode).map_err(crate::IndependentUsageError::new_err)?;
        let imported =
            imports::load_module(py, &path, &root, mode, imports::ImportPurpose::TestModule)?;
        module.setattr("_obj", &imported)?;
        if module.getattr("_ALLOW_MARKERS")?.is_truthy()? {
            let markers = module.getattr("own_markers")?;
            let keywords = module.getattr("keywords")?;
            for mark in crate::independent::module_mark_objects(imported.bind(py))? {
                let actual = mark.getattr_opt("mark")?.unwrap_or(mark);
                keywords.set_item(actual.getattr("name")?, &actual)?;
                markers.call_method1("append", (actual,))?;
            }
        }
        Ok(imported)
    }

    /// Whether ``path`` is a command-line target, or contains one with ``with_parents``.
    fn is_initial_path(&self, path: &str, with_parents: bool) -> bool {
        let path = Path::new(path);
        self.initial_paths
            .iter()
            .any(|initial| initial == path || (with_parents && initial.starts_with(path)))
    }

    fn is_async_test(&self, item: &Bound<'_, PyAny>) -> bool {
        self.objects
            .get(&(item.as_ptr() as usize))
            .is_some_and(|entry| entry.asynchronous)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.items)?;
        for node in &self.nodes {
            visit.call(&node.object)?;
            if let CollectionState::Complete(report) = &node.collection {
                visit.call(report)?;
            }
            if let ClassFactoryState::Parsing(holder) = &node.class_factory {
                visit.call(holder)?;
            }
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut arena = slf.borrow_mut();
            arena.directories.clear();
            arena.objects.clear();
            (std::mem::take(&mut arena.nodes), arena.items.take())
        };
        drop(removed);
    }
}
