//! Call preparation, subsetting, and default or extra callback insertion.

use super::record::{HookBody, Implementation, Style, WrapperFlags};
use super::registry::NativeHookRegistry;
use super::selection::HookSelection;
use super::signature::argument_names;
use super::specification::{verify, verify_call};
use super::{invocation, monitoring, specification};
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet, PyTuple};

/// Source of a default callback's parameter names.
#[derive(Clone, Copy)]
pub(super) enum CallbackSignature {
    /// Derive the names with `inspect.signature`, as for Python callables.
    Inspect,
    /// The native callback takes no hook parameters, so inspection is skipped.
    NoParameters,
}

pub(super) fn prepare_call(
    registry: &Bound<'_, NativeHookRegistry>,
    py: Python<'_>,
    name: &str,
    kwargs: &invocation::CallArguments<'_, '_>,
    firstresult: bool,
) -> PyResult<(HookSelection, bool)> {
    let implementations = NativeHookRegistry::selected(registry, name);
    let (specification, historic) = {
        let registry = registry.borrow();
        (
            registry
                .specifications
                .get(name)
                .map(|spec| spec.clone_ref(py)),
            registry.history.contains_key(name),
        )
    };
    if historic {
        return Err(PyAssertionError::new_err(
            "Cannot directly call a historic hook - use call_historic instead.",
        ));
    }
    let firstresult = if let Some(specification) = specification {
        verify_call(py, &specification, kwargs)?;
        specification::first_result(py, &specification)?
    } else {
        firstresult
    };
    Ok((implementations, firstresult))
}

/// What joins the selected implementations for one call.
pub(super) enum Extension<'a, 'py> {
    None,
    /// Inserted before the first trylast plain implementation.
    Default {
        callback: Py<PyAny>,
        plugin_name: &'a str,
        signature: CallbackSignature,
    },
    /// Inserted before the last implementation that is neither a wrapper nor tryfirst.
    Extra(&'a Bound<'py, PyAny>),
}

/// One hook call. Plugins in `remove_plugins` are subtracted before `extension` joins.
pub(super) struct Multicall<'a, 'py> {
    pub(super) name: &'a str,
    pub(super) kwargs: &'a Bound<'py, PyDict>,
    pub(super) firstresult: bool,
    pub(super) remove_plugins: Option<&'a Bound<'py, PyAny>>,
    pub(super) extension: Extension<'a, 'py>,
}

impl<'a, 'py> Multicall<'a, 'py> {
    pub(super) fn new(name: &'a str, kwargs: &'a Bound<'py, PyDict>, firstresult: bool) -> Self {
        Self {
            name,
            kwargs,
            firstresult,
            remove_plugins: None,
            extension: Extension::None,
        }
    }
}

/// The native operation and its observable Python callback counterpart.
pub(crate) struct NativeHookDefault<F, G> {
    pub(crate) execute: F,
    pub(crate) materialize: G,
}

/// The single native multicall: select, subset, extend, then dispatch in call order.
pub(super) fn multicall(
    registry: &Bound<'_, NativeHookRegistry>,
    py: Python<'_>,
    request: Multicall<'_, '_>,
) -> PyResult<Py<PyAny>> {
    let Multicall {
        name,
        kwargs,
        firstresult,
        remove_plugins,
        extension,
    } = request;
    let (mut implementations, firstresult) = prepare_call(
        registry,
        py,
        name,
        &invocation::CallArguments::Borrowed(kwargs),
        firstresult,
    )?;
    if let Some(remove_plugins) = remove_plugins {
        implementations = subset(py, name, implementations, remove_plugins)?;
    }
    match extension {
        Extension::None => {}
        Extension::Default {
            callback,
            plugin_name,
            signature,
        } => {
            let mut owned = implementations.into_owned(py);
            insert_default(
                registry,
                py,
                name,
                &mut owned,
                callback,
                plugin_name,
                signature,
            )?;
            implementations = HookSelection::Owned(owned);
        }
        Extension::Extra(methods) => {
            let mut owned = implementations.into_owned(py);
            insert_extra(py, name, &mut owned, methods)?;
            implementations = HookSelection::Owned(owned);
        }
    }
    monitoring::execute(registry, name, implementations, kwargs, firstresult)
}

pub(super) fn insert_default(
    registry: &Bound<'_, NativeHookRegistry>,
    py: Python<'_>,
    name: &str,
    implementations: &mut Vec<Py<Implementation>>,
    callback: Py<PyAny>,
    plugin_name: &str,
    signature: CallbackSignature,
) -> PyResult<()> {
    let plugin = match callback
        .bind(py)
        .getattr_opt(pyo3::intern!(py, "__self__"))?
    {
        Some(plugin) if !plugin.is_none() => plugin.unbind(),
        _ => callback.clone_ref(py),
    };
    let callback = callback_implementation(py, name, callback, plugin_name, plugin, signature)?;
    let specification = registry
        .borrow()
        .specifications
        .get(name)
        .map(|specification| specification.clone_ref(py));
    if let Some(specification) = specification {
        verify(py, &specification, &callback)?;
    }
    let callback = Py::new(py, callback)?;
    let position = implementations
        .iter()
        .position(|implementation| {
            let implementation = implementation.borrow(py);
            implementation.style == Style::Plain && implementation.priority == -1
        })
        .unwrap_or(implementations.len());
    implementations.insert(position, callback);
    Ok(())
}

/// Places each method as pluggy's `call_extra` does. The scan runs over the storage
/// order (the reverse of call order) from its end, skipping wrappers and tryfirst.
fn insert_extra(
    py: Python<'_>,
    name: &str,
    implementations: &mut Vec<Py<Implementation>>,
    methods: &Bound<'_, PyAny>,
) -> PyResult<()> {
    implementations.reverse();
    for callback in methods.try_iter()? {
        let callback = callback?.unbind();
        let extra = Py::new(
            py,
            callback_implementation(
                py,
                name,
                callback,
                "<temp>",
                py.None(),
                CallbackSignature::Inspect,
            )?,
        )?;
        let mut position = implementations.len();
        while position > 0 {
            let implementation = implementations[position - 1].borrow(py);
            if implementation.style != Style::Plain || implementation.tryfirst {
                position -= 1;
            } else {
                break;
            }
        }
        implementations.insert(position, extra);
    }
    implementations.reverse();
    Ok(())
}

pub(super) fn subset(
    py: Python<'_>,
    name: &str,
    implementations: HookSelection,
    remove_plugins: &Bound<'_, PyAny>,
) -> PyResult<HookSelection> {
    let mut plugins = remove_plugins.try_iter()?.peekable();
    if plugins.peek().is_none() {
        return Ok(implementations);
    }
    let removed = PySet::empty(py)?;
    for plugin in plugins {
        let plugin = plugin?;
        if plugin.hasattr(name)? {
            removed.add(plugin)?;
        }
    }
    let mut retained = Vec::new();
    for implementation in implementations.into_owned(py) {
        let plugin = implementation.borrow(py).plugin.clone_ref(py);
        if !removed.contains(plugin)? {
            retained.push(implementation);
        }
    }
    Ok(HookSelection::Owned(retained))
}

fn callback_implementation(
    py: Python<'_>,
    name: &str,
    callback: Py<PyAny>,
    plugin_name: &str,
    plugin: Py<PyAny>,
    signature: CallbackSignature,
) -> PyResult<Implementation> {
    let (arguments, keyword_arguments) = match signature {
        CallbackSignature::Inspect => argument_names(callback.bind(py))?,
        CallbackSignature::NoParameters => {
            (PyTuple::empty(py).unbind(), PyTuple::empty(py).unbind())
        }
    };
    Ok(Implementation {
        name: name.to_owned(),
        plugin_name: plugin_name.to_owned(),
        invocation: None,
        plugin,
        function: callback,
        body: HookBody::Dynamic,
        arguments,
        keyword_arguments,
        style: Style::Plain,
        wrapper_flags: WrapperFlags::Plain,
        priority: 0,
        tryfirst: false,
        trylast: false,
        optionalhook: false,
        options: None,
    })
}
