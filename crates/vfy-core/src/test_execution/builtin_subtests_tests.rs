//! Builtin subreports preserve one indexed history and parent status boundaries.

use super::{CaseResult, ExecutionOutcome, valid_case_events, valid_case_phases};
use serde_json::{Value, json};

fn document() -> Value {
    json!({
        "nodeid": "test_body.py::test_body", "duration_ms": 4.0,
        "phases": [
            {"outcome":"passed","phase":"setup","duration_ms":1.0,"exception":null,"stdout":"","stderr":"","log":"","detail":""},
            {"outcome":"passed","phase":"call","duration_ms":2.0,"exception":null,"stdout":"","stderr":"","log":"","detail":""},
            {"outcome":"passed","phase":"teardown","duration_ms":1.0,"exception":null,"stdout":"","stderr":"","log":"","detail":""}
        ],
        "subtests": [{
            "outcome":"failed", "context":"[child]", "exception":null, "detail":"child detail",
            "report": {
                "context":{"msg":"child","kwargs":[["ordered","1"],["next","2"]]},
                "duration_ms":0.5,"start":10.0,"stop":10.0005,
                "stdout":"child output\n","stderr":"","log":"child log\n","report_sections":null
            }
        }],
        "report_events":[
            {"kind":"phase","index":0},
            {"kind":"subtest","index":0},
            {"kind":"phase","index":1},
            {"kind":"phase","index":2}
        ]
    })
}

#[test]
fn builtin_report_history_roundtrip_preserves_context_capture_and_partial_abort() {
    let source = document();
    let case: CaseResult = serde_json::from_value(source.clone()).expect("valid history");
    assert!(valid_case_events(&case));
    let roundtrip = serde_json::to_value(&case).expect("history serialization");
    for field in ["subtests", "report_events"] {
        assert_eq!(roundtrip[field], source[field]);
    }
    let mut partial = source;
    partial["phases"]
        .as_array_mut()
        .expect("phases")
        .truncate(1);
    partial["report_events"]
        .as_array_mut()
        .expect("events")
        .truncate(2);
    let partial: CaseResult = serde_json::from_value(partial).expect("aborted after child");
    assert!(valid_case_phases(&partial));
    assert!(valid_case_events(&partial));
}

#[test]
fn malformed_report_histories_reject_duplicates_omissions_and_bad_indices() {
    for invalid in [
        json!([{"kind":"phase","index":0}]),
        json!([{"kind":"phase","index":0},{"kind":"phase","index":0}]),
        json!([{"kind":"subtest","index":99}]),
    ] {
        let mut source = document();
        source["report_events"] = invalid;
        assert!(serde_json::from_value::<CaseResult>(source).is_err());
    }
    let mut case: CaseResult = serde_json::from_value(document()).expect("valid history");
    case.set_report_events(vec![super::CaseReportEvent::Subtest(99)].into_boxed_slice());
    assert!(!valid_case_events(&case));
}

#[test]
fn builtin_report_metadata_requires_present_nonempty_history() {
    for events in [None, Some(json!([]))] {
        let mut source = document();
        source
            .as_object_mut()
            .expect("case document")
            .remove("report_events");
        if let Some(events) = events {
            source["report_events"] = events;
        }
        assert!(serde_json::from_value::<CaseResult>(source).is_err());
    }
    let mut case: CaseResult = serde_json::from_value(document()).expect("valid history");
    case.set_report_events(Box::default());
    assert!(!valid_case_events(&case));
}

#[test]
fn ordinary_and_legacy_subtest_records_roundtrip_without_event_indices() {
    for events in [None, Some(json!([]))] {
        for legacy_child in [false, true] {
            let mut source = document();
            source
                .as_object_mut()
                .expect("case document")
                .remove("report_events");
            if let Some(events) = &events {
                source["report_events"] = events.clone();
            }
            if legacy_child {
                source["subtests"][0]
                    .as_object_mut()
                    .expect("subtest document")
                    .remove("report");
            } else {
                source["subtests"] = json!([]);
            }
            let case: CaseResult = serde_json::from_value(source).expect("legacy history");
            assert!(valid_case_events(&case));
            assert_eq!(case.subtests().len(), usize::from(legacy_child));
            let roundtrip = serde_json::to_value(&case).expect("legacy serialization");
            assert!(roundtrip.get("report_events").is_none());
            let restored: CaseResult = serde_json::from_value(roundtrip).expect("legacy roundtrip");
            assert!(valid_case_events(&restored));
            assert_eq!(restored.subtests().len(), usize::from(legacy_child));
            assert!(
                restored
                    .subtests()
                    .iter()
                    .all(|subtest| subtest.report.is_none())
            );
        }
    }
}

#[test]
fn builtin_child_failure_does_not_infer_completed_or_nonpassing_parent_status() {
    let mut case: CaseResult = serde_json::from_value(document()).expect("valid history");
    assert_eq!(
        case.report_phase_outcome(&case.phases[1]),
        ExecutionOutcome::Passed
    );
    case.phases[1].outcome = ExecutionOutcome::Skipped;
    assert_eq!(
        case.report_phase_outcome(&case.phases[1]),
        ExecutionOutcome::Skipped
    );
    // The native status hook makes the parent mutation at publication time.
    // A later teardown child cannot retroactively change an earlier call.
    case.phases[1].outcome = ExecutionOutcome::Passed;
    case.set_report_events(
        vec![
            super::CaseReportEvent::Phase(0),
            super::CaseReportEvent::Phase(1),
            super::CaseReportEvent::Subtest(0),
            super::CaseReportEvent::Phase(2),
        ]
        .into_boxed_slice(),
    );
    assert_eq!(
        case.report_phase_outcome(&case.phases[1]),
        ExecutionOutcome::Passed
    );
}

#[test]
fn taking_report_events_preserves_history_and_other_metadata() {
    let mut case: CaseResult = serde_json::from_value(document()).expect("valid history");
    case.set_test_id(crate::test_plan::TestId::from_index(3));
    case.set_head_line(Some("headline".to_owned()));
    let expected = serde_json::to_value(&case).expect("original case");
    assert!(valid_case_events(&case));
    let original = case.report_events().collect::<Vec<_>>();
    let events = case.take_report_events();
    assert_eq!(events.as_ref(), original);
    assert!(case.report_events().is_empty());
    assert_eq!(
        case.test_id(),
        Some(crate::test_plan::TestId::from_index(3))
    );
    assert_eq!(case.head_line(), Some("headline"));
    assert_eq!(case.subtests()[0].detail, "child detail");
    case.set_report_events(events);
    assert_eq!(case.report_events().collect::<Vec<_>>(), original);
    assert!(valid_case_events(&case));
    assert_eq!(
        serde_json::to_value(&case).expect("restored case"),
        expected
    );
}

#[test]
fn implicit_phase_events_preserve_wire_order_and_validate_count() {
    let mut source = document();
    source["subtests"] = json!([]);
    source["report_events"] = json!([
        {"kind":"phase", "index":0},
        {"kind":"phase", "index":1},
        {"kind":"phase", "index":2}
    ]);
    let mut case: CaseResult = serde_json::from_value(source.clone()).expect("phase history");
    assert!(matches!(
        case.report_events,
        super::CaseEventStorage::ImplicitPhases(3)
    ));
    assert_eq!(
        serde_json::to_value(&case).unwrap()["report_events"],
        source["report_events"]
    );
    assert!(valid_case_events(&case));
    case.phases.pop();
    assert!(!valid_case_events(&case));
    assert!(serde_json::from_value::<CaseResult>(serde_json::to_value(&case).unwrap()).is_err());

    let mut legacy = source;
    legacy.as_object_mut().unwrap().remove("report_events");
    let legacy: CaseResult = serde_json::from_value(legacy).expect("legacy phase history");
    assert!(legacy.report_events().is_empty());
    assert!(
        serde_json::to_value(legacy)
            .unwrap()
            .get("report_events")
            .is_none()
    );
}

#[test]
fn case_phase_storage_roundtrips_inline_and_spilled_histories() {
    let mut source = document();
    source["subtests"] = json!([]);
    source["report_events"] = json!([
        {"kind":"phase", "index":0},
        {"kind":"phase", "index":1},
        {"kind":"phase", "index":2}
    ]);
    let mut ordinary: CaseResult = serde_json::from_value(source.clone()).expect("ordinary case");
    ordinary.set_test_id(crate::test_plan::TestId::from_index(5));
    ordinary.set_head_line(Some("test_body".to_owned()));
    assert!(!ordinary.phases.spilled());
    assert!(ordinary.auxiliary.is_none());
    assert_eq!(
        serde_json::to_value(&ordinary).unwrap()["report_events"],
        source["report_events"]
    );

    let extra = source["phases"][1].clone();
    source["phases"].as_array_mut().unwrap().push(extra);
    source["report_events"]
        .as_array_mut()
        .unwrap()
        .push(json!({"kind":"phase", "index":3}));
    let spilled: CaseResult = serde_json::from_value(source.clone()).expect("long case history");
    assert!(spilled.phases.spilled());
    let encoded = serde_json::to_value(&spilled).expect("long case serialization");
    let expected: Vec<super::PhaseResult> =
        serde_json::from_value(source["phases"].clone()).expect("sequence phase history");
    assert_eq!(encoded["phases"], serde_json::to_value(expected).unwrap());
    assert_eq!(encoded["report_events"], source["report_events"]);
}

#[test]
fn malformed_phase_history_retains_sequence_diagnostic() {
    let mut source = document();
    source["phases"] = serde_json::Value::Null;
    let error = serde_json::from_value::<CaseResult>(source).unwrap_err();
    assert_eq!(error.to_string(), "invalid type: null, expected a sequence");
}
