//! Session-owned active nodes and their finalizers.

use pyo3::exceptions::{PyAssertionError, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use std::sync::Mutex;
use vfy_core::CollectionNodeId;

pub(super) struct ActiveNode {
    pub(super) identity: CollectionNodeId,
    pub(super) node: Py<PyAny>,
    pub(super) callbacks: Option<Py<PyList>>,
}

/// Session-owned state for active nodes and their finalizers.
#[pyclass(frozen, weakref, module = "vfy._native._pytest")]
pub(super) struct NativeNodeFinalizers {
    pub(super) nodes: Mutex<Vec<ActiveNode>>,
    // Public Node.addfinalizer resolves equal nodes through Python's dictionary rules.
    pub(super) canonical: Py<PyDict>,
}

impl NativeNodeFinalizers {
    pub(super) fn locked(&self) -> PyResult<std::sync::MutexGuard<'_, Vec<ActiveNode>>> {
        self.nodes
            .lock()
            .map_err(|_| PyRuntimeError::new_err("node finalizer lock is poisoned"))
    }

    pub(super) fn callbacks(
        &self,
        py: Python<'_>,
        identity: CollectionNodeId,
    ) -> PyResult<Py<PyList>> {
        let existing = {
            let nodes = self.locked()?;
            let node = nodes
                .iter()
                .find(|node| node.identity == identity)
                .ok_or_else(|| PyAssertionError::new_err("node is not active"))?;
            node.callbacks
                .as_ref()
                .map(|callbacks| callbacks.clone_ref(py))
        };
        if let Some(existing) = existing {
            return Ok(existing);
        }
        // Allocation may trigger a finalizer that changes the active stack.
        let candidate = PyList::empty(py).unbind();
        let (selected, unused) = {
            let mut nodes = self.locked()?;
            let node = nodes
                .iter_mut()
                .find(|node| node.identity == identity)
                .ok_or_else(|| PyAssertionError::new_err("node is not active"))?;
            if let Some(existing) = &node.callbacks {
                (existing.clone_ref(py), Some(candidate))
            } else {
                let selected = candidate.clone_ref(py);
                node.callbacks = Some(candidate);
                (selected, None)
            }
        };
        drop(unused);
        Ok(selected)
    }
}

#[pymethods]
impl NativeNodeFinalizers {
    fn addfinalizer(
        &self,
        py: Python<'_>,
        finalizer: &Bound<'_, PyAny>,
        node: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if !node.is_truthy()? || node.is_instance_of::<PyTuple>() || !finalizer.is_callable() {
            return Err(PyAssertionError::new_err(()));
        }
        let canonical = self.canonical.bind(py);
        let present = canonical.contains(node)?;
        let canonical_node = if present {
            Some(canonical.as_any().get_item(node)?)
        } else {
            None
        };
        let (identity, existing, stack) = {
            let nodes = self.locked()?;
            let active = canonical_node.as_ref().and_then(|canonical_node| {
                nodes
                    .iter()
                    .rev()
                    .find(|active| active.node.bind(py).is(canonical_node))
            });
            let identity = active.map(|active| active.identity);
            let existing = active.and_then(|active| {
                active
                    .callbacks
                    .as_ref()
                    .map(|callbacks| callbacks.clone_ref(py))
            });
            let stack = if identity.is_none() {
                nodes
                    .iter()
                    .map(|active| {
                        (
                            active.node.clone_ref(py),
                            active
                                .callbacks
                                .as_ref()
                                .map(|callbacks| callbacks.clone_ref(py)),
                        )
                    })
                    .collect::<Vec<_>>()
            } else {
                Vec::new()
            };
            (identity, existing, stack)
        };
        let Some(identity) = identity else {
            let view = PyDict::new(py);
            for (owner, callbacks) in stack {
                let callbacks = callbacks.unwrap_or_else(|| PyList::empty(py).unbind());
                view.set_item(owner, (callbacks, py.None()))?;
            }
            return Err(PyAssertionError::new_err(((
                node.clone().unbind(),
                view.unbind(),
            ),)));
        };
        if let Some(callbacks) = existing {
            callbacks.bind(py).append(finalizer)
        } else {
            self.callbacks(py, identity)?.bind(py).append(finalizer)
        }
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its GC visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.canonical)?;
        if let Ok(nodes) = self.nodes.try_lock() {
            for node in &*nodes {
                visit.call(&node.node)?;
                visit.call(&node.callbacks)?;
            }
        }
        Ok(())
    }

    pub(super) fn __clear__(&self, py: Python<'_>) {
        let removed = self
            .nodes
            .lock()
            .ok()
            .map(|mut nodes| std::mem::take(&mut *nodes));
        self.canonical.bind(py).clear();
        drop(removed);
    }
}

pub(in crate::independent) fn install_node_finalizers(
    py: Python<'_>,
    session: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let state = Py::new(
        py,
        NativeNodeFinalizers {
            nodes: Mutex::new(Vec::new()),
            canonical: PyDict::new(py).unbind(),
        },
    )?;
    session.setattr("_setupstate", &state)?;
    session.setattr("_vfy_native_setupstate", state)
}
