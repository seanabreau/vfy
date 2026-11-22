use super::{CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, RunRecord};
use crate::results::native_run_result;
use vfy_core::selection::{TestOutcome, TestRun, read_test_run};

fn project(record: RunRecord, infrastructure_failed: bool) -> TestRun {
    let directory = tempfile::tempdir().expect("create result projection directory");
    let path = directory.path().join("result.json");
    native_run_result(record, None)
        .write_vfy_result(
            path.to_str().expect("temporary result path is UTF-8"),
            infrastructure_failed,
        )
        .expect("write native execution evidence");
    read_test_run(&path).expect("read projected execution evidence")
}

fn independent(cases: Vec<CaseResult>, collected: usize) -> RunRecord {
    let mut record = super::sample_record();
    record.engine = "vfy-independent-v1".to_owned();
    record.collected = collected;
    record.cases = cases;
    record
}

fn setup(outcome: ExecutionOutcome) -> CaseResult {
    CaseResult::new(
        "test_example.py::test_setup".to_owned(),
        0.5,
        vec![PhaseResult::new(
            outcome,
            ExecutionPhase::Setup,
            0.5,
            PhasePayload::default(),
        )],
    )
}

#[test]
fn independent_projection_marks_missing_or_unfinished_identities_incomplete() {
    let passed = || super::worker_case("test_example.py::test_pass", 1.0, ExecutionOutcome::Passed);
    for record in [
        independent(Vec::new(), 2),
        independent(vec![passed()], 2),
        independent(vec![setup(ExecutionOutcome::Passed)], 1),
        independent(vec![passed(), passed()], 2),
    ] {
        assert!(project(record, false).infrastructure_failed);
    }
}

#[test]
fn independent_projection_retains_real_terminal_outcomes_after_deselection() {
    for (case, expected) in [
        (
            super::worker_case("test_pass", 1.0, ExecutionOutcome::Passed),
            TestOutcome::Passed,
        ),
        (
            super::worker_case("test_fail", 1.0, ExecutionOutcome::Failed),
            TestOutcome::Failed,
        ),
        (
            super::worker_case("test_xpass", 1.0, ExecutionOutcome::XPassed),
            TestOutcome::Passed,
        ),
        (setup(ExecutionOutcome::Skipped), TestOutcome::Skipped),
        (setup(ExecutionOutcome::XFailed), TestOutcome::Skipped),
        (setup(ExecutionOutcome::Error), TestOutcome::Failed),
    ] {
        let mut record = independent(vec![case], 1);
        record.deselected_nodeids = vec!["test_example.py::not_selected".to_owned()];
        let run = project(record, false);
        assert!(!run.infrastructure_failed);
        assert_eq!(run.tests.len(), 1);
        assert_eq!(run.tests[0].outcome, expected);
    }
    assert!(!project(independent(Vec::new(), 0), false).infrastructure_failed);
    assert!(project(independent(Vec::new(), 0), true).infrastructure_failed);
}

#[test]
fn native_partial_projection_keeps_its_existing_infrastructure_policy() {
    for infrastructure_failed in [false, true] {
        let mut record = super::sample_record();
        record.collected = 2;
        let run = project(record, infrastructure_failed);
        assert_eq!(run.infrastructure_failed, infrastructure_failed);
        assert_eq!(run.tests.len(), 1);
    }
}
