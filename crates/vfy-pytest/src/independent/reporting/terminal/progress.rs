//! Live compact progress reads the reporter's counters and writer resources.

use pyo3::basic::CompareOp;
use pyo3::exceptions::{PyAssertionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyString};
use std::path::PathBuf;

pub(in crate::independent::reporting) fn information(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = reporter.py();
    if reporter
        .getattr("config")?
        .call_method1("getoption", ("capture", "no"))?
        .eq("no")?
        && !reporter
            .getattr("config")?
            .call_method1("getini", ("console_output_style",))?
            .eq("progress-even-when-capture-no")?
    {
        return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
    }
    if reporter
        .getattr("config")?
        .call_method1("getoption", ("setupshow", false))?
        .is_truthy()?
    {
        return Ok(false.into_pyobject(py)?.to_owned().into_any().unbind());
    }
    let style = reporter
        .getattr("config")?
        .call_method1("getini", ("console_output_style",))?;
    let progress = PyFrozenSet::new(py, ["progress", "progress-even-when-capture-no"])?;
    let selected = if progress.contains(&style)? {
        Some("progress")
    } else if style.eq("count")? {
        Some("count")
    } else if style.eq("times")? {
        Some("times")
    } else {
        None
    };
    if let Some(style) = selected {
        Ok(PyString::new(py, style).into_any().unbind())
    } else {
        Ok(false.into_pyobject(py)?.to_owned().into_any().unbind())
    }
}

pub(in crate::independent::reporting) fn main_color(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
    if reporter.getattr("_main_color")?.is_none()
        || reporter.getattr("_known_types")?.is_none()
        || reporter.getattr("_is_last_item")?.is_truthy()?
    {
        reporter.call_method0("_set_main_color")?;
        if !reporter.getattr("_main_color")?.is_truthy()?
            || !reporter.getattr("_known_types")?.is_truthy()?
        {
            return Err(PyAssertionError::new_err(""));
        }
    }
    Ok((
        reporter.getattr("_main_color")?.unbind(),
        reporter.getattr("_known_types")?.unbind(),
    ))
}

pub(in crate::independent::reporting) fn collected(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    if !reporter.getattr("_session")?.is_truthy()? {
        return Err(PyAssertionError::new_err(""));
    }
    Ok(reporter
        .getattr("_session")?
        .getattr("testscollected")?
        .unbind())
}

pub(in crate::independent::reporting) fn is_count(reporter: &Bound<'_, PyAny>) -> PyResult<bool> {
    reporter.getattr("_show_progress_info")?.eq("count")
}

/// Select resource assignment before rendering without rereading live flags.
pub(in crate::independent::reporting) fn message_kind(
    reporter: &Bound<'_, PyAny>,
    collected: &Bound<'_, PyAny>,
) -> PyResult<&'static str> {
    Ok(if is_count(reporter)? {
        if collected.is_truthy()? {
            "count"
        } else {
            "empty_count"
        }
    } else {
        "other"
    })
}

pub(in crate::independent::reporting) fn message(
    reporter: &Bound<'_, PyAny>,
    collected: &Bound<'_, PyAny>,
    kind: &str,
    progress: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = reporter.py();
    match kind {
        "count" => {
            let digits = collected.str()?.len()?;
            return Ok(PyString::new(py, &format!(" [{{:{digits}d}}/{{}}]"))
                .call_method1("format", (progress, collected))?
                .unbind());
        }
        "empty_count" => {
            return Ok(PyString::new(py, " [ {} / {} ]")
                .call_method1("format", (collected, collected))?
                .unbind());
        }
        "other" => {}
        _ => {
            return Err(PyValueError::new_err(
                "unknown terminal message resource kind",
            ));
        }
    }
    if reporter.getattr("_show_progress_info")?.eq("times")? {
        if !collected.is_truthy()? {
            return Ok(PyString::new(py, "").into_any().unbind());
        }
        return Err(crate::UnsupportedFeatureError::new_err(
            "live terminal timing progress requires its report-duration consumer",
        ));
    }
    if collected.is_truthy()? {
        let percent = {
            let product = {
                let progress = reporter.getattr("reported_progress")?;
                progress.mul(100)?
            };
            product.floor_div(collected)?
        };
        Ok(PyString::new(py, " [{:3d}%]")
            .call_method1("format", (percent,))?
            .unbind())
    } else {
        Ok(PyString::new(py, " [100%]").into_any().unbind())
    }
}

pub(in crate::independent::reporting) fn count_length(
    collected: &Bound<'_, PyAny>,
) -> PyResult<usize> {
    PyString::new(collected.py(), " [{}/{}]")
        .call_method1("format", (collected, collected))?
        .len()
}

pub(in crate::independent::reporting) fn edge_length(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<usize> {
    Ok(if reporter.getattr("_show_progress_info")?.eq("times")? {
        " 99h 59m".len()
    } else {
        " [100%]".len()
    })
}

pub(in crate::independent::reporting) fn past_edge(
    reporter: &Bound<'_, PyAny>,
    width: &Bound<'_, PyAny>,
    length: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let total = {
        let subtotal = width.add(length)?;
        subtotal.add(1)?
    };
    Ok(total
        .rich_compare(reporter.getattr("_screen_width")?, CompareOp::Ge)?
        .unbind())
}

pub(in crate::independent::reporting) fn fill(
    reporter: &Bound<'_, PyAny>,
    width: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let fill = reporter
        .getattr("_tw")?
        .getattr("fullwidth")?
        .sub(width)?
        .sub(1)?;
    Ok(fill.unbind())
}

pub(in crate::independent::reporting) fn markup(color: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    let markup = PyDict::new(color.py());
    markup.set_item(color, true)?;
    Ok(markup.unbind())
}

pub(in crate::independent::reporting) fn fspath(
    reporter: &Bound<'_, PyAny>,
    nodeid: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let root = reporter.getattr("config")?.getattr("rootpath")?;
    let split = nodeid.getattr("split")?;
    let kwargs = PyDict::new(nodeid.py());
    kwargs.set_item("maxsplit", 1)?;
    let parts = split.call(("::",), Some(&kwargs))?;
    Ok(root.div(parts.get_item(0)?)?.unbind())
}

pub(in crate::independent::reporting) fn path_prefix(
    reporter: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if reporter.getattr("currentfspath")?.is_none() || path.ne(reporter.getattr("currentfspath")?)?
    {
        if !reporter.getattr("currentfspath")?.is_none()
            && reporter.getattr("_show_progress_info")?.is_truthy()?
        {
            reporter.call_method0("_write_progress_information_filling_space")?;
        }
        reporter.setattr("currentfspath", path)?;
        let start = reporter.getattr("startpath")?.extract::<PathBuf>()?;
        let path = path.extract::<PathBuf>()?;
        let relative = super::super::relative_location(&start, &path);
        let relative = if relative.is_empty() { "." } else { &relative };
        reporter.getattr("_tw")?.call_method0("line")?;
        let write = reporter.getattr("_tw")?.getattr("write")?;
        write.call1((format!("{relative} "),))?;
    }
    Ok(())
}
