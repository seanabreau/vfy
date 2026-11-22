use super::{
    ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, PythonText, ReportSummary,
};

#[test]
fn worker_summary_inputs_round_trip_without_changing_legacy_phase_documents() {
    let phase = PhaseResult::new(
        ExecutionOutcome::Failed,
        ExecutionPhase::Call,
        1.0,
        PhasePayload {
            report_summary: Some(ReportSummary {
                keywords: vec!["skip".to_owned(), "pytestmark".to_owned()],
                message: Some(
                    PythonText::from_codepoints(vec![0xd800, 0x61])
                        .expect("valid Python code points"),
                ),
                hypothesis_statistics: None,
                hypothesis_failing_examples: None,
            }),
            ..PhasePayload::default()
        },
    );
    let wire = serde_json::to_value(&phase).expect("serialize worker summary inputs");
    assert_eq!(
        wire["report_summary"].as_object().expect("summary").len(),
        2
    );
    let restored: PhaseResult =
        serde_json::from_value(wire.clone()).expect("decode worker summary inputs");
    assert_eq!(restored.report_summary, phase.report_summary);
    let mut legacy = wire;
    legacy
        .as_object_mut()
        .expect("phase object")
        .remove("report_summary");
    let restored: PhaseResult =
        serde_json::from_value(legacy.clone()).expect("decode legacy phase");
    assert!(restored.report_summary.is_none());
    assert_eq!(
        serde_json::to_value(restored).expect("encode legacy phase"),
        legacy
    );
}

#[test]
fn hypothesis_summary_preserves_literal_text_and_rejects_untyped_fields() {
    let summary = ReportSummary {
        keywords: vec!["hypothesis".to_owned()],
        message: None,
        hypothesis_statistics: Some(
            PythonText::from_codepoints(vec![0x61, 0xd800]).expect("Python statistics text"),
        ),
        hypothesis_failing_examples: Some(PythonText::from(
            "[\"test_property.py\",\"before\",\"after\"]".to_owned(),
        )),
    };
    let wire = serde_json::to_value(&summary).expect("serialize upstream text shapes");
    let restored: ReportSummary =
        serde_json::from_value(wire.clone()).expect("decode literal Hypothesis metadata");
    assert_eq!(restored, summary);
    for field in ["hypothesis_statistics", "hypothesis_failing_examples"] {
        for invalid in [serde_json::json!(false), serde_json::json!(["not text"])] {
            let mut corrupt = wire.clone();
            corrupt[field] = invalid;
            assert!(serde_json::from_value::<ReportSummary>(corrupt).is_err());
        }
    }
    let mut unknown = wire;
    unknown["unadmitted_plugin_payload"] = serde_json::json!("unchecked");
    assert!(serde_json::from_value::<ReportSummary>(unknown).is_err());
}
