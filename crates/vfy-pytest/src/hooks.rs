//! Validated hook records, registration, and `CPython` generator dispatch.

use pyo3::exceptions::{PyException, PyUserWarning};
use pyo3::prelude::*;

mod admission;
mod discovery;
mod dispatch;
pub(crate) mod invocation;
mod monitoring;
mod multicall;
pub(crate) mod native_caller;
pub(crate) mod nativeop;
mod observations;
mod plugins;
mod record;
mod registration;
mod registry;
mod result;
mod selection;
mod signature;
mod specification;

use discovery::{implementation, metadata};
pub(crate) use multicall::NativeHookDefault;
use record::{HookArguments, HookBody, Implementation, Style};
pub(crate) use registry::NativeHookRegistry;
use result::NativeHookResult;
use selection::{HookSelection, RegisteredImplementation};
use signature::argument_names;
use specification::{Specification, verify};

pyo3::create_exception!(vfy._native._pytest, HookCallError, PyException);
pyo3::create_exception!(vfy._native._pytest, PluginValidationError, PyException);
pyo3::create_exception!(vfy._native._pytest, PluggyWarning, PyUserWarning);
pyo3::create_exception!(
    vfy._native._pytest,
    PluggyTeardownRaisedWarning,
    PluggyWarning
);

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    dispatch::initialize_generator_type(module.py())?;
    nativeop::initialize(module.py())?;
    monitoring::register(module)?;
    module.add_class::<native_caller::NativeHookCall>()?;
    module.add_class::<NativeHookRegistry>()?;
    module.add_class::<NativeHookResult>()?;
    module.add_class::<Implementation>()?;
    module.add_class::<Specification>()?;
    module.add("HookCallError", module.py().get_type::<HookCallError>())?;
    let validation_error = module.py().get_type::<PluginValidationError>();
    validation_error.setattr(
        "__init__",
        module
            .py()
            .import("vfy._hook_callbacks")?
            .getattr("plugin_validation_error_init")?,
    )?;
    module.add("PluginValidationError", validation_error)?;
    module.add("PluggyWarning", module.py().get_type::<PluggyWarning>())?;
    module.add(
        "PluggyTeardownRaisedWarning",
        module.py().get_type::<PluggyTeardownRaisedWarning>(),
    )?;
    Ok(())
}

#[cfg(test)]
mod tests;
