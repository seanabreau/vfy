use super::*;
use vfy_core::test_execution::{
    CaseResult, Counts, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, RunRecord,
};

pub(super) fn worker_result(count: usize) -> NativeRunResult {
    let cases = (0..count)
        .map(|index| {
            let phases: Vec<_> = [
                ExecutionPhase::Setup,
                ExecutionPhase::Call,
                ExecutionPhase::Teardown,
            ]
            .into_iter()
            .map(|phase| {
                PhaseResult::new(
                    if index == 0 && phase == ExecutionPhase::Call {
                        ExecutionOutcome::Failed
                    } else {
                        ExecutionOutcome::Passed
                    },
                    phase,
                    0.125,
                    PhasePayload::default(),
                )
            })
            .collect();
            let mut case = CaseResult::new(format!("test_body.py::test_{index}"), 0.375, phases);
            case.set_test_id(vfy_core::test_plan::TestId::from_index(index));
            case
        })
        .collect();
    crate::results::native_run_result(
        RunRecord {
            internal_errors: Vec::new(),
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
            engine: ENGINE.to_owned(),
            observations: None,
            exitstatus: Some(1),
            execution_identity: None,
            collected: count.max(2),
            counts: Counts {
                failed: usize::from(count > 0),
                passed: count.saturating_sub(1),
                ..Counts::default()
            },
            duration_ms: 0.375,
            warnings: Vec::new(),
            cases,
        },
        None,
    )
}

/// The execution document worker `index` publishes for `result`, with its run header.
pub(super) fn execution_document(
    result: &NativeRunResult,
    identity: &BootstrapIdentity,
    index: usize,
) -> String {
    let record = ExecutionRecord {
        independent: RecordHeader::for_worker(identity, index),
        bundle: super::super::WorkerResultBundle {
            result: result.transport(),
            junit_properties: super::super::WorkerJUnitProperties::default(),
            metadata: Vec::new(),
            collection_warnings: Vec::new(),
            finish_warnings: Vec::new(),
        },
    };
    let mut bytes = serde_json::to_string(&record).expect("actual worker document bytes");
    bytes.push('\n');
    bytes
}

/// A fresh serial invocation's identity and a completion for worker zero, assigned cases 0 and 1.
pub(super) fn issued() -> (BootstrapIdentity, InvocationCompletion) {
    let directory = tempfile::tempdir().expect("native issue directory");
    let identity = BootstrapIdentity::create_fresh(directory.path(), 1, None)
        .expect("actual fresh baseline identity");
    let completion = InvocationCompletion {
        consumed_arguments: None,
        nonce: identity.nonce.clone(),
        purpose: identity.purpose,
        pid: 1234,
        index: 0,
        stage: LifecycleStage::Running,
        disposition: Disposition::Return(1),
        origin: FailureOrigin::SessionFailure,
        outcome: InvocationOutcome::Result,
        session_exitstatus: Some(1),
        assignment: Some(AssignmentRecord {
            indices: vec![0, 1],
        }),
        mutation: None,
    };
    (identity, completion)
}

fn assigned_expectation<'a>(
    identity: &'a BootstrapIdentity,
    assigned: &'a [usize],
) -> Expectation<'a> {
    Expectation {
        identity,
        index: 0,
        pid: 1234,
        assigned: Some(assigned),
    }
}

fn result_of(
    completion: &InvocationCompletion,
    expected: Expectation<'_>,
    published: PublishedResult,
    status: i64,
) -> PyResult<ValidatedResult> {
    match completion.validate(expected, published, status)? {
        ValidatedCompletion::Result(result) => Ok(result),
        ValidatedCompletion::Failure(..) => Err(PyValueError::new_err("expected a result")),
    }
}

#[test]
fn fresh_baseline_identity_requires_actual_width_one_and_fail_fast_assignment() {
    let directory = tempfile::tempdir().expect("native bootstrap directory");
    assert!(BootstrapIdentity::create_fresh(directory.path(), 2, None).is_err());
    let identity = BootstrapIdentity::create_fresh(directory.path(), 1, None)
        .expect("unmutated baseline is still fresh serial");
    let retained = read_identity(directory.path())
        .expect("read identity")
        .expect("issued identity");
    assert_eq!(retained.purpose, InvocationPurpose::FreshSerial);
    let parallel = BootstrapIdentity::create(directory.path(), 1).expect("ordinary worker");
    assert_eq!(parallel.purpose, InvocationPurpose::Parallel);
    let mut plan = ParallelPlan {
        scheduling: super::super::ParallelScheduling::File,
        nodeids: vec!["test_body.py::test_first".into()],
        assignments: vec![vec![0]],
        maxfail: 0,
        initial_failures: 0,
    };
    assert!(identity.check_plan(&plan).is_err());
    plan.maxfail = 1;
    assert!(identity.check_plan(&plan).is_ok());
    plan.assignments.push(Vec::new());
    assert!(identity.check_plan(&plan).is_err());
}

fn collection_witness() -> (BootstrapIdentity, InvocationCompletion) {
    let directory = tempfile::tempdir().expect("actual fresh collection metadata");
    let identity = BootstrapIdentity::create_fresh_collection(directory.path(), 1)
        .expect("native fresh collection identity");
    let completion = InvocationCompletion {
        nonce: identity.nonce.clone(),
        purpose: identity.purpose,
        pid: 1234,
        index: 0,
        stage: LifecycleStage::Finished,
        disposition: Disposition::Return(0),
        origin: FailureOrigin::Unhandled,
        outcome: InvocationOutcome::Result,
        session_exitstatus: Some(0),
        assignment: Some(AssignmentRecord {
            indices: vec![0, 1],
        }),
        mutation: None,
        consumed_arguments: Some(ConsumedArguments::Stable {
            arguments: vec!["--collect-only".to_owned()],
            parser: vfy_core::pytest_config::PytestParserBehavior::Current,
        }),
    };
    (identity, completion)
}

#[test]
fn fresh_collection_completion_preserves_execute_omission_and_mutation_guards() {
    let (identity, mut completion) = collection_witness();
    let assigned = [0, 1];
    result_of(
        &completion,
        assigned_expectation(&identity, &assigned),
        PublishedResult::Execution,
        0,
    )
    .expect("the exact collection completion belongs to its planned assignment");
    completion
        .validate_fresh_prefix(&identity, 0, 2)
        .expect("only the collect-only purpose may return zero executed cases");
    assert!(completion.validate_fresh_prefix(&identity, 1, 2).is_err());
    completion.consumed_arguments = None;
    assert!(completion.validate_fresh_prefix(&identity, 0, 2).is_err());
    completion.consumed_arguments = Some(ConsumedArguments::Unavailable);
    completion
        .validate_fresh_prefix(&identity, 0, 2)
        .expect("an unavailable vector denies caching without fabricating execution");
    completion.mutation = Some(MutationObservation {
        id: "foreign-mutation".to_owned(),
        applied: true,
    });
    assert!(completion.validate_fresh_prefix(&identity, 0, 2).is_err());
    let (serial, mut serial_completion) = issued();
    serial_completion.stage = LifecycleStage::Finished;
    serial_completion.origin = FailureOrigin::Unhandled;
    assert!(
        serial_completion
            .validate_fresh_prefix(&serial, 0, 2)
            .is_err()
    );
}

#[test]
fn fresh_prefix_requires_running_stop_even_with_matching_case_counts() {
    let (identity, mut completion) = issued();
    assert!(completion.validate_fresh_prefix(&identity, 1, 2).is_ok());
    for origin in [
        FailureOrigin::Unhandled,
        FailureOrigin::Internal,
        FailureOrigin::Usage,
    ] {
        completion.origin = origin;
        assert!(completion.validate_fresh_prefix(&identity, 1, 2).is_err());
        assert!(completion.validate_fresh_prefix(&identity, 2, 2).is_ok());
    }
    completion.origin = FailureOrigin::SessionFailure;
    completion.stage = LifecycleStage::Finishing;
    assert!(completion.validate_fresh_prefix(&identity, 1, 2).is_err());
    completion.stage = LifecycleStage::Running;
    completion.origin =
        FailureOrigin::Control(crate::independent::controls::ControlKind::KeyboardInterrupt);
    assert!(completion.validate_fresh_prefix(&identity, 1, 2).is_ok());
    assert!(completion.validate_fresh_prefix(&identity, 3, 2).is_err());
}

#[test]
fn fresh_purpose_is_checked_against_native_supervisor_identity() {
    let (identity, witness) = issued();
    let completion = WorkerCollectionCompletion {
        index: 0,
        returncode: 1,
        infrastructure_failed: false,
        warnings: Vec::new(),
        exit_reason: None,
        invocation: Some(witness),
    };
    let bytes = execution_document(&worker_result(1), &identity, 0);
    completion
        .validate_fresh_result(&identity, &bytes, 2)
        .expect("actual typed prefix document");
    let assigned = [0, 1];
    let witness = completion.invocation.as_ref().expect("actual witness");
    result_of(
        witness,
        assigned_expectation(&identity, &assigned),
        PublishedResult::Execution,
        1,
    )
    .expect("the completion belongs to this run");
    let mut foreign = identity.clone();
    foreign.purpose = InvocationPurpose::Parallel;
    assert!(
        completion
            .validate_fresh_result(&foreign, &bytes, 2)
            .is_err()
    );
    assert!(
        result_of(
            witness,
            assigned_expectation(&foreign, &assigned),
            PublishedResult::Execution,
            1
        )
        .is_err()
    );
    let mut collection = witness.clone();
    collection.assignment = None;
    assert!(
        result_of(
            &collection,
            assigned_expectation(&identity, &assigned),
            PublishedResult::Collection,
            1
        )
        .is_err()
    );
    let mut relabeled = completion.clone();
    relabeled
        .invocation
        .as_mut()
        .expect("actual witness")
        .purpose = InvocationPurpose::Parallel;
    assert!(
        relabeled
            .validate_fresh_result(&identity, &bytes, 2)
            .is_err()
    );
}

#[test]
fn fresh_worker_document_rejects_unwitnessed_prefix_and_over_count() {
    let (identity, witness) = issued();
    let mut completion = WorkerCollectionCompletion {
        index: 0,
        returncode: 1,
        infrastructure_failed: false,
        warnings: Vec::new(),
        exit_reason: None,
        invocation: Some(witness),
    };
    let partial = execution_document(&worker_result(1), &identity, 0);
    completion
        .validate_fresh_result(&identity, &partial, 2)
        .expect("owned original stop permits its prefix");
    completion.invocation.as_mut().expect("witness").origin = FailureOrigin::Unhandled;
    assert!(
        completion
            .validate_fresh_result(&identity, &partial, 2)
            .is_err()
    );
    let full = execution_document(&worker_result(2), &identity, 0);
    completion
        .validate_fresh_result(&identity, &full, 2)
        .expect("full assigned result requires no stop");
    let excessive = execution_document(&worker_result(3), &identity, 0);
    assert!(
        completion
            .validate_fresh_result(&identity, &excessive, 2)
            .is_err()
    );
}

#[test]
fn execution_document_from_another_run_or_worker_is_rejected() {
    let (identity, witness) = issued();
    let completion = WorkerCollectionCompletion {
        index: 0,
        returncode: 1,
        infrastructure_failed: false,
        warnings: Vec::new(),
        exit_reason: None,
        invocation: Some(witness),
    };
    let mut other_run = identity.clone();
    other_run.nonce = "another-run".to_owned();
    let stale = execution_document(&worker_result(2), &other_run, 0);
    assert!(
        completion
            .validate_fresh_result(&identity, &stale, 2)
            .is_err()
    );
    let other_worker = execution_document(&worker_result(2), &identity, 1);
    assert!(
        completion
            .validate_fresh_result(&identity, &other_worker, 2)
            .is_err()
    );
}
