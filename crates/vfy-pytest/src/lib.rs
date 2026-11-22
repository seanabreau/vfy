//! Native pytest runner, collector, and plug-in host exposed to the `vfy._native` extension.
//!
//! The crate builds only on Unix; `register` adds its classes and functions to the
//! `vfy._native._pytest` submodule.

#![forbid(unsafe_code)]
#![cfg(unix)]
#![cfg_attr(
    test,
    allow(
        clippy::disallowed_methods,
        reason = "tests write fixture inputs directly; atomic replacement is a production invariant"
    )
)]

use pyo3::prelude::*;

pyo3::create_exception!(
    vfy._native._pytest,
    IndependentUsageError,
    pyo3::exceptions::PyValueError
);

pyo3::create_exception!(
    vfy._native._pytest,
    UnsupportedFeatureError,
    pyo3::exceptions::PyException
);
pyo3::create_exception!(
    vfy._native._pytest,
    CachePathError,
    pyo3::exceptions::PyOSError
);
pyo3::create_exception!(
    vfy._native._pytest,
    CacheWriteError,
    pyo3::exceptions::PyOSError
);
pyo3::create_exception!(
    vfy._native._pytest,
    NativeParallelError,
    pyo3::exceptions::PyException
);

mod assertion_cache;
mod assertions;
mod cache;
mod collection_order;
mod collection_selection;
mod deferred_function;
mod dense_callspec;
mod dense_reader;
mod execnet_codec;
mod execnet_encode;
mod function_batch;
mod hooks;
mod independent;
mod lifecycle;
mod logging;
mod module_cache;
mod mutation_watchdog;
mod parallel;
mod parameter_family;
mod progress;
mod raises_matcher;
mod reporting;
mod results;

pub use results::NativeRunResult;
mod runner;
#[cfg(test)]
mod test_gc;
mod warnings;

/// Register the native runner in the owning Python extension.
///
/// # Errors
///
/// Returns the Python error if a type or function cannot be added to the module.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module_cache::initialize_optional_getattr(module.py())?;
    independent::initialize_protocol_strings(module.py());
    hooks::register(module)?;
    raises_matcher::register(module)?;
    register_assertions(module)?;
    register_fixture_types(module)?;
    independent::register_value_representations(module)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_fixture_resource,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_capture_fixture,
        module
    )?)?;
    register_item_protocol(module)?;
    module.add_class::<independent::NativeIndependentReport>()?;
    independent::subtests::register(module)?;
    independent::register_unraisable(module)?;
    independent::register_runtime_providers(module)?;
    independent::hypothesis_report::register(module)?;
    module.add_class::<independent::reporting::subtest_report::NativeSubtestReport>()?;
    register_collection_types(module)?;
    independent::reporting::writer::register(module)?;
    module.add_class::<independent::NativeIndependentSession>()?;
    module.add_class::<independent::NativeIndependentInvocation>()?;
    module.add_class::<independent::monkeypatch::NativeMonkeyPatchState>()?;
    module.add_function(wrap_pyfunction!(
        independent::session_hooks::render_terminal_separator,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::session_hooks::render_terminal_text,
        module
    )?)?;
    module.add(
        "IndependentUsageError",
        module.py().get_type::<IndependentUsageError>(),
    )?;
    module.add(
        "UnsupportedFeatureError",
        module.py().get_type::<UnsupportedFeatureError>(),
    )?;
    module.add("CachePathError", module.py().get_type::<CachePathError>())?;
    module.add("CacheWriteError", module.py().get_type::<CacheWriteError>())?;
    module.add(
        "NativeParallelError",
        module.py().get_type::<NativeParallelError>(),
    )?;
    module.add_function(wrap_pyfunction!(
        parameter_family::parametrize_family,
        module
    )?)?;
    module.add_class::<cache::NativeCacheService>()?;
    module.add_class::<cache::NativeCacheSession>()?;
    module.add_class::<dense_callspec::DenseCallSpecFamily>()?;
    module.add_class::<deferred_function::NativeDeferredFunctionInventory>()?;
    module.add_class::<deferred_function::NativeDenseCallSpecRow>()?;
    module.add_class::<deferred_function::NativeCallSpecField>()?;
    module.add_class::<deferred_function::NativeCallSpecGetAttribute>()?;
    module.add_function(wrap_pyfunction!(
        collection_selection::selected_parameter_indices,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(runner::run_native_suite, module)?)?;
    module.add_function(wrap_pyfunction!(
        independent::run_independent_suite,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::collect_independent_suite,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(runner::native_unsupported_items, module)?)?;
    independent::register_node_protocol(module)?;
    register_junit(module)?;
    module.add_class::<runner::NativeRequest>()?;
    module.add_class::<runner::NativeExecutionSession>()?;
    module.add_class::<execnet_codec::NativeExecnetDecoder>()?;
    module.add_class::<execnet_encode::NativeExecnetEncoder>()?;
    module.add_class::<results::NativeRunResult>()?;
    module.add_function(wrap_pyfunction!(
        independent::install_mutation_code,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::has_collectable_mutations,
        module
    )?)?;
    module.add_class::<parallel::NativeParallelSession>()?;
    module.add_function(wrap_pyfunction!(
        independent::fresh_mutation_test_plans,
        module
    )?)?;
    module.add_class::<parallel::NativeMutationPublication>()?;
    module.add_class::<parallel::NativeParallelWorker>()?;
    module.add_class::<assertion_cache::NativeAssertionCacheLease>()?;
    module.add_class::<mutation_watchdog::MutationProcessWatchdog>()?;
    Ok(())
}

/// Functions the Python item and collector protocols call into.
fn register_item_protocol(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        independent::independent_parameter_iterator_warning,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_collector_repr_failure,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_item_repr_failure,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::callspec::independent_callspec_setmulti,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::callspec::direct_parameter,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_item_reportinfo,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_import_path,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        independent::independent_matches_python_name,
        module
    )?)?;
    Ok(())
}

fn register_junit(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(reporting::publish_junit_xml, module)?)
}

fn register_assertions(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(
        assertions::render_independent_assertion,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        assertions::get_independent_verbosity,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        assertions::describe_independent_comparison,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        assertions::render_independent_hook_explanation,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        assertions::current_assertion_item,
        module
    )?)?;
    Ok(())
}

/// Register the collection call, report, arena and session-history protocol types.
fn register_collection_types(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<independent::NativeIndependentCallInfo>()?;
    module.add_class::<independent::NativeIndependentCollectReport>()?;
    module.add_class::<independent::NativeCollectionArena>()?;
    module.add_class::<independent::NativeSessionReports>()?;
    module.add_class::<independent::NativeSessionLogReport>()?;
    module.add_class::<independent::NativeSessionLogReportDescriptor>()?;
    module.add_class::<independent::NativeTerminalStats>()?;
    module.add_class::<independent::NativeTerminalAddStats>()?;
    module.add_class::<independent::NativeTerminalLogReport>()?;
    module.add_class::<independent::NativeTerminalLogReportDescriptor>()?;
    module.add_class::<independent::NativeTerminalLogStartDescriptor>()?;
    Ok(())
}

fn register_fixture_types(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<independent::NativeIndependentRequest>()?;
    independent::register_capture_fixture(module)?;
    independent::register_warning_filter(module)?;
    module.add_class::<independent::NativeResolvedFixtureDefinitions>()?;
    independent::register_fixture_lookup(module)?;
    independent::register_temporary_paths(module)?;
    Ok(())
}
