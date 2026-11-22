//! Shared exception text and failure diagnostics.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

#[derive(Clone, Copy)]
pub(super) enum Diagnostic<'a> {
    ExceptionInfo,
    Matcher(Option<&'a str>),
}

pub(super) fn type_names(categories: &Bound<'_, PyTuple>) -> PyResult<String> {
    let names = categories
        .iter()
        .map(|category| category.getattr("__name__")?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?;
    Ok(match names.as_slice() {
        [name] => name.clone(),
        _ => format!("({})", names.join(", ")),
    })
}

pub(super) fn type_mismatch(
    _py: Python<'_>,
    expected: &Bound<'_, PyTuple>,
    exception: &Bound<'_, PyAny>,
) -> PyResult<Option<String>> {
    if expected.is_empty() || exception.is_instance(expected)? {
        return Ok(None);
    }
    let actual = exception
        .get_type()
        .getattr("__name__")?
        .extract::<String>()?;
    Ok(Some(format!(
        "`{actual}()` is not an instance of `{}`",
        type_names(expected)?
    )))
}

pub(super) fn display_pattern<'py>(
    py: Python<'py>,
    pattern: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let re = py.import("re")?;
    if pattern.is_instance(&re.getattr("Pattern")?)?
        && pattern.getattr("flags")?.extract::<u32>()? == re.getattr("UNICODE")?.extract::<u32>()?
    {
        return pattern.getattr("pattern");
    }
    Ok(pattern.clone())
}

pub(super) fn raw_pattern(py: Python<'_>, pattern: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    let display = display_pattern(py, pattern)?;
    let Ok(pattern) = display.cast::<PyString>() else {
        return Ok(None);
    };
    let pattern = pattern.to_str()?;
    let Some(body) = pattern
        .strip_prefix('^')
        .and_then(|body| body.strip_suffix('$'))
    else {
        return Ok(None);
    };
    let mut characters = body.chars();
    while let Some(character) = characters.next() {
        if character == '\\' && characters.clone().next().is_some_and(|next| next != '\n') {
            characters.next();
        } else if "{}()+.*?^$[]|".contains(character) {
            return Ok(None);
        }
    }
    let mut literal = String::new();
    let mut characters = body.chars().peekable();
    while let Some(character) = characters.next() {
        if character == '\\'
            && let Some(next) = characters.peek()
            && ("{}()+-.*?^$[]\\|".contains(*next)
                || next.is_whitespace()
                || ('\x1c'..='\x1f').contains(next))
        {
            literal.push(*next);
            characters.next();
            continue;
        }
        literal.push(character);
    }
    Ok(Some(literal))
}

pub(super) fn exception_text(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
    diagnostic: &Diagnostic<'_>,
) -> PyResult<String> {
    let notes = match exception.getattr("__notes__") {
        Ok(notes) => notes,
        Err(error) if error.is_instance_of::<PyAttributeError>(py) => PyList::empty(py).into_any(),
        Err(error) => return Err(error),
    };
    let group = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    let message =
        if matches!(diagnostic, Diagnostic::Matcher(_)) && exception.is_instance(&group)? {
            exception.getattr("message")?
        } else {
            exception.str()?.into_any()
        };
    let lines = PyList::new(py, [message])?;
    for note in notes.try_iter()? {
        lines.append(note?)?;
    }
    PyString::new(py, "\n")
        .call_method1("join", (lines,))?
        .extract()
}

pub(super) fn regex_failure(
    py: Python<'_>,
    exception: &Bound<'_, PyAny>,
    pattern: &Bound<'_, PyAny>,
    diagnostic: Diagnostic<'_>,
) -> PyResult<Option<String>> {
    let text = exception_text(py, exception, &diagnostic)?;
    let search = || -> PyResult<bool> {
        Ok(!py
            .import("re")?
            .call_method1("search", (pattern, &text))?
            .is_none())
    };
    if matches!(diagnostic, Diagnostic::Matcher(_)) && search()? {
        return Ok(None);
    }
    let display = match diagnostic {
        Diagnostic::ExceptionInfo => pattern.clone(),
        Diagnostic::Matcher(_) => display_pattern(py, pattern)?,
    };
    let shown_pattern = display.repr()?.to_str()?.to_owned();
    let shown_text = PyString::new(py, &text).repr()?.to_str()?.to_owned();
    let group = py.import("builtins")?.getattr("BaseExceptionGroup")?;
    let suffix = if matches!(diagnostic, Diagnostic::Matcher(_)) && exception.is_instance(&group)? {
        format!(
            " the `{}()`",
            exception
                .get_type()
                .getattr("__name__")?
                .extract::<String>()?
        )
    } else {
        String::new()
    };
    let mut reason = format!(
        "Regex pattern did not match{suffix}.\n  Expected regex: {shown_pattern}\n  Actual message: {shown_text}"
    );
    if display.eq(PyString::new(py, &text))? {
        reason.push_str("\n Did you mean to `re.escape()` the regex?");
    }
    if matches!(diagnostic, Diagnostic::ExceptionInfo) && search()? {
        return Ok(None);
    }
    if let Diagnostic::Matcher(Some(raw)) = diagnostic {
        let lines = literal_difference(py, raw, &text)?;
        let prefix = if lines.first().is_some_and(|line| line.starts_with('-')) {
            "\n"
        } else {
            ""
        };
        reason = format!("{prefix}{}", lines.join("\n"));
    }
    Ok(Some(reason))
}

// The maintained text policy is shared with the assertion renderer. CPython's
// stdlib supplies the exact ndiff alignment primitive, without importing pytest.
fn literal_difference(py: Python<'_>, left: &str, right: &str) -> PyResult<Vec<String>> {
    let prepared = vfy_core::assertion_diagnostics::prepare_text_difference(left, right, 0);
    let mut lines = prepared.notices;
    let left = PyString::new(py, &prepared.left);
    let right = PyString::new(py, &prepared.right);
    let (left, right) = if prepared.escape_whitespace {
        lines.push("Strings contain only whitespace, escaping them using repr()".to_owned());
        (left.repr()?, right.repr()?)
    } else {
        (left, right)
    };
    let differences = py.import("difflib")?.call_method1(
        "ndiff",
        (
            right.call_method1("splitlines", (true,))?,
            left.call_method1("splitlines", (true,))?,
        ),
    )?;
    let joined = differences
        .try_iter()?
        .map(|line| line?.call_method1("strip", ("\n",))?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?
        .join("\n");
    lines.extend(
        PyString::new(py, &joined)
            .call_method0("splitlines")?
            .extract::<Vec<String>>()?,
    );
    Ok(lines)
}
