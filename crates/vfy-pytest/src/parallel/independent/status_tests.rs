use super::*;
use crate::results::{IndependentMergePolicy, ParallelResultMode};
use fresh_tests::{execution_document, issued, worker_result};

#[test]
fn independent_demand_unclaimed_work_requires_native_control_or_observed_failure_budget() {
    pyo3::Python::initialize();
    for (command, interrupted, maxfail, accepted) in [
        (2, true, 0, true),
        (1, false, 0, false),
        (1, false, 2, true),
        (1, false, 3, false),
    ] {
        let (mut identity, mut completion, mut result, _) = completed(command, command);
        identity.purpose = InvocationPurpose::Parallel;
        let invocation = completion.invocation.as_mut().expect("native completion");
        invocation.purpose = InvocationPurpose::Parallel;
        if interrupted {
            invocation.stage = LifecycleStage::Running;
            invocation.origin = FailureOrigin::Control(
                crate::independent::controls::ControlKind::KeyboardInterrupt,
            );
        }
        result.record_mut().observations = Some(
            serde_json::from_value(serde_json::json!({
                "configuration": {"ini_path": null, "ini_sha256": null, "values": {},
                    "strict_config": false, "strict_markers": false},
                "case_markers": (0..2).map(|index| serde_json::json!({
                    "nodeid": format!("test_body.py::test_{index}"), "names": []
                })).collect::<Vec<_>>(),
                "plugins": [], "execution_topology": {"requested_workers": 2,
                    "active_workers": 1, "worker_pids": [1234]}
            }))
            .expect("finalized observations for the two actually claimed cases"),
        );
        let bytes = execution_document(&result, &identity, 0);
        let assigned = [0, 1];
        let proof = validated_result(&completion, &identity, &assigned, i64::from(command));
        let nodeids = (0..4)
            .map(|index| format!("test_body.py::test_{index}"))
            .collect::<Vec<_>>();
        let merged = crate::results::merge_independent_demand_parallel_results(
            [Ok(bytes.clone())].into_iter(),
            &[std::iter::once(0..2).collect()],
            &nodeids,
            &[Some(command)],
            IndependentMergePolicy {
                maxfail,
                initial_failures: 0,
                duration_ms: 1.0,
                mode: ParallelResultMode::Execute,
                completions: &[proof],
            },
        );
        assert_eq!(
            merged.is_ok(),
            accepted,
            "interrupted={interrupted}, maxfail={maxfail}: {:?}",
            merged.as_ref().err()
        );
        let wrong = ValidatedResult { index: 1, ..proof };
        assert!(
            crate::results::merge_independent_demand_parallel_results(
                [Ok(bytes)].into_iter(),
                &[std::iter::once(0..2).collect()],
                &nodeids,
                &[Some(command)],
                IndependentMergePolicy {
                    maxfail,
                    initial_failures: 0,
                    duration_ms: 1.0,
                    mode: ParallelResultMode::Execute,
                    completions: &[wrong]
                },
            )
            .is_err(),
            "another worker's completion cannot authenticate the same record"
        );
    }
}

fn completed(
    command: i32,
    session: i32,
) -> (
    BootstrapIdentity,
    WorkerCollectionCompletion,
    NativeRunResult,
    String,
) {
    let (identity, mut witness) = issued();
    witness.stage = LifecycleStage::Finished;
    witness.origin = FailureOrigin::Unhandled;
    witness.disposition = Disposition::Return(command);
    witness.session_exitstatus = Some(session);
    let mut result = worker_result(2);
    for case in &mut result.record_mut().cases {
        for phase in &mut case.phases {
            if phase.phase == vfy_core::test_execution::ExecutionPhase::Call {
                phase.outcome = vfy_core::test_execution::ExecutionOutcome::Failed;
            }
        }
    }
    let counts = vfy_core::test_execution::compute_counts(&result.record().cases);
    result.record_mut().counts = counts;
    result.record_mut().exitstatus = Some(session);
    let bytes = execution_document(&result, &identity, 0);
    let completion = WorkerCollectionCompletion {
        index: 0,
        returncode: i64::from(command),
        infrastructure_failed: false,
        warnings: Vec::new(),
        exit_reason: None,
        invocation: Some(witness),
    };
    (identity, completion, result, bytes)
}

fn validated_result(
    completion: &WorkerCollectionCompletion,
    identity: &BootstrapIdentity,
    assigned: &[usize],
    command: i64,
) -> ValidatedResult {
    let invocation = completion
        .invocation
        .as_ref()
        .expect("actual result witness");
    let expected = Expectation {
        identity,
        index: 0,
        pid: 1234,
        assigned: Some(assigned),
    };
    match invocation
        .validate(expected, PublishedResult::Execution, command)
        .expect("independently owned command status")
    {
        ValidatedCompletion::Result(result) => result,
        ValidatedCompletion::Failure(..) => panic!("the completion is a result"),
    }
}

#[test]
fn command_override_preserves_actual_session_result_through_merge() {
    let (identity, completion, result, bytes) = completed(4, 1);
    completion
        .validate_fresh_result(&identity, &bytes, 2)
        .expect("same actual session result");
    let assigned = [0, 1];
    let proof = validated_result(&completion, &identity, &assigned, 4);
    let assignments = [vec![0, 1]];
    let nodeids = result
        .record()
        .cases
        .iter()
        .map(|case| case.nodeid.clone())
        .collect::<Vec<_>>();
    let merged = crate::results::merge_independent_parallel_results(
        [Ok(bytes.clone())].into_iter(),
        &assignments,
        &nodeids,
        &[Some(4)],
        IndependentMergePolicy {
            maxfail: 0,
            initial_failures: 0,
            duration_ms: 1.0,
            mode: ParallelResultMode::Execute,
            completions: &[proof],
        },
    )
    .expect("actual validated native result merge");
    assert_eq!(merged.record().exitstatus, Some(1));
    assert_eq!(merged.record().counts, result.record().counts);
    assert!(
        crate::results::merge_parallel_results(
            [Ok(bytes)].into_iter(),
            &assignments,
            &nodeids,
            &[Some(4)],
            0,
            0,
            1.0,
        )
        .is_err(),
        "the unvalidated legacy route must retain its status equality guard"
    );
    for (index, engine, session, process) in [
        (1, ENGINE, Some(1), Some(4)),
        (0, "pytest-runner-native-v1", Some(1), Some(4)),
        (0, ENGINE, Some(4), Some(4)),
        (0, ENGINE, None, Some(4)),
        (0, ENGINE, Some(1), Some(0)),
        (0, ENGINE, Some(1), None),
    ] {
        assert!(
            proof
                .validate_record(index, engine, session, process)
                .is_err()
        );
    }
}

#[test]
fn changed_or_missing_session_status_cannot_relabel_actual_record() {
    let (identity, mut completion, _, bytes) = completed(260, 1);
    let assigned = [0, 1];
    validated_result(&completion, &identity, &assigned, 260)
        .validate_record(0, ENGINE, Some(1), Some(4))
        .expect("actual low eight process bits");
    completion
        .invocation
        .as_mut()
        .expect("witness")
        .session_exitstatus = Some(0);
    assert!(
        completion
            .validate_fresh_result(&identity, &bytes, 2)
            .is_err(),
        "a relabeled session cannot validate the document"
    );
    completion
        .invocation
        .as_mut()
        .expect("witness")
        .session_exitstatus = None;
    assert!(
        completion
            .validate_fresh_result(&identity, &bytes, 2)
            .is_err(),
        "a missing session status cannot validate the document"
    );
}

#[test]
fn original_control_uses_session_stop_and_failure_cannot_claim_a_result_status() {
    let (identity, mut completion, mut result, _) = completed(4, 2);
    let witness = completion.invocation.as_mut().expect("actual witness");
    witness.stage = LifecycleStage::Running;
    witness.origin =
        FailureOrigin::Control(crate::independent::controls::ControlKind::KeyboardInterrupt);
    let assigned = [0, 1];
    let proof = validated_result(&completion, &identity, &assigned, 4);
    assert_eq!(proof.interruption().expect("control stop").status(), 2);
    result.record_mut().exitstatus = Some(4);
    let changed = execution_document(&result, &identity, 0);
    assert!(
        completion
            .validate_fresh_result(&identity, &changed, 2)
            .is_err()
    );
    let witness = completion.invocation.as_mut().expect("failure witness");
    witness.outcome = InvocationOutcome::Failure;
    witness.origin = FailureOrigin::Control(crate::independent::controls::ControlKind::Exit);
    let expected = Expectation {
        identity: &identity,
        index: 0,
        pid: 1234,
        assigned: Some(&assigned),
    };
    assert!(
        witness
            .validate(expected, PublishedResult::Missing, 4)
            .is_err()
    );
    witness.session_exitstatus = None;
    assert!(matches!(
        witness
            .validate(expected, PublishedResult::Missing, 4)
            .expect("owned failure has no suite"),
        ValidatedCompletion::Failure(_, 4)
    ));
}

#[test]
fn collection_peer_checks_session_independently_of_command() {
    use vfy_core::test_execution::{ConfigurationObservation, ExecutionTopology, RunObservations};
    let (directory, binding) = collection_tests::issued(InvocationPurpose::Parallel, 2);
    let mut result = worker_result(0);
    let record = result.record_mut();
    record.collected = 0;
    record.exitstatus = Some(0);
    record.observations = Some(RunObservations {
        configuration: ConfigurationObservation {
            ini_path: None,
            ini_sha256: None,
            values: std::collections::BTreeMap::default(),
            strict_config: false,
            strict_markers: false,
        },
        coordinator_configuration: None,
        case_markers: Vec::new(),
        plugins: Vec::new(),
        execution_topology: ExecutionTopology {
            requested_workers: 2,
            active_workers: 1,
            worker_pids: vec![std::process::id()],
        },
    });
    assert_eq!(
        binding
            .result(
                &mut result,
                None,
                LifecycleStage::Finished,
                Disposition::Return(4),
                FailureOrigin::Unhandled
            )
            .expect("actual canonical collection publication"),
        4
    );
    let mut completion: WorkerCollectionCompletion =
        read_json(&directory.path().join("collection-finish.0.json"))
            .expect("native-published completion");
    let expected = Expectation {
        identity: &binding.identity,
        index: 0,
        pid: std::process::id(),
        assigned: None,
    };
    assert!(matches!(
        completion
            .invocation
            .as_ref()
            .expect("witness")
            .validate(expected, PublishedResult::Collection, 4)
            .expect("native identity"),
        ValidatedCompletion::Result(_)
    ));
    read_collection_record(directory.path(), 0, &binding.identity, &completion)
        .expect("session 0 and command 4 are recorded separately");
    let mut stale = binding.identity.clone();
    stale.nonce = "another-run".to_owned();
    assert!(read_collection_record(directory.path(), 0, &stale, &completion).is_err());
    completion
        .invocation
        .as_mut()
        .expect("witness")
        .session_exitstatus = Some(4);
    assert!(read_collection_record(directory.path(), 0, &binding.identity, &completion).is_err());
}
