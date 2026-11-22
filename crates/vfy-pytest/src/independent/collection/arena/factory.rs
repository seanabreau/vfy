//! Allocate Python protocol resources in their canonical native arena.

use super::{CollectionNodeId, CollectionNodeKind, NativeCollectionArena, for_context};
use crate::independent::lifecycle::Context;
use crate::module_cache::Module;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};
use std::path::{Path, PathBuf};
use vfy_core::test_plan::TestPlan;

pub(in crate::independent) fn collector(
    py: Python<'_>,
    context: &Context,
    kind: CollectionNodeKind,
    parent: CollectionNodeId,
    nodeid: &str,
    path: &Path,
    marks: &Bound<'_, PyList>,
) -> PyResult<CollectionNodeId> {
    let class = match kind {
        CollectionNodeKind::Directory => "Dir",
        CollectionNodeKind::Package => "Package",
        CollectionNodeKind::Module => "Module",
        CollectionNodeKind::Class => "Class",
        CollectionNodeKind::Session
        | CollectionNodeKind::FunctionDefinition
        | CollectionNodeKind::Function
        | CollectionNodeKind::CustomCollector
        | CollectionNodeKind::CustomItem => {
            return Err(PyValueError::new_err(
                "node kind is not a filesystem or class collector",
            ));
        }
    };
    collector_object(
        py,
        context,
        CollectorProtocol {
            kind,
            module: "vfy.independent.collectors",
            name: class,
            unittest: false,
        },
        parent,
        nodeid,
        path,
        marks,
    )
}

pub(in crate::independent) fn unittest_collector(
    py: Python<'_>,
    context: &Context,
    parent: CollectionNodeId,
    nodeid: &str,
    path: &Path,
    marks: &Bound<'_, PyList>,
) -> PyResult<CollectionNodeId> {
    collector_object(
        py,
        context,
        CollectorProtocol {
            kind: CollectionNodeKind::Class,
            module: "vfy.independent.unittest_support",
            name: "UnitTestCase",
            unittest: true,
        },
        parent,
        nodeid,
        path,
        marks,
    )
}

#[derive(Clone, Copy)]
struct CollectorProtocol {
    kind: CollectionNodeKind,
    module: &'static str,
    name: &'static str,
    unittest: bool,
}

fn collector_object(
    py: Python<'_>,
    context: &Context,
    protocol: CollectorProtocol,
    parent: CollectionNodeId,
    nodeid: &str,
    path: &Path,
    marks: &Bound<'_, PyList>,
) -> PyResult<CollectionNodeId> {
    let CollectorProtocol {
        kind,
        module,
        name: class,
        unittest,
    } = protocol;
    let arena = for_context(py, context)?;
    let parent_object = arena.borrow().object(py, parent)?;
    let name = if kind == CollectionNodeKind::Class {
        nodeid.rsplit("::").next().unwrap_or(nodeid).to_owned()
    } else {
        path.file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned()
    };
    let node_path = if kind == CollectionNodeKind::Class {
        parent_object.bind(py).getattr("path")?
    } else {
        PyString::new(py, path.to_string_lossy().as_ref()).into_any()
    };
    let object = py.import(module)?.getattr(class)?.call1((
        name,
        nodeid,
        node_path,
        &context.config,
        &context.session,
        parent_object,
        marks,
    ))?;
    object.setattr("_vfy_arena", &arena)?;
    let id =
        arena
            .borrow_mut()
            .add_object(kind, parent, object.unbind(), path.to_path_buf(), false)?;
    arena.borrow_mut().node_mut(id)?.unittest = unittest;
    Ok(id)
}

/// Register a node that a plugin constructed through `from_parent`.
pub(in crate::independent) fn custom(
    py: Python<'_>,
    context: &Context,
    parent: CollectionNodeId,
    object: &Bound<'_, PyAny>,
) -> PyResult<CollectionNodeId> {
    let item = object.is_instance(&py.import("vfy.independent.protocol")?.getattr("Item")?)?;
    let kind = if item {
        CollectionNodeKind::CustomItem
    } else {
        CollectionNodeKind::CustomCollector
    };
    let arena = for_context(py, context)?;
    let expected = arena.borrow().object(py, parent)?;
    if !object.getattr("parent")?.is(expected.bind(py)) {
        return Err(PyValueError::new_err(
            "a collected node must be created from its collector with from_parent",
        ));
    }
    let path = PathBuf::from(object.getattr("path")?.str()?.to_str()?);
    let id = arena
        .borrow_mut()
        .add_object(kind, parent, object.clone().unbind(), path, false)?;
    Ok(id)
}

pub(in crate::independent) fn directory(
    py: Python<'_>,
    context: &Context,
    directory: &Path,
) -> PyResult<CollectionNodeId> {
    let arena = for_context(py, context)?;
    let root = arena.borrow().root.clone();
    let cutoff = arena.borrow().cutoff.clone();
    let paths = vfy_core::independent_collection::collection_directories(&cutoff, directory);
    let mut parent = CollectionNodeId::from_index(0);
    for path in paths {
        let existing = arena
            .borrow()
            .directories
            .get(&path)
            .and_then(|entry| entry.node);
        parent = if let Some(id) = existing {
            id
        } else {
            let nodeid = {
                let state = arena.borrow();
                vfy_core::independent_collection::filesystem_nodeid(
                    &root,
                    &state.initial_paths,
                    &path,
                )
            };
            let nodeid = if let Some(nodeid) = nodeid {
                nodeid
            } else {
                let parent = arena.borrow().object(py, parent)?;
                let parent_id = parent.bind(py).getattr("nodeid")?.extract::<String>()?;
                let name = path.file_name().unwrap_or_default().to_string_lossy();
                if name == "()" {
                    parent_id
                } else {
                    format!("{parent_id}::{name}")
                }
            };
            let kind = if path.join("__init__.py").is_file() {
                CollectionNodeKind::Package
            } else {
                CollectionNodeKind::Directory
            };
            let id = collector(
                py,
                context,
                kind,
                parent,
                &nodeid,
                &path,
                &PyList::empty(py),
            )?;
            arena.borrow_mut().directories.entry(path).or_default().node = Some(id);
            id
        };
    }
    Ok(parent)
}

#[derive(Clone, Copy)]
pub(in crate::independent) struct FunctionInput<'a, 'py> {
    pub(in crate::independent) kind: CollectionNodeKind,
    pub(in crate::independent) plan: &'a TestPlan,
    pub(in crate::independent) fixture_names: &'a Bound<'py, PyList>,
    pub(in crate::independent) callable: &'a Bound<'py, PyAny>,
    pub(in crate::independent) class: Option<&'a Bound<'py, PyAny>>,
    pub(in crate::independent) marks: &'a Bound<'py, PyList>,
    pub(in crate::independent) own_mark_count: usize,
}

fn function_owners(
    py: Python<'_>,
    arena: &NativeCollectionArena,
    parent: CollectionNodeId,
) -> PyResult<(Py<PyAny>, Py<PyAny>, PathBuf)> {
    let parent_object = arena.object(py, parent)?;
    let mut owner = arena.node(parent)?;
    while owner.kind != CollectionNodeKind::Module {
        owner = arena.node(
            owner
                .parent
                .ok_or_else(|| PyRuntimeError::new_err("function has no module parent"))?,
        )?;
    }
    let module = owner.object.clone_ref(py);
    Ok((parent_object, module, owner.path.clone()))
}

pub(in crate::independent) fn function(
    py: Python<'_>,
    context: &Context,
    parent: CollectionNodeId,
    input: FunctionInput<'_, '_>,
) -> PyResult<CollectionNodeId> {
    let arena = for_context(py, context)?;
    let (parent_object, module, path) = function_owners(py, &arena.borrow(), parent)?;
    let class = match input.kind {
        CollectionNodeKind::Function => "Function",
        CollectionNodeKind::FunctionDefinition => "FunctionDefinition",
        CollectionNodeKind::Session
        | CollectionNodeKind::Directory
        | CollectionNodeKind::Package
        | CollectionNodeKind::Module
        | CollectionNodeKind::Class
        | CollectionNodeKind::CustomCollector
        | CollectionNodeKind::CustomItem => {
            return Err(PyValueError::new_err("node kind is not a function"));
        }
    };
    // The owning collector was classified when it was created; pytest uses
    // that collector kind for every item even if a class changes afterwards.
    let unittest = arena.borrow().is_unittest_class(parent)?;
    let (adapter_module, class) = if unittest {
        (
            py.import("vfy.independent.unittest_support")?,
            "TestCaseFunction",
        )
    } else {
        (Module::Collectors.get(py)?, class)
    };
    let object = adapter_module.getattr(class)?.call1((
        &input.plan.nodeid,
        input.callable,
        &context.config,
        &context.session,
        input.fixture_names,
        input.marks,
        input.own_mark_count,
        parent_object,
    ))?;
    let id = arena.borrow_mut().add_object(
        input.kind,
        parent,
        object.clone().unbind(),
        path,
        input.plan.async_loop_scope.is_some(),
    )?;
    let instance = if input.kind == CollectionNodeKind::Function && input.class.is_some() {
        let instance = if unittest {
            object
                .getattr("parent")?
                .getattr("obj")?
                .call1((object.getattr("originalname")?,))?
        } else {
            object.getattr("parent")?.call_method0("newinstance")?
        };
        object.setattr("instance", &instance)?;
        NativeCollectionArena::validate_parent(&arena, parent)?;
        let method = object.getattr("originalname")?;
        let callable = instance.getattr(method.cast::<pyo3::types::PyString>()?)?;
        object.setattr("obj", &callable)?;
        object.setattr(
            "function",
            callable.getattr("__func__").or_else(|error| {
                if error.is_instance_of::<PyAttributeError>(py) {
                    Ok(callable.clone())
                } else {
                    Err(error)
                }
            })?,
        )?;
        instance
    } else {
        input.callable.getattr("__self__").or_else(|error| {
            if error.is_instance_of::<PyAttributeError>(py) {
                Ok(py.None().into_bound(py))
            } else {
                Err(error)
            }
        })?
    };
    object.setattr("instance", instance)?;
    if input.class.is_some() {
        object.setattr("cls", object.getattr("parent")?.getattr("obj")?)?;
    } else {
        object.setattr("cls", py.None())?;
    }
    object.setattr("module", module.bind(py).getattr("obj")?)?;
    Ok(id)
}
