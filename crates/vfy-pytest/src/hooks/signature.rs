//! Hook parameter-name discovery from Python callables.

use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFunction, PyTuple};

fn ordinary_parameter_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    if !bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
        || !bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        return false;
    }
    !matches!(
        name,
        "False"
            | "None"
            | "True"
            | "and"
            | "as"
            | "assert"
            | "async"
            | "await"
            | "break"
            | "class"
            | "continue"
            | "def"
            | "del"
            | "elif"
            | "else"
            | "except"
            | "finally"
            | "for"
            | "from"
            | "global"
            | "if"
            | "import"
            | "in"
            | "is"
            | "lambda"
            | "nonlocal"
            | "not"
            | "or"
            | "pass"
            | "raise"
            | "return"
            | "try"
            | "while"
            | "with"
            | "yield"
    )
}

struct FunctionDefaults<'py> {
    count: usize,
    // Keep the metadata alive through signature construction, including any
    // callbacks triggered while inspecting the remaining parameters.
    _kwdefaults: Bound<'py, PyAny>,
    _defaults: Bound<'py, PyAny>,
    _annotations: Bound<'py, PyAny>,
}

fn function_defaults<'py>(
    source: &Bound<'py, PyAny>,
    positional_count: usize,
) -> PyResult<Option<FunctionDefaults<'py>>> {
    let annotations = source.getattr("__annotations__")?;
    let defaults = source.getattr("__defaults__")?;
    let kwdefaults = source.getattr("__kwdefaults__")?;
    // inspect reads both mappings by parameter name; subclasses and custom
    // keys may execute callbacks in those lookups.
    for value in [&annotations, &kwdefaults] {
        if value.is_none() {
            continue;
        }
        if !value.is_exact_instance_of::<PyDict>() {
            return Ok(None);
        }
        if value
            .cast::<PyDict>()?
            .iter()
            .any(|(key, _)| !key.is_exact_instance_of::<pyo3::types::PyString>())
        {
            return Ok(None);
        }
    }
    let count = if defaults.is_none() {
        0
    } else {
        if !defaults.is_exact_instance_of::<PyTuple>() {
            return Ok(None);
        }
        defaults.cast::<PyTuple>()?.len()
    };
    if count > positional_count {
        return Ok(None);
    }
    if count > 0 {
        let empty = source
            .py()
            .import("inspect")?
            .getattr("Parameter")?
            .getattr("empty")?;
        if defaults
            .cast::<PyTuple>()?
            .iter()
            .any(|value| value.is(&empty))
        {
            return Ok(None);
        }
    }
    Ok(Some(FunctionDefaults {
        count,
        _kwdefaults: kwdefaults,
        _defaults: defaults,
        _annotations: annotations,
    }))
}

/// Read the live signature of an ordinary Python function without constructing
/// inspect's Parameter objects. Custom signature protocols retain inspect's
/// observable behavior.
fn function_argument_names(
    function: &Bound<'_, PyAny>,
) -> PyResult<Option<(Vec<String>, Vec<String>)>> {
    let py = function.py();
    let (source, bound) = if function.is_exact_instance_of::<PyFunction>() {
        (function.clone(), false)
    } else if function.is_instance_of::<PyCFunction>() {
        return Ok(None);
    } else if function
        .get_type()
        .is(&py.import("types")?.getattr("MethodType")?)
    {
        let source = function.getattr("__func__")?;
        if !source.is_exact_instance_of::<PyFunction>() {
            return Ok(None);
        }
        (source, true)
    } else {
        return Ok(None);
    };
    for name in ["__signature__", "__wrapped__"] {
        if source.hasattr(name)? {
            return Ok(None);
        }
    }
    if let Some(text) = source.getattr_opt("__text_signature__")?
        && !text.is_none()
        && (!text.is_exact_instance_of::<pyo3::types::PyString>() || text.is_truthy()?)
    {
        return Ok(None);
    }
    // Python 3.14 annotation evaluation may execute arbitrary code; let
    // inspect own its ordering and avoid triggering that callback twice.
    if source
        .getattr_opt("__annotate__")?
        .is_some_and(|annotate| !annotate.is_none())
    {
        return Ok(None);
    }
    let code = source.getattr("__code__")?;
    let count = code.getattr("co_argcount")?.extract::<usize>()?;
    let names = code.getattr("co_varnames")?.cast_into::<PyTuple>()?;
    let keyword_count = code.getattr("co_kwonlyargcount")?.extract::<usize>()?;
    let flags = code.getattr("co_flags")?.extract::<u32>()?;
    // Code metadata is captured before annotations, which may be evaluated by
    // CPython and execute application code on newer interpreters.
    let Some(defaults) = function_defaults(&source, count)? else {
        return Ok(None);
    };
    let start = usize::from(bound && count > 0);
    if bound && count == 0 && flags & 4 == 0 {
        return Ok(None);
    }
    let parameter_count =
        count + keyword_count + usize::from(flags & 4 != 0) + usize::from(flags & 8 != 0);
    let mut parameter_names = Vec::with_capacity(parameter_count);
    for index in 0..parameter_count {
        let value = names.get_item(index)?;
        if !value.is_exact_instance_of::<pyo3::types::PyString>() {
            return Ok(None);
        }
        let Ok(name) = value.extract::<String>() else {
            return Ok(None);
        };
        // Replaced code objects can contain names inspect normalizes or rejects.
        if !ordinary_parameter_name(&name) || parameter_names.contains(&name) {
            return Ok(None);
        }
        parameter_names.push(name);
    }
    let required_end = count - defaults.count;
    let mut required = Vec::new();
    let mut optional = Vec::new();
    for (index, name) in parameter_names
        .into_iter()
        .enumerate()
        .take(count)
        .skip(start)
    {
        if index < required_end {
            required.push(name);
        } else {
            optional.push(name);
        }
    }
    Ok(Some((required, optional)))
}

pub(super) fn argument_names(function: &Bound<'_, PyAny>) -> PyResult<(Py<PyTuple>, Py<PyTuple>)> {
    let py = function.py();
    if let Some((mut required, optional)) = function_argument_names(function)? {
        if required.first().is_some_and(|name| name == "self")
            && function
                .getattr("__qualname__")
                .ok()
                .and_then(|value| value.extract::<String>().ok())
                .is_some_and(|name| name.contains('.'))
        {
            required.remove(0);
        }
        return Ok((
            PyTuple::new(py, required)?.unbind(),
            PyTuple::new(py, optional)?.unbind(),
        ));
    }
    let inspect = py.import("inspect")?;
    let empty = inspect.getattr("Parameter")?.getattr("empty")?;
    let signature = inspect.call_method1("signature", (function,))?;
    let mut required = Vec::new();
    let mut optional = Vec::new();
    for parameter in signature
        .getattr("parameters")?
        .call_method0("values")?
        .try_iter()?
    {
        let parameter = parameter?;
        let kind: u8 = parameter.getattr("kind")?.extract()?;
        if kind > 1 {
            continue;
        }
        let name: String = parameter.getattr("name")?.extract()?;
        if parameter.getattr("default")?.is(&empty) {
            required.push(name);
        } else {
            optional.push(name);
        }
    }
    if required.first().is_some_and(|name| name == "self")
        && function
            .getattr("__qualname__")
            .ok()
            .and_then(|value| value.extract::<String>().ok())
            .is_some_and(|name| name.contains('.'))
    {
        required.remove(0);
    }
    Ok((
        PyTuple::new(py, required)?.unbind(),
        PyTuple::new(py, optional)?.unbind(),
    ))
}
