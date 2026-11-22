//! Shared exception matching over real `CPython` types, regexes, notes, and checks.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::{PyTraverseError, PyVisit};
use std::sync::Mutex;

mod format;
mod group;
mod options;

struct Options {
    expected: Py<PyTuple>,
    pattern: Option<Py<PyAny>>,
    raw_pattern: Option<String>,
    check: Option<Py<PyAny>>,
    is_baseexception: bool,
}

impl Options {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            expected: self.expected.clone_ref(py),
            pattern: self.pattern.as_ref().map(|value| value.clone_ref(py)),
            raw_pattern: self.raw_pattern.clone(),
            check: self.check.as_ref().map(|value| value.clone_ref(py)),
            is_baseexception: self.is_baseexception,
        }
    }
}

#[derive(Default)]
struct Progress {
    fail_reason: Option<String>,
    just_propagate: bool,
}

/// One matching implementation shared by raises contexts and xfail matchers.
#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeRaisesMatcher {
    options: Mutex<Option<Options>>,
    progress: Mutex<Progress>,
}

impl NativeRaisesMatcher {
    fn options(&self, py: Python<'_>) -> PyResult<Options> {
        self.options
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            .map(|options| options.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("exception matcher has been cleared"))
    }

    fn failure(&self, reason: Option<String>) {
        self.progress
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .fail_reason = reason;
    }
}

#[pymethods]
impl NativeRaisesMatcher {
    #[new]
    #[pyo3(signature = (expected=None, *, pattern=None, check=None))]
    fn new(
        py: Python<'_>,
        expected: Option<&Bound<'_, PyAny>>,
        pattern: Option<&Bound<'_, PyAny>>,
        check: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Self> {
        Ok(Self {
            options: Mutex::new(Some(options::build(py, expected, pattern, check)?)),
            progress: Mutex::default(),
        })
    }

    fn matches(&self, py: Python<'_>, exception: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let options = self.options(py)?;
        self.progress
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .just_propagate = false;
        let Some(exception) = exception else {
            self.failure(Some("exception is None".to_owned()));
            return Ok(false);
        };
        let mismatch = format::type_mismatch(py, options.expected.bind(py), exception)?;
        let matched = mismatch.is_none();
        self.failure(mismatch);
        if !matched {
            self.progress
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .just_propagate = true;
            return Ok(false);
        }
        if let Some(pattern) = &options.pattern
            && let Some(reason) = format::regex_failure(
                py,
                exception,
                pattern.bind(py),
                format::Diagnostic::Matcher(options.raw_pattern.as_deref()),
            )?
        {
            self.failure(Some(reason));
            return Ok(false);
        }
        if let Some(check) = options.check
            && !check.bind(py).call1((exception,))?.is_truthy()?
        {
            let representation = check.bind(py).repr()?;
            self.failure(Some(format!(
                "check {} did not return True",
                representation.to_str()?
            )));
            return Ok(false);
        }
        Ok(true)
    }

    #[getter]
    fn fail_reason(&self) -> Option<String> {
        self.progress
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .fail_reason
            .clone()
    }

    #[getter]
    fn just_propagate(&self) -> bool {
        self.progress
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .just_propagate
    }

    #[getter]
    fn expected_exceptions(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        Ok(self.options(py)?.expected)
    }

    #[getter(pattern)]
    fn pattern_value(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        Ok(self.options(py)?.pattern)
    }

    #[getter(check)]
    fn check_value(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        Ok(self.options(py)?.check)
    }

    #[getter]
    fn is_baseexception(&self, py: Python<'_>) -> PyResult<bool> {
        Ok(self.options(py)?.is_baseexception)
    }

    fn missing_exception_message(&self, py: Python<'_>) -> PyResult<String> {
        let options = self.options(py)?;
        let types = options.expected.bind(py);
        let names = types
            .iter()
            .map(|category| category.getattr("__name__")?.extract::<String>())
            .collect::<PyResult<Vec<_>>>()?;
        Ok(match names.as_slice() {
            [] => "DID NOT RAISE any exception".to_owned(),
            [name] => format!("DID NOT RAISE {name}"),
            _ => format!("DID NOT RAISE any of ({})", names.join(", ")),
        })
    }

    fn description(&self, py: Python<'_>) -> PyResult<String> {
        let options = self.options(py)?;
        let mut fields = Vec::new();
        if !options.expected.bind(py).is_empty() {
            fields.push(format::type_names(options.expected.bind(py))?);
        }
        if let Some(pattern) = options.pattern {
            fields.push(format!(
                "match={}",
                format::display_pattern(py, pattern.bind(py))?
                    .repr()?
                    .to_str()?
            ));
        }
        if let Some(check) = options.check {
            fields.push(format!("check={}", check.bind(py).repr()?.to_str()?));
        }
        Ok(format!("RaisesExc({})", fields.join(", ")))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        let options = self
            .options
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(options) = options.as_ref() {
            visit.call(&options.expected)?;
            visit.call(&options.pattern)?;
            visit.call(&options.check)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .options
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        drop(removed);
    }
}

#[pyfunction]
fn match_exception_message(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
    pattern: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if let Some(reason) =
        format::regex_failure(py, exception, pattern, format::Diagnostic::ExceptionInfo)?
    {
        return Err(pyo3::exceptions::PyAssertionError::new_err(reason));
    }
    Ok(true)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeRaisesMatcher>()?;
    module.add_function(wrap_pyfunction!(match_exception_message, module)?)?;
    module.add_function(wrap_pyfunction!(group::exception_group_contains, module)?)
}
