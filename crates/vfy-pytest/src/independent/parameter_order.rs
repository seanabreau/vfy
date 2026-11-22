//! Group live high-scope parameter resources without moving unrelated prefixes.

use super::lifecycle::Context;
use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use rustc_hash::FxHashSet;
use std::cell::RefCell;
use std::collections::VecDeque;

mod members;

use members::{MemberId, Members};

#[derive(Clone, Copy)]
struct Membership {
    key: usize,
    node: MemberId,
}

struct Resources {
    item_keys: Vec<Vec<Vec<Membership>>>,
    members: Vec<Vec<Members>>,
}

impl Resources {
    fn project(py: Python<'_>, items: &Bound<'_, PyList>) -> PyResult<Self> {
        let scope_type = py.import("vfy.independent.parameters")?.getattr("Scope")?;
        let mut result = Self {
            item_keys: std::iter::once(Vec::new())
                .chain((1..5).map(|_| vec![Vec::new(); items.len()]))
                .collect(),
            members: (0..5).map(|_| Vec::new()).collect(),
        };
        for (rank, scope_name) in ["class", "module", "package", "session"]
            .into_iter()
            .enumerate()
        {
            let rank = rank + 1;
            let scope = scope_type.call1((scope_name,))?;
            let keys = PyDict::new(py);
            for (item_index, item) in items.iter().enumerate() {
                let Some(callspec) = item.getattr_opt("callspec")? else {
                    continue;
                };
                let (path, class) = match scope_name {
                    "class" => (
                        item.getattr("path")?.unbind(),
                        item.getattr("cls")?.unbind(),
                    ),
                    "module" => (item.getattr("path")?.unbind(), py.None()),
                    "package" => (item.getattr("path")?.getattr("parent")?.unbind(), py.None()),
                    _ => (py.None(), py.None()),
                };
                for name in callspec.getattr("indices")?.try_iter()? {
                    let name = name?;
                    if !callspec
                        .getattr("_arg2scope")?
                        .get_item(&name)?
                        .eq(&scope)?
                    {
                        continue;
                    }
                    let index = callspec.getattr("indices")?.get_item(&name)?;
                    let key = PyTuple::new(py, [&name, &index, path.bind(py), class.bind(py)])?;
                    let key_id = if let Some(existing) = keys.get_item(&key)? {
                        existing.extract()?
                    } else {
                        let key_id = result.members[rank].len();
                        keys.set_item(&key, key_id)?;
                        result.members[rank].push(Members::default());
                        key_id
                    };
                    let node = result.members[rank][key_id].push(item_index);
                    result.item_keys[rank][item_index].push(Membership { key: key_id, node });
                }
            }
        }
        Ok(result)
    }

    fn prioritize(&mut self, item: usize) {
        for rank in 1..5 {
            for membership in &self.item_keys[rank][item] {
                self.members[rank][membership.key].prioritize(membership.node);
            }
        }
    }

    fn arrange(&mut self, input: Vec<usize>, rank: usize) -> Vec<usize> {
        if rank == 0 || input.len() < 3 {
            return input;
        }
        if self.members[rank].is_empty() {
            return self.arrange(input, rank - 1);
        }
        let allowed = input.iter().copied().collect::<FxHashSet<_>>();
        let mut pending = VecDeque::from(input);
        let mut ignored = FxHashSet::default();
        let mut emitted = FxHashSet::default();
        let mut output = Vec::new();
        while !pending.is_empty() {
            let mut prefix = Vec::new();
            let mut prefix_set = FxHashSet::default();
            let mut selected = None;
            while let Some(item) = pending.pop_front() {
                if emitted.contains(&item) || !prefix_set.insert(item) {
                    continue;
                }
                let key = self.item_keys[rank][item]
                    .iter()
                    .rev()
                    .find(|membership| !ignored.contains(&membership.key))
                    .map(|membership| membership.key);
                if let Some(key) = key {
                    let matching = self.members[rank][key]
                        .iter()
                        .filter(|item| allowed.contains(item))
                        .collect::<Vec<_>>();
                    for member in matching.into_iter().rev() {
                        pending.push_front(member);
                        self.prioritize(member);
                    }
                    selected = Some(key);
                    break;
                }
                prefix.push(item);
            }
            for item in self.arrange(prefix, rank - 1) {
                if emitted.insert(item) {
                    output.push(item);
                }
            }
            if let Some(key) = selected {
                ignored.insert(key);
            }
        }
        output
    }
}

#[pyclass(module = "vfy._native", frozen, unsendable)]
struct NativeResourceGrouping;

#[pymethods]
impl NativeResourceGrouping {
    #[expect(
        clippy::unused_self,
        reason = "the registry calls this bound plugin hook"
    )]
    fn pytest_collection_modifyitems(
        &self,
        py: Python<'_>,
        items: &Bound<'_, PyList>,
    ) -> PyResult<()> {
        let unique = PyList::empty(py);
        let mut seen = FxHashSet::default();
        for item in items.iter() {
            if seen.insert(item.as_ptr() as usize) {
                unique.append(item)?;
            }
        }
        let mut resources = Resources::project(py, &unique)?;
        let order = resources.arrange((0..unique.len()).collect(), 4);
        let grouped = PyList::empty(py);
        for index in order {
            grouped.append(unique.get_item(index)?)?;
        }
        items.set_slice(0, items.len(), &grouped)
    }
}

#[pyclass(frozen, unsendable)]
struct InstallResourceGrouping {
    config: RefCell<Option<Py<PyAny>>>,
}

#[pymethods]
impl InstallResourceGrouping {
    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "the callback exposes its bound signature to hook dispatch"
    )]
    fn __signature__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        crate::independent::callable_signature(py)
    }

    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let config = self
            .config
            .borrow()
            .as_ref()
            .map(|config| config.clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyAssertionError::new_err(
                    "fixture manager callback has been cleared",
                )
            })?;
        config.bind(py).getattr("pluginmanager")?.call_method1(
            "register",
            (Py::new(py, NativeResourceGrouping)?, "funcmanage"),
        )?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(config) = self.config.try_borrow() {
            visit.call(&*config)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let config = self.config.borrow_mut().take();
        drop(config);
    }
}

pub(super) fn session_start(py: Python<'_>, context: &Context) -> PyResult<()> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("session", &context.session)?;
    NativeHookRegistry::call_with_default(
        context.registry.bind(py),
        py,
        "pytest_sessionstart",
        &kwargs,
        Py::new(
            py,
            InstallResourceGrouping {
                config: RefCell::new(Some(context.config.clone_ref(py))),
            },
        )?
        .into_any(),
        false,
        "fixtures",
    )?;
    Ok(())
}

#[cfg(test)]
mod tests;
