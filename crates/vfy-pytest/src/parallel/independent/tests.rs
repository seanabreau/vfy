use super::*;
use crate::independent::controls::ControlKind;

fn interruption_case() -> (BootstrapIdentity, InvocationCompletion) {
    let identity = BootstrapIdentity {
        engine: ENGINE.to_owned(),
        nonce: "this-run".to_owned(),
        width: 2,
        purpose: InvocationPurpose::Parallel,
        mutation: None,
    };
    let completion = InvocationCompletion {
        nonce: identity.nonce.clone(),
        purpose: identity.purpose,
        pid: 1234,
        index: 0,
        stage: LifecycleStage::Running,
        disposition: Disposition::Return(42),
        origin: FailureOrigin::Control(ControlKind::Exit),
        outcome: InvocationOutcome::Result,
        session_exitstatus: Some(42),
        assignment: Some(AssignmentRecord {
            indices: vec![0, 2],
        }),
        mutation: None,
        consumed_arguments: None,
    };
    (identity, completion)
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
fn fixture_cleanup_completion_requires_native_finalization_and_failure_channel() {
    let (identity, mut completion) = interruption_case();
    let assigned = [0, 2];
    let expected = || Expectation {
        identity: &identity,
        index: 0,
        pid: 1234,
        assigned: Some(&assigned),
    };
    completion.outcome = InvocationOutcome::Failure;
    completion.origin = FailureOrigin::FixtureCleanup;
    completion.stage = LifecycleStage::Finishing;
    completion.disposition = Disposition::Return(1);
    completion.session_exitstatus = None;
    assert!(matches!(
        completion.validate(expected(), PublishedResult::Missing, 1),
        Ok(ValidatedCompletion::Failure(_, 1))
    ));
    completion.disposition = Disposition::Return(0);
    assert!(
        completion
            .validate(expected(), PublishedResult::Missing, 0)
            .is_err()
    );
    completion.disposition = Disposition::Return(1);
    completion.stage = LifecycleStage::Running;
    assert!(
        completion
            .validate(expected(), PublishedResult::Missing, 1)
            .is_err()
    );
    completion.stage = LifecycleStage::Finishing;
    assert!(
        completion
            .validate(expected(), PublishedResult::Execution, 1)
            .is_err()
    );
    completion.assignment = None;
    assert!(
        completion
            .validate(expected(), PublishedResult::Missing, 1)
            .is_err()
    );
}

#[test]
fn parallel_maxfail_completion_requires_its_running_assignment() {
    let (identity, mut completion) = interruption_case();
    let assigned = [0, 2];
    let expected = || Expectation {
        identity: &identity,
        index: 0,
        pid: 1234,
        assigned: Some(&assigned),
    };
    completion.origin = FailureOrigin::SessionFailure;
    completion.disposition = Disposition::Return(1);
    completion.session_exitstatus = Some(1);
    result_of(&completion, expected(), PublishedResult::Execution, 1)
        .expect("native maxfail owns a partial parallel result");
    completion.stage = LifecycleStage::Finishing;
    assert!(result_of(&completion, expected(), PublishedResult::Execution, 1).is_err());
    completion.stage = LifecycleStage::Running;
    completion.assignment = None;
    assert!(result_of(&completion, expected(), PublishedResult::Collection, 1).is_err());
}

#[test]
fn interruption_requires_matching_identity_and_original_control() {
    let (identity, mut completion) = interruption_case();
    let assigned = [0, 2];
    let expected = || Expectation {
        identity: &identity,
        index: 0,
        pid: 1234,
        assigned: Some(&assigned),
    };
    let check = |completion: &InvocationCompletion| {
        result_of(completion, expected(), PublishedResult::Execution, 42)
    };
    assert_eq!(
        check(&completion)
            .expect("owned control")
            .interruption()
            .expect("interruption")
            .status(),
        42
    );
    completion.origin = FailureOrigin::Unhandled;
    assert!(
        check(&completion)
            .expect("ordinary custom status")
            .interruption()
            .is_none()
    );
    completion.origin = FailureOrigin::Control(ControlKind::Exit);
    completion.stage = LifecycleStage::Finishing;
    assert!(
        check(&completion)
            .expect("late control")
            .interruption()
            .is_none()
    );
    completion.stage = LifecycleStage::Running;
    for field in ["nonce", "pid", "index", "assignment"] {
        let mut corrupted: InvocationCompletion =
            serde_json::from_value(serde_json::to_value(&completion).expect("serialize"))
                .expect("clone");
        match field {
            "nonce" => corrupted.nonce = "other-run".to_owned(),
            "pid" => corrupted.pid += 1,
            "index" => corrupted.index = 1,
            "assignment" => corrupted
                .assignment
                .as_mut()
                .expect("assignment")
                .indices
                .reverse(),
            _ => unreachable!(),
        }
        assert!(check(&corrupted).is_err(), "accepted corrupt {field}");
    }
    assert!(
        result_of(&completion, expected(), PublishedResult::Missing, 42).is_err(),
        "a result channel is required"
    );
    assert!(
        result_of(&completion, expected(), PublishedResult::Execution, 41).is_err(),
        "the process status must match the completion"
    );
}
