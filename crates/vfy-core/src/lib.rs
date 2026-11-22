//! Coverage data, source analysis, reports, and test execution models.

#![cfg_attr(
    test,
    allow(
        clippy::disallowed_methods,
        reason = "tests write fixture inputs directly; atomic replacement is a production invariant"
    )
)]

pub mod analysis;
pub mod assertion_diagnostics;
mod asyncio;
mod cache;
#[cfg(unix)]
mod capture;
mod collection_identity;
mod config;
pub mod data;
mod error;
mod evidence;
mod fixture_cache;
mod html;
pub mod independent_collection;
pub mod mutation;
mod output;
mod path;
pub mod pytest_config;
mod pytest_doctor;
mod pytest_report;
pub mod report;
mod runtime;
pub mod selection;
pub mod semantic;
mod source;
pub mod terminal_separator;
pub mod test_cli;
pub mod test_diagnostics;
pub mod test_execution;
pub mod test_invocation;
pub mod test_plan;
mod trace;
mod watch;
mod workers;

pub use asyncio::{
    AsyncioLoopScope, AsyncioMode, plan_asyncio_loop_scope, validate_asyncio_config,
};
pub use cache::{CacheCleanup, clean_cache};
#[cfg(unix)]
pub use capture::CaptureReader;
pub use collection_identity::{CollectionNodeId, FixtureOwner};
pub use config::resolve_cli_options;

/// The largest worker count `vfy test` and `vfy mutate` accept.
pub const MAX_WORKERS: u32 = 256;
/// The accepted worker values, worded for diagnostics.
pub const WORKERS_EXPECTED: &str = "auto, logical, or an integer from 1 to 256";
pub use data::encode_lines;
pub use error::CoreError;
pub use fixture_cache::{
    FixtureCacheIdentity, FixtureCacheKey, FixtureGeneration, FixtureId, FixtureScope,
};
pub use mutation::{
    MutationIdentity, build_mutation_report, mutation_baseline_valid, mutation_contexts,
    mutation_file_digest, mutation_fingerprints, mutation_sources, mutation_suite_state,
    mutation_timeout, read_mutation_cache, render_mutation_report, select_mutation_sources,
    source_hash, write_mutation_baseline, write_mutation_cache,
};
pub use output::{atomic_write, file_digest, python_compact_json, python_json};
pub use path::{
    CoverageGlobSet, PathPatternError, UnicodeCaseVersion, absolute_path, canonical_path,
    discover_python_source_files, glob_path, glob_set, glob_set_with_unicode_version,
    relative_location, relative_path, strip_verbatim_prefix,
};
pub use pytest_config::{
    PytestConfigError, PytestConfigPolicy, PytestConfigProjection, resolve_pytest_configuration,
};
pub use pytest_doctor::{
    ConftestFinding, ConftestFindingKind, ConftestScan, SUPPORTED_CONFTEST_HOOKS,
    native_plugin_hook_supported, native_plugin_module_supported, scan_conftests,
};
pub use pytest_report::{
    PytestHtmlExtra, PytestHtmlReport, native_pytest_html_results_json, write_pytest_html_report,
};
pub use runtime::{PythonVersion, RequestedCore, RuntimePolicy};
pub use selection::{TestHistory, read_test_history, replay_fingerprint, write_test_history};
pub use selection::{green_test_run_valid, write_green_test_run};
pub use semantic::{
    DecisionDefinition, Logic, LogicParseError, PathDecisionDefinition, PathDecisionKind,
    PathObservation, PathOutcomeDefinition, PathOutcomeRole, PathReportRecord, SemanticData,
    SemanticEvidenceRecord, SemanticObservation, SemanticReportRecord, SemanticValidationError,
    analyze_semantic_source_data, mcdc_indices, parse_logic, parse_vector, render_vector,
    valid_observation,
};
pub use source::{SourceLocator, read_python_source};
pub use trace::{TraceLines, TraceOptions, TraceStore};
pub use watch::{
    RunCurrency, RunOutcome, WatchBatch, WatchRun, WatchSession, WatchState, generated_path,
    source_tree_identity,
};
pub use workers::recommended_worker_count;
