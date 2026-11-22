//! Resolve positional targets through the builtin collector cache.

use super::arena::{self, CollectionNodeId, CollectionNodeKind, NativeCollectionArena};
use super::{NativeVisitor, discovery_error, path_object};
use crate::independent::Collector;
use crate::independent::global_capture::Capture;
use crate::independent::reporting::NativeIndependentCollectReport;
use crate::independent::reporting::call::PhaseClock;
use crate::module_cache::Module;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyList;
use std::path::Path;
use vfy_core::independent_collection::{
    CollectionEntryKind, CollectionFile, CollectionSpecification, SelectorCursor,
};

struct Traversal<'a, 'py> {
    collector: &'a mut Collector<'py>,
    root: &'a Path,
    specification: CollectionSpecification,
    arena: Py<NativeCollectionArena>,
}

/// Collect after sessionstart, retaining the original callback exception authority.
pub(in crate::independent) fn collect(collector: &mut Collector<'_>, root: &Path) -> PyResult<()> {
    let specification = collector
        .specification
        .as_ref()
        .expect("resolved collection specification")
        .clone();
    let context = collector
        .context
        .as_ref()
        .expect("configured collection session");
    let arena = arena::for_context(collector.py, context)?;
    arena.borrow_mut().initial_paths(
        specification
            .targets()
            .map(|target| target.path.to_path_buf())
            .collect(),
    );
    let mut traversal = Traversal {
        collector,
        root,
        specification,
        arena: arena.unbind(),
    };
    let (report, _) = traversal.collect_one(CollectionNodeId::from_index(0), true)?;
    traversal.deliver(CollectionNodeId::from_index(0), &report)?;
    if report
        .bind(traversal.collector.py)
        .getattr("passed")?
        .is_truthy()?
    {
        let nodes = traversal.children(&report)?;
        for node in nodes {
            traversal.genitems(node)?;
        }
    }
    traversal.collector.collection_items()?;
    Ok(())
}

impl Traversal<'_, '_> {
    fn object(&self, id: CollectionNodeId) -> PyResult<Py<PyAny>> {
        let py = self.collector.py;
        self.arena.bind(py).borrow().object(py, id)
    }

    fn kind(&self, id: CollectionNodeId) -> PyResult<CollectionNodeKind> {
        self.arena.bind(self.collector.py).borrow().kind(id)
    }

    fn node_file(&self, id: CollectionNodeId) -> PyResult<CollectionFile> {
        let py = self.collector.py;
        let arena = self.arena.bind(py).borrow();
        let module = arena.scope_owner(id, vfy_core::FixtureScope::Module, "")?;
        let path = arena.path(module)?.to_path_buf();
        Ok(CollectionFile {
            nodeid: self.specification.nodeid(&path),
            path,
            selectors: Vec::new(),
        })
    }

    fn children(
        &self,
        report: &Py<NativeIndependentCollectReport>,
    ) -> PyResult<Vec<CollectionNodeId>> {
        let py = self.collector.py;
        let result = report.bind(py).getattr("result")?;
        let mut children = Vec::new();
        for object in result.try_iter()? {
            let object = object?;
            let id = self.arena.bind(py).borrow().find_object(&object)?;
            NativeCollectionArena::validate_parent(self.arena.bind(py), id)?;
            children.push(id);
        }
        Ok(children)
    }

    fn collect_one(
        &mut self,
        id: CollectionNodeId,
        cached: bool,
    ) -> PyResult<(Py<NativeIndependentCollectReport>, bool)> {
        let py = self.collector.py;
        if let Some(report) =
            NativeCollectionArena::begin_collection(self.arena.bind(py), id, cached)?
        {
            return Ok((report, true));
        }
        let clock = PhaseClock::start();
        let initial_tests = self.collector.tests.len();
        let capture = if self.kind(id)? == CollectionNodeKind::Module {
            Some(Capture::start(py, self.collector.context.as_ref())?)
        } else {
            None
        };
        let collected = self.collect_children(id).and_then(|children| {
            let objects = PyList::empty(py);
            for child in children {
                objects.append(self.object(child)?)?;
            }
            Ok(objects.into_any().unbind())
        });
        if collected.is_err() {
            self.collector.tests.truncate(initial_tests);
            self.collector.test_bindings.truncate(initial_tests);
        }
        // Session collection does not start module capture. Propagate target usage errors
        // to the command boundary instead of turning them into collection reports.
        let collected = match collected {
            Err(error)
                if self.kind(id)? == CollectionNodeKind::Session
                    && error.is_instance_of::<crate::IndependentUsageError>(py) =>
            {
                self.arena.bind(py).borrow_mut().abandon_collection(id)?;
                return Err(error);
            }
            result => result,
        };
        let completion = (|| {
            let (collected, stdout, stderr) = if let Some(capture) = capture {
                capture.complete(collected)?
            } else {
                (collected, String::new(), String::new())
            };
            let call = clock.complete_collection(py, collected)?;
            let object = self.object(id)?;
            NativeIndependentCollectReport::from_collection_call(
                py,
                object.bind(py),
                call.bind(py),
                stdout,
                stderr,
            )
        })();
        let report = match completion {
            Ok(report) => report,
            Err(error) => {
                self.arena.bind(py).borrow_mut().abandon_collection(id)?;
                return Err(error);
            }
        };
        self.arena
            .bind(py)
            .borrow_mut()
            .complete_collection(id, report.clone_ref(py))?;
        Ok((report, false))
    }

    fn collect_children(&mut self, id: CollectionNodeId) -> PyResult<Vec<CollectionNodeId>> {
        match self.kind(id)? {
            CollectionNodeKind::Session => self.resolve_targets(),
            CollectionNodeKind::Directory | CollectionNodeKind::Package => {
                self.directory_children(id)
            }
            CollectionNodeKind::Module => {
                let file = self.node_file(id)?;
                self.collector.collect_module(self.root, &file, id)
            }
            CollectionNodeKind::Class => {
                let file = self.node_file(id)?;
                self.collector.collect_class_node(&file, id)
            }
            CollectionNodeKind::CustomCollector => self.custom_children(id),
            CollectionNodeKind::FunctionDefinition
            | CollectionNodeKind::Function
            | CollectionNodeKind::CustomItem => Err(PyRuntimeError::new_err(
                "executable item is not a collector",
            )),
        }
    }

    /// Register the nodes a plugin collector yields from `collect()`.
    fn custom_children(&mut self, id: CollectionNodeId) -> PyResult<Vec<CollectionNodeId>> {
        let py = self.collector.py;
        let object = self.object(id)?;
        let mut children = Vec::new();
        for child in object.bind(py).call_method0("collect")?.try_iter()? {
            let context = self
                .collector
                .context
                .as_ref()
                .expect("configured collection session");
            let child = arena::custom(py, context, id, &child?)?;
            if self.kind(child)? == CollectionNodeKind::CustomItem {
                self.collector.add_custom_item(child)?;
            }
            children.push(child);
        }
        Ok(children)
    }

    /// The builtin module collector for the file at ``path``.
    fn file_module(&self, parent: CollectionNodeId, path: &Path) -> PyResult<CollectionNodeId> {
        let py = self.collector.py;
        let context = self
            .collector
            .context
            .as_ref()
            .expect("configured collection session");
        arena::collector(
            py,
            context,
            CollectionNodeKind::Module,
            parent,
            &self.specification.nodeid(path),
            path,
            &PyList::empty(py),
        )
    }

    /// A plug-in's `pytest_pycollect_makemodule` collector for a Python file,
    /// or `None` for the native module.
    fn make_module(&self, directory: CollectionNodeId, path: &Path) -> PyResult<Option<Py<PyAny>>> {
        let py = self.collector.py;
        let context = self
            .collector
            .context
            .as_ref()
            .expect("configured collection session");
        let registry = context.registry.bind(py);
        if registry
            .call_method1("get_hookimpls", ("pytest_pycollect_makemodule",))?
            .is_empty()?
        {
            return Ok(None);
        }
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("module_path", path_object(py, path)?)?;
        kwargs.set_item("parent", self.object(directory)?)?;
        let removed = context.removed_plugins(py, path.parent().unwrap_or(path))?;
        let result = registry.call_method1(
            "call_subset",
            ("pytest_pycollect_makemodule", kwargs, removed, true),
        )?;
        Ok((!result.is_none()).then(|| result.unbind()))
    }

    /// Offer one file to `pytest_collect_file`; the builtin result is its Python module.
    fn collect_file(
        &mut self,
        directory: CollectionNodeId,
        path: &Path,
    ) -> PyResult<Vec<CollectionNodeId>> {
        let py = self.collector.py;
        let context = self
            .collector
            .context
            .as_ref()
            .expect("configured collection session");
        let explicit = self
            .specification
            .targets()
            .any(|target| target.path == path);
        // `-p no:python` removes the builtin Python module collector for every file.
        let python = path.extension().is_some_and(|extension| extension == "py")
            && (explicit || self.collector.matches_python_files(path)?)
            && !context
                .config
                .bind(py)
                .getattr("pluginmanager")?
                .call_method1("is_blocked", ("python",))?
                .is_truthy()?;
        let collectors = Module::Collectors.get(py)?;
        let default = collectors.getattr(if python {
            "_collect_python_file"
        } else {
            "_collect_no_file"
        })?;
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("file_path", path_object(py, path)?)?;
        kwargs.set_item("parent", self.object(directory)?)?;
        let removed = context.removed_plugins(py, path.parent().unwrap_or(path))?;
        let results = context.registry.bind(py).call_method1(
            "call_subset_with_default",
            (
                "pytest_collect_file",
                kwargs,
                removed,
                default,
                false,
                "python",
            ),
        )?;
        let marker = collectors.getattr("PYTHON_FILE")?;
        let mut nodes = Vec::new();
        for result in results.try_iter()? {
            let result = result?;
            if result.is_none() {
                continue;
            }
            nodes.push(if result.is(&marker) {
                match self.make_module(directory, path)? {
                    Some(module) => arena::custom(py, context, directory, module.bind(py))?,
                    None => self.file_module(directory, path)?,
                }
            } else {
                arena::custom(py, context, directory, &result)?
            });
        }
        Ok(nodes)
    }

    fn directory_children(&mut self, id: CollectionNodeId) -> PyResult<Vec<CollectionNodeId>> {
        let py = self.collector.py;
        let (path, lineage) = {
            let arena = self.arena.bind(py).borrow();
            (arena.path(id)?.to_path_buf(), arena.lineage(id)?)
        };
        let cutoff = self.collector.conftest_cutoff()?;
        let batch = self
            .specification
            .directory_children(
                &path,
                &lineage,
                &mut NativeVisitor {
                    conftest_cutoff: cutoff,
                    collector: self.collector,
                },
            )
            .map_err(discovery_error)?;
        let hooked = self.collector.collect_file_hooked()?;
        let mut nodes = Vec::new();
        for entry in batch.entries {
            if hooked && entry.kind == CollectionEntryKind::File {
                nodes.extend(self.collect_file(id, &entry.path)?);
                continue;
            }
            if entry.kind == CollectionEntryKind::File
                && entry
                    .path
                    .extension()
                    .is_none_or(|extension| extension != "py")
            {
                continue;
            }
            let context = self
                .collector
                .context
                .as_ref()
                .expect("configured collection session");
            let child = match entry.kind {
                CollectionEntryKind::Directory => {
                    let child = arena::directory(py, context, &entry.path)?;
                    self.arena
                        .bind(py)
                        .borrow_mut()
                        .set_lineage(child, batch.lineage.clone())?;
                    child
                }
                CollectionEntryKind::File => self.file_module(id, &entry.path)?,
            };
            nodes.push(child);
        }
        Ok(nodes)
    }

    fn resolve_targets(&mut self) -> PyResult<Vec<CollectionNodeId>> {
        let py = self.collector.py;
        let specification = self.specification.clone();
        let mut selected = Vec::new();
        for target in specification.targets() {
            let context = self
                .collector
                .context
                .as_ref()
                .expect("configured collection session");
            if target.path.is_dir() {
                selected.push(arena::directory(py, context, target.path)?);
                continue;
            }
            let parent = target.path.parent().ok_or_else(|| {
                PyValueError::new_err("collection target has no parent directory")
            })?;
            let directory = arena::directory(py, context, parent)?;
            let Some(report) = self.collect_candidate(directory, !target.selector.is_terminal())?
            else {
                continue;
            };
            let mut found = false;
            for child in self.children(&report)? {
                let path = self.arena.bind(py).borrow().path(child)?.to_path_buf();
                if path == target.path {
                    found = true;
                    self.match_selector(child, target.selector, &mut selected)?;
                }
            }
            if !found
                && target
                    .path
                    .extension()
                    .is_none_or(|extension| extension != "py")
            {
                return Err(crate::IndependentUsageError::new_err(format!(
                    "no collector for test target: {}",
                    target.path.display()
                )));
            }
        }
        Ok(selected)
    }

    fn match_selector(
        &mut self,
        id: CollectionNodeId,
        selector: SelectorCursor<'_>,
        selected: &mut Vec<CollectionNodeId>,
    ) -> PyResult<()> {
        if selector.is_terminal() {
            selected.push(id);
            return Ok(());
        }
        let py = self.collector.py;
        if self.kind(id)?.is_item() {
            return Ok(());
        }
        let Some(report) = self.collect_candidate(id, true)? else {
            return Ok(());
        };
        for child in self.children(&report)? {
            let object = self.object(child)?;
            let name = object.bind(py).getattr("name")?.extract::<String>()?;
            if let Some(remaining) = selector.match_child(&name) {
                self.match_selector(child, remaining, selected)?;
            }
        }
        Ok(())
    }

    /// Collect a candidate that a target or selector names. A failed report is delivered
    /// unless the collection cache already delivered it.
    fn collect_candidate(
        &mut self,
        id: CollectionNodeId,
        cached: bool,
    ) -> PyResult<Option<Py<NativeIndependentCollectReport>>> {
        let (report, duplicate) = self.collect_one(id, cached)?;
        let py = self.collector.py;
        if report.bind(py).getattr("passed")?.is_truthy()? {
            return Ok(Some(report));
        }
        if !duplicate {
            self.deliver(id, &report)?;
        }
        Ok(None)
    }

    fn genitems(&mut self, id: CollectionNodeId) -> PyResult<()> {
        let py = self.collector.py;
        let kind = self.kind(id)?;
        if kind.is_item() {
            return self.collector.item_collected(id);
        }
        let (report, duplicate) = self.collect_one(id, kind != CollectionNodeKind::Module)?;
        if report.bind(py).getattr("passed")?.is_truthy()? {
            for child in self.children(&report)? {
                self.genitems(child)?;
            }
        }
        if !duplicate {
            self.deliver(id, &report)?;
        }
        Ok(())
    }

    fn deliver(
        &mut self,
        id: CollectionNodeId,
        report: &Py<NativeIndependentCollectReport>,
    ) -> PyResult<()> {
        let py = self.collector.py;
        let object = self.object(id)?;
        let relay = object.bind(py).getattr("ihook")?;
        let kwargs = pyo3::types::PyDict::new(py);
        kwargs.set_item("report", report)?;
        relay
            .getattr("pytest_collectreport")?
            .call((), Some(&kwargs))?;
        self.collector
            .collection_reports
            .push(NativeIndependentCollectReport::snapshot(report.bind(py))?);
        Ok(())
    }
}
