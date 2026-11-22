//! Supervisor state for one parallel run.

use crate::results::ParallelResultMode;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use rustix::process::Pid;
use std::borrow::Cow;
use std::path::PathBuf;
use std::process::{Child, ExitStatus};
use std::sync::Arc;
use std::time::Instant;
use tempfile::TempDir;

mod collection;
mod completion;
mod failure;
mod startup;
mod surplus;
mod wait;

use super::{
    CollectionErrorPolicy, CollectionRoute, ParallelChild, ParallelPlan,
    WorkerCollectionCompletion, WorkerCompletionRoute, WorkerProgress, cancellation, demand,
    independent, mutation,
};

struct StreamBoundaries {
    stdout_ends_with_newline: bool,
    stderr_ends_with_newline: bool,
}

pub(super) struct ParallelSupervisor<'a> {
    pub(in crate::parallel) directory: TempDir,
    pub(in crate::parallel) plan: Cow<'a, ParallelPlan>,
    pub(in crate::parallel) collection_route: CollectionRoute,
    result_mode: ParallelResultMode,
    collection_error_policy: CollectionErrorPolicy,
    completion_route: WorkerCompletionRoute,
    invocation_identity: Option<independent::BootstrapIdentity>,
    pub(in crate::parallel) fresh_execution: Option<&'a mutation::FreshExecution>,
    pub(in crate::parallel) fresh_inputs: Option<mutation::cache::FreshInputs>,
    pub(in crate::parallel) fresh_collection_nodeids: Option<Vec<String>>,
    pub(in crate::parallel) fresh_tests: Vec<String>,
    pub(in crate::parallel) fresh_deadline: Option<Instant>,
    pub(in crate::parallel) fresh_timed_out: bool,
    pub(in crate::parallel) fresh_abrupt_exit: Option<mutation::FreshAbruptExit>,
    /// Completions that passed validation, indexed by worker.
    pub(in crate::parallel) authenticated_completions: Vec<Option<WorkerCollectionCompletion>>,
    pub(in crate::parallel) completed_workers: Vec<Option<independent::ValidatedResult>>,
    pub(in crate::parallel) control_drain: Option<independent::ValidatedInterruption>,
    pub(in crate::parallel) cancellation: cancellation::Cancellation,
    pub(in crate::parallel) parent_interruption: Option<crate::results::ParentInterruption>,
    pub(in crate::parallel) checked_cleanup: Option<cancellation::CleanupOutcome>,
    pub(in crate::parallel) demand: Option<Arc<demand::DemandQueue>>,
    pub(in crate::parallel) children: Vec<Option<ParallelChild>>,
    pub(in crate::parallel) process_groups: Vec<Option<Pid>>,
    pub(in crate::parallel) worker_pids: Vec<Option<Pid>>,
    pub(in crate::parallel) statuses: Vec<Option<ExitStatus>>,
    pub(in crate::parallel) coverage_shards: Vec<PathBuf>,
    /// Workers started alongside worker zero, adopted once its collection fixes the width.
    prespawned: Vec<(usize, Pid, Child)>,
    /// Prespawned workers beyond the collected width, finishing their sessions without tests.
    surplus: Vec<(usize, Pid, Child)>,
    speculative_coverage_base: Option<String>,
    stream_boundaries: StreamBoundaries,
    pub(in crate::parallel) released: bool,
    complete: bool,
    progress_config: Option<&'a Py<PyAny>>,
    progress: Option<WorkerProgress>,
}

impl Drop for ParallelSupervisor<'_> {
    fn drop(&mut self) {
        self.discard_prespawned();
        if !self.complete {
            if self.cooperative_execution() {
                self.retry_execution_cleanup();
            } else if self.released {
                let _ = self.terminate_all();
            } else {
                self.abort("native parallel session did not complete");
            }
        }
    }
}
