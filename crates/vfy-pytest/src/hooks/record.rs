//! Validated hook implementation records exposed to Python.

use super::HookCallError;
use super::{invocation, observations};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Style {
    Plain,
    Wrapper,
    Legacy,
}
#[derive(Clone, Copy)]
pub(super) enum WrapperFlags {
    Plain,
    New,
    Legacy,
    Both,
}
impl WrapperFlags {
    fn new_style(self) -> bool {
        matches!(self, Self::New | Self::Both)
    }
    fn old_style(self) -> bool {
        matches!(self, Self::Legacy | Self::Both)
    }
}

pub(super) enum HookBody {
    Dynamic,
    SkippingCall,
    SkippingReport,
    SkippingStatus,
    SubtestsStatus,
    HypothesisReport(Py<crate::independent::hypothesis_report::NativeHypothesisReport>),
    RunnerMakeReport,
    RunnerStatus,
    TerminalStatus,
    SessionLogReport(Py<crate::independent::reporting::NativeSessionLogReport>),
    TerminalLogReport(Py<crate::independent::reporting::terminal::NativeTerminalLogReport>),
    TerminalLogStart(Py<crate::independent::reporting::terminal::NativeTerminalLogReport>),
}

impl HookBody {
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Dynamic => Self::Dynamic,
            Self::SkippingCall => Self::SkippingCall,
            Self::SkippingReport => Self::SkippingReport,
            Self::SkippingStatus => Self::SkippingStatus,
            Self::SubtestsStatus => Self::SubtestsStatus,
            Self::HypothesisReport(owner) => Self::HypothesisReport(owner.clone_ref(py)),
            Self::RunnerMakeReport => Self::RunnerMakeReport,
            Self::RunnerStatus => Self::RunnerStatus,
            Self::TerminalStatus => Self::TerminalStatus,
            Self::SessionLogReport(owner) => Self::SessionLogReport(owner.clone_ref(py)),
            Self::TerminalLogReport(owner) => Self::TerminalLogReport(owner.clone_ref(py)),
            Self::TerminalLogStart(owner) => Self::TerminalLogStart(owner.clone_ref(py)),
        }
    }
}

#[pyclass(name = "NativeHookImpl", module = "vfy._native._pytest")]
pub(crate) struct Implementation {
    pub(super) name: String,
    pub(super) plugin_name: String,
    pub(super) invocation: Option<std::sync::Arc<observations::InvocationCount>>,
    pub(super) plugin: Py<PyAny>,
    pub(super) function: Py<PyAny>,
    pub(super) body: HookBody,
    pub(super) arguments: Py<PyTuple>,
    pub(super) keyword_arguments: Py<PyTuple>,
    pub(super) style: Style,
    pub(super) wrapper_flags: WrapperFlags,
    pub(super) priority: i8,
    pub(super) tryfirst: bool,
    pub(super) trylast: bool,
    pub(super) optionalhook: bool,
    /// Ephemeral callbacks materialize options only when an observer receives them.
    pub(super) options: Option<Py<PyDict>>,
}

// Retain the selected arguments until dispatch has processed the result. A hook
// can remove them from kwargs while it runs, so their lifetime is observable.
pub(super) enum HookArguments<'py> {
    Empty,
    One(Bound<'py, PyAny>),
    Two {
        // Match CPython tuple destruction, which releases elements in reverse.
        second: Bound<'py, PyAny>,
        first: Bound<'py, PyAny>,
    },
    Many(Bound<'py, PyTuple>),
}

impl<'py> HookArguments<'py> {
    pub(super) fn call(&self, function: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Empty => function.call0(),
            Self::One(first) => function.call1((first,)),
            Self::Two { first, second } => function.call1((first, second)),
            Self::Many(arguments) => function.call1(arguments),
        }
    }
}

impl Implementation {
    pub(super) fn arguments<'py>(
        py: Python<'py>,
        names: &Bound<'py, PyTuple>,
        arguments: &mut invocation::CallArguments<'_, 'py>,
    ) -> PyResult<HookArguments<'py>> {
        let mut lookup = |argument: Bound<'py, PyAny>| {
            arguments.get_argument(py, &argument)?.ok_or_else(|| {
                HookCallError::new_err(format!("hook call must provide argument '{argument}'"))
            })
        };
        let arguments = match names.len() {
            0 => HookArguments::Empty,
            1 => HookArguments::One(lookup(names.get_item(0)?)?),
            2 => HookArguments::Two {
                first: lookup(names.get_item(0)?)?,
                second: lookup(names.get_item(1)?)?,
            },
            _ => {
                let mut arguments = Vec::new();
                for argument in names.iter() {
                    arguments.push(lookup(argument)?);
                }
                HookArguments::Many(PyTuple::new(py, arguments)?)
            }
        };
        Ok(arguments)
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            name: self.name.clone(),
            plugin_name: self.plugin_name.clone(),
            invocation: self.invocation.clone(),
            plugin: self.plugin.clone_ref(py),
            function: self.function.clone_ref(py),
            body: self.body.clone_ref(py),
            arguments: self.arguments.clone_ref(py),
            keyword_arguments: self.keyword_arguments.clone_ref(py),
            style: self.style,
            wrapper_flags: self.wrapper_flags,
            priority: self.priority,
            tryfirst: self.tryfirst,
            trylast: self.trylast,
            optionalhook: self.optionalhook,
            options: self.options.as_ref().map(|options| options.clone_ref(py)),
        }
    }

    pub(super) fn options(slf: &Bound<'_, Self>) -> PyResult<Py<PyDict>> {
        let py = slf.py();
        if let Some(options) = &slf.borrow().options {
            return Ok(options.clone_ref(py));
        }
        let options = PyDict::new(py);
        for name in [
            "wrapper",
            "hookwrapper",
            "tryfirst",
            "trylast",
            "optionalhook",
        ] {
            options.set_item(name, false)?;
        }
        options.set_item("specname", py.None())?;
        let selected = slf
            .borrow_mut()
            .options
            .get_or_insert_with(|| options.clone().unbind())
            .clone_ref(py);
        Ok(selected)
    }
}

#[pymethods]
impl Implementation {
    #[getter]
    fn function(&self, py: Python<'_>) -> Py<PyAny> {
        self.function.clone_ref(py)
    }
    #[setter]
    fn set_function(slf: &Bound<'_, Self>, value: Py<PyAny>) -> PyResult<()> {
        let old = {
            let mut implementation = slf.try_borrow_mut()?;
            let body = std::mem::replace(&mut implementation.body, HookBody::Dynamic);
            (std::mem::replace(&mut implementation.function, value), body)
        };
        drop(old);
        Ok(())
    }
    #[getter]
    fn plugin(&self, py: Python<'_>) -> Py<PyAny> {
        self.plugin.clone_ref(py)
    }
    #[getter]
    fn plugin_name(&self) -> &str {
        &self.plugin_name
    }
    #[getter]
    fn argnames(&self, py: Python<'_>) -> Py<PyTuple> {
        self.arguments.clone_ref(py)
    }
    #[getter]
    fn kwargnames(&self, py: Python<'_>) -> Py<PyTuple> {
        self.keyword_arguments.clone_ref(py)
    }
    #[getter]
    pub(super) fn wrapper(&self) -> bool {
        self.wrapper_flags.new_style()
    }
    #[getter]
    pub(super) fn hookwrapper(&self) -> bool {
        self.wrapper_flags.old_style()
    }
    #[getter]
    fn tryfirst(&self) -> bool {
        self.tryfirst
    }
    #[getter]
    fn trylast(&self) -> bool {
        self.trylast
    }
    #[getter]
    fn optionalhook(&self) -> bool {
        self.optionalhook
    }
    #[getter]
    fn opts(slf: &Bound<'_, Self>) -> PyResult<Py<PyDict>> {
        Self::options(slf)
    }
    fn __repr__(&self, py: Python<'_>) -> PyResult<String> {
        Ok(format!(
            "<HookImpl plugin_name='{}', plugin={}>",
            self.plugin_name,
            self.plugin.bind(py).repr()?
        ))
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.plugin)?;
        visit.call(&self.function)?;
        match &self.body {
            HookBody::TerminalLogReport(owner) | HookBody::TerminalLogStart(owner) => {
                visit.call(owner)?;
            }
            HookBody::SessionLogReport(owner) => visit.call(owner)?,
            HookBody::HypothesisReport(owner) => visit.call(owner)?,
            _ => {}
        }
        visit.call(&self.arguments)?;
        visit.call(&self.keyword_arguments)?;
        visit.call(&self.options)
    }
}
