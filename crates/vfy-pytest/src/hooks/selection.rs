//! Immutable implementation rows and per-call selections.

use super::record::{Implementation, Style};
use super::registry::NativeHookRegistry;
use super::{dispatch, invocation};
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(super) struct RegisteredImplementation {
    pub(super) sequence: u64,
    pub(super) implementation: Py<Implementation>,
}

impl RegisteredImplementation {
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            sequence: self.sequence,
            implementation: self.implementation.clone_ref(py),
        }
    }
}

/// A normal call retains one GC-visible owner; exceptional protocol paths may
/// materialize individual handles for mutable monitoring/subset operations.
pub(super) enum HookSelection {
    Empty,
    Rows {
        rows: std::sync::Arc<[RegisteredImplementation]>,
        registry: Py<NativeHookRegistry>,
    },
    Owned(Vec<Py<Implementation>>),
}

pub(super) struct NativeDispatch<'a> {
    pub(super) position: usize,
    pub(super) callback: &'a dyn Fn() -> PyResult<Py<PyAny>>,
}

impl HookSelection {
    pub(super) fn len(&self, _py: Python<'_>) -> usize {
        match self {
            Self::Empty => 0,
            Self::Rows { rows, .. } => rows.len(),
            Self::Owned(entries) => entries.len(),
        }
    }
    pub(super) fn default_position(&self, py: Python<'_>) -> usize {
        let is_last = |implementation: &Py<Implementation>| {
            let implementation = implementation.borrow(py);
            implementation.style == Style::Plain && implementation.priority == -1
        };
        match self {
            Self::Empty => 0,
            Self::Rows { rows, .. } => rows
                .iter()
                .position(|entry| is_last(&entry.implementation))
                .unwrap_or(rows.len()),
            Self::Owned(entries) => entries.iter().position(is_last).unwrap_or(entries.len()),
        }
    }
    pub(super) fn run<'py>(
        &self,
        registry: &Bound<'py, NativeHookRegistry>,
        name: &str,
        kwargs: &Bound<'py, PyDict>,
        firstresult: bool,
        default: Option<NativeDispatch<'_>>,
    ) -> PyResult<Py<PyAny>> {
        let mut arguments = invocation::CallArguments::Borrowed(kwargs);
        self.run_arguments(registry, name, &mut arguments, firstresult, default)?
            .into_python(registry.py())
    }

    pub(super) fn run_arguments<'py>(
        &self,
        registry: &Bound<'py, NativeHookRegistry>,
        name: &str,
        arguments: &mut invocation::CallArguments<'_, 'py>,
        firstresult: bool,
        default: Option<NativeDispatch<'_>>,
    ) -> PyResult<invocation::HookReturn> {
        match (self, default) {
            (Self::Empty, None) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::Selected(&[]),
                arguments,
                firstresult,
            ),
            (
                Self::Empty,
                Some(NativeDispatch {
                    position,
                    callback: default,
                }),
            ) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::SelectedWithNative {
                    methods: &[],
                    position,
                    default,
                },
                arguments,
                firstresult,
            ),
            (Self::Rows { rows, .. }, None) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::Rows(rows),
                arguments,
                firstresult,
            ),
            (
                Self::Rows { rows, .. },
                Some(NativeDispatch {
                    position,
                    callback: default,
                }),
            ) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::RowsWithNative {
                    methods: rows,
                    position,
                    default,
                },
                arguments,
                firstresult,
            ),
            (Self::Owned(entries), None) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::Selected(entries),
                arguments,
                firstresult,
            ),
            (
                Self::Owned(entries),
                Some(NativeDispatch {
                    position,
                    callback: default,
                }),
            ) => dispatch::run_arguments(
                registry,
                name,
                dispatch::Methods::SelectedWithNative {
                    methods: entries,
                    position,
                    default,
                },
                arguments,
                firstresult,
            ),
        }
    }
    pub(super) fn release(self) {
        match self {
            Self::Owned(entries) => {
                for entry in entries {
                    drop(entry);
                }
            }
            Self::Rows { rows, registry } => {
                drop(rows);
                drop(registry);
            }
            Self::Empty => {}
        }
    }
    pub(super) fn into_owned(self, py: Python<'_>) -> Vec<Py<Implementation>> {
        match self {
            Self::Empty => Vec::new(),
            Self::Rows { rows, registry } => {
                let result = rows
                    .iter()
                    .map(|entry| entry.implementation.clone_ref(py))
                    .collect();
                drop(rows);
                drop(registry);
                result
            }
            Self::Owned(entries) => entries,
        }
    }
}

pub(super) fn registration_order(
    py: Python<'_>,
    left: &RegisteredImplementation,
    right: &RegisteredImplementation,
) -> std::cmp::Ordering {
    let left_implementation = left.implementation.borrow(py);
    let right_implementation = right.implementation.borrow(py);
    let left_wrap = left_implementation.style != Style::Plain;
    let right_wrap = right_implementation.style != Style::Plain;
    right_wrap
        .cmp(&left_wrap)
        .then_with(|| {
            right_implementation
                .priority
                .cmp(&left_implementation.priority)
        })
        .then_with(|| {
            if left_implementation.priority == -1 {
                left.sequence.cmp(&right.sequence)
            } else {
                right.sequence.cmp(&left.sequence)
            }
        })
}

/// Release detached rows with callbacks retained in registration order, after
/// ending every registry borrow. Active snapshots own their own Arc and pin.
pub(super) fn release_hook_rows(
    py: Python<'_>,
    owners: Vec<std::sync::Arc<[RegisteredImplementation]>>,
) {
    let mut entries = Vec::new();
    for rows in &owners {
        entries.extend(rows.iter().map(|entry| entry.clone_ref(py)));
    }
    drop(owners);
    entries.sort_by_key(|entry| entry.sequence);
    drop(entries);
}
