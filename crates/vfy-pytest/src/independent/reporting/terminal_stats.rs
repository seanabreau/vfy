//! Native operations over the terminal's mutable Python protocol resources.

use pyo3::basic::CompareOp;
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

/// Distinct upstream consumers deliberately filter different report attributes.
#[derive(Clone, Copy)]
pub(super) enum ReportFilter {
    DebuggerHidden,
    SummaryCounted,
}

pub(super) fn add_stats(
    reporter: &Bound<'_, PyAny>,
    category: &Bound<'_, PyAny>,
    items: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = reporter.py();
    let new_category = {
        let stats = reporter.getattr(pyo3::intern!(py, "stats"))?;
        !stats.contains(category)?
    };
    {
        // Re-read after membership: that callback may replace the live mapping.
        let bucket = {
            let setdefault = reporter
                .getattr(pyo3::intern!(py, "stats"))?
                .getattr(pyo3::intern!(py, "setdefault"))?;
            setdefault.call1((category, PyList::empty(py)))?
        };
        bucket.call_method1(pyo3::intern!(py, "extend"), (items,))?;
    }
    if new_category {
        reporter.call_method0(pyo3::intern!(py, "_set_main_color"))?;
    }
    Ok(())
}

pub(super) fn reported_progress(reporter: &Bound<'_, PyAny>) -> PyResult<usize> {
    reporter.getattr("_progress_nodeids_reported")?.len()
}

pub(super) fn bucket_count(reporter: &Bound<'_, PyAny>, category: &str) -> PyResult<usize> {
    if let Some(counts) = super::terminal::native_bucket_counts(reporter)? {
        return Ok(counts
            .iter()
            .find_map(|(name, count)| (name == category).then_some(*count))
            .unwrap_or(0));
    }
    let stats = reporter.getattr("stats")?;
    let get = stats.getattr("get")?;
    get.call1((category, PyList::empty(reporter.py())))?.len()
}

pub(super) fn collection_counts(reporter: &Bound<'_, PyAny>) -> PyResult<(usize, usize, usize)> {
    if let Some(counts) = super::terminal::native_bucket_counts(reporter)? {
        let count = |category: &str| {
            counts
                .iter()
                .find_map(|(name, count)| (name == category).then_some(*count))
                .unwrap_or(0)
        };
        return Ok((count("error"), count("skipped"), count("deselected")));
    }
    let stats = reporter.getattr("stats")?;
    let count = |category: &str| {
        let get = stats.getattr("get")?;
        get.call1((category, PyList::empty(reporter.py())))?.len()
    };
    Ok((count("error")?, count("skipped")?, count("deselected")?))
}

pub(super) fn verbosity<'py>(reporter: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    reporter
        .getattr("config")?
        .getattr("option")?
        .getattr("verbose")
}

pub(super) fn is_last_item<'py>(reporter: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if reporter.getattr("_session")?.is_none() {
        return Err(PyAssertionError::new_err(""));
    }
    reporter.getattr("reported_progress")?.rich_compare(
        reporter.getattr("_session")?.getattr("testscollected")?,
        CompareOp::Eq,
    )
}

pub(super) fn determine_main_color(
    reporter: &Bound<'_, PyAny>,
    unknown: &Bound<'_, PyAny>,
) -> PyResult<&'static str> {
    if let Some(counts) = super::terminal::native_bucket_counts(reporter)? {
        let contains = |name: &str| counts.iter().any(|(category, _)| category == name);
        if contains("failed") || contains("error") {
            return Ok("red");
        }
        if contains("warnings") || contains("xpassed") || unknown.is_truthy()? {
            return Ok("yellow");
        }
        return Ok(
            if contains("passed") || !reporter.getattr("_is_last_item")?.is_truthy()? {
                "green"
            } else {
                "yellow"
            },
        );
    }
    let stats = reporter.getattr("stats")?;
    if stats.contains("failed")? || stats.contains("error")? {
        Ok("red")
    } else if stats.contains("warnings")? || stats.contains("xpassed")? || unknown.is_truthy()? {
        Ok("yellow")
    } else if stats.contains("passed")? || !reporter.getattr("_is_last_item")?.is_truthy()? {
        Ok("green")
    } else {
        Ok("yellow")
    }
}

pub(super) fn set_main_color(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = reporter.py();
    let unknown = PyList::empty(py);
    let names = match super::terminal::native_bucket_counts(reporter)? {
        Some(counts) => PyList::new(py, counts.into_iter().map(|(name, _)| name))?.into_any(),
        None => reporter.getattr("stats")?,
    };
    for found in names.try_iter()? {
        let found = found?;
        if found.is_truthy()?
            && !py
                .import("vfy.independent.terminal")?
                .getattr("KNOWN_TYPES")?
                .contains(&found)?
            && !unknown.contains(&found)?
        {
            unknown.append(found)?;
        }
    }
    let known = py.get_type::<PyList>().call1((py
        .import("vfy.independent.terminal")?
        .getattr("KNOWN_TYPES")?,))?;
    reporter.setattr("_known_types", known.call_method1("__add__", (&unknown,))?)?;
    let determine = reporter.getattr("_determine_main_color")?;
    reporter.setattr("_main_color", determine.call1((unknown.is_truthy()?,))?)?;
    Ok(())
}

pub(super) fn reports(
    reporter: &Bound<'_, PyAny>,
    category: &Bound<'_, PyAny>,
    filter: ReportFilter,
) -> PyResult<Py<PyList>> {
    let py = reporter.py();
    let native = if category.is_exact_instance_of::<PyString>() {
        super::terminal::native_category_reports(reporter, category.cast::<PyString>()?.to_str()?)?
    } else {
        None
    };
    let reports = if let Some(reports) = native {
        reports.into_any()
    } else {
        let get = reporter.getattr("stats")?.getattr("get")?;
        match filter {
            ReportFilter::DebuggerHidden => get.call1((category, PyTuple::empty(py)))?,
            ReportFilter::SummaryCounted => get.call1((category, PyList::empty(py)))?,
        }
    };
    let iterator = reports.try_iter()?;
    let result = PyList::empty(py);
    for report in iterator {
        let report = report?;
        let include = match filter {
            ReportFilter::DebuggerHidden => !report.hasattr("_pdbshown")?,
            ReportFilter::SummaryCounted => py
                .import("builtins")?
                .getattr("getattr")?
                .call1((&report, "count_towards_summary", true))?
                .is_truthy()?,
        };
        if include {
            result.append(report)?;
        }
    }
    Ok(result.unbind())
}
