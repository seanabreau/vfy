use super::*;

#[test]
fn scalar_text_serializes_as_a_string_and_rejects_invalid_code_points() {
    let text = PythonText::from("reason: 界 & <tag>");
    assert_eq!(
        serde_json::to_string(&text).unwrap(),
        "\"reason: 界 & <tag>\""
    );
    assert!(serde_json::from_value::<PythonText>(serde_json::json!({})).is_err());
    assert!(PythonText::from_codepoints(vec![0x11_0000]).is_err());
    assert!(NonScalarText::new(vec![0x61]).is_err());
}

#[test]
fn non_scalar_exception_wire_roundtrips_through_canonical_phase_and_subtest() {
    use crate::test_execution::{
        ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, SubtestResult,
    };
    let reason = PythonText::from_codepoints(vec![0x72, 0xdcff]).unwrap();
    let phase = PhaseResult::new(
        ExecutionOutcome::XFailed,
        ExecutionPhase::Call,
        0.0,
        PhasePayload {
            exception: Some(reason.clone()),
            ..PhasePayload::default()
        },
    );
    let wire = serde_json::to_value(&phase).unwrap();
    assert_eq!(
        wire["exception"],
        serde_json::json!({"codepoints":[0x72,0xdcff]})
    );
    let decoded: PhaseResult = serde_json::from_value(wire).unwrap();
    assert_eq!(decoded.exception, Some(reason.clone()));
    let child = SubtestResult {
        outcome: ExecutionOutcome::Skipped,
        context: "[child]".to_owned(),
        exception: Some(reason.clone()),
        detail: String::new(),
        skip_location: None,
        terminal_lead: String::new(),
        report: None,
    };
    let decoded: SubtestResult =
        serde_json::from_value(serde_json::to_value(child).unwrap()).unwrap();
    assert_eq!(decoded.exception, Some(reason));
}

#[test]
fn tagged_text_rejects_invalid_or_noncanonical_external_payloads() {
    for invalid in [
        serde_json::json!({"codepoints":[0x0011_0000]}),
        serde_json::json!({"codepoints":[-1]}),
        serde_json::json!({"codepoints":[0x61]}),
        serde_json::json!({"codepoints":[0xdcff],"extra":true}),
    ] {
        assert!(serde_json::from_value::<PythonText>(invalid).is_err());
    }
}

#[test]
fn message_prefixes_and_concatenation_preserve_surrogate_position() {
    let text = PythonText::joined(&[
        &"reason: ".into(),
        &PythonText::from_codepoints(vec![0xdcff, 0x78]).unwrap(),
    ]);
    let suffix = text.strip_prefix("reason: ").unwrap();
    assert_eq!(suffix.codepoints(), vec![0xdcff, 0x78]);
    assert!(suffix.as_scalar().is_none());
    assert_eq!(
        PythonText::joined(&[&" (".into(), &suffix, &")".into()]).codepoints(),
        vec![0x20, 0x28, 0xdcff, 0x78, 0x29]
    );
}
