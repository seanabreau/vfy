use super::*;
use crate::independent::reporting::{ReportBuffers, ReportSnapshot};
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, SubtestResult,
};

#[test]
fn final_snapshots_keep_equal_display_ids_in_their_actual_source_cases() {
    let mut first = CaseResult::new("same.py::test_same".to_owned(), 0.0, Vec::new());
    first.set_test_id(TestId::from_index(4));
    let mut second = CaseResult::new(first.nodeid.clone(), 0.0, Vec::new());
    second.set_test_id(TestId::from_index(9));
    let mut cases = [first, second];
    let phase = |outcome| {
        ReportSnapshot::Phase(PhaseResult::new(
            outcome,
            ExecutionPhase::Call,
            0.0,
            PhasePayload::default(),
        ))
    };
    let mut buffers = ReportBuffers::default();
    for (test, snapshot) in [
        (TestId::from_index(4), phase(ExecutionOutcome::Passed)),
        (TestId::from_index(9), phase(ExecutionOutcome::Failed)),
        (TestId::from_index(4), phase(ExecutionOutcome::Skipped)),
    ] {
        buffers.push(test, "".into(), snapshot);
    }
    buffers.bind_cases(&cases);
    buffers.apply(&mut cases);
    assert_eq!(cases[0].phases[0].outcome, ExecutionOutcome::Skipped);
    assert_eq!(cases[1].phases[0].outcome, ExecutionOutcome::Failed);
    assert_eq!(cases[0].test_id(), Some(TestId::from_index(4)));
    assert_eq!(cases[1].test_id(), Some(TestId::from_index(9)));
}

#[test]
fn repeated_snapshots_replace_report_arrays_and_preserve_case_metadata() {
    let test = TestId::from_index(4);
    let mut case = CaseResult::new("case.py::test_value".to_owned(), 17.0, Vec::new());
    case.set_test_id(test);
    case.set_head_line(Some("retained headline".to_owned()));
    case.set_user_properties(Box::new([("property".to_owned(), "value".to_owned())]));
    case.set_call_failure_properties(Some(Box::new([(
        "call property".to_owned(),
        "call value".to_owned(),
    )])));
    case.set_xml_attributes(Box::new([("attribute".to_owned(), "value".to_owned())]));
    case.set_teardown_testcase_unwritten(true);
    let metadata = |case: &CaseResult| {
        let mut value = serde_json::to_value(case).expect("case is serializable");
        let fields = value.as_object_mut().expect("case serializes as an object");
        for name in ["phases", "subtests", "report_events"] {
            fields.remove(name);
        }
        value
    };
    let original_metadata = metadata(&case);
    let phase = |when, outcome| {
        ReportSnapshot::Phase(PhaseResult::new(
            outcome,
            when,
            0.0,
            PhasePayload::default(),
        ))
    };
    let mut cases = [case];
    let mut buffers = ReportBuffers::default();
    for (test, snapshot) in [
        (test, phase(ExecutionPhase::Call, ExecutionOutcome::Passed)),
        (
            test,
            ReportSnapshot::Subtest(SubtestResult {
                report: None,
                outcome: ExecutionOutcome::Failed,
                context: "retained subtest".to_owned(),
                exception: None,
                detail: "failure detail".to_owned(),
                skip_location: None,
                terminal_lead: String::new(),
            }),
        ),
        (test, phase(ExecutionPhase::Call, ExecutionOutcome::Skipped)),
        (
            test,
            phase(ExecutionPhase::Teardown, ExecutionOutcome::Passed),
        ),
    ] {
        buffers.push(test, "".into(), snapshot);
    }
    buffers.bind_cases(&cases);
    buffers.apply(&mut cases);
    assert_eq!(cases[0].phases.len(), 2);
    assert_eq!(cases[0].phases[0].outcome, ExecutionOutcome::Skipped);
    assert_eq!(cases[0].subtests()[0].detail, "failure detail");
    assert_eq!(
        serde_json::to_value(cases[0].report_events()).expect("events are serializable"),
        serde_json::json!([
            {"kind": "phase", "index": 0},
            {"kind": "subtest", "index": 0},
            {"kind": "phase", "index": 1},
        ])
    );
    assert_eq!(metadata(&cases[0]), original_metadata);

    let mut buffers = ReportBuffers::default();
    buffers.push(
        test,
        "".into(),
        phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
    );
    buffers.bind_cases(&cases);
    buffers.apply(&mut cases);
    assert_eq!(cases[0].phases.len(), 1);
    assert_eq!(cases[0].phases[0].phase, ExecutionPhase::Setup);
    assert!(cases[0].subtests().is_empty());
    assert_eq!(
        serde_json::to_value(cases[0].report_events()).expect("events are serializable"),
        serde_json::json!([{"kind": "phase", "index": 0}])
    );
    assert_eq!(metadata(&cases[0]), original_metadata);

    let unchanged = serde_json::to_value(&cases[0]).expect("case is serializable");
    let mut buffers = ReportBuffers::default();
    buffers.bind_cases(&cases);
    buffers.apply(&mut cases);
    assert_eq!(
        serde_json::to_value(&cases[0]).expect("case is serializable"),
        unchanged
    );
}

#[test]
fn interleaved_snapshots_update_only_first_case_for_each_identity() {
    let mut first = CaseResult::new("first".to_owned(), 1.0, Vec::new());
    first.set_test_id(TestId::from_index(8));
    let mut second = CaseResult::new("second".to_owned(), 2.0, Vec::new());
    second.set_test_id(TestId::from_index(3));
    let mut duplicate = CaseResult::new("duplicate".to_owned(), 3.0, Vec::new());
    duplicate.set_test_id(TestId::from_index(8));
    let unbound = CaseResult::new("unbound".to_owned(), 4.0, Vec::new());
    let mut cases = [first, second, duplicate, unbound];
    let phase = |when, outcome| {
        ReportSnapshot::Phase(PhaseResult::new(
            outcome,
            when,
            0.0,
            PhasePayload::default(),
        ))
    };
    let subtest = |detail: &str| {
        ReportSnapshot::Subtest(SubtestResult {
            report: None,
            outcome: ExecutionOutcome::Passed,
            context: String::new(),
            exception: None,
            detail: detail.to_owned(),
            skip_location: None,
            terminal_lead: String::new(),
        })
    };
    let mut buffers = ReportBuffers::default();
    for (test, snapshot) in [
        (
            TestId::from_index(8),
            phase(ExecutionPhase::Call, ExecutionOutcome::Passed),
        ),
        (TestId::from_index(3), subtest("second-first")),
        (TestId::from_index(42), subtest("unknown")),
        (TestId::from_index(8), subtest("first-subtest")),
        (
            TestId::from_index(3),
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
        ),
        (
            TestId::from_index(8),
            phase(ExecutionPhase::Call, ExecutionOutcome::Skipped),
        ),
        (TestId::from_index(3), subtest("second-last")),
    ] {
        buffers.push(test, "".into(), snapshot);
    }
    buffers.bind_cases(&cases);
    buffers.apply(&mut cases);
    assert_eq!(cases[0].phases[0].outcome, ExecutionOutcome::Skipped);
    assert_eq!(cases[0].subtests()[0].detail, "first-subtest");
    assert_eq!(
        cases[1]
            .subtests()
            .iter()
            .map(|subtest| subtest.detail.as_str())
            .collect::<Vec<_>>(),
        ["second-first", "second-last"]
    );
    assert_eq!(
        serde_json::to_value(cases[1].report_events()).unwrap(),
        serde_json::json!([
            {"kind": "subtest", "index": 0}, {"kind": "phase", "index": 0}, {"kind": "subtest", "index": 1}
        ])
    );
    assert!(cases[2].phases.is_empty());
    assert!(cases[2].report_events().is_empty());
    assert!(cases[3].phases.is_empty());
}

#[test]
fn buffered_validation_uses_last_duplicate_and_reports_first_mismatch_without_mutation() {
    Python::initialize();
    Python::attach(|py| {
        let sparse = TestId::from_index(usize::MAX - 1);
        let other = TestId::from_index(2);
        let mut first = CaseResult::new("first".to_owned(), 0.0, Vec::new());
        first.set_test_id(sparse);
        let mut duplicate = CaseResult::new("last".to_owned(), 0.0, Vec::new());
        duplicate.set_test_id(sparse);
        let mut second = CaseResult::new("second".to_owned(), 0.0, Vec::new());
        second.set_test_id(other);
        let cases = [first, duplicate, second];
        let before = serde_json::to_value(&cases).unwrap();
        let phase = || {
            ReportSnapshot::Phase(PhaseResult::new(
                ExecutionOutcome::Passed,
                ExecutionPhase::Call,
                0.0,
                PhasePayload::default(),
            ))
        };
        let mut buffers = ReportBuffers::default();
        buffers.push(sparse, "first".into(), phase());
        buffers.push(other, "also wrong".into(), phase());
        buffers.bind_cases(&cases);
        let error = buffers.validate(&cases).unwrap_err();
        assert!(error.to_string().contains("another native case: first"));
        assert_eq!(serde_json::to_value(&cases).unwrap(), before);
        drop(error.into_value(py));

        let mut buffers = ReportBuffers::default();
        buffers.push(sparse, "last".into(), phase());
        buffers.push(TestId::from_index(usize::MAX), "unknown".into(), phase());
        buffers.bind_cases(&cases);
        buffers.validate(&cases).unwrap();
        let mut cases = cases;
        buffers.apply(&mut cases);
        assert_eq!(cases[0].phases.len(), 1);
        assert!(cases[1].phases.is_empty());
        assert!(cases[2].phases.is_empty());
    });
}
