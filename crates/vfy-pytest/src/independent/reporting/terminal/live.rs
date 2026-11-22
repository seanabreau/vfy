//! Consume each terminal hook invocation before its producer resumes.

use crate::module_cache::Module;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

/// Exact builtin operands need no Python frame to retain custom resources.
pub(super) fn consume_plain(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
    result: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let Ok(result) = result.cast_exact::<PyTuple>() else {
        return Ok(false);
    };
    if result.len() != 3 {
        return Ok(false);
    }
    let category = result.get_item(0)?;
    let letter = result.get_item(1)?;
    let word = result.get_item(2)?;
    if [&category, &letter, &word]
        .iter()
        .any(|value| value.cast_exact::<PyString>().is_err())
    {
        return Ok(false);
    }
    if let Some(markup) = prepare(
        report,
        reporter,
        &category,
        &letter,
        &word,
        report.py().None().bind(report.py()),
    )? {
        emit(report, reporter, &letter, &word, markup.bind(report.py()))?;
    }
    Ok(true)
}

pub(in crate::independent::reporting) fn status(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = report.py();
    reporter.setattr(pyo3::intern!(py, "_tests_ran"), true)?;
    let callback = reporter
        .getattr(pyo3::intern!(py, "config"))?
        .getattr(pyo3::intern!(py, "hook"))?
        .getattr(pyo3::intern!(py, "pytest_report_teststatus"))?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(pyo3::intern!(py, "report"), report)?;
    kwargs.set_item(
        pyo3::intern!(py, "config"),
        reporter.getattr(pyo3::intern!(py, "config"))?,
    )?;
    Ok(callback.call((), Some(&kwargs))?.unbind())
}

pub(in crate::independent::reporting) fn word(
    word: &Bound<'_, PyAny>,
) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
    let py = word.py();
    if word.is_instance_of::<PyTuple>() {
        let [letter, markup] = crate::independent::reporting::unpack::<2>(word)?;
        Ok((letter.unbind(), markup.unbind()))
    } else {
        Ok((word.clone().unbind(), py.None()))
    }
}

pub(in crate::independent::reporting) fn prepare(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
    category: &Bound<'_, PyAny>,
    letter: &Bound<'_, PyAny>,
    word: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyAny>,
) -> PyResult<Option<Py<PyAny>>> {
    let py = report.py();
    {
        let callback = reporter.getattr(pyo3::intern!(py, "_add_stats"))?;
        let items = PyList::new(py, [report])?;
        callback.call1((category, items))?;
    }
    if !letter.is_truthy()? && !word.is_truthy()? {
        return Ok(None);
    }
    if !markup.is_none() {
        return Ok(Some(markup.clone().unbind()));
    }
    let was_xfail =
        crate::module_cache::optional_attr(report, pyo3::intern!(py, "wasxfail"))?.is_some();
    let color = if report.getattr(pyo3::intern!(py, "passed"))?.is_truthy()? && !was_xfail {
        Some("green")
    } else if report.getattr(pyo3::intern!(py, "passed"))?.is_truthy()? && was_xfail {
        Some("yellow")
    } else if report.getattr(pyo3::intern!(py, "failed"))?.is_truthy()? {
        Some("red")
    } else if report.getattr(pyo3::intern!(py, "skipped"))?.is_truthy()? {
        Some("yellow")
    } else {
        None
    };
    let selected = PyDict::new(py);
    if let Some(color) = color {
        selected.set_item(color, true)?;
    }
    Ok(Some(selected.into_any().unbind()))
}

pub(in crate::independent::reporting) fn verbosity<'py>(
    reporter: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = reporter.py();
    let callback = reporter
        .getattr(pyo3::intern!(py, "config"))?
        .getattr(pyo3::intern!(py, "get_verbosity"))?;
    let kind = Module::Protocol
        .get(py)?
        .getattr(pyo3::intern!(py, "Config"))?
        .getattr(pyo3::intern!(py, "VERBOSITY_TEST_CASES"))?;
    callback.call1((kind,))
}

/// A parallel worker's progress and status lines belong to the controller. The
/// worker's config carries the basetemp index only for parallel workers, so this
/// check needs no invocation. User and plug-in output keeps the writer.
fn suppressed_for_worker(reporter: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = reporter.py();
    let index = reporter
        .getattr(pyo3::intern!(py, "_status_index"))?
        .cast_into::<super::super::NativeSessionReports>()?;
    if index.borrow().terminal.parallel_progress() {
        return Ok(true);
    }
    crate::module_cache::has_attr(
        &reporter
            .getattr(pyo3::intern!(py, "config"))?
            .getattr(pyo3::intern!(py, "option"))?,
        pyo3::intern!(py, "_worker_basetemp_index"),
    )
}

pub(in crate::independent::reporting) fn emit(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
    letter: &Bound<'_, PyAny>,
    word: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyAny>,
) -> PyResult<()> {
    emit_with(
        reporter,
        letter,
        word,
        markup,
        || Ok(report.getattr("nodeid")?.unbind()),
        || Ok(report.clone().unbind()),
    )
}

pub(super) fn emit_handle(
    report: &super::super::ReportHandle,
    reporter: &Bound<'_, PyAny>,
    letter: &Bound<'_, PyAny>,
    word: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = reporter.py();
    emit_with(
        reporter,
        letter,
        word,
        markup,
        || report.getattr(py, "nodeid"),
        || Ok(report.to_python(py)?.into_any()),
    )
}

fn emit_with(
    reporter: &Bound<'_, PyAny>,
    letter: &Bound<'_, PyAny>,
    word: &Bound<'_, PyAny>,
    markup: &Bound<'_, PyAny>,
    nodeid: impl FnOnce() -> PyResult<Py<PyAny>>,
    materialize: impl FnOnce() -> PyResult<Py<PyAny>>,
) -> PyResult<()> {
    let py = reporter.py();
    let progress = reporter.getattr(pyo3::intern!(py, "_progress_nodeids_reported"))?;
    progress.call_method1(pyo3::intern!(py, "add"), (nodeid()?,))?;
    if suppressed_for_worker(reporter)? {
        return Ok(());
    }
    if verbosity(reporter)?.le(0)? {
        Module::TerminalResources.get(py)?.call_method1(
            pyo3::intern!(py, "write_letter"),
            (reporter, letter, markup),
        )?;
        if reporter
            .getattr(pyo3::intern!(py, "_show_progress_info"))?
            .is_truthy()?
            && !reporter
                .getattr(pyo3::intern!(py, "_is_last_item"))?
                .is_truthy()?
        {
            reporter.call_method0(pyo3::intern!(
                py,
                "_write_progress_information_if_past_edge"
            ))?;
        }
    } else {
        Module::TerminalResources.get(py)?.call_method1(
            pyo3::intern!(py, "verbose_report"),
            (reporter, materialize()?, word, markup),
        )?;
        return Ok(());
    }
    reporter.call_method0(pyo3::intern!(py, "flush"))?;
    Ok(())
}

pub(in crate::independent::reporting) fn finish_loop(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    if suppressed_for_worker(reporter)? {
        return Ok(());
    }
    if verbosity(reporter)?.le(0)?
        && reporter.getattr("_show_progress_info")?.is_truthy()?
        && reporter.getattr("reported_progress")?.is_truthy()?
    {
        reporter.call_method0("_write_progress_information_filling_space")?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn logstart(
    reporter: &Bound<'_, PyAny>,
    nodeid: &Bound<'_, PyAny>,
    location: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let py = reporter.py();
    if suppressed_for_worker(reporter)? {
        return Ok(());
    }
    if reporter
        .getattr(pyo3::intern!(py, "showlongtestinfo"))?
        .is_truthy()?
    {
        Module::TerminalResources.get(py)?.call_method1(
            pyo3::intern!(py, "verbose_start"),
            (reporter, nodeid, location),
        )?;
        return Ok(());
    }
    if reporter
        .getattr(pyo3::intern!(py, "showfspath"))?
        .is_truthy()?
    {
        reporter.call_method1(pyo3::intern!(py, "write_fspath_result"), (nodeid, ""))?;
        reporter.call_method0(pyo3::intern!(py, "flush"))?;
    }
    Ok(())
}
