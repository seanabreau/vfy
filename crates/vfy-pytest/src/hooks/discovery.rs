//! Hook implementation discovery from plugin attributes.

use super::record::{HookBody, Implementation, Style, WrapperFlags};
use super::signature::argument_names;
use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict};

pub(super) fn metadata<'py>(
    function: &Bound<'py, PyAny>,
    local: &str,
    external: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let py = function.py();
    for name in [local, external] {
        match function.getattr(name) {
            Ok(value) if value.is_none() => (),
            Ok(value) => return Ok(Some(value)),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => (),
            Err(error) => return Err(error),
        }
    }
    Ok(None)
}

fn option(options: Option<&Bound<'_, PyDict>>, name: &str) -> PyResult<bool> {
    options.map_or(Ok(false), |options| {
        options
            .get_item(name)?
            .map_or(Ok(false), |value| value.is_truthy())
    })
}

#[expect(
    clippy::too_many_lines,
    reason = "hook discovery keeps native declaration and pluggy option admission in one ordered operation"
)]
pub(super) fn implementation(
    plugin: &Bound<'_, PyAny>,
    plugin_name: &str,
    attribute: &str,
    project_name: &str,
    discover_prefix: bool,
) -> PyResult<Option<Implementation>> {
    // Pytest filters names before descriptor access; generic Pluggy discovery
    // still inspects marked methods without requiring the project prefix.
    if discover_prefix
        && project_name == "pytest"
        && (!attribute.starts_with("pytest_") || attribute == "pytest_plugins")
    {
        return Ok(None);
    }
    let py = plugin.py();
    let function = plugin.getattr(attribute)?;
    if !py
        .import("inspect")?
        .call_method1("isroutine", (&function,))?
        .is_truthy()?
    {
        return Ok(None);
    }
    // The actual bound native callable declares its program; this is established
    // during admission, before the implementation is published.
    let body = if function.is_instance_of::<PyCFunction>() {
        let owner = function.getattr("__self__")?;
        if owner.is_instance_of::<crate::independent::skipping::NativeSkippingCall>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_call"
        {
            HookBody::SkippingCall
        } else if owner.is_instance_of::<crate::independent::skipping::NativeSkippingReport>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_makereport"
        {
            HookBody::SkippingReport
        } else if owner.is_instance_of::<crate::independent::subtests::NativeSubtestsStatus>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_report_teststatus"
        {
            HookBody::SubtestsStatus
        } else if owner.is_instance_of::<crate::independent::skipping::NativeSkippingHooks>()
            && function.getattr("__name__")?.extract::<&str>()? == "status"
        {
            HookBody::SkippingStatus
        } else if owner
            .is_instance_of::<crate::independent::hypothesis_report::NativeHypothesisReport>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_makereport"
        {
            HookBody::HypothesisReport(
                owner
                    .cast_into::<crate::independent::hypothesis_report::NativeHypothesisReport>()?
                    .unbind(),
            )
        } else if owner.is_instance_of::<crate::independent::reporting::NativeSessionLogReport>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_logreport"
        {
            HookBody::SessionLogReport(
                owner
                    .cast_into::<crate::independent::reporting::NativeSessionLogReport>()?
                    .unbind(),
            )
        } else if owner
            .is_instance_of::<crate::independent::reporting::terminal::NativeTerminalLogReport>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_logreport"
        {
            HookBody::TerminalLogReport(
                owner
                    .cast_into::<crate::independent::reporting::terminal::NativeTerminalLogReport>(
                    )?
                    .unbind(),
            )
        } else if owner
            .is_instance_of::<crate::independent::reporting::terminal::NativeTerminalLogReport>()
            && function.getattr("__name__")?.extract::<&str>()? == "pytest_runtest_logstart"
        {
            HookBody::TerminalLogStart(
                owner
                    .cast_into::<crate::independent::reporting::terminal::NativeTerminalLogReport>(
                    )?
                    .unbind(),
            )
        } else if owner
            .is_instance_of::<crate::independent::reporting::terminal::NativeStatusCallbacks>()
        {
            match function.getattr("__name__")?.extract::<&str>()? {
                "runner_makereport" => HookBody::RunnerMakeReport,
                "runner_status" => HookBody::RunnerStatus,
                "terminal_status" => HookBody::TerminalStatus,
                _ => HookBody::Dynamic,
            }
        } else {
            HookBody::Dynamic
        }
    } else {
        HookBody::Dynamic
    };
    let options = metadata(
        &function,
        "__vfy_hookimpl__",
        &format!("{project_name}_impl"),
    )?;
    let options = options.and_then(|options| options.cast_into::<PyDict>().ok());
    if (!discover_prefix || !attribute.starts_with(&format!("{project_name}_")))
        && options.is_none()
    {
        return Ok(None);
    }
    let options = options.unwrap_or_else(|| PyDict::new(py));
    if matches!(&body, HookBody::SkippingCall | HookBody::SkippingReport) {
        options.set_item("wrapper", true)?;
    }
    if matches!(&body, HookBody::HypothesisReport(_)) {
        options.set_item("hookwrapper", true)?;
    }
    if matches!(
        &body,
        HookBody::SubtestsStatus | HookBody::SessionLogReport(_)
    ) {
        options.set_item("tryfirst", true)?;
    }
    if matches!(&body, HookBody::TerminalStatus) {
        options.set_item("trylast", true)?;
    }
    for key in [
        "wrapper",
        "hookwrapper",
        "tryfirst",
        "trylast",
        "optionalhook",
    ] {
        if !options.contains(key)? {
            options.set_item(key, false)?;
        }
    }
    if !options.contains("specname")? {
        options.set_item("specname", py.None())?;
    }
    let name = options
        .get_item("specname")?
        .filter(|value| !value.is_none())
        .map_or_else(
            || Ok(attribute.to_owned()),
            |value| {
                if value.is_truthy()? {
                    value.extract::<String>()
                } else {
                    Ok(attribute.to_owned())
                }
            },
        )?;
    let wrapper = option(Some(&options), "wrapper")?;
    let legacy = option(Some(&options), "hookwrapper")?;
    let tryfirst = option(Some(&options), "tryfirst")?;
    let trylast = option(Some(&options), "trylast")?;
    let optionalhook = option(Some(&options), "optionalhook")?;
    let (arguments, keyword_arguments) = argument_names(&function)?;
    Ok(Some(Implementation {
        name,
        plugin_name: plugin_name.to_owned(),
        invocation: None,
        plugin: plugin.clone().unbind(),
        function: function.unbind(),
        body,
        arguments,
        keyword_arguments,
        style: if legacy {
            Style::Legacy
        } else if wrapper {
            Style::Wrapper
        } else {
            Style::Plain
        },
        wrapper_flags: match (wrapper, legacy) {
            (false, false) => WrapperFlags::Plain,
            (true, false) => WrapperFlags::New,
            (false, true) => WrapperFlags::Legacy,
            (true, true) => WrapperFlags::Both,
        },
        priority: if trylast { -1 } else { i8::from(tryfirst) },
        tryfirst,
        trylast,
        optionalhook,
        options: Some(options.unbind()),
    }))
}
