//! Pytest report-character selection over the live terminal report buckets.

use pyo3::exceptions::{PyAssertionError, PyAttributeError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PySet, PyString, PyTuple};
use std::fmt::Write;

fn normalize(requested: &str, disable_warnings: bool) -> String {
    let mut selected = String::new();
    for character in requested.chars() {
        let character = match character {
            'F' => 'f',
            'S' => 's',
            other => other,
        };
        match character {
            'a' => "sxXEf".clone_into(&mut selected),
            'A' => "PpsxXEf".clone_into(&mut selected),
            'N' => selected.clear(),
            other if !selected.contains(other) => selected.push(other),
            _ => {}
        }
    }
    if disable_warnings {
        selected.retain(|character| character != 'w');
    } else if !selected.contains('w') {
        selected.insert(0, 'w');
    }
    selected
}

pub(in crate::independent::reporting) fn reportchars(
    config: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let options = config.getattr("option")?;
    Ok(normalize(
        &options.getattr("reportchars")?.extract::<String>()?,
        options.getattr("disable_warnings")?.is_truthy()?,
    ))
}

pub(in crate::independent::reporting) fn keywords(item: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
    let py = item.py();
    if let Some(source) = item.getattr_opt(pyo3::intern!(py, "keywords"))? {
        // Exact dict and set inputs let fromkeys reuse stored hashes. Reports
        // instead hash each yielded key, including string subclasses, as pytest does.
        let source = if source.is_exact_instance_of::<PyDict>()
            || source.is_exact_instance_of::<PySet>()
            || source.is_exact_instance_of::<PyFrozenSet>()
        {
            source.try_iter()?.into_any()
        } else {
            source
        };
        return Ok(py
            .get_type::<PyDict>()
            .call_method1(pyo3::intern!(py, "fromkeys"), (source, 1))?
            .cast_into::<PyDict>()?
            .unbind());
    }
    let keywords = PyDict::new(py);
    // Independent items expose marker and callable resources directly.
    for marker in item.call_method0("iter_markers")?.try_iter()? {
        keywords.set_item(marker?.getattr("name")?, 1)?;
    }
    if item.hasattr("obj")? {
        let object = item.getattr("obj")?;
        if object.hasattr("__dict__")? {
            for key in object.getattr("__dict__")?.try_iter()? {
                keywords.set_item(key?, 1)?;
            }
        }
    }
    Ok(keywords.unbind())
}

fn reports<'py>(reporter: &Bound<'py, PyAny>, category: &str) -> PyResult<Bound<'py, PyAny>> {
    if let Some(reports) = super::native_category_reports(reporter, category)? {
        return Ok(reports.into_any());
    }
    reporter
        .getattr("stats")?
        .call_method1("get", (category, PyList::empty(reporter.py())))
}

fn markup_word(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
    color: &str,
) -> PyResult<String> {
    let py = reporter.py();
    let status = super::live::status(report, reporter)?;
    let value = status.bind(py).get_item(2)?;
    let (word, markup) = super::live::word(&value)?;
    let default = PyDict::new(py);
    default.set_item(color, true)?;
    let markup = if markup.bind(py).is_none() {
        default.as_any()
    } else {
        markup.bind(py)
    };
    py.import("vfy.independent.terminal_resources")?
        .call_method1("summary_markup", (reporter.getattr("_tw")?, word, markup))?
        .extract()
}

fn nodeid(report: &Bound<'_, PyAny>, reporter: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = reporter.py();
    let nodeid = reporter
        .getattr("config")?
        .call_method1("cwd_relative_nodeid", (report.getattr("nodeid")?,))?
        .extract::<String>()?;
    if let Some((path, parts)) = nodeid.split_once("::") {
        let markup = PyDict::new(py);
        markup.set_item("bold", true)?;
        let parts = reporter
            .getattr("_tw")?
            .getattr("markup")?
            .call((parts,), Some(&markup))?
            .extract::<String>()?;
        Ok(format!("{path}::{parts}"))
    } else {
        Ok(nodeid)
    }
}

fn summary_message<'py>(report: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyAny>>> {
    let changed = report
        .cast::<super::super::NativeIndependentReport>()
        .is_ok_and(|report| report.borrow().representation_changed);
    if !changed && report.hasattr("_vfy_summary_message")? {
        let message = report.getattr("_vfy_summary_message")?;
        return Ok((!message.is_none()).then_some(message));
    }
    let representation = report.getattr("longrepr")?;
    summary_message_from_representation(&representation)
}

fn summary_message_from_representation<'py>(
    representation: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if representation.is_instance_of::<PyString>() {
        return Ok(Some(representation.clone()));
    }
    match representation
        .getattr("reprcrash")
        .and_then(|crash| crash.getattr("message"))
    {
        Ok(message) => Ok(Some(message)),
        Err(error) if error.is_instance_of::<PyAttributeError>(representation.py()) => Ok(None),
        Err(error) => Err(error),
    }
}

fn has_hypothesis(extras: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = extras.py();
    Ok(extras.contains(pyo3::intern!(py, "_hypothesis_stats"))?
        || extras.contains(pyo3::intern!(py, "_hypothesis_failing_examples"))?)
}

pub(in crate::independent::reporting) fn phase_transport_metadata(
    report: &Bound<'_, PyAny>,
    transport_summary: bool,
) -> PyResult<Option<vfy_core::test_execution::ReportSummary>> {
    let wanted = transport_summary
        || has_hypothesis(&report.getattr(pyo3::intern!(report.py(), "__dict__"))?)?;
    wanted.then(|| metadata(report)).transpose()
}

pub(in crate::independent::reporting) fn transport_metadata(
    report: &Bound<'_, PyAny>,
) -> PyResult<Option<vfy_core::test_execution::ReportSummary>> {
    let py = report.py();
    let extras = report.getattr(pyo3::intern!(py, "__dict__"))?;
    let wanted = extras
        .call_method1(
            pyo3::intern!(py, "get"),
            (pyo3::intern!(py, "_vfy_transport_summary"), false),
        )?
        .is_truthy()?
        || has_hypothesis(&extras)?;
    wanted.then(|| metadata(report)).transpose()
}

pub(in crate::independent::reporting) fn metadata(
    report: &Bound<'_, PyAny>,
) -> PyResult<vfy_core::test_execution::ReportSummary> {
    let py = report.py();
    metadata_resources(
        summary_message(report)?,
        || {
            py.import("builtins")?
                .call_method1("getattr", (report, "keywords", PyDict::new(py)))
        },
        || {
            report
                .getattr("__dict__")?
                .cast_into::<PyDict>()
                .map(Some)
                .map_err(Into::into)
        },
    )
}

/// Native reports already own these resources; reading them needs no Python report object.
pub(in crate::independent::reporting) fn native_metadata(
    py: Python<'_>,
    representation: Option<&Py<PyAny>>,
    attributes: Option<&super::super::PendingAttributes>,
    published: impl Fn() -> Option<Py<super::super::NativeIndependentReport>>,
) -> PyResult<vfy_core::test_execution::ReportSummary> {
    let message = representation
        .map(|representation| summary_message_from_representation(representation.bind(py)))
        .transpose()?
        .flatten();
    metadata_resources(
        message,
        || {
            if let Some(report) = published() {
                return py
                    .import("builtins")?
                    .call_method1("getattr", (report, "keywords", PyDict::new(py)));
            }
            Ok(attributes.map_or_else(
                || PyDict::new(py).into_any(),
                |attributes| attributes.keywords.bind(py).clone().into_any(),
            ))
        },
        || {
            if let Some(report) = published() {
                return report
                    .bind(py)
                    .getattr("__dict__")?
                    .cast_into::<PyDict>()
                    .map(Some)
                    .map_err(Into::into);
            }
            Ok(None)
        },
    )
}

fn metadata_resources<'py>(
    message: Option<Bound<'py, PyAny>>,
    keywords: impl FnOnce() -> PyResult<Bound<'py, PyAny>>,
    extras: impl FnOnce() -> PyResult<Option<Bound<'py, PyDict>>>,
) -> PyResult<vfy_core::test_execution::ReportSummary> {
    let message = message
        .map(|message| crate::reporting::python_text_from_python(&message))
        .transpose()?;
    let keywords = keywords()?
        .try_iter()?
        .map(|key| key?.extract::<String>())
        .collect::<PyResult<Vec<_>>>()?;
    let extras = extras()?;
    let text = |name: &str| {
        extras.as_ref().map_or(Ok(None), |extras| {
            extras
                .get_item(name)?
                .map(|value| crate::reporting::python_text_from_python(&value))
                .transpose()
        })
    };
    Ok(vfy_core::test_execution::ReportSummary {
        keywords,
        message,
        hypothesis_statistics: text("_hypothesis_stats")?,
        hypothesis_failing_examples: text("_hypothesis_failing_examples")?,
    })
}

fn simple_line(
    report: &Bound<'_, PyAny>,
    reporter: &Bound<'_, PyAny>,
    color: &str,
) -> PyResult<String> {
    let py = reporter.py();
    let mut line = format!(
        "{} {}",
        markup_word(report, reporter, color)?,
        nodeid(report, reporter)?
    );
    let message = summary_message(report)?;
    if let Some(message) = message {
        let environment = py.import("os")?.getattr("environ")?;
        let verbose = reporter
            .getattr("config")?
            .getattr("option")?
            .getattr("verbose")?
            .ge(2)?;
        if environment.call_method1("get", ("CI",))?.is_truthy()?
            || environment
                .call_method1("get", ("BUILD_NUMBER",))?
                .is_truthy()?
            || verbose
        {
            let _ = write!(line, " - {}", message.str()?);
        } else {
            let width = reporter
                .getattr("_tw")?
                .getattr("fullwidth")?
                .extract::<i64>()?;
            let used = super::super::writer::measure_width(PyString::new(py, &line).as_any())?;
            let suffix = py
                .import("vfy.independent.terminal_resources")?
                .call_method1("format_trimmed", (" - {}", message, width - used))?;
            if !suffix.is_none() {
                line.push_str(suffix.extract::<String>()?.as_str());
            }
        }
    }
    Ok(line)
}

fn skipped_lines(reporter: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let py = reporter.py();
    let skipped = reports(reporter, "skipped")?;
    let groups = PyDict::new(py);
    let mut word = None;
    for report in skipped.try_iter()? {
        let report = report?;
        if word.is_none() {
            word = Some(markup_word(&report, reporter, "yellow")?);
        }
        let representation = report.getattr("longrepr")?;
        let tuple = representation.cast::<PyTuple>().map_err(|_| {
            PyAssertionError::new_err("skipped report requires a three-part longrepr")
        })?;
        if tuple.len() != 3 {
            return Err(PyAssertionError::new_err(
                "skipped report requires a three-part longrepr",
            ));
        }
        let path = py.import("os.path")?.call_method1(
            "relpath",
            (tuple.get_item(0)?, reporter.getattr("startpath")?),
        )?;
        let mut line = tuple.get_item(1)?;
        let keywords = py
            .import("builtins")?
            .call_method1("getattr", (&report, "keywords", PyDict::new(py)))?;
        if report.getattr("when")?.eq("setup")?
            && keywords.contains("skip")?
            && !keywords.contains("pytestmark")?
        {
            line = py.None().into_bound(py);
        }
        let key = PyTuple::new(py, [path, line, tuple.get_item(2)?])?;
        let count = groups
            .get_item(&key)?
            .map_or(Ok(0), |value| value.extract::<usize>())?;
        groups.set_item(key, count + 1)?;
    }
    let mut lines = Vec::new();
    for (key, count) in groups.iter() {
        let path = key.get_item(0)?.extract::<String>()?;
        let line = key.get_item(1)?;
        let reason = key.get_item(2)?.extract::<String>()?;
        let reason = reason.strip_prefix("Skipped: ").unwrap_or(&reason);
        let location = if line.is_none() {
            path
        } else {
            format!("{path}:{}", line.str()?)
        };
        if let Some(word) = &word {
            lines.push(format!(
                "{word} [{}] {location}: {reason}",
                count.extract::<usize>()?
            ));
        }
    }
    Ok(lines)
}

pub(in crate::independent::reporting) fn short_summary(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let mut lines = Vec::new();
    for requested in reporter
        .getattr("reportchars")?
        .extract::<String>()?
        .chars()
    {
        let (category, color) = match requested {
            'f' => ("failed", "red"),
            'E' => ("error", "red"),
            'p' => ("passed", "green"),
            'x' => ("xfailed", "yellow"),
            'X' => ("xpassed", "yellow"),
            's' => {
                lines.extend(skipped_lines(reporter)?);
                continue;
            }
            _ => continue,
        };
        for report in reports(reporter, category)?.try_iter()? {
            let report = report?;
            if matches!(requested, 'x' | 'X') {
                let mut line = format!(
                    "{} {}",
                    markup_word(&report, reporter, color)?,
                    nodeid(&report, reporter)?
                );
                let reason = report.getattr("wasxfail")?;
                if reason.is_truthy()? {
                    let _ = write!(line, " - {}", reason.str()?);
                }
                lines.push(line);
            } else {
                lines.push(simple_line(&report, reporter, color)?);
            }
        }
    }
    if !lines.is_empty() {
        let markup = PyDict::new(reporter.py());
        markup.set_item("cyan", true)?;
        markup.set_item("bold", true)?;
        reporter
            .getattr("write_sep")?
            .call(("=", "short test summary info"), Some(&markup))?;
        for line in lines {
            reporter.call_method1("write_line", (line,))?;
        }
    }
    Ok(())
}

fn capture_sections(
    report: &Bound<'_, PyAny>,
    writer: &Bound<'_, PyAny>,
    teardown_only: bool,
) -> PyResult<()> {
    for section in report.getattr("sections")?.try_iter()? {
        let (title, mut content) = section?.extract::<(String, String)>()?;
        if teardown_only && !title.contains("teardown") {
            continue;
        }
        writer.call_method1("sep", ("-", title))?;
        if content.ends_with('\n') {
            content.pop();
        }
        writer.call_method1("line", (content,))?;
    }
    Ok(())
}

pub(in crate::independent::reporting) fn capture_summary(
    reporter: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let selected = reporter.getattr("reportchars")?.extract::<String>()?;
    let writer = reporter.getattr("_tw")?;
    for (option, category, heading) in [('P', "passed", "PASSES"), ('X', "xpassed", "XPASSES")] {
        if !selected.contains(option) {
            continue;
        }
        let reports = reporter.call_method1("getreports", (category,))?;
        if !reports.is_truthy()? {
            continue;
        }
        reporter.call_method1("write_sep", ("=", heading))?;
        for report in reports.try_iter()? {
            let report = report?;
            if report.getattr("sections")?.is_truthy()? {
                let location = report.getattr("location")?;
                reporter.call_method1("write_sep", ("_", location.get_item(2)?))?;
                capture_sections(&report, &writer, false)?;
            }
            for teardown in self::reports(reporter, "")?.try_iter()? {
                let teardown = teardown?;
                if teardown.getattr("when")?.eq("teardown")?
                    && teardown.getattr("nodeid")?.eq(report.getattr("nodeid")?)?
                {
                    capture_sections(&teardown, &writer, true)?;
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
