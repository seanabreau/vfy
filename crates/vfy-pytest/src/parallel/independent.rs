//! Independent worker records name the run, worker, and purpose they belong to.
//!
//! The controller draws one nonce per invocation and passes it to each worker in
//! `NONCE_ENV`. Workers write it into every record they publish. The controller
//! reads records only from its private invocation directory, accepts only regular
//! files, and accepts a record only when its nonce, worker index, and purpose match.

use super::{
    MAX_PLAN_BYTES, NativeParallelWorker, ParallelPlan, WorkerCollectionCompletion,
    WorkerResultBundle, io_error, read_json, write_json_atomic,
};
use crate::independent::{Disposition, FailureOrigin, LifecycleStage};
use crate::results::{NativeRunResult, NativeRunResultTransport};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyWeakrefMethods, PyWeakrefReference};
use rustix::process::Pid;
#[cfg(target_os = "linux")]
use rustix::process::{Signal, getpid, getppid, kill_process, set_parent_process_death_signal};
use serde::{Deserialize, Serialize};
use std::fmt::Write as _;
use std::fs::{self, File};
use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use vfy_core::test_execution::RunRecord;

#[cfg(test)]
mod collection_tests;
#[cfg(test)]
mod fresh_tests;
#[cfg(test)]
mod status_tests;
#[cfg(test)]
mod tests;

const ENGINE: &str = "vfy-independent-v1";
/// Carries the run nonce from the controller into each worker process.
pub(crate) const NONCE_ENV: &str = "VFY_NATIVE_PARALLEL_NONCE";
#[cfg(target_os = "linux")]
pub(super) const PARENT_PID_ENV: &str = "VFY_NATIVE_PARALLEL_PARENT_PID";

#[cfg(target_os = "linux")]
fn bind_worker_parent() -> PyResult<()> {
    let expected = std::env::var(PARENT_PID_ENV)
        .ok()
        .and_then(|value| value.parse::<i32>().ok())
        .ok_or_else(|| {
            PyValueError::new_err("independent worker omitted its controller process identity")
        })?;
    bind_owned_parent(expected)
}

pub(crate) fn bind_owned_parent(expected: i32) -> PyResult<()> {
    let expected = Pid::from_raw(expected)
        .ok_or_else(|| PyValueError::new_err("invalid independent parent process identity"))?;
    #[cfg(target_os = "linux")]
    {
        // Owned processes cannot outlive their parent, even inside user code or
        // shutdown handlers. Recheck after arming to cover death before registration.
        set_parent_process_death_signal(Some(Signal::KILL)).map_err(io_error)?;
        if getppid() != Some(expected) {
            kill_process(getpid(), Signal::KILL).map_err(io_error)?;
            return Err(PyValueError::new_err(
                "independent parent exited before binding",
            ));
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = expected;
    Ok(())
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum InvocationPurpose {
    Parallel,
    FreshSerial,
    FreshCollection,
}

/// Final parser input retained by the actual native invocation, not public fields.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ConsumedArguments {
    Stable {
        arguments: Vec<String>,
        parser: vfy_core::pytest_config::PytestParserBehavior,
    },
    Unavailable,
}

/// Names the run, worker, and purpose that a worker record belongs to.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecordHeader {
    nonce: String,
    index: usize,
    purpose: InvocationPurpose,
}

impl RecordHeader {
    fn for_worker(identity: &BootstrapIdentity, index: usize) -> Self {
        Self {
            nonce: identity.nonce.clone(),
            index,
            purpose: identity.purpose,
        }
    }
}

/// An execution document: the worker bundle with its run header alongside.
#[derive(Serialize)]
struct ExecutionRecord<'a> {
    independent: RecordHeader,
    #[serde(flatten)]
    bundle: WorkerResultBundle<'a>,
}

/// A collection document: the run header wraps the transport record.
#[derive(Serialize)]
struct CollectionRecord<'a> {
    independent: RecordHeader,
    record: NativeRunResultTransport<'a>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    collection_warnings: Vec<vfy_core::test_execution::WarningRecord>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    finish_warnings: Vec<vfy_core::test_execution::WarningRecord>,
}

/// Enforces the one-worker width of the fresh invocation purposes.
fn single_worker(width: usize, what: &str) -> PyResult<()> {
    if width != 1 {
        return Err(PyValueError::new_err(format!("{what} requires one worker")));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct CollectionRecordOwned {
    independent: RecordHeader,
    pub(super) record: RunRecord,
    #[serde(default)]
    pub(super) collection_warnings: Vec<vfy_core::test_execution::WarningRecord>,
    #[serde(default)]
    pub(super) finish_warnings: Vec<vfy_core::test_execution::WarningRecord>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct BootstrapIdentity {
    engine: String,
    nonce: String,
    width: usize,
    purpose: InvocationPurpose,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    mutation: Option<vfy_core::mutation::MutationInput>,
}

impl BootstrapIdentity {
    pub(super) fn is_parallel(&self) -> bool {
        self.purpose == InvocationPurpose::Parallel
    }
    pub(super) fn permits_selected_collection(
        &self,
        collected: &[String],
        selected: &[String],
    ) -> bool {
        self.purpose == InvocationPurpose::FreshSerial
            && self.width == 1
            && vfy_core::mutation::mutation_collection_order(collected, selected).is_some()
    }

    pub(super) fn create(directory: &Path, width: usize) -> PyResult<Self> {
        Self::issue(directory, width, InvocationPurpose::Parallel, None)
    }

    pub(super) fn create_fresh(
        directory: &Path,
        width: usize,
        mutation: Option<&vfy_core::mutation::FreshMutation>,
    ) -> PyResult<Self> {
        single_worker(width, "fresh serial invocation")?;
        Self::issue(directory, width, InvocationPurpose::FreshSerial, mutation)
    }

    pub(super) fn create_fresh_collection(directory: &Path, width: usize) -> PyResult<Self> {
        single_worker(width, "fresh collection invocation")?;
        Self::issue(directory, width, InvocationPurpose::FreshCollection, None)
    }

    fn issue(
        directory: &Path,
        width: usize,
        purpose: InvocationPurpose,
        mutation: Option<&vfy_core::mutation::FreshMutation>,
    ) -> PyResult<Self> {
        let identity = Self {
            engine: ENGINE.to_owned(),
            nonce: run_nonce()?,
            width,
            purpose,
            mutation: mutation.map(|value| value.input().clone()),
        };
        write_json_atomic(&directory.join("invocation-bootstrap.json"), &identity)
            .map_err(io_error)?;
        Ok(identity)
    }

    /// The run nonce that workers receive in `NONCE_ENV`.
    pub(super) fn nonce(&self) -> &str {
        &self.nonce
    }

    /// Fresh invocations admit only the width-one plan their purpose defines.
    pub(super) fn check_plan(&self, plan: &ParallelPlan) -> PyResult<()> {
        if matches!(
            self.purpose,
            InvocationPurpose::FreshSerial | InvocationPurpose::FreshCollection
        ) && (self.width != 1
            || plan.assignments.len() != 1
            || (self.purpose == InvocationPurpose::FreshSerial && plan.maxfail != 1)
            || (self.purpose == InvocationPurpose::FreshCollection && plan.maxfail != 0))
        {
            return Err(PyValueError::new_err(
                "fresh serial assignment requires its width-one fail-fast plan",
            ));
        }
        Ok(())
    }
}

/// Draw the per-run nonce from the operating system's entropy source.
fn run_nonce() -> PyResult<String> {
    let mut bytes = [0_u8; 32];
    File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(io_error)?;
    Ok(bytes
        .iter()
        .fold(String::with_capacity(64), |mut hex, byte| {
            // Writing to a String cannot fail.
            let _ = write!(hex, "{byte:02x}");
            hex
        }))
}

#[derive(Clone)]
pub(crate) enum AssignedInvocation {
    Static(Vec<usize>),
    Demand(Arc<super::DemandQueue>),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct AssignmentRecord {
    indices: Vec<usize>,
}

#[derive(Clone)]
pub(crate) struct InvocationBinding {
    directory: PathBuf,
    index: usize,
    identity: BootstrapIdentity,
    pid: u32,
    assignment: Option<Arc<AssignedInvocation>>,
    mutation: Option<vfy_core::mutation::FreshMutation>,
    source_applied: bool,
    consumed_arguments: Option<ConsumedArguments>,
}

impl AssignedInvocation {
    pub(crate) fn is_demand(&self) -> bool {
        matches!(self, Self::Demand(_))
    }

    pub(crate) fn test_id(&self, local: usize) -> PyResult<Option<vfy_core::test_plan::TestId>> {
        let index = match self {
            Self::Static(indices) => indices.get(local).copied(),
            Self::Demand(queue) => queue.owns(local)?.then_some(local),
        };
        Ok(index.map(vfy_core::test_plan::TestId::from_index))
    }

    pub(crate) fn indices(&self) -> &[usize] {
        match self {
            Self::Static(indices) => indices,
            Self::Demand(_) => &[],
        }
    }

    pub(crate) fn claimed_indices(&self) -> PyResult<Vec<usize>> {
        match self {
            Self::Static(indices) => Ok(indices.clone()),
            Self::Demand(queue) => queue.owned_indices(),
        }
    }
}

impl InvocationBinding {
    pub(crate) fn is_surplus(&self) -> bool {
        self.assignment.is_none() && super::surplus_path(&self.directory, self.index).is_file()
    }

    pub(crate) fn validate_collection_binding(
        &self,
        directory: &Path,
        index: usize,
        width: usize,
    ) -> PyResult<()> {
        let current = read_identity(directory)?.ok_or_else(|| {
            PyValueError::new_err("missing independent collection invocation identity")
        })?;
        let valid_width = match self.identity.purpose {
            InvocationPurpose::Parallel => {
                (2..=super::MAX_PARALLEL_WORKERS).contains(&width) && index < width
            }
            InvocationPurpose::FreshSerial | InvocationPurpose::FreshCollection => {
                width == 1 && index == 0
            }
        };
        if !valid_width
            || self.directory != directory
            || self.index != index
            || self.pid != std::process::id()
            || self.assignment.is_some()
            || self.identity.width != width
            || current.engine != self.identity.engine
            || current.nonce != self.identity.nonce
            || current.width != self.identity.width
            || current.purpose != self.identity.purpose
        {
            return Err(PyValueError::new_err(
                "worker collection has no matching native invocation binding",
            ));
        }
        Ok(())
    }

    pub(crate) fn purpose(&self) -> InvocationPurpose {
        self.identity.purpose
    }

    pub(crate) fn assignment(&self) -> Option<&Arc<AssignedInvocation>> {
        self.assignment.as_ref()
    }

    /// Bind a worker to the invocation named by its bootstrap and the run nonce in its environment.
    pub(crate) fn read(_py: Python<'_>, directory: &str, index: usize) -> PyResult<Self> {
        let directory = PathBuf::from(directory);
        let mut identity = read_identity(&directory)?
            .ok_or_else(|| PyValueError::new_err("missing independent invocation bootstrap"))?;
        let nonce = std::env::var(NONCE_ENV)
            .map_err(|_| PyValueError::new_err("missing independent invocation nonce"))?;
        if identity.engine != ENGINE
            || nonce.is_empty()
            || identity.nonce != nonce
            || index >= identity.width
            || (matches!(
                identity.purpose,
                InvocationPurpose::FreshSerial | InvocationPurpose::FreshCollection
            ) && (identity.width != 1 || index != 0))
            || (identity.purpose == InvocationPurpose::FreshCollection
                && identity.mutation.is_some())
        {
            return Err(PyValueError::new_err(
                "invalid independent invocation bootstrap",
            ));
        }
        let mutation = identity
            .mutation
            .take()
            .map(vfy_core::mutation::FreshMutation::new)
            .transpose()
            .map_err(PyValueError::new_err)?;
        #[cfg(target_os = "linux")]
        bind_worker_parent()?;
        Ok(Self {
            directory,
            index,
            identity,
            pid: std::process::id(),
            assignment: None,
            mutation,
            source_applied: false,
            consumed_arguments: None,
        })
    }

    pub(crate) fn with_consumed_arguments(mut self, arguments: Option<ConsumedArguments>) -> Self {
        self.consumed_arguments = arguments;
        self
    }

    pub(crate) fn mutation_input(&self) -> PyResult<Option<String>> {
        self.mutation
            .as_ref()
            .map(|value| {
                serde_json::to_string(value.input())
                    .map_err(|error| PyValueError::new_err(error.to_string()))
            })
            .transpose()
    }

    pub(crate) fn patch_mutation_source(&mut self, path: &str, source: &[u8]) -> PyResult<Vec<u8>> {
        use std::os::unix::fs::MetadataExt as _;
        let mutation = self
            .mutation
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("invocation has no mutation input"))?;
        let actual = std::fs::metadata(path).map_err(io_error)?;
        let expected = std::fs::metadata(&mutation.input().path).map_err(io_error)?;
        if (actual.dev(), actual.ino()) != (expected.dev(), expected.ino()) {
            return Err(PyValueError::new_err(
                "mutation loader belongs to a different source file",
            ));
        }
        let patched = mutation.patch(source).map_err(PyValueError::new_err)?;
        self.source_applied = true;
        Ok(patched)
    }

    fn mutation_observation(&self) -> Option<MutationObservation> {
        self.mutation.as_ref().map(|value| MutationObservation {
            id: value.input().id.clone(),
            applied: self.source_applied,
        })
    }

    pub(crate) fn assign(&mut self, worker: &NativeParallelWorker) -> PyResult<()> {
        if worker.directory != self.directory
            || worker.index != self.index
            || self.assignment.is_some()
            || self.pid != std::process::id()
        {
            return Err(PyValueError::new_err(
                "worker assignment belongs to a different invocation",
            ));
        }
        self.assignment = Some(Arc::new(match &worker.demand {
            Some(queue) => AssignedInvocation::Demand(Arc::clone(queue)),
            None => AssignedInvocation::Static(worker.indices.clone()),
        }));
        Ok(())
    }

    fn validate_assigned_result(
        &self,
        result: &NativeRunResult,
        assignment: &AssignedInvocation,
        invocation: &InvocationCompletion,
    ) -> PyResult<()> {
        let indices = assignment.claimed_indices()?;
        if result.record().cases.len() > indices.len() {
            return Err(PyValueError::new_err(
                "native worker result exceeds its actual case assignment",
            ));
        }
        for (case, expected) in result.record().cases.iter().zip(&indices) {
            if case.test_id().map(vfy_core::test_plan::TestId::index) != Some(*expected) {
                return Err(PyValueError::new_err(
                    "native worker result has no matching actual case source index",
                ));
            }
        }
        invocation.validate_fresh_prefix(&self.identity, result.record().cases.len(), indices.len())
    }

    pub(crate) fn result(
        &self,
        result: &mut NativeRunResult,
        worker: Option<&NativeParallelWorker>,
        stage: LifecycleStage,
        disposition: Disposition,
        origin: FailureOrigin,
    ) -> PyResult<i32> {
        let Disposition::Return(status) = disposition else {
            return Err(PyValueError::new_err(
                "worker result has no native return status",
            ));
        };
        let session_exitstatus = result
            .record()
            .exitstatus
            .ok_or_else(|| PyValueError::new_err("worker result has no native session status"))?;
        if result.record().engine != ENGINE
            || self.pid != std::process::id()
            || (origin == FailureOrigin::SessionFailure && worker.is_none())
        {
            return Err(PyValueError::new_err(
                "worker result disagrees with its native invocation",
            ));
        }
        let invocation = InvocationCompletion {
            session_exitstatus: Some(session_exitstatus),
            consumed_arguments: self.consumed_arguments.clone(),
            ..self.completion(InvocationOutcome::Result, stage, disposition, origin)?
        };
        match (worker, &self.assignment) {
            (Some(worker), Some(assignment))
                if worker.directory == self.directory
                    && worker.index == self.index
                    && match (&worker.demand, assignment.as_ref()) {
                        (Some(queue), AssignedInvocation::Demand(owned)) => {
                            Arc::ptr_eq(queue, owned)
                        }
                        (None, AssignedInvocation::Static(indices)) => worker.indices == *indices,
                        _ => false,
                    } =>
            {
                if assignment.is_demand() {
                    result.project_independent_claims(&assignment.claimed_indices()?)?;
                }
                self.validate_assigned_result(result, assignment, &invocation)?;
                let bundle = ExecutionRecord {
                    independent: RecordHeader::for_worker(&self.identity, self.index),
                    bundle: worker.result_bundle(
                        result,
                        None,
                        Some(result.worker_metadata_records()),
                        None,
                        None,
                    )?,
                };
                write_json_atomic(&worker.result_path(), &bundle).map_err(io_error)?;
            }
            (None, None) if result.record().cases.is_empty() => {
                let (collection_warnings, finish_warnings) =
                    result.worker_session_warning_records();
                let record = CollectionRecord {
                    independent: RecordHeader::for_worker(&self.identity, self.index),
                    record: result.transport(),
                    collection_warnings,
                    finish_warnings,
                };
                write_json_atomic(
                    &self
                        .directory
                        .join(format!("collection-result.{}.json", self.index)),
                    &record,
                )
                .map_err(io_error)?;
            }
            _ => {
                return Err(PyValueError::new_err(
                    "worker result has no matching native assignment",
                ));
            }
        }
        self.publish_completion(status, invocation)?;
        Ok(status)
    }

    pub(crate) fn failure(
        &self,
        stage: LifecycleStage,
        disposition: Disposition,
        origin: FailureOrigin,
    ) -> PyResult<i32> {
        let Disposition::Return(status) = disposition else {
            return Err(PyValueError::new_err(
                "unhandled worker errors cannot publish an owned return",
            ));
        };
        if self.pid != std::process::id()
            || matches!(
                origin,
                FailureOrigin::Unhandled | FailureOrigin::SessionFailure
            )
        {
            return Err(PyValueError::new_err(
                "worker invocation is not owned by this process",
            ));
        }
        let invocation = self.completion(InvocationOutcome::Failure, stage, disposition, origin)?;
        self.publish_completion(status, invocation)?;
        Ok(status)
    }

    fn completion(
        &self,
        outcome: InvocationOutcome,
        stage: LifecycleStage,
        disposition: Disposition,
        origin: FailureOrigin,
    ) -> PyResult<InvocationCompletion> {
        Ok(InvocationCompletion {
            nonce: self.identity.nonce.clone(),
            purpose: self.identity.purpose,
            pid: self.pid,
            index: self.index,
            stage,
            disposition,
            origin,
            outcome,
            session_exitstatus: None,
            assignment: self
                .assignment
                .as_ref()
                .map(|assignment| {
                    assignment
                        .claimed_indices()
                        .map(|indices| AssignmentRecord { indices })
                })
                .transpose()?,
            mutation: self.mutation_observation(),
            consumed_arguments: None,
        })
    }

    fn publish_completion(&self, status: i32, invocation: InvocationCompletion) -> PyResult<()> {
        WorkerCollectionCompletion {
            index: self.index,
            returncode: i64::from(status),
            infrastructure_failed: false,
            warnings: Vec::new(),
            exit_reason: None,
            invocation: Some(invocation),
        }
        .publish(&self.directory)
    }

    pub(crate) fn begin_publication(&self) -> PyResult<()> {
        if self.pid != std::process::id() {
            return Err(PyValueError::new_err(
                "worker invocation is not owned by this process",
            ));
        }
        // This rejection-only envelope is replaced by the validated completion.
        // Failed publication must not look like abrupt death inside a mutant body.
        WorkerCollectionCompletion {
            index: self.index,
            returncode: 3,
            infrastructure_failed: true,
            warnings: Vec::new(),
            exit_reason: None,
            invocation: Some(self.completion(
                InvocationOutcome::Failure,
                LifecycleStage::Materializing,
                Disposition::Propagate,
                FailureOrigin::Unhandled,
            )?),
        }
        .publish(&self.directory)
    }

    pub(crate) fn invalidate_completion(&self) -> PyResult<()> {
        if self.pid != std::process::id() {
            return Err(PyValueError::new_err(
                "worker invocation is not owned by this process",
            ));
        }
        match std::fs::remove_file(
            self.directory
                .join(format!("collection-finish.{}.json", self.index)),
        ) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(io_error(error)),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum InvocationOutcome {
    Failure,
    Result,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct MutationObservation {
    id: String,
    applied: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct InvocationCompletion {
    nonce: String,
    purpose: InvocationPurpose,
    pid: u32,
    index: usize,
    stage: LifecycleStage,
    disposition: Disposition,
    origin: FailureOrigin,
    outcome: InvocationOutcome,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    session_exitstatus: Option<i32>,
    assignment: Option<AssignmentRecord>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    mutation: Option<MutationObservation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    consumed_arguments: Option<ConsumedArguments>,
}

/// Which result channel a worker published. Both channels present is an error.
#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum PublishedResult {
    Missing,
    Execution,
    Collection,
}

impl PublishedResult {
    pub(super) fn read(directory: &Path, index: usize) -> PyResult<Self> {
        match (
            is_record(&directory.join(format!("result.{index}.json"))),
            is_record(&directory.join(format!("collection-result.{index}.json"))),
        ) {
            (false, false) => Ok(Self::Missing),
            (true, false) => Ok(Self::Execution),
            (false, true) => Ok(Self::Collection),
            (true, true) => Err(PyValueError::new_err(
                "native worker published conflicting result channels",
            )),
        }
    }
}

/// Only a regular file inside the invocation directory is a record; symlinks are not.
fn is_record(path: &Path) -> bool {
    path.symlink_metadata()
        .is_ok_and(|metadata| metadata.file_type().is_file())
}

/// Constructed only after the supervisor validates an owned Running control.
#[derive(Clone, Copy)]
pub(crate) struct ValidatedInterruption {
    status: i32,
    origin: FailureOrigin,
}
impl ValidatedInterruption {
    pub(super) fn status(self) -> i32 {
        self.status
    }

    pub(super) fn failure(self) -> ValidatedFailure {
        ValidatedFailure {
            stage: LifecycleStage::Running,
            disposition: Disposition::Return(self.status),
            origin: self.origin,
        }
    }
}

#[derive(Clone, Copy)]
pub(crate) struct ValidatedFailure {
    pub(crate) stage: LifecycleStage,
    pub(crate) disposition: Disposition,
    pub(crate) origin: FailureOrigin,
}

impl ValidatedFailure {
    pub(super) fn stage_name(self) -> &'static str {
        self.stage.name()
    }
}

/// The payload names its exception weakly, so it forms no reference cycle and needs no GC visitor.
#[pyclass(frozen, unsendable, weakref)]
struct NativeValidatedFailure {
    failure: ValidatedFailure,
    error: Py<PyWeakrefReference>,
}

pub(super) fn attach_failure(
    py: Python<'_>,
    error: &PyErr,
    failure: ValidatedFailure,
) -> PyResult<()> {
    let value = error.value(py);
    let owner = PyWeakrefReference::new(value.as_any())?.unbind();
    let payload = Py::new(
        py,
        NativeValidatedFailure {
            failure,
            error: owner,
        },
    )?;
    value.setattr("_vfy_invocation_failure", payload)
}

pub(crate) fn validated_failure(py: Python<'_>, error: &PyErr) -> Option<ValidatedFailure> {
    if !error.is_instance_of::<crate::NativeParallelError>(py) {
        return None;
    }
    let payload = error.value(py).getattr("_vfy_invocation_failure").ok()?;
    let payload = payload.cast::<NativeValidatedFailure>().ok()?.borrow();
    payload
        .error
        .bind(py)
        .upgrade()?
        .is(error.value(py))
        .then_some(payload.failure)
}

#[derive(Clone, Copy)]
pub(crate) struct ValidatedResult {
    index: usize,
    command_status: i32,
    session_status: i32,
    interruption: Option<ValidatedInterruption>,
    delivered_interruption: Option<super::cancellation::DeliveredInterruption>,
}

impl ValidatedResult {
    pub(super) fn with_delivered_interruption(
        mut self,
        interruption: super::cancellation::DeliveredInterruption,
    ) -> Self {
        self.delivered_interruption = Some(interruption);
        self
    }

    pub(crate) fn interruption(self) -> Option<ValidatedInterruption> {
        self.interruption
    }

    pub(crate) fn validate_record(
        self,
        index: usize,
        engine: &str,
        session_status: Option<i32>,
        process_status: Option<i32>,
    ) -> PyResult<()> {
        if self.index != index
            || engine != ENGINE
            || session_status != Some(self.session_status)
            || !(process_status == Some(self.command_status.rem_euclid(256))
                || (process_status.is_none()
                    && self
                        .delivered_interruption
                        .is_some_and(|interruption| interruption.worker() == index)))
        {
            return Err(PyValueError::new_err(
                "worker result disagrees with its validated command/session completion",
            ));
        }
        Ok(())
    }
}

pub(super) enum ValidatedCompletion {
    Failure(ValidatedFailure, i32),
    Result(ValidatedResult),
}

/// What the controller expects a worker's completion to describe.
#[derive(Clone, Copy)]
pub(super) struct Expectation<'a> {
    pub(super) identity: &'a BootstrapIdentity,
    pub(super) index: usize,
    pub(super) pid: u32,
    /// The worker's planned indices, or `None` before the plan is final.
    pub(super) assigned: Option<&'a [usize]>,
}

impl WorkerCollectionCompletion {
    /// Accept an execution document only when it names this run and worker, and its
    /// session status and prefix agree with the completion.
    pub(super) fn validate_fresh_result(
        &self,
        identity: &BootstrapIdentity,
        record: &str,
        assigned: usize,
    ) -> PyResult<crate::results::WorkerResultSummary> {
        let summary = crate::results::worker_result_summary(record)?;
        if summary.engine != ENGINE {
            return Err(PyValueError::new_err(
                "independent worker result has another engine",
            ));
        }
        if summary.independent.as_ref() != Some(&RecordHeader::for_worker(identity, self.index)) {
            return Err(PyValueError::new_err(
                "independent worker result belongs to another invocation",
            ));
        }
        let invocation = self.invocation.as_ref().ok_or_else(|| {
            PyValueError::new_err("independent worker result has no invocation completion")
        })?;
        invocation.validate_session_status(summary.exitstatus)?;
        invocation.validate_fresh_prefix(identity, summary.completed, assigned)?;
        Ok(summary)
    }
}

impl InvocationCompletion {
    pub(super) fn consumed_arguments(&self) -> Option<&ConsumedArguments> {
        self.consumed_arguments.as_ref()
    }

    /// Accept a worker completion only when it names this run, worker, and process,
    /// and its outcome agrees with the process status and the published channel.
    pub(super) fn validate(
        &self,
        expected: Expectation<'_>,
        published: PublishedResult,
        status: i64,
    ) -> PyResult<ValidatedCompletion> {
        match (&self.mutation, &expected.identity.mutation) {
            (None, None) => (),
            (Some(observed), Some(input)) if observed.id == input.id => (),
            _ => {
                return Err(PyValueError::new_err(
                    "native mutation observation disagrees with its issued input",
                ));
            }
        }
        if self.nonce != expected.identity.nonce
            || self.purpose != expected.identity.purpose
            || self.index != expected.index
            || self.pid != expected.pid
        {
            return Err(PyValueError::new_err(
                "independent worker completion does not belong to this invocation",
            ));
        }
        let status = i32::try_from(status)
            .map_err(|_| PyValueError::new_err("invalid worker completion status"))?;
        // A planned worker's assignment must equal the adopted plan; an unplanned
        // completion carries no assignment.
        let assignment_agrees = match (&self.assignment, expected.assigned) {
            (Some(assigned), Some(indices)) => assigned.indices == indices,
            (Some(_), None) => false,
            (None, _) => true,
        };
        match self.outcome {
            InvocationOutcome::Failure => {
                let Disposition::Return(declared) = self.disposition else {
                    return Err(PyValueError::new_err(
                        "worker completion declared propagation as a return",
                    ));
                };
                if declared != status
                    || self.session_exitstatus.is_some()
                    || matches!(
                        self.origin,
                        FailureOrigin::Unhandled | FailureOrigin::SessionFailure
                    )
                    || published != PublishedResult::Missing
                    || !assignment_agrees
                {
                    return Err(PyValueError::new_err(
                        "independent worker failure completion disagrees with its native process",
                    ));
                }
                self.validate_fixture_cleanup(expected.identity.purpose, declared)?;
                let failure = ValidatedFailure {
                    stage: self.stage,
                    disposition: self.disposition,
                    origin: self.origin,
                };
                Ok(ValidatedCompletion::Failure(failure, declared))
            }
            InvocationOutcome::Result => {
                let channel_agrees = match published {
                    PublishedResult::Execution => self.assignment.is_some() && assignment_agrees,
                    PublishedResult::Collection => self.assignment.is_none(),
                    PublishedResult::Missing => false,
                };
                if self.disposition != Disposition::Return(status)
                    || !channel_agrees
                    || self.origin == FailureOrigin::FixtureCleanup
                {
                    return Err(PyValueError::new_err(
                        "independent worker result disagrees with its native process",
                    ));
                }
                if self.origin == FailureOrigin::SessionFailure
                    && (expected.identity.purpose == InvocationPurpose::FreshCollection
                        || self.stage != LifecycleStage::Running
                        || published != PublishedResult::Execution)
                {
                    return Err(PyValueError::new_err(
                        "worker session failure has no bound execution",
                    ));
                }
                let session_status = self.session_exitstatus.ok_or_else(|| {
                    PyValueError::new_err("worker result omitted its native session status")
                })?;
                Ok(ValidatedCompletion::Result(ValidatedResult {
                    index: self.index,
                    command_status: status,
                    session_status,
                    delivered_interruption: None,
                    interruption: (self.stage == LifecycleStage::Running
                        && matches!(self.origin, FailureOrigin::Control(_)))
                    .then_some(ValidatedInterruption {
                        status: session_status,
                        origin: self.origin,
                    }),
                }))
            }
        }
    }

    fn validate_fixture_cleanup(&self, purpose: InvocationPurpose, declared: i32) -> PyResult<()> {
        if self.origin == FailureOrigin::FixtureCleanup
            && (declared != 1
                || self.stage != LifecycleStage::Finishing
                || self.assignment.is_none()
                || purpose == InvocationPurpose::FreshCollection)
        {
            return Err(PyValueError::new_err(
                "fixture cleanup failure has no bound native finalization",
            ));
        }
        Ok(())
    }

    pub(super) fn mutation_applied(
        &self,
        input: &vfy_core::mutation::MutationInput,
    ) -> PyResult<bool> {
        match &self.mutation {
            Some(observed) if observed.id == input.id => Ok(observed.applied),
            _ => Err(PyValueError::new_err(
                "fresh mutation completion identity disagrees with its input",
            )),
        }
    }

    fn validate_session_status(&self, session_status: Option<i32>) -> PyResult<()> {
        if self.outcome != InvocationOutcome::Result
            || self.session_exitstatus.is_none()
            || self.session_exitstatus != session_status
        {
            return Err(PyValueError::new_err(
                "worker session result disagrees with its validated completion",
            ));
        }
        Ok(())
    }

    fn validate_fresh_prefix(
        &self,
        identity: &BootstrapIdentity,
        completed: usize,
        assigned: usize,
    ) -> PyResult<()> {
        if identity.purpose == InvocationPurpose::FreshCollection {
            if self.purpose != identity.purpose
                || identity.width != 1
                || self.index != 0
                || completed != 0
                || self.mutation.is_some()
                || self.consumed_arguments.is_none()
                || self.stage != LifecycleStage::Finished
            {
                return Err(PyValueError::new_err(
                    "fresh collection result has no validated collect-only completion",
                ));
            }
            return Ok(());
        }
        if identity.purpose != InvocationPurpose::FreshSerial {
            return Ok(());
        }
        if self.purpose != identity.purpose
            || identity.width != 1
            || self.index != 0
            || completed > assigned
            || (completed < assigned
                && !(self.stage == LifecycleStage::Running
                    && matches!(
                        self.origin,
                        FailureOrigin::SessionFailure | FailureOrigin::Control(_)
                    )))
        {
            return Err(PyValueError::new_err(
                "fresh serial result omitted assigned cases without its original native stop",
            ));
        }
        Ok(())
    }
}

/// Parse a collection record and check that it belongs to this run and worker.
pub(super) fn read_collection_record(
    directory: &Path,
    index: usize,
    identity: &BootstrapIdentity,
    completion: &WorkerCollectionCompletion,
) -> PyResult<RunRecord> {
    Ok(read_collection_document(directory, index, identity, completion)?.record)
}

pub(super) fn read_collection_document(
    directory: &Path,
    index: usize,
    identity: &BootstrapIdentity,
    completion: &WorkerCollectionCompletion,
) -> PyResult<CollectionRecordOwned> {
    let path = directory.join(format!("collection-result.{index}.json"));
    let metadata = path.symlink_metadata().map_err(io_error)?;
    if !metadata.file_type().is_file() {
        return Err(PyValueError::new_err(
            "independent collection result is not a regular file",
        ));
    }
    if metadata.len() > MAX_PLAN_BYTES {
        return Err(PyValueError::new_err(
            "independent collection result exceeds the supported size",
        ));
    }
    let bytes = fs::read_to_string(&path).map_err(io_error)?;
    let record: CollectionRecordOwned = serde_json::from_str(&bytes).map_err(io_error)?;
    if record.independent != RecordHeader::for_worker(identity, index) {
        return Err(PyValueError::new_err(
            "independent collection result belongs to another invocation",
        ));
    }
    vfy_core::test_execution::validate_run_record(&record.record)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    crate::results::validate_worker_observations(record.record.observations.as_ref())?;
    if record.record.engine != ENGINE || !record.record.cases.is_empty() {
        return Err(PyValueError::new_err(
            "invalid independent collection result",
        ));
    }
    completion
        .invocation
        .as_ref()
        .ok_or_else(|| {
            PyValueError::new_err("native collection result has no invocation completion")
        })?
        .validate_session_status(record.record.exitstatus)?;
    Ok(record)
}

pub(super) fn read_identity(directory: &Path) -> PyResult<Option<BootstrapIdentity>> {
    let path = directory.join("invocation-bootstrap.json");
    match path.metadata() {
        Ok(metadata) if metadata.len() > MAX_PLAN_BYTES => Err(PyValueError::new_err(
            "oversized native invocation identity",
        )),
        Ok(_) => read_json(&path).map(Some).map_err(io_error),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(io_error(error)),
    }
}
