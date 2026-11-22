//! Live serial verbose output uses the location and prefix protocols.

use pyo3::basic::CompareOp;
use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySlice, PyString};
use std::path::PathBuf;

pub(in crate::independent::reporting) fn has_path(path: &Bound<'_, PyAny>) -> PyResult<bool> {
    path.is_truthy()
}

pub(in crate::independent::reporting) fn domain_matches(
    line: &Bound<'_, PyAny>,
    domain: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    Ok(domain.is_truthy()? && line.call_method1("endswith", (domain,))?.is_truthy()?)
}

pub(in crate::independent::reporting) fn trim_domain(
    line: &Bound<'_, PyAny>,
    domain: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let length = isize::try_from(domain.len()?)
        .map_err(|_| PyOverflowError::new_err("location domain length exceeds the native range"))?;
    let py = line.py();
    let slice = py
        .get_type::<PySlice>()
        .call1((py.None(), -length, py.None()))?;
    Ok(line.get_item(slice)?.unbind())
}

pub(in crate::independent::reporting) fn domain_value(
    values: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let replace = {
        let value = values.get_item(0)?;
        value.getattr("replace")?
    };
    Ok(replace.call1((".", "::"))?.unbind())
}

pub(in crate::independent::reporting) fn append_domain(
    line: &Bound<'_, PyAny>,
    values: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = line.py();
    let suffix = PyString::new(py, "[").call_method1("join", (values,))?;
    Ok(py
        .import("operator")?
        .call_method1("iadd", (line, suffix))?
        .unbind())
}

pub(in crate::independent::reporting) fn needs_origin(
    reporter: &Bound<'_, PyAny>,
    nodeid: &Bound<'_, PyAny>,
    fspath: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let verbose = {
        let comparison = {
            let value = reporter.getattr("verbosity")?;
            value.rich_compare(2, CompareOp::Ge)?
        };
        comparison.is_truthy()?
    };
    if !verbose {
        return Ok(false);
    }
    let py = reporter.py();
    let path = {
        let split = nodeid.getattr("split")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("maxsplit", 1)?;
        let parts = split.call(("::",), Some(&kwargs))?;
        parts.get_item(0)?
    };
    let normalized = {
        let replace = {
            let path = py.import("os")?.call_method1("fspath", (fspath,))?;
            path.getattr("replace")?
        };
        replace.call1(("\\", "/"))?
    };
    path.ne(normalized)
}

pub(in crate::independent::reporting) fn append_origin(
    reporter: &Bound<'_, PyAny>,
    line: &Bound<'_, PyAny>,
    fspath: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = reporter.py();
    let relative = {
        let startpath = reporter.getattr("startpath")?;
        let path = py.import("pathlib")?.getattr("Path")?.call1((fspath,))?;
        let start = startpath.extract::<PathBuf>()?;
        let path = path.extract::<PathBuf>()?;
        let relative = vfy_core::relative_path(&start, &path);
        let relative = if relative.as_os_str().is_empty() {
            std::path::Path::new(".")
        } else {
            relative.as_path()
        };
        relative.as_os_str().into_pyobject(py)?.into_any()
    };
    let suffix = PyString::new(py, " <- ").add(relative)?;
    Ok(py
        .import("operator")?
        .call_method1("iadd", (line, suffix))?
        .unbind())
}

pub(in crate::independent::reporting) fn missing(py: Python<'_>) -> Py<PyAny> {
    PyString::new(py, "[location]").into_any().unbind()
}

pub(in crate::independent::reporting) fn finish(line: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(line.add(" ")?.unbind())
}

pub(in crate::independent::reporting) fn ensure_newline(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if reporter.getattr("currentfspath")?.is_truthy()? {
        let result = {
            let line = {
                let writer = reporter.getattr("_tw")?;
                writer.getattr("line")?
            };
            line.call0()?
        };
        drop(result);
        reporter.setattr("currentfspath", reporter.py().None())?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn write_prefix(
    reporter: &Bound<'_, PyAny>,
    prefix: &Bound<'_, PyAny>,
    extra: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyDict>,
) -> PyResult<()> {
    let changed = {
        let comparison = {
            let current = reporter.getattr("currentfspath")?;
            current.rich_compare(prefix, CompareOp::Ne)?
        };
        comparison.is_truthy()?
    };
    if changed {
        let result = {
            let line = {
                let writer = reporter.getattr("_tw")?;
                writer.getattr("line")?
            };
            line.call0()?
        };
        drop(result);
        reporter.setattr("currentfspath", prefix)?;
        let result = {
            let write = {
                let writer = reporter.getattr("_tw")?;
                writer.getattr("write")?
            };
            write.call1((prefix,))?
        };
        drop(result);
    }
    if extra.is_truthy()? {
        let result = {
            let write = reporter.getattr("_tw")?.getattr("write")?;
            write.call((extra,), Some(markup))?
        };
        drop(result);
        reporter.setattr("currentfspath", -2)?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn running_xdist(
    report: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    if report.hasattr("node")? {
        return Err(crate::UnsupportedFeatureError::new_err(
            "live verbose worker output requires its controller consumer",
        ));
    }
    Ok(false)
}

pub(in crate::independent::reporting) fn after_prefix(
    _report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if reporter.getattr("_show_progress_info")?.is_truthy()? {
        reporter.call_method0("_write_progress_information_filling_space")?;
    }
    Ok(())
}
