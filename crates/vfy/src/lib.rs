//! The `vfy._native` `CPython` extension: monitoring callbacks and object conversion.

#![cfg_attr(
    test,
    allow(
        clippy::disallowed_methods,
        reason = "tests write fixture inputs directly; atomic replacement is a production invariant"
    )
)]

use pyo3::prelude::*;

mod analysis;
mod asyncio;
mod cache;
mod collector;
mod config;
mod data;
mod mutation;
mod pytest_config;
mod pytest_doctor;
mod pytest_report;
mod pytest_version;
mod report;
mod selection;
mod semantic;
mod test_cli;
mod test_invocation;
mod trace;
mod watch;

fn uses_sysmon_branch_arcs(
    implementation: &str,
    version: (u8, u8, u8, &str, u8),
    requested_core: Option<&str>,
) -> bool {
    vfy_core::RuntimePolicy {
        implementation,
        version: vfy_core::PythonVersion {
            major: version.0,
            minor: version.1,
            micro: version.2,
            release_level: version.3,
            serial: version.4,
        },
        requested_core: requested_core.map_or(
            vfy_core::RequestedCore::Default,
            vfy_core::RequestedCore::Named,
        ),
    }
    .uses_sysmon_branch_arcs()
}

/// Whether Coverage.py would record branch arcs for the resolved core.
#[pyfunction]
#[pyo3(signature = (configured_core = None))]
fn coverage_uses_sysmon_branch_arcs(
    py: Python<'_>,
    configured_core: Option<&str>,
) -> PyResult<bool> {
    if let Some(core) = configured_core {
        config::validate_core_name(core)?;
    }
    let sys = py.import("sys")?;
    let implementation: String = sys.getattr("implementation")?.getattr("name")?.extract()?;
    let version: (u8, u8, u8, String, u8) = sys.getattr("version_info")?.extract()?;
    Ok(uses_sysmon_branch_arcs(
        &implementation,
        (version.0, version.1, version.2, &version.3, version.4),
        configured_core,
    ))
}

/// Return `debug` or `release` for the profile this extension was built with.
#[pyfunction]
const fn build_profile() -> &'static str {
    if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    }
}

/// Return the name of the Python source parser backend.
#[pyfunction]
const fn parser_backend() -> &'static str {
    "ruff"
}

/// Return the extension's package version.
#[pyfunction]
const fn package_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Return `min(pending, capacity)`, or one if either input is zero.
#[pyfunction]
const fn recommended_worker_count(pending: usize, capacity: usize) -> usize {
    vfy_core::recommended_worker_count(pending, capacity)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let pytest = PyModule::new(module.py(), "vfy._native._pytest")?;
    #[cfg(unix)]
    vfy_pytest::register(&pytest)?;
    module.add("_pytest", &pytest)?;
    module
        .py()
        .import("sys")?
        .getattr("modules")?
        .set_item("vfy._native._pytest", &pytest)?;
    module.add_class::<analysis::PySourceAnalysis>()?;
    module.add_class::<collector::CollectorCore>()?;
    collector::prepare_frame_lookup(module.py())?;
    module.add_class::<data::CoverageData>()?;
    module.add_class::<report::ReportCounts>()?;
    module.add_class::<report::ReportRow>()?;
    module.add_class::<semantic::PySemanticReport>()?;
    module.add_class::<semantic::PySemanticReportRow>()?;
    module.add_class::<trace::TraceStore>()?;
    pytest_doctor::register(module)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_sources, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_suite_state, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_file_digest, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_contexts, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_executed_paths, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_test_durations, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_fingerprints, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_baseline_valid, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::write_mutation_baseline, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::rank_mutation_tests, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::plan_mutation_tests, module)?)?;
    module.add_function(wrap_pyfunction!(
        mutation::mutation_test_preferences,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        mutation::plan_mutation_test_groups,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(mutation::order_mutation_jobs, module)?)?;
    module.add_function(wrap_pyfunction!(
        mutation::collected_mutation_worker_limit,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        mutation::mutation_portable_worker_limit,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(mutation::next_mutation_job, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::select_mutation_catalog, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::select_mutation_sources, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::mutation_timeout, module)?)?;
    module.add_function(wrap_pyfunction!(
        mutation::mutation_baseline_timeout,
        module
    )?)?;
    config::register(module)?;
    test_cli::register(module)?;
    test_invocation::register(module)?;
    pytest_config::register(module)?;
    asyncio::register(module)?;
    module.add_function(wrap_pyfunction!(cache::clean_cache, module)?)?;
    module.add_function(wrap_pyfunction!(coverage_uses_sysmon_branch_arcs, module)?)?;
    module.add_function(wrap_pyfunction!(build_profile, module)?)?;
    module.add_function(wrap_pyfunction!(parser_backend, module)?)?;
    module.add_function(wrap_pyfunction!(package_version, module)?)?;
    module.add_function(wrap_pyfunction!(
        pytest_version::pytest_plugin_version_supported,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(
        pytest_version::pytest_version_less_than,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(recommended_worker_count, module)?)?;
    selection::register(module)?;
    module.add_function(wrap_pyfunction!(analysis::analysis_cache_stats, module)?)?;
    module.add_function(wrap_pyfunction!(analysis::clear_analysis_cache, module)?)?;
    module.add_function(wrap_pyfunction!(analysis::analyze_file, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::analyze_mutations, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::build_mutation_report, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::render_mutation_report, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::read_mutation_cache, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::write_mutation_cache, module)?)?;
    module.add_function(wrap_pyfunction!(mutation::analyze_mutation_files, module)?)?;
    module.add_function(wrap_pyfunction!(semantic::analyze_semantic_source, module)?)?;
    module.add_function(wrap_pyfunction!(semantic::build_semantic_report, module)?)?;
    module.add_function(wrap_pyfunction!(semantic::render_semantic_text, module)?)?;
    module.add_function(wrap_pyfunction!(semantic::render_semantic_json, module)?)?;
    report::register(module)?;
    pytest_report::register(module)?;
    watch::register(module)?;
    Ok(())
}

#[cfg(test)]
mod tests;
