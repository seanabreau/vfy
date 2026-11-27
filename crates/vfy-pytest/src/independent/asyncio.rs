//! Async discovery and scoped runner requirements share the native fixture plan.
use super::lifecycle::Context;
use super::parameters::ParameterRow;
use super::plugins::ASYNCIO_PLUGIN;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFunction, PyList, PyTuple};
use vfy_core::test_plan::FixtureExecution;
use vfy_core::{AsyncioLoopScope, AsyncioMode, FixtureScope};

const CO_GENERATOR: u32 = 0x20;
const CO_COROUTINE: u32 = 0x80;
const CO_ASYNC_GENERATOR: u32 = 0x200;

#[cfg(test)]
mod tests;

pub(super) fn enabled(context: Option<&Context>, py: Python<'_>) -> PyResult<bool> {
    context.map_or(Ok(false), |context| {
        let enabled = context
            .registry
            .bind(py)
            .try_borrow()?
            .has_plugin(ASYNCIO_PLUGIN);
        Ok(enabled)
    })
}

fn optional(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if value.is_none() {
        return Ok(None);
    }
    let value = value.extract::<String>()?;
    Ok((!value.is_empty()).then_some(value))
}

pub(super) fn mode(context: &Context, py: Python<'_>) -> PyResult<AsyncioMode> {
    let config = context.config.bind(py);
    let option = config.call_method1(
        pyo3::intern!(py, "getoption"),
        (pyo3::intern!(py, "asyncio_mode"),),
    )?;
    let value = if option.is_none() {
        config.call_method1(
            pyo3::intern!(py, "getini"),
            (pyo3::intern!(py, "asyncio_mode"),),
        )?
    } else {
        option
    };
    AsyncioMode::parse(&value.extract::<String>()?).map_err(crate::IndependentUsageError::new_err)
}

pub(super) fn fixture_scope(
    context: Option<&Context>,
    callable: &Bound<'_, PyAny>,
    scope: FixtureScope,
) -> PyResult<Option<AsyncioLoopScope>> {
    let py = callable.py();
    if !enabled(context, py)? {
        return Ok(None);
    }
    let Some(context) = context else {
        return Ok(None);
    };
    let explicit = callable
        .getattr_opt(pyo3::intern!(py, "_force_asyncio_fixture"))?
        .map(|value| value.is_truthy())
        .transpose()?
        .unwrap_or(false);
    if !explicit && mode(context, py)? == AsyncioMode::Strict {
        return Ok(None);
    }
    if !explicit {
        let inspect = py.import("inspect")?;
        if !inspect
            .call_method1("iscoroutinefunction", (callable,))?
            .is_truthy()?
            && !inspect
                .call_method1("isasyncgenfunction", (callable,))?
                .is_truthy()?
        {
            return Ok(None);
        }
    }
    let selected = callable
        .getattr_opt("_loop_scope")?
        .map(|value| optional(&value))
        .transpose()?
        .flatten();
    let configured = optional(&context.config.bind(py).call_method1(
        pyo3::intern!(py, "getini"),
        ("asyncio_default_fixture_loop_scope",),
    )?)?;
    vfy_core::plan_asyncio_loop_scope(selected.as_deref(), configured.as_deref(), scope.as_str())
        .map(Some)
        .map_err(PyValueError::new_err)
}

fn marker_error(py: Python<'_>, nodeid: &str, message: &str) -> PyResult<PyErr> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    let failure = py
        .import("vfy.independent.facade")?
        .getattr("Failed")?
        .call((format!("{nodeid}: {message}"),), Some(&kwargs))?;
    Ok(PyErr::from_value(failure))
}

fn test_scope(
    context: Option<&Context>,
    nodeid: &str,
    marks: &Bound<'_, PyList>,
) -> PyResult<Option<AsyncioLoopScope>> {
    let py = marks.py();
    if !enabled(context, py)? {
        return Ok(None);
    }
    let Some(context) = context else {
        return Ok(None);
    };
    let marker = marks
        .iter()
        .find_map(|mark| {
            match mark
                .getattr("name")
                .and_then(|name| name.extract::<String>())
            {
                Ok(name) if name == "asyncio" => Some(Ok(mark)),
                Ok(_) => None,
                Err(error) => Some(Err(error)),
            }
        })
        .transpose()?;
    if marker.is_none() && mode(context, py)? == AsyncioMode::Strict {
        return Ok(None);
    }
    if let Some(marker) = marker {
        let kwargs = marker.getattr("kwargs")?.cast_into::<PyDict>()?;
        if marker.getattr("args")?.len()? != 0 {
            return Err(marker_error(
                py,
                nodeid,
                "mark.asyncio accepts only keyword arguments 'loop_scope' and 'loop_factories'.",
            )?);
        }
        for (name, _) in kwargs.iter() {
            let name = name.extract::<String>()?;
            if name == "loop_factories" {
                return Err(marker_error(
                    py,
                    nodeid,
                    "the asyncio marker argument loop_factories is not supported by the \
                     independent engine; use --engine=pytest-native",
                )?);
            }
            if name != "loop_scope" {
                return Err(marker_error(
                    py,
                    nodeid,
                    "mark.asyncio accepts only keyword arguments 'loop_scope' and 'loop_factories'.",
                )?);
            }
        }
        if let Some(scope) = kwargs.get_item("loop_scope")?
            && scope.is_truthy()?
        {
            if let Some(scope) = scope
                .extract::<String>()
                .ok()
                .and_then(|scope| AsyncioLoopScope::parse(&scope).ok())
            {
                return Ok(Some(scope));
            }
            let value = scope.repr()?;
            return Err(marker_error(
                py,
                nodeid,
                &format!(
                    "mark.asyncio got an unexpected loop scope value {value}. Valid scopes are: function, class, module, package, session."
                ),
            )?);
        }
    }
    let configured = optional(&context.config.bind(py).call_method1(
        pyo3::intern!(py, "getini"),
        ("asyncio_default_test_loop_scope",),
    )?)?;
    AsyncioLoopScope::parse(configured.as_deref().unwrap_or("function"))
        .map(Some)
        .map_err(PyValueError::new_err)
}

pub(super) fn runner_name(scope: AsyncioLoopScope) -> String {
    format!("_{}_scoped_runner", scope.as_str())
}

pub(super) fn live_test_scope(
    context: Option<&Context>,
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
) -> PyResult<Option<AsyncioLoopScope>> {
    let marker = item.call_method1("get_closest_marker", ("asyncio",))?;
    let live = PyList::empty(py);
    if !marker.is_none() {
        live.append(marker)?;
    }
    let nodeid = item.getattr("nodeid")?.extract::<String>()?;
    test_scope(context, &nodeid, &live)
}

pub(super) fn unhandled_test(py: Python<'_>) -> PyResult<()> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    py.import("vfy.independent.facade")?.getattr("fail")?.call((
        "async def functions are not natively supported.\nYou need to install a suitable plugin for your async framework, for example:\n  - anyio\n  - pytest-asyncio\n  - pytest-tornasync\n  - pytest-trio\n  - pytest-twisted",
    ), Some(&kwargs))?;
    Ok(())
}

pub(super) fn classify(callable: &Bound<'_, PyAny>) -> PyResult<FixtureExecution> {
    let py = callable.py();
    if callable.is_exact_instance_of::<PyCFunction>() {
        // Exact builtin functions cannot carry a Python code object or coroutine marker.
        return Ok(FixtureExecution::Callable);
    }
    if callable.is_exact_instance_of::<PyFunction>() {
        return classify_function(callable, callable);
    }
    if crate::hooks::nativeop::bound_method_type(py)
        .is_some_and(|method_type| callable.get_type().is(method_type))
    {
        let function = callable.getattr(pyo3::intern!(py, "__func__"))?;
        if function.is_exact_instance_of::<PyFunction>() {
            return classify_function(&function, callable);
        }
    }
    let inspect = callable.py().import("inspect")?;
    for (predicate, kind) in [
        ("iscoroutinefunction", FixtureExecution::AsyncCoroutine),
        ("isasyncgenfunction", FixtureExecution::AsyncGenerator),
        ("isgeneratorfunction", FixtureExecution::Generator),
    ] {
        if inspect.call_method1(predicate, (callable,))?.is_truthy()? {
            return Ok(kind);
        }
    }
    Ok(FixtureExecution::Callable)
}

fn classify_function(
    function: &Bound<'_, PyAny>,
    callable: &Bound<'_, PyAny>,
) -> PyResult<FixtureExecution> {
    let py = callable.py();
    // The live function owns its code and marker even when called as a method.
    let flags = function
        .getattr(pyo3::intern!(py, "__code__"))?
        .getattr(pyo3::intern!(py, "co_flags"))?
        .extract::<u32>()?;
    if flags & CO_COROUTINE != 0 {
        return Ok(FixtureExecution::AsyncCoroutine);
    }
    if crate::module_cache::optional_attr(function, pyo3::intern!(py, "_is_coroutine_marker"))?
        .is_some()
        && py
            .import("inspect")?
            .call_method1("iscoroutinefunction", (callable,))?
            .is_truthy()?
    {
        return Ok(FixtureExecution::AsyncCoroutine);
    }
    Ok(if flags & CO_ASYNC_GENERATOR != 0 {
        FixtureExecution::AsyncGenerator
    } else if flags & CO_GENERATOR != 0 {
        FixtureExecution::Generator
    } else {
        FixtureExecution::Callable
    })
}

pub(super) fn fixture_plan(
    context: Option<&Context>,
    callable: &Bound<'_, PyAny>,
    scope: FixtureScope,
) -> PyResult<(FixtureExecution, Option<AsyncioLoopScope>)> {
    let execution = classify(callable)?;
    let explicitly_async = callable
        .getattr_opt(pyo3::intern!(callable.py(), "_force_asyncio_fixture"))?
        .map(|value| value.is_truthy())
        .transpose()?
        .unwrap_or(false);
    let asynchronous = matches!(
        execution,
        FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
    );
    let loop_scope = if explicitly_async || asynchronous {
        fixture_scope(context, callable, scope)?
    } else {
        None
    };
    let execution = if asynchronous && loop_scope.is_none() && enabled(context, callable.py())? {
        FixtureExecution::UnsupportedAsync
    } else {
        execution
    };
    Ok((execution, loop_scope))
}

fn test_requirement(
    context: Option<&Context>,
    callable: &Bound<'_, PyAny>,
    nodeid: &str,
    marks: &Bound<'_, PyList>,
) -> PyResult<Option<AsyncioLoopScope>> {
    if !enabled(context, callable.py())? {
        return Ok(None);
    }
    if matches!(
        classify(callable)?,
        FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
    ) {
        test_scope(context, nodeid, marks)
    } else {
        Ok(None)
    }
}

pub(super) fn mark_async_generator(
    callable: &Bound<'_, PyAny>,
    name: &str,
    scope: Option<AsyncioLoopScope>,
    marks: &Bound<'_, PyList>,
    own_mark_count: usize,
) -> PyResult<usize> {
    if scope.is_none() || classify(callable)? != FixtureExecution::AsyncGenerator {
        return Ok(0);
    }
    let py = callable.py();
    let message = format!(
        "Tests based on asynchronous generators are not supported. {} will be ignored.",
        name.rsplit("::").next().unwrap_or(name)
    );
    let facade = py.import("vfy.independent.facade")?;
    py.import("warnings")?.call_method1(
        "warn",
        (&message, facade.getattr("PytestCollectionWarning")?),
    )?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("run", false)?;
    kwargs.set_item("reason", message)?;
    let mark = facade
        .getattr("MarkDecorator")?
        .call1(("xfail", PyTuple::empty(py), kwargs))?;
    marks.insert(own_mark_count, mark)?;
    Ok(1)
}

impl super::Collector<'_> {
    pub(super) fn asyncio_test_plan(
        &self,
        callable: &Bound<'_, PyAny>,
        nodeid: &str,
        marks: &Bound<'_, PyList>,
        own_mark_count: usize,
    ) -> PyResult<(Option<AsyncioLoopScope>, usize)> {
        let scope = test_requirement(self.context.as_ref(), callable, nodeid, marks)?;
        let added = mark_async_generator(callable, nodeid, scope, marks, own_mark_count)?;
        Ok((scope, own_mark_count + added))
    }

    pub(super) fn asyncio_roots(
        &self,
        callable: &Bound<'_, PyAny>,
        nodeid: &str,
        marks: &Bound<'_, PyList>,
        rows: &[ParameterRow],
        function_mark_count: usize,
        roots: &mut Vec<String>,
    ) -> PyResult<()> {
        if !enabled(self.context.as_ref(), callable.py())? {
            return Ok(());
        }
        if !matches!(
            classify(callable)?,
            FixtureExecution::AsyncCoroutine | FixtureExecution::AsyncGenerator
        ) {
            return Ok(());
        }
        for row in rows {
            let planning_marks = PyList::new(callable.py(), marks.iter())?;
            for (offset, mark) in row.marks.iter().enumerate() {
                planning_marks.insert(function_mark_count + offset, mark.bind(callable.py()))?;
            }
            if let Some(scope) = test_scope(self.context.as_ref(), nodeid, &planning_marks)? {
                let name = runner_name(scope);
                if !roots.contains(&name) {
                    roots.push(name);
                }
            }
        }
        Ok(())
    }
}
