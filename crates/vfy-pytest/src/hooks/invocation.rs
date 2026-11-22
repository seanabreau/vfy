//! Hook arguments retain their native owner until a Python observer needs them.

use crate::independent::reporting::{
    NativeIndependentCallInfo, NativeSessionReports, ReportHandle,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

/// The borrowed item belongs to the suite for the entire synchronous multicall.
#[expect(
    clippy::large_enum_variant,
    reason = "Keeping the raw phase call inline avoids a heap allocation for every test phase"
)]
pub(crate) enum CallArguments<'a, 'py> {
    Borrowed(&'a Bound<'py, PyDict>),
    Item(&'a Bound<'py, PyAny>),
    ItemNext {
        item: &'a Bound<'py, PyAny>,
        nextitem: Option<&'a Bound<'py, PyAny>>,
    },
    Boundary {
        nodeid: &'a Bound<'py, PyAny>,
        location: &'a Bound<'py, PyAny>,
    },
    ProjectedItem(Bound<'py, PyDict>),
    ProjectedPhase {
        kwargs: Option<Bound<'py, PyDict>>,
        original: Py<NativeIndependentCallInfo>,
    },
    ProjectedLogReport(Option<Bound<'py, PyDict>>),
    RawLogReport(&'a ReportHandle),
    RawStatus {
        report: &'a ReportHandle,
        config: &'a Bound<'py, PyAny>,
    },
    Phase {
        item: &'a Bound<'py, PyAny>,
        call: Option<NativeIndependentCallInfo>,
        history: &'a Bound<'py, NativeSessionReports>,
    },
}

#[derive(Clone, Copy)]
pub(crate) enum OriginalPhaseCall<'a, 'py> {
    Native(&'a NativeIndependentCallInfo),
    Python(&'a Bound<'py, NativeIndependentCallInfo>),
}

impl<'a, 'py> CallArguments<'a, 'py> {
    pub(crate) fn phase(
        item: &'a Bound<'py, PyAny>,
        call: NativeIndependentCallInfo,
        history: &'a Bound<'py, NativeSessionReports>,
    ) -> Self {
        Self::Phase {
            item,
            call: Some(call),
            history,
        }
    }

    /// The original call survives a hook replacing the public kwargs entry.
    pub(crate) fn original_phase_call<'b>(
        &'b self,
        py: Python<'py>,
    ) -> Option<OriginalPhaseCall<'b, 'py>> {
        match self {
            Self::Phase {
                call: Some(call), ..
            } => Some(OriginalPhaseCall::Native(call)),
            Self::ProjectedPhase { original, .. } => {
                Some(OriginalPhaseCall::Python(original.bind(py)))
            }
            Self::Borrowed(_)
            | Self::Item(_)
            | Self::ItemNext { .. }
            | Self::Boundary { .. }
            | Self::ProjectedItem(_)
            | Self::ProjectedLogReport(_)
            | Self::RawLogReport(_)
            | Self::RawStatus { .. }
            | Self::Phase { call: None, .. } => None,
        }
    }

    /// Check the original call without projecting successful phases.
    pub(crate) fn original_has_exception(&self, py: Python<'py>) -> PyResult<bool> {
        match self.original_phase_call(py) {
            Some(OriginalPhaseCall::Native(call)) => Ok(call.has_exception()),
            Some(OriginalPhaseCall::Python(call)) => Ok(call.borrow().has_exception()),
            None => Err(PyRuntimeError::new_err(
                "original phase call is unavailable",
            )),
        }
    }

    /// Exception interaction needs the original public `CallInfo` even after kwargs release.
    pub(crate) fn original_call_for_interaction(
        &mut self,
        py: Python<'py>,
    ) -> PyResult<Py<NativeIndependentCallInfo>> {
        if let Self::Phase { call, .. } = self {
            let call = call
                .take()
                .ok_or_else(|| PyRuntimeError::new_err("original phase call is unavailable"))?;
            let original = Py::new(py, call)?;
            *self = Self::ProjectedPhase {
                kwargs: None,
                original,
            };
        }
        match self {
            Self::ProjectedPhase { original, .. } => Ok(original.clone_ref(py)),
            _ => Err(PyRuntimeError::new_err(
                "original phase call is unavailable",
            )),
        }
    }

    /// Hook signature names have already been normalized to exact Python strings.
    pub(super) fn contains(&self, argument: &Bound<'_, PyAny>) -> PyResult<bool> {
        match self {
            Self::Borrowed(kwargs) => kwargs.contains(argument),
            Self::ProjectedItem(kwargs) => kwargs.contains(argument),
            Self::Item(_) => Ok(argument.cast::<PyString>()?.to_str()? == "item"),
            Self::ItemNext { .. } => Ok(matches!(
                argument.cast::<PyString>()?.to_str()?,
                "item" | "nextitem"
            )),
            Self::Boundary { .. } => Ok(matches!(
                argument.cast::<PyString>()?.to_str()?,
                "nodeid" | "location"
            )),
            Self::ProjectedPhase { kwargs, .. } => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("phase hook arguments have been released"))?
                .contains(argument),
            Self::ProjectedLogReport(kwargs) => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("report hook arguments have been released"))?
                .contains(argument),
            Self::RawLogReport(_) => Ok(argument.cast::<PyString>()?.to_str()? == "report"),
            Self::RawStatus { .. } => Ok(matches!(
                argument.cast::<PyString>()?.to_str()?,
                "report" | "config"
            )),
            Self::Phase { .. } => {
                let name = argument.cast::<PyString>()?.to_str()?;
                Ok(matches!(name, "item" | "call"))
            }
        }
    }

    /// Read from the published dictionary when one exists; otherwise use its native owner.
    pub(super) fn get_argument(
        &mut self,
        py: Python<'py>,
        argument: &Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let name = argument.cast::<PyString>()?.to_str()?;
        if matches!(self, Self::Phase { .. }) && name == "call" {
            return self.python_kwargs(py)?.get_item(argument);
        }
        match self {
            Self::Borrowed(kwargs) => kwargs.get_item(argument),
            Self::ProjectedItem(kwargs) => kwargs.get_item(argument),
            Self::ProjectedPhase { kwargs, .. } => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("phase hook arguments have been released"))?
                .get_item(argument),
            Self::ProjectedLogReport(kwargs) => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("report hook arguments have been released"))?
                .get_item(argument),
            Self::Item(item) | Self::Phase { item, .. } => {
                Ok((name == "item").then(|| (*item).clone()))
            }
            Self::ItemNext { item, nextitem } => match name {
                "item" => Ok(Some((*item).clone())),
                "nextitem" => Ok(Some(
                    nextitem.map_or_else(|| py.None().into_bound(py), Clone::clone),
                )),
                _ => Ok(None),
            },
            Self::Boundary { nodeid, location } => match name {
                "nodeid" => Ok(Some((*nodeid).clone())),
                "location" => Ok(Some((*location).clone())),
                _ => Ok(None),
            },
            Self::RawLogReport(report) => {
                if name == "report" {
                    Ok(Some(report.to_python(py)?.into_bound(py).into_any()))
                } else {
                    Ok(None)
                }
            }
            Self::RawStatus { report, config } => match name {
                "report" => Ok(Some(report.to_python(py)?.into_bound(py).into_any())),
                "config" => Ok(Some((*config).clone())),
                _ => Ok(None),
            },
        }
    }

    /// Materialization moves the authoritative call into its only Python object.
    /// Once returned, this same dictionary remains authoritative for later hooks.
    pub(super) fn python_kwargs(&mut self, py: Python<'py>) -> PyResult<&Bound<'py, PyDict>> {
        if let Self::Item(item) = self {
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "item"), &**item)?;
            *self = Self::ProjectedItem(kwargs);
        }
        if let Self::ItemNext { item, nextitem } = self {
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "item"), &**item)?;
            kwargs.set_item(pyo3::intern!(py, "nextitem"), *nextitem)?;
            *self = Self::ProjectedItem(kwargs);
        }
        if let Self::Boundary { nodeid, location } = self {
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "nodeid"), &**nodeid)?;
            kwargs.set_item(pyo3::intern!(py, "location"), &**location)?;
            *self = Self::ProjectedItem(kwargs);
        }
        if let Self::Phase { item, call, .. } = self {
            let call = call.take().ok_or_else(|| {
                PyRuntimeError::new_err("phase hook arguments are already being projected")
            })?;
            let call = Py::new(py, call)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "item"), &**item)?;
            kwargs.set_item(pyo3::intern!(py, "call"), &call)?;
            *self = Self::ProjectedPhase {
                kwargs: Some(kwargs),
                original: call,
            };
        }
        if let Self::RawLogReport(report) = self {
            let report = report.to_python(py)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "report"), report)?;
            *self = Self::ProjectedLogReport(Some(kwargs));
        }
        if let Self::RawStatus { report, config } = self {
            let report = report.to_python(py)?;
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "report"), report)?;
            kwargs.set_item(pyo3::intern!(py, "config"), &**config)?;
            *self = Self::ProjectedLogReport(Some(kwargs));
        }
        match self {
            Self::Borrowed(kwargs) => Ok(kwargs),
            Self::ProjectedItem(kwargs) => Ok(kwargs),
            Self::ProjectedPhase { kwargs, .. } => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("phase hook arguments have been released")),
            Self::ProjectedLogReport(kwargs) => kwargs
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("report hook arguments have been released")),
            Self::Item(_)
            | Self::ItemNext { .. }
            | Self::Boundary { .. }
            | Self::Phase { .. }
            | Self::RawLogReport(_)
            | Self::RawStatus { .. } => Err(PyRuntimeError::new_err(
                "phase hook arguments are unavailable",
            )),
        }
    }

    /// Release the maker kwargs before publishing or attaching the report.
    pub(crate) fn release_phase_kwargs(&mut self) {
        let removed = match self {
            Self::ProjectedPhase { kwargs, .. } => kwargs.take(),
            _ => None,
        };
        drop(removed);
    }

    /// Release logreport kwargs at the end of the public hook call.
    pub(crate) fn release_log_kwargs(&mut self) {
        let removed = match self {
            Self::ProjectedLogReport(kwargs) => kwargs.take(),
            _ => None,
        };
        drop(removed);
    }

    /// Release original pending report metadata after the maker hook completes.
    pub(crate) fn finish_phase(&mut self, py: Python<'_>) {
        let pending = match self {
            Self::Phase {
                call: Some(call), ..
            } => call.take_pending_report(),
            Self::ProjectedPhase { original, .. } => original.borrow_mut(py).take_pending_report(),
            Self::Borrowed(_)
            | Self::Item(_)
            | Self::ItemNext { .. }
            | Self::Boundary { .. }
            | Self::ProjectedItem(_)
            | Self::ProjectedLogReport(_)
            | Self::RawLogReport(_)
            | Self::RawStatus { .. }
            | Self::Phase { call: None, .. } => None,
        };
        drop(pending);
    }
}

/// Hook results share one report owner until a public wrapper needs a `PyObject`.
pub(crate) enum HookReturn {
    EmptyList,
    Python(Py<PyAny>),
    NativeReport(ReportHandle),
    NativeStatus((&'static str, &'static str, &'static str)),
}

impl HookReturn {
    pub(crate) fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::EmptyList => Self::EmptyList,
            Self::Python(value) => Self::Python(value.clone_ref(py)),
            Self::NativeReport(report) => Self::NativeReport(report.clone_ref(py)),
            Self::NativeStatus(status) => Self::NativeStatus(*status),
        }
    }

    pub(crate) fn into_python(self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::EmptyList => Ok(PyList::empty(py).into_any().unbind()),
            Self::Python(value) => Ok(value),
            Self::NativeReport(report) => Ok(report.into_python(py)?.into_any()),
            Self::NativeStatus((category, letter, word)) => {
                Ok(PyTuple::new(py, [category, letter, word])?
                    .into_any()
                    .unbind())
            }
        }
    }

    pub(crate) fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match self {
            Self::EmptyList => Ok(PyList::empty(py).into_any().unbind()),
            Self::Python(value) => Ok(value.clone_ref(py)),
            Self::NativeReport(report) => Ok(report.to_python(py)?.into_any()),
            Self::NativeStatus((category, letter, word)) => {
                Ok(PyTuple::new(py, [*category, *letter, *word])?
                    .into_any()
                    .unbind())
            }
        }
    }
}
