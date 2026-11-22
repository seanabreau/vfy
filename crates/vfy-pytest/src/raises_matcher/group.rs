//! `ExceptionInfo` group searches include exception notes and preserve nesting depth.

use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::PyIterator;

use super::format::{Diagnostic, exception_text};

enum SearchStep<'py> {
    Children(Bound<'py, PyIterator>, usize),
    Match(Bound<'py, PyAny>, usize),
}

#[pyfunction]
#[pyo3(signature = (exception, expected, pattern=None, depth=None))]
pub(super) fn exception_group_contains(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
    expected: &Bound<'_, PyAny>,
    pattern: Option<&Bound<'_, PyAny>>,
    depth: Option<&Bound<'_, PyAny>>,
) -> PyResult<bool> {
    let group_type = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    if !exception.is_instance(&group_type)? {
        return Err(PyAssertionError::new_err(
            "Captured exception is not an instance of `BaseExceptionGroup`",
        ));
    }
    if let Some(depth) = depth
        && !depth.ge(1)?
    {
        return Err(PyAssertionError::new_err(
            "`depth` must be >= 1 if specified",
        ));
    }
    if let Some(depth) = depth
        && depth.lt(1)?
    {
        return Ok(false);
    }
    let mut steps = vec![SearchStep::Children(
        exception.getattr("exceptions")?.try_iter()?,
        1,
    )];
    while let Some(step) = steps.pop() {
        match step {
            SearchStep::Children(mut children, level) => {
                let Some(child) = children.next().transpose()? else {
                    continue;
                };
                steps.push(SearchStep::Children(children, level));
                steps.push(SearchStep::Match(child.clone(), level));
                // pytest searches a group's children before matching the group itself.
                if child.is_instance(&group_type)?
                    && !depth
                        .map(|depth| depth.lt(level + 1))
                        .transpose()?
                        .unwrap_or(false)
                {
                    steps.push(SearchStep::Children(
                        child.getattr("exceptions")?.try_iter()?,
                        level + 1,
                    ));
                }
            }
            SearchStep::Match(child, level) => {
                if depth
                    .map(|depth| depth.ne(level))
                    .transpose()?
                    .unwrap_or(false)
                    || !child.is_instance(expected)?
                {
                    continue;
                }
                if let Some(pattern) = pattern {
                    let text = exception_text(py, &child, &Diagnostic::ExceptionInfo)?;
                    if py
                        .import("re")?
                        .call_method1("search", (pattern, text))?
                        .is_none()
                    {
                        continue;
                    }
                }
                return Ok(true);
            }
        }
    }
    Ok(false)
}
