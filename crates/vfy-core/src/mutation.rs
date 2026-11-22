//! Mutation discovery, scheduling, caching, and reporting.

mod baseline;
mod cache;
mod execution;
pub use execution::{FreshMutation, mutation_body_isolated};
mod publication;
pub use baseline::{mutation_baseline_valid, write_mutation_baseline};
pub use publication::{MutationOutput, MutationPublication};
mod identity;
pub use identity::{
    fresh_full_suite_mutation_fingerprints, mutation_collection_order, mutation_fingerprints,
};
mod report;
mod schedule;
mod sources;
pub use cache::{
    ExecutionOwner, MutationInput, MutationPhase, MutationResult, MutationStatus, OwnerKind,
    read_mutation_cache, write_mutation_cache,
};
pub use report::{build_mutation_report, render_mutation_report};
pub use schedule::{
    MutationJobKind, MutationTestIsolation, MutationTimeoutPolicy, MutationWorkerLease,
    collected_mutation_worker_limit, mutation_baseline_timeout, mutation_portable_worker_limit,
    mutation_test_context_group, mutation_timeout, next_mutation_job, order_mutation_jobs,
    plan_mutation_test_groups,
};
pub use sources::{
    ExecutedFiles, MutationScope, MutationScopeEvidence, MutationSources, mutation_contexts,
    mutation_executed_paths, mutation_file_digest, mutation_location, mutation_sources,
    mutation_suite_state, mutation_unattributed_lines, select_mutation_sources,
};

use std::fmt::Write;

use sha2::{Digest, Sha256};

/// Hash source bytes with the identity used by mutation discovery.
#[must_use]
pub fn source_hash(source: &str) -> String {
    let digest = Sha256::digest(source.as_bytes());
    let mut output = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(output, "{byte:02x}");
    }
    output
}

/// Fields that identify one source mutation across runs.
///
/// The key covers source and execution fields, so an edit to either invalidates
/// persisted results.
pub struct MutationIdentity<'a> {
    pub path: &'a str,
    pub digest: &'a str,
    pub kind: &'a str,
    pub operator: &'a str,
    pub start: u32,
    pub end: u32,
    pub replacement: &'a str,
}

impl MutationIdentity<'_> {
    /// Compute the stable persisted key for this identity.
    #[must_use]
    pub fn key(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.path.as_bytes());
        hasher.update(b"\0");
        hasher.update(self.digest.as_bytes());
        hasher.update(b"\0");
        hasher.update(self.kind.as_bytes());
        hasher.update(b"\0");
        hasher.update(self.operator.as_bytes());
        hasher.update(b"\0");
        hasher.update(self.start.to_le_bytes());
        hasher.update(self.end.to_le_bytes());
        hasher.update(b"\0");
        hasher.update(self.replacement.as_bytes());
        let digest = hasher.finalize();
        let mut output = String::with_capacity(digest.len() * 2 + 18);
        output.push_str("vfy:mutation:");
        for byte in digest {
            let _ = write!(output, "{byte:02x}");
        }
        output
    }
}

#[cfg(test)]
mod tests;
