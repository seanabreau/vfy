//! Batched assertion explanation rendering after `CPython` object protocols finish.

use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyInt, PyString};
use std::sync::{Mutex, MutexGuard, PoisonError};
use vfy_core::assertion_diagnostics::{AssertionTruncation, AssertionVerbosity};

type AssertionScope = (Py<PyAny>, Py<PyAny>);
static ACTIVE_ITEM: Mutex<Option<AssertionScope>> = Mutex::new(None);

fn active_item() -> MutexGuard<'static, Option<AssertionScope>> {
    // The lock protects only infallible reference swaps and clones, so even a
    // poisoned lock retains a complete, valid item binding.
    ACTIVE_ITEM.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Restore the previous assertion-hook scope when the item protocol finishes.
pub(crate) struct AssertionItemGuard(Option<AssertionScope>);

impl AssertionItemGuard {
    pub(crate) fn bind(item: &Bound<'_, PyAny>, exclusions: &Bound<'_, PyAny>) -> Self {
        let item = item.clone().unbind();
        let exclusions = exclusions.clone().unbind();
        Self(active_item().replace((item, exclusions)))
    }
}

impl Drop for AssertionItemGuard {
    fn drop(&mut self) {
        let previous = std::mem::replace(&mut *active_item(), self.0.take());
        drop(previous);
    }
}

#[pyfunction]
pub(crate) fn current_assertion_item(py: Python<'_>) -> Option<AssertionScope> {
    active_item()
        .as_ref()
        .map(|(item, exclusions)| (item.clone_ref(py), exclusions.clone_ref(py)))
}

#[path = "assertions/approximation.rs"]
mod approximation;
#[path = "assertions/containers.rs"]
mod containers;
#[path = "assertions/iterable_diff.rs"]
mod iterable_diff;

/// Map the live assertion verbosity level to its finite rendering policy.
fn verbosity_level(level: Option<&Bound<'_, PyAny>>) -> PyResult<AssertionVerbosity> {
    Ok(match level {
        Some(level) if level.cast::<PyInt>()?.ge(2)? => AssertionVerbosity::Full,
        Some(level) if level.ge(1)? => AssertionVerbosity::Verbose,
        _ => AssertionVerbosity::Normal,
    })
}

#[pyfunction]
#[pyo3(signature = (message, expression_json, verbosity=None, max_lines=8, max_chars=640, on_ci=false))]
pub(crate) fn render_independent_assertion(
    message: Option<&str>,
    expression_json: &str,
    verbosity: Option<&Bound<'_, PyAny>>,
    max_lines: i64,
    max_chars: i64,
    on_ci: bool,
) -> PyResult<String> {
    let verbosity = verbosity_level(verbosity)?;
    vfy_core::assertion_diagnostics::render_assertion_explanation_with_options(
        message,
        expression_json,
        verbosity,
        AssertionTruncation {
            max_lines,
            max_chars,
            on_ci,
        },
    )
    .map_err(PyValueError::new_err)
}

/// Render the lines a plug-in's `pytest_assertrepr_compare` returned.
#[pyfunction]
#[pyo3(signature = (lines, verbosity=None, max_lines=8, max_chars=640, on_ci=false))]
pub(crate) fn render_independent_hook_explanation(
    lines: Vec<String>,
    verbosity: Option<&Bound<'_, PyAny>>,
    max_lines: i64,
    max_chars: i64,
    on_ci: bool,
) -> PyResult<String> {
    Ok(vfy_core::assertion_diagnostics::render_hook_explanation(
        lines,
        verbosity_level(verbosity)?,
        AssertionTruncation {
            max_lines,
            max_chars,
            on_ci,
        },
    ))
}

#[pyfunction]
#[pyo3(signature = (operator, left, right, verbosity=None, max_lines=8, max_chars=640, on_ci=false))]
pub(crate) fn describe_independent_comparison(
    operator: &str,
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    verbosity: Option<&Bound<'_, PyAny>>,
    max_lines: i64,
    max_chars: i64,
    on_ci: bool,
) -> PyResult<Option<String>> {
    if operator != "==" {
        return Ok(None);
    }
    let verbosity = verbosity_level(verbosity)?;
    let truncation = AssertionTruncation {
        max_lines,
        max_chars,
        on_ci,
    };
    if let Some(explanation) = approximation::describe(left, right, verbosity, truncation)? {
        return Ok(Some(explanation));
    }
    let Some(comparison) = containers::describe(left, right)? else {
        return Ok(None);
    };
    let expand = on_ci || !matches!(verbosity, AssertionVerbosity::Normal);
    let full_diff = if expand && containers::builtin(left) && containers::builtin(right) {
        Some(iterable_diff::lines(left, right)?)
    } else {
        None
    };
    Ok(Some(
        comparison.render_with_options(verbosity, truncation, full_diff),
    ))
}

/// Resolve a public verbosity query from the live configuration and declarations.
#[pyfunction]
#[pyo3(signature = (config, verbosity_type=None))]
pub(crate) fn get_independent_verbosity<'py>(
    config: &Bound<'py, PyAny>,
    verbosity_type: Option<&str>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = config.py();
    let global = config.call_method1(
        pyo3::intern!(py, "getoption"),
        (pyo3::intern!(py, "verbose"), 0),
    )?;
    if !global.is_instance_of::<PyInt>() {
        return Err(PyAssertionError::new_err(""));
    }
    let Some(kind) = verbosity_type else {
        return Ok(global);
    };
    let name = PyString::new(py, &format!("verbosity_{kind}"));
    if !config.getattr(pyo3::intern!(py, "_ini"))?.contains(&name)? {
        return Ok(global);
    }
    let configured = config.call_method1(pyo3::intern!(py, "getini"), (&name,))?;
    if configured.eq(pyo3::intern!(py, "auto"))? {
        return Ok(global);
    }
    config
        .py()
        .import("builtins")?
        .getattr("int")?
        .call1((configured,))
}
