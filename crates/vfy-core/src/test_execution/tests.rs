use super::{
    CaseResult, CollectionOutcome, CollectionReport, Counts, EmptyPhasePayload, ExecutionOutcome,
    ExecutionPhase, PhasePayload, PhaseResult, PythonText, ReportSection, ReportSummary, RunRecord,
    SkipLocation, SubtestResult, WarningRecord, compute_counts, compute_run_counts,
    merge_run_records, validate_run_record,
};

const EMPTY_PHASE_DOCUMENT: &str = r#"{"outcome":"passed","phase":"call","duration_ms":1.25,"exception":null,"stdout":"","stderr":"","log":"","detail":"","skip_location":null}"#;
const CAPTURED_PHASE_DOCUMENT: &str = r#"{"outcome":"skipped","phase":"setup","duration_ms":2.5,"exception":"Skipped: unavailable λ","stdout":"setup output\n","stderr":"setup error\n","log":"WARNING setup","detail":"skip diagnostic","skip_location":{"path":"tests/test_case.py","line":12}}"#;

#[test]
fn internal_errors_round_trip_and_merge_without_inventing_test_outcomes() {
    let mut first = record("test_a.py::test_a", ExecutionOutcome::Passed, 1.0);
    let legacy = serde_json::to_value(&first).expect("legacy record");
    assert!(legacy.get("internal_errors").is_none());
    assert!(
        serde_json::from_value::<RunRecord>(legacy)
            .expect("legacy record remains readable")
            .internal_errors
            .is_empty()
    );
    first.exitstatus = Some(3);
    first.internal_errors.push(PhasePayload {
        exception: Some("RuntimeError: session hook failed".into()),
        detail: "session hook failed".to_owned(),
        stdout: "hook output\n".to_owned(),
        ..PhasePayload::default()
    });
    let value = serde_json::to_value(&first).expect("internal error record");
    let first: RunRecord = serde_json::from_value(value).expect("restore internal error");
    assert!(validate_run_record(&first).is_ok());
    let mut second = record("test_b.py::test_b", ExecutionOutcome::Passed, 2.0);
    second.internal_errors.push(PhasePayload {
        exception: Some("ValueError: later hook failed".into()),
        ..PhasePayload::default()
    });
    let merged = merge_run_records(vec![first, second]).expect("merge internal errors");
    assert_eq!(merged.exitstatus, Some(3));
    assert_eq!(merged.counts.passed, 2);
    assert_eq!(merged.counts.failed, 0);
    assert_eq!(merged.counts.errors, 0);
    assert_eq!(merged.cases.len(), 2);
    assert!(merged.collection_reports.is_empty());
    assert_eq!(merged.internal_errors.len(), 2);
    assert_eq!(
        merged.internal_errors[0]
            .exception
            .as_ref()
            .and_then(PythonText::as_scalar),
        Some("RuntimeError: session hook failed")
    );
    assert_eq!(
        merged.internal_errors[1]
            .exception
            .as_ref()
            .and_then(PythonText::as_scalar),
        Some("ValueError: later hook failed")
    );
}

#[test]
fn measured_observations_round_trip_merge_workers_and_reject_config_mismatch() {
    use super::{ConfigurationObservation, ExecutionTopology, RunObservations};
    let mut first = record("test_a.py::test_a", ExecutionOutcome::Passed, 1.0);
    first.observations = Some(RunObservations {
        coordinator_configuration: None,
        configuration: ConfigurationObservation {
            ini_path: Some("/suite/pytest.ini".to_owned()),
            ini_sha256: Some("a".repeat(64)),
            values: std::collections::BTreeMap::from([(
                "markers".to_owned(),
                vec!["slow".to_owned()],
            )]),
            strict_config: true,
            strict_markers: false,
        },
        case_markers: Vec::new(),
        plugins: Vec::new(),
        execution_topology: ExecutionTopology {
            requested_workers: 2,
            active_workers: 1,
            worker_pids: vec![123],
        },
    });
    let json = serde_json::to_value(&first).expect("serialize measured observations");
    assert!(json.get("observations").is_none());
    assert!(json.get("coordinator_configuration").is_none());
    assert_eq!(json["configuration"]["strict_config"], true);
    let restored: RunRecord = serde_json::from_value(json).expect("decode observed record");
    assert_eq!(restored.observations, first.observations);
    let mut second = record("test_b.py::test_b", ExecutionOutcome::Passed, 1.0);
    second.observations = first.observations.clone();
    second
        .observations
        .as_mut()
        .unwrap()
        .execution_topology
        .worker_pids = vec![456];
    let merged = merge_run_records(vec![first, second]).expect("merge measured workers");
    let observations = merged.observations.as_ref().unwrap();
    assert_eq!(observations.execution_topology.worker_pids, [123, 456]);
    assert_eq!(observations.execution_topology.active_workers, 2);
    let mut inconsistent = record("test_c.py::test_c", ExecutionOutcome::Passed, 1.0);
    inconsistent.observations = merged.observations.clone();
    inconsistent
        .observations
        .as_mut()
        .unwrap()
        .configuration
        .strict_config = false;
    assert_eq!(
        merge_run_records(vec![merged, inconsistent]).unwrap_err(),
        super::RunRecordMergeError::Observations
    );
}

#[test]
fn run_record_collection_metadata_round_trips_and_preserves_merge_order() {
    let mut first = record("test.py::test_first", ExecutionOutcome::Passed, 1.0);
    first.engine = "vfy-independent-v1".to_owned();
    let empty_json = serde_json::to_string(&first).expect("serialize independent metadata");
    assert!(empty_json.contains("\"collection_reports\":[]"));
    assert!(empty_json.contains("\"deselected_nodeids\":[]"));
    let restored: RunRecord = serde_json::from_str(&empty_json).expect("restore empty metadata");
    assert_eq!(serde_json::to_string(&restored).unwrap(), empty_json);

    first.deselected_nodeids = vec!["test.py::test_z".to_owned(), "test.py::test_a".to_owned()];
    first.collection_reports.push(CollectionReport {
        nodeid: "test.py".to_owned(),
        outcome: CollectionOutcome::Passed,
        duration_ms: 0.25,
        payload: PhasePayload::default(),
    });
    let json = serde_json::to_string(&first).expect("serialize populated metadata");
    let restored: RunRecord = serde_json::from_str(&json).expect("restore populated metadata");
    assert_eq!(serde_json::to_string(&restored).unwrap(), json);
    let mut second = record("other.py::test", ExecutionOutcome::Passed, 2.0);
    second.engine = first.engine.clone();
    second.deselected_nodeids = vec!["other.py::test_b".to_owned()];
    second.collection_reports.push(CollectionReport {
        nodeid: "other.py".to_owned(),
        outcome: CollectionOutcome::Passed,
        duration_ms: 0.5,
        payload: PhasePayload::default(),
    });
    let merged = merge_run_records(vec![restored, second]).expect("merge collection metadata");
    assert_eq!(
        merged.deselected_nodeids,
        ["test.py::test_z", "test.py::test_a", "other.py::test_b"]
    );
    assert_eq!(
        merged
            .collection_reports
            .iter()
            .map(|report| report.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["test.py", "other.py"]
    );

    let legacy = record("legacy.py::test", ExecutionOutcome::Passed, 1.0);
    let legacy_json = serde_json::to_string(&legacy).expect("serialize legacy record");
    assert!(!legacy_json.contains("collection_reports"));
    assert!(!legacy_json.contains("deselected_nodeids"));
    let restored: RunRecord = serde_json::from_str(&legacy_json).expect("restore legacy record");
    assert!(restored.collection_reports.is_empty());
    assert!(restored.deselected_nodeids.is_empty());
    assert_eq!(serde_json::to_string(&restored).unwrap(), legacy_json);
}

#[test]
fn collection_reports_preserve_skips_without_creating_test_cases() {
    let mut skipped = record("test.py::test_passes", ExecutionOutcome::Passed, 1.0);
    skipped.collection_reports.push(CollectionReport {
        nodeid: "test_optional.py".to_owned(),
        outcome: CollectionOutcome::Skipped,
        duration_ms: 0.25,
        payload: PhasePayload {
            exception: Some("Skipped: optional dependency missing".into()),
            detail: "optional dependency missing".to_owned(),
            ..PhasePayload::default()
        },
    });
    skipped.counts = compute_run_counts(&skipped.cases, &skipped.collection_reports);
    let json = serde_json::to_string(&skipped).expect("serialize collection skip");
    let decoded: RunRecord = serde_json::from_str(&json).expect("restore collection skip");
    assert!(validate_run_record(&decoded).is_ok());
    let merged = merge_run_records(vec![decoded]).expect("merge collection skip");
    assert_eq!(merged.collected, 1);
    assert_eq!(merged.cases.len(), 1);
    assert_eq!(merged.counts.passed, 1);
    assert_eq!(merged.counts.skipped, 1);
    assert_eq!(merged.collection_reports[0].nodeid, "test_optional.py");
    assert_eq!(serde_json::to_string(&merged).unwrap(), json);
    let legacy = serde_json::to_value(record("test.py::test", ExecutionOutcome::Passed, 1.0))
        .expect("serialize legacy record");
    assert!(legacy.get("collection_reports").is_none());
}

#[test]
fn collection_errors_are_valid_without_collected_tests_and_reject_false_counts() {
    let reports = vec![CollectionReport {
        nodeid: "test_broken.py".to_owned(),
        outcome: CollectionOutcome::Failed,
        duration_ms: 0.125,
        payload: PhasePayload {
            exception: Some("ImportError: broken module".into()),
            stdout: "before import failed\n".to_owned(),
            detail: "broken module".to_owned(),
            ..PhasePayload::default()
        },
    }];
    let mut failed = RunRecord {
        internal_errors: Vec::new(),
        engine: "vfy-independent-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 0,
        counts: compute_run_counts(&[], &reports),
        duration_ms: 0.125,
        warnings: Vec::new(),
        cases: Vec::new(),
        collection_reports: reports,
        deselected_nodeids: Vec::new(),
    };
    assert!(validate_run_record(&failed).is_ok());
    assert_eq!(failed.counts.errors, 1);
    failed.counts.errors = 0;
    assert_eq!(
        validate_run_record(&failed),
        Err(super::RunRecordMergeError::CountMismatch)
    );
    failed.counts.errors = 1;
    failed.collection_reports[0].duration_ms = f64::NAN;
    assert_eq!(
        validate_run_record(&failed),
        Err(super::RunRecordMergeError::InvalidDuration)
    );
}

#[test]
fn phase_documents_preserve_field_order_and_full_round_trip() {
    for document in [EMPTY_PHASE_DOCUMENT, CAPTURED_PHASE_DOCUMENT] {
        let phase: PhaseResult = serde_json::from_str(document).expect("public phase document");
        let encoded = serde_json::to_string(&phase).expect("serialized public phase");
        assert_eq!(encoded, document);
        let restored: PhaseResult = serde_json::from_str(&encoded).expect("restored phase");
        assert_eq!(
            serde_json::to_string(&restored.clone()).expect("serialized cloned phase"),
            document
        );
    }
}

#[test]
fn phase_documents_retain_optional_defaults_and_ignore_unknown_fields() {
    let document = r#"{"outcome":"passed","phase":"call","duration_ms":1.25,"stdout":"","stderr":"","log":"","detail":"","future":{"version":2}}"#;
    let phase: PhaseResult = serde_json::from_str(document).expect("optional phase fields");
    assert_eq!(
        serde_json::to_string(&phase).expect("serialized default phase fields"),
        EMPTY_PHASE_DOCUMENT
    );
}

#[test]
fn phase_documents_reject_missing_required_and_malformed_fields() {
    let empty: serde_json::Value =
        serde_json::from_str(EMPTY_PHASE_DOCUMENT).expect("empty phase document");
    for field in [
        "outcome",
        "phase",
        "duration_ms",
        "stdout",
        "stderr",
        "log",
        "detail",
    ] {
        let mut missing = empty.clone();
        missing.as_object_mut().expect("phase object").remove(field);
        assert!(
            serde_json::from_value::<PhaseResult>(missing).is_err(),
            "{field}"
        );
    }
    for (field, value) in [
        ("outcome", serde_json::json!("unknown")),
        ("phase", serde_json::json!("unknown")),
        ("duration_ms", serde_json::json!(null)),
        ("exception", serde_json::json!(42)),
        ("stdout", serde_json::json!(42)),
        ("stderr", serde_json::json!(42)),
        ("log", serde_json::json!(42)),
        ("detail", serde_json::json!(42)),
        ("skip_location", serde_json::json!({"path": "test_case.py"})),
    ] {
        let mut malformed = empty.clone();
        malformed[field] = value;
        assert!(
            serde_json::from_value::<PhaseResult>(malformed).is_err(),
            "{field}"
        );
    }
    for field in ["outcome", "stdout", "skip_location"] {
        let duplicate = format!(
            "{{{field:?}:{},{}",
            empty[field],
            &EMPTY_PHASE_DOCUMENT[1..]
        );
        assert!(
            serde_json::from_str::<PhaseResult>(&duplicate).is_err(),
            "{field}"
        );
    }
}

#[test]
fn phase_documents_accept_existing_sequence_form_and_reject_missing_elements() {
    for document in [EMPTY_PHASE_DOCUMENT, CAPTURED_PHASE_DOCUMENT] {
        let object: serde_json::Value = serde_json::from_str(document).expect("phase document");
        let fields = [
            "outcome",
            "phase",
            "duration_ms",
            "exception",
            "stdout",
            "stderr",
            "log",
            "detail",
            "skip_location",
        ];
        let values: Vec<_> = fields.iter().map(|field| object[field].clone()).collect();
        let phase: PhaseResult = serde_json::from_value(serde_json::json!(values))
            .expect("existing phase sequence form");
        assert_eq!(
            serde_json::to_string(&phase).expect("serialized sequence phase"),
            document
        );
        let missing = &values[..values.len() - 1];
        assert!(serde_json::from_value::<PhaseResult>(serde_json::json!(missing)).is_err());
    }
}

#[test]
fn report_sections_transport_distinguishes_legacy_projection_from_plugin_edits() {
    let mut phase: PhaseResult = serde_json::from_str(EMPTY_PHASE_DOCUMENT).unwrap();
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Legacy));
    phase.set_report_sections(Some(Vec::new()));
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Sections));
    assert!(
        phase.clone().payload.is_none()
            && matches!(phase.clone().empty_payload, EmptyPhasePayload::Sections)
    );
    let encoded = serde_json::to_value(&phase).unwrap();
    assert_eq!(encoded["report_sections"], serde_json::json!([]));
    let mut restored: PhaseResult = serde_json::from_value(encoded).unwrap();
    assert!(
        restored.payload.is_none() && matches!(restored.empty_payload, EmptyPhasePayload::Sections)
    );
    restored.clear_capture();
    assert_eq!(restored.report_sections.as_deref(), Some([].as_slice()));
    restored.set_report_sections(None);
    assert!(
        restored.payload.is_none() && matches!(restored.empty_payload, EmptyPhasePayload::Legacy)
    );
    assert_eq!(
        serde_json::to_string(&restored).unwrap(),
        EMPTY_PHASE_DOCUMENT
    );

    let note = ReportSection {
        name: "plugin note".to_owned(),
        content: "retained".to_owned(),
    };
    for initial in [None, Some(Vec::new()), Some(vec![note.clone()])] {
        for next in [None, Some(Vec::new()), Some(vec![note.clone()])] {
            let mut phase = PhaseResult::new(
                ExecutionOutcome::Passed,
                ExecutionPhase::Call,
                1.25,
                PhasePayload {
                    report_sections: initial.clone(),
                    ..PhasePayload::default()
                },
            );
            phase.set_report_sections(next.clone());
            assert_eq!(phase.report_sections, next);
            match next.as_deref() {
                None => assert!(
                    phase.payload.is_none()
                        && matches!(phase.empty_payload, EmptyPhasePayload::Legacy)
                ),
                Some([]) => assert!(
                    phase.payload.is_none()
                        && matches!(phase.empty_payload, EmptyPhasePayload::Sections)
                ),
                Some(_) => assert!(phase.payload.is_some()),
            }
            let encoded = serde_json::to_value(phase.clone()).unwrap();
            assert_eq!(serde_json::to_value(&phase).unwrap(), encoded);
            let restored: PhaseResult = serde_json::from_value(encoded.clone()).unwrap();
            assert_eq!(serde_json::to_value(restored).unwrap(), encoded);
        }
    }

    let mut document: serde_json::Value = serde_json::from_str(CAPTURED_PHASE_DOCUMENT).unwrap();
    document["report_sections"] = serde_json::json!([
        {"name": "plugin note", "content": "first"},
        {"name": "plugin note", "content": "second"}
    ]);
    let restored: PhaseResult = serde_json::from_value(document.clone()).unwrap();
    assert_eq!(serde_json::to_value(restored).unwrap(), document);
    document["report_sections"][0]["content"] = serde_json::json!(3);
    assert!(serde_json::from_value::<PhaseResult>(document).is_err());
}

#[test]
fn explicit_empty_sections_survive_payload_mutation_and_compaction() {
    let mut phase: PhaseResult = serde_json::from_str(EMPTY_PHASE_DOCUMENT).unwrap();
    phase.set_report_sections(Some(Vec::new()));
    phase.set_exception(None);
    phase.set_skip_location(None);
    phase.set_log(String::new());
    phase.set_terminal_lead(String::new());
    phase.set_capture(String::new(), String::new(), String::new());
    phase.append_capture("", "", "");
    phase.append_detail("");
    phase.clear_capture();
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Sections));

    phase.append_capture("output", "error", "log");
    assert!(phase.payload.is_some());
    assert_eq!(phase.report_sections.as_deref(), Some([].as_slice()));
    phase.set_report_sections(None);
    assert_eq!(phase.stdout, "output");
    phase.set_report_sections(Some(Vec::new()));
    phase.clear_capture();
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Sections));

    phase.set_exception(Some(String::new()));
    phase.clear_capture();
    assert!(phase.payload.is_some());
    assert_eq!(phase.exception, Some(PythonText::from("")));
    phase.set_exception(None);
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Sections));

    phase.set_skip_location(Some(SkipLocation {
        path: "case.py".to_owned(),
        line: 1,
    }));
    phase.set_skip_location(None);
    phase.set_terminal_lead("\n".to_owned());
    phase.set_terminal_lead(String::new());
    phase.set_log("warning".to_owned());
    phase.set_log(String::new());
    assert!(phase.payload.is_none() && matches!(phase.empty_payload, EmptyPhasePayload::Sections));

    phase.append_detail("retained diagnostic");
    phase.clear_capture();
    assert!(phase.payload.is_some());
    assert_eq!(phase.detail, "retained diagnostic");
    assert_eq!(phase.report_sections.as_deref(), Some([].as_slice()));
}

#[test]
fn payload_compaction_retains_present_empty_report_summaries() {
    let summary = ReportSummary {
        keywords: Vec::new(),
        message: None,
        hypothesis_statistics: None,
        hypothesis_failing_examples: None,
    };
    let mut phase = PhaseResult::new(
        ExecutionOutcome::Passed,
        ExecutionPhase::Call,
        1.25,
        PhasePayload {
            stdout: "temporary output".to_owned(),
            report_sections: Some(Vec::new()),
            report_summary: Some(summary.clone()),
            ..PhasePayload::default()
        },
    );
    phase.clear_capture();
    phase.set_exception(None);
    phase.set_report_sections(None);
    phase.set_report_sections(Some(Vec::new()));
    assert!(phase.payload.is_some());
    assert_eq!(phase.report_summary, Some(summary));
    let encoded = serde_json::to_value(phase.clone()).unwrap();
    assert_eq!(serde_json::to_value(&phase).unwrap(), encoded);
    assert_eq!(encoded["report_sections"], serde_json::json!([]));
    assert_eq!(encoded["report_summary"]["keywords"], serde_json::json!([]));
    let restored: PhaseResult = serde_json::from_value(encoded.clone()).unwrap();
    assert!(restored.payload.is_some());
    assert_eq!(serde_json::to_value(restored).unwrap(), encoded);
}

#[test]
fn phase_capture_cleanup_retains_diagnostics_and_append_order() {
    let mut phase: PhaseResult =
        serde_json::from_str(CAPTURED_PHASE_DOCUMENT).expect("captured phase");
    phase.append_capture("next output\n", "next error\n", "\nWARNING next");
    phase.append_detail("\nadditional diagnostic");
    assert_eq!(phase.stdout, "setup output\nnext output\n");
    assert_eq!(phase.stderr, "setup error\nnext error\n");
    assert_eq!(phase.log, "WARNING setup\nWARNING next");
    phase.clear_capture();
    let mut expected: serde_json::Value =
        serde_json::from_str(CAPTURED_PHASE_DOCUMENT).expect("original phase document");
    expected["stdout"] = serde_json::json!("");
    expected["stderr"] = serde_json::json!("");
    expected["log"] = serde_json::json!("");
    expected["detail"] = serde_json::json!("skip diagnostic\nadditional diagnostic");
    assert_eq!(
        serde_json::to_value(&phase).expect("cleaned phase"),
        expected
    );

    let mut empty: PhaseResult = serde_json::from_str(EMPTY_PHASE_DOCUMENT).expect("empty phase");
    empty.set_exception(None);
    empty.set_skip_location(None);
    empty.set_log(String::new());
    empty.set_capture("temporary output".to_owned(), String::new(), String::new());
    empty.clear_capture();
    empty.append_capture("", "", "");
    empty.append_detail("");
    assert_eq!(
        serde_json::to_string(&empty).expect("cleaned empty phase"),
        EMPTY_PHASE_DOCUMENT
    );
    empty.set_exception(Some(String::new()));
    empty.clear_capture();
    let mut expected: serde_json::Value =
        serde_json::from_str(EMPTY_PHASE_DOCUMENT).expect("empty phase document");
    expected["exception"] = serde_json::json!("");
    let encoded = serde_json::to_string(&empty.clone()).expect("present empty exception");
    let mut restored: PhaseResult =
        serde_json::from_str(&encoded).expect("restored empty exception");
    restored.clear_capture();
    assert_eq!(
        serde_json::to_value(&restored).expect("retained exception"),
        expected
    );
    empty.set_exception(None);
    assert_eq!(
        serde_json::to_string(&empty).expect("removed exception"),
        EMPTY_PHASE_DOCUMENT
    );
}

#[test]
fn subtest_skip_location_round_trip_preserves_old_records() {
    let old = r#"{"outcome":"skipped","context":"(value=1)","exception":"Skipped: later","detail":"skip diagnostic"}"#;
    let mut subtest: SubtestResult = serde_json::from_str(old).expect("old subtest record");
    assert!(subtest.skip_location.is_none());
    assert_eq!(
        serde_json::to_value(&subtest).expect("serialized old subtest"),
        serde_json::from_str::<serde_json::Value>(old).expect("old subtest document")
    );
    subtest.skip_location = Some(SkipLocation {
        path: "tests/test_case.py".to_owned(),
        line: 12,
    });
    let json = serde_json::to_string(&subtest).expect("serialized subtest location");
    let restored: SubtestResult = serde_json::from_str(&json).expect("restored subtest location");
    let location = restored
        .skip_location
        .expect("retained subtest skip location");
    assert_eq!(
        (location.path.as_str(), location.line),
        ("tests/test_case.py", 12)
    );
}

fn phase(phase: ExecutionPhase, outcome: ExecutionOutcome) -> PhaseResult {
    PhaseResult::new(
        outcome,
        phase,
        1.25,
        PhasePayload {
            exception: None,
            stdout: String::new(),
            stderr: String::new(),
            log: String::new(),
            detail: String::new(),
            skip_location: None,
            terminal_lead: String::new(),
            report_sections: None,
            report_summary: None,
        },
    )
}

fn case(nodeid: &str, phases: Vec<PhaseResult>, subtests: Vec<SubtestResult>) -> CaseResult {
    let mut case = CaseResult::new(nodeid.to_owned(), 1.0, phases);
    case.set_subtests(subtests.into_boxed_slice());
    case
}

fn record(nodeid: &str, outcome: ExecutionOutcome, duration_ms: f64) -> RunRecord {
    let cases = vec![case(
        nodeid,
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
            phase(ExecutionPhase::Call, outcome),
        ],
        Vec::new(),
    )];
    RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: compute_counts(&cases),
        duration_ms,
        warnings: Vec::new(),
        cases,
    }
}

#[test]
fn counts_keep_phase_errors_separate_from_test_and_subtest_failures() {
    let cases = vec![
        {
            let mut case = CaseResult::new(
                "test_example.py::test_pass".to_owned(),
                3.75,
                vec![
                    phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
                    phase(ExecutionPhase::Call, ExecutionOutcome::Passed),
                    phase(ExecutionPhase::Teardown, ExecutionOutcome::Passed),
                ],
            );
            case.set_subtests(
                vec![
                    SubtestResult {
                        report: None,
                        outcome: ExecutionOutcome::Passed,
                        context: "(value=1)".to_owned(),
                        exception: None,
                        detail: String::new(),
                        skip_location: None,
                        terminal_lead: String::new(),
                    },
                    SubtestResult {
                        report: None,
                        outcome: ExecutionOutcome::Failed,
                        context: "(value=2)".to_owned(),
                        exception: Some("AssertionError".into()),
                        detail: String::new(),
                        skip_location: None,
                        terminal_lead: String::new(),
                    },
                    SubtestResult {
                        report: None,
                        outcome: ExecutionOutcome::XPassed,
                        context: "(value=3)".to_owned(),
                        exception: None,
                        detail: "unexpected success".to_owned(),
                        skip_location: None,
                        terminal_lead: String::new(),
                    },
                ]
                .into_boxed_slice(),
            );
            case
        },
        CaseResult::new(
            "test_example.py::test_failure".to_owned(),
            2.5,
            vec![
                phase(ExecutionPhase::Call, ExecutionOutcome::Failed),
                phase(ExecutionPhase::Teardown, ExecutionOutcome::Error),
            ],
        ),
        CaseResult::new(
            "test_example.py::test_setup_failure".to_owned(),
            1.25,
            vec![phase(ExecutionPhase::Setup, ExecutionOutcome::Error)],
        ),
    ];

    let counts = compute_counts(&cases);

    assert_eq!(
        cases
            .iter()
            .map(CaseResult::failure_count)
            .collect::<Vec<_>>(),
        [1, 2, 1]
    );
    assert_eq!(counts.passed, 1);
    assert_eq!(counts.failed, 2);
    assert_eq!(counts.errors, 2);
    assert_eq!(counts.subtests_passed, 1);
    assert_eq!(counts.xpassed, 1);
}

#[test]
fn expected_failures_capture_and_properties_survive_serialization() {
    let mut skipped = phase(ExecutionPhase::Setup, ExecutionOutcome::XFailed);
    skipped.set_exception(Some("[NOTRUN] requires remote service".to_owned()));
    skipped.set_capture(
        "setup output\n".to_owned(),
        "setup error\n".to_owned(),
        String::new(),
    );
    let case = {
        let mut case = CaseResult::new(
            "test_example.py::test_service".to_owned(),
            1.25,
            vec![skipped],
        );
        case.set_user_properties(
            vec![
                ("phase".to_owned(), "setup".to_owned()),
                ("phase".to_owned(), "teardown".to_owned()),
            ]
            .into_boxed_slice(),
        );
        case.set_xml_attributes(vec![("assertions".to_owned(), "7".to_owned())].into_boxed_slice());
        case
    };

    let serialized = serde_json::to_value(&case).expect("serialize an execution result");
    assert_eq!(
        serialized["user_properties"],
        serde_json::json!([["phase", "setup"], ["phase", "teardown"]])
    );
    assert_eq!(
        serialized["xml_attributes"],
        serde_json::json!([["assertions", "7"]])
    );
    assert_eq!(serialized["phases"][0]["phase"], "setup");
    assert_eq!(serialized["phases"][0]["outcome"], "xfailed");
    let decoded: CaseResult =
        serde_json::from_value(serialized.clone()).expect("decode an execution result");
    assert_eq!(
        serde_json::to_value(&decoded).expect("serialize the decoded execution result"),
        serialized
    );
    let counts = compute_counts(&[decoded]);
    assert_eq!(counts.xfailed, 1);
    assert_eq!(counts.passed, 0);
    assert_eq!(counts.failed, 0);
}

#[test]
fn first_failure_ignores_xfails_and_non_strict_xpasses_but_includes_subtests() {
    let mut expected = case(
        "test_example.py::test_expected",
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
            phase(ExecutionPhase::Call, ExecutionOutcome::XFailed),
            phase(ExecutionPhase::Teardown, ExecutionOutcome::Passed),
        ],
        Vec::new(),
    );
    let mut non_strict_xpass = case(
        "test_example.py::test_xpass",
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
            phase(ExecutionPhase::Call, ExecutionOutcome::XPassed),
        ],
        Vec::new(),
    );
    let mut subtest = case(
        "test_example.py::test_subtests",
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
            phase(ExecutionPhase::Call, ExecutionOutcome::Passed),
            phase(ExecutionPhase::Teardown, ExecutionOutcome::Error),
        ],
        vec![SubtestResult {
            report: None,
            outcome: ExecutionOutcome::Failed,
            context: "(value=2)".to_owned(),
            exception: Some("AssertionError".into()),
            detail: "assert value == 1".to_owned(),
            skip_location: None,
            terminal_lead: String::new(),
        }],
    );
    expected.duration_ms = 1.0;
    non_strict_xpass.duration_ms = 1.0;
    subtest.duration_ms = 1.0;
    let cases = vec![expected, non_strict_xpass, subtest];
    let result = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: cases.len(),
        counts: compute_counts(&cases),
        duration_ms: 3.0,
        warnings: Vec::new(),
        cases,
    };

    assert_eq!(
        result.first_failure(),
        Some(("test_example.py::test_subtests", ExecutionPhase::Call))
    );
}

#[test]
fn first_failure_treats_strict_xpass_as_a_real_call_failure() {
    let mut strict_xpass = phase(ExecutionPhase::Call, ExecutionOutcome::Failed);
    strict_xpass.append_detail("[XPASS(strict)] expected failure");
    let cases = vec![case(
        "test_example.py::test_strict_xpass",
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Passed),
            strict_xpass,
        ],
        Vec::new(),
    )];
    let result = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: compute_counts(&cases),
        duration_ms: 1.0,
        warnings: Vec::new(),
        cases,
    };

    assert_eq!(
        result.first_failure(),
        Some(("test_example.py::test_strict_xpass", ExecutionPhase::Call))
    );
}

#[test]
fn warning_record_roundtrip_preserves_legacy_and_structured_metadata() {
    let legacy = serde_json::json!({
        "message": "rendered warning", "nodeid": "", "filename": "warning.py", "lineno": 4
    });
    let record: WarningRecord =
        serde_json::from_value(legacy.clone()).expect("legacy warning record decodes");
    assert_eq!(
        serde_json::to_value(record).expect("warning encodes"),
        legacy
    );
    let structured = serde_json::json!({
        "message": "rendered warning", "nodeid": "", "filename": "warning.py", "lineno": 4,
        "category": "PytestRemovedIn10Warning", "raw_message": "original payload"
    });
    let record: WarningRecord =
        serde_json::from_value(structured.clone()).expect("structured warning record decodes");
    assert_eq!(
        serde_json::to_value(record).expect("warning encodes"),
        structured
    );
}

#[test]
fn file_group_merge_preserves_input_order_and_aggregates_authoritative_counts() {
    let mut first = record("test_first.py::test_first", ExecutionOutcome::Passed, 2.5);
    first.collected = 2;
    first.warnings.push(WarningRecord {
        message: "first warning".to_owned(),
        nodeid: "test_first.py::test_first".to_owned(),
        filename: "test_first.py".to_owned(),
        lineno: 1,
        ..WarningRecord::default()
    });
    let mut second = record(
        "test_second.py::test_second",
        ExecutionOutcome::Failed,
        4.25,
    );
    second.collected = 3;
    second.warnings.push(WarningRecord {
        message: "second warning".to_owned(),
        nodeid: "test_second.py::test_second".to_owned(),
        filename: "test_second.py".to_owned(),
        lineno: 2,
        ..WarningRecord::default()
    });

    let merged = merge_run_records(vec![first, second]).expect("merge native file groups");

    assert_eq!(merged.collected, 5);
    assert!((merged.duration_ms - 6.75).abs() < f64::EPSILON);
    assert_eq!(merged.counts.passed, 1);
    assert_eq!(merged.counts.failed, 1);
    assert_eq!(
        merged
            .cases
            .iter()
            .map(|case| case.nodeid.as_str())
            .collect::<Vec<_>>(),
        ["test_first.py::test_first", "test_second.py::test_second"]
    );
    assert_eq!(
        merged
            .warnings
            .iter()
            .map(|warning| warning.message.as_str())
            .collect::<Vec<_>>(),
        ["first warning", "second warning"]
    );
}

fn assert_invalid_record(record: RunRecord, expected: &super::RunRecordMergeError) {
    assert_eq!(&validate_run_record(&record).unwrap_err(), expected);
    assert_eq!(&merge_run_records(vec![record]).unwrap_err(), expected);
}

#[test]
fn file_group_merge_rejects_invalid_or_conflicting_records() {
    assert_eq!(
        merge_run_records(Vec::new()).unwrap_err().to_string(),
        "cannot merge an empty set of run records"
    );

    let mut inconsistent_counts = record("test.py::test", ExecutionOutcome::Passed, 1.0);
    inconsistent_counts.counts = Counts {
        failed: 1,
        ..Counts::default()
    };
    assert_invalid_record(
        inconsistent_counts,
        &super::RunRecordMergeError::CountMismatch,
    );

    let mut trusted_record = record("test.py::test", ExecutionOutcome::Passed, 1.0);
    trusted_record.execution_identity = Some(super::ExecutionRecordIdentity {
        producer_pid: 1,
        session_token: "a".repeat(64),
        observer_nonce: "b".repeat(64),
        session_depth: 0,
        rootdir: "/tmp".to_owned(),
    });
    assert_invalid_record(
        trusted_record,
        &super::RunRecordMergeError::ExecutionIdentity,
    );

    let mut invalid_duration = record("test.py::test", ExecutionOutcome::Passed, f64::NAN);
    assert_invalid_record(
        invalid_duration,
        &super::RunRecordMergeError::InvalidDuration,
    );
    invalid_duration = record("test.py::test", ExecutionOutcome::Passed, 1.0);
    invalid_duration.collected = 0;
    assert_invalid_record(
        invalid_duration,
        &super::RunRecordMergeError::CollectedCountTooSmall,
    );

    let mut invalid_phases = record("test.py::test", ExecutionOutcome::Passed, 1.0);
    invalid_phases.cases[0].phases =
        vec![phase(ExecutionPhase::Call, ExecutionOutcome::Passed)].into();
    assert_invalid_record(
        invalid_phases,
        &super::RunRecordMergeError::InvalidCasePhases("test.py::test".to_owned()),
    );

    let setup_failure_case = case(
        "test.py::test_setup_failure",
        vec![
            phase(ExecutionPhase::Setup, ExecutionOutcome::Error),
            phase(ExecutionPhase::Teardown, ExecutionOutcome::Passed),
        ],
        Vec::new(),
    );
    let setup_failure = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: compute_counts(std::slice::from_ref(&setup_failure_case)),
        duration_ms: 1.0,
        warnings: Vec::new(),
        cases: vec![setup_failure_case],
    };
    assert!(validate_run_record(&setup_failure).is_ok());
    assert!(merge_run_records(vec![setup_failure]).is_ok());
}

#[test]
fn file_group_merge_accepts_a_collected_empty_run() {
    let empty = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 0,
        counts: Counts::default(),
        duration_ms: 0.0,
        warnings: Vec::new(),
        cases: Vec::new(),
    };

    let merged = merge_run_records(vec![empty]).expect("merge a valid empty native run");

    assert_eq!(merged.collected, 0);
    assert!(merged.cases.is_empty());
    assert_eq!(merged.counts, Counts::default());
}

#[test]
fn file_group_merge_preserves_valid_repeated_nodeids() {
    let merged = merge_run_records(vec![
        record("test.py::test_same", ExecutionOutcome::Passed, 1.0),
        record("test.py::test_same", ExecutionOutcome::Failed, 2.0),
    ])
    .expect("preserve native records for repeated pytest paths");

    assert_eq!(merged.cases.len(), 2);
    assert_eq!(merged.cases[0].nodeid, merged.cases[1].nodeid);
    assert_eq!(merged.counts.passed, 1);
    assert_eq!(merged.counts.failed, 1);
}

const EMPTY_CASE_DOCUMENT: &str =
    r#"{"nodeid":"test_case","duration_ms":1.25,"phases":[],"subtests":[]}"#;

#[test]
fn case_documents_require_subtests_and_default_omitted_metadata() {
    let case: CaseResult =
        serde_json::from_str(EMPTY_CASE_DOCUMENT).expect("case with omitted optional metadata");
    assert_eq!(
        serde_json::to_string(&case).expect("serialized default case metadata"),
        EMPTY_CASE_DOCUMENT
    );

    let mut explicit_empty: serde_json::Value =
        serde_json::from_str(EMPTY_CASE_DOCUMENT).expect("public case document");
    explicit_empty["user_properties"] = serde_json::json!([]);
    explicit_empty["xml_attributes"] = serde_json::json!([]);
    let case: CaseResult =
        serde_json::from_value(explicit_empty).expect("explicit empty case metadata");
    assert_eq!(
        serde_json::to_string(&case).expect("serialized explicit empty case metadata"),
        EMPTY_CASE_DOCUMENT
    );

    let mut missing: serde_json::Value =
        serde_json::from_str(EMPTY_CASE_DOCUMENT).expect("public case document");
    missing
        .as_object_mut()
        .expect("case document object")
        .remove("subtests");
    let error = serde_json::from_value::<CaseResult>(missing)
        .expect_err("subtests remains a required public field");
    assert!(error.to_string().contains("missing field `subtests`"));
}

#[test]
fn case_documents_reject_null_and_malformed_auxiliary_arrays() {
    let document: serde_json::Value =
        serde_json::from_str(EMPTY_CASE_DOCUMENT).expect("public case document");
    for field in ["subtests", "user_properties", "xml_attributes"] {
        for value in [
            serde_json::Value::Null,
            serde_json::json!(true),
            serde_json::json!({}),
            serde_json::json!([null]),
        ] {
            let mut malformed = document.clone();
            malformed[field] = value.clone();
            assert!(
                serde_json::from_value::<CaseResult>(malformed).is_err(),
                "case field {field} must reject {value}"
            );
        }
    }
    for field in ["user_properties", "xml_attributes"] {
        for value in [
            serde_json::json!([["missing-value"]]),
            serde_json::json!([["owner", 1]]),
        ] {
            let mut malformed = document.clone();
            malformed[field] = value.clone();
            assert!(
                serde_json::from_value::<CaseResult>(malformed).is_err(),
                "case field {field} must retain string-pair validation for {value}"
            );
        }
    }
}

#[test]
fn case_documents_reject_duplicate_fields_and_ignore_unknown_metadata() {
    let document: serde_json::Value =
        serde_json::from_str(EMPTY_CASE_DOCUMENT).expect("public case document");
    for field in ["subtests", "user_properties", "xml_attributes"] {
        let mut without_field = document.clone();
        without_field
            .as_object_mut()
            .expect("case document object")
            .remove(field);
        let prefix = serde_json::to_string(&without_field).expect("case without duplicate field");
        let duplicate = format!(
            "{},\"{field}\":[],\"{field}\":[]}}",
            prefix
                .strip_suffix('}')
                .expect("case document closing brace")
        );
        let error = serde_json::from_str::<CaseResult>(&duplicate)
            .expect_err("duplicate case metadata fields remain invalid");
        assert!(
            error
                .to_string()
                .contains(&format!("duplicate field `{field}`")),
            "unexpected diagnostic for duplicate {field}: {error}"
        );
    }

    let mut extended = document;
    extended["future"] = serde_json::json!({"version": 2});
    let case: CaseResult =
        serde_json::from_value(extended).expect("unknown case metadata remains forward compatible");
    assert_eq!(
        serde_json::to_string(&case).expect("serialized case with unknown metadata"),
        EMPTY_CASE_DOCUMENT
    );
}

#[test]
fn late_metadata_changes_preserve_subtests_and_attribute_duplicates() {
    let document = serde_json::json!({
        "nodeid": "test_example.py::test_subtests",
        "duration_ms": 1.0,
        "phases": [],
        "subtests": [{
            "outcome": "failed",
            "context": "(value=1)",
            "exception": "AssertionError",
            "detail": "assert value == 2"
        }],
        "user_properties": [["phase", "call"], ["phase", "teardown"]],
        "xml_attributes": [["assertions", "1"]]
    });
    let mut case: CaseResult =
        serde_json::from_value(document.clone()).expect("decode case metadata");
    assert_eq!(
        serde_json::to_value(&case).expect("serialize case metadata"),
        document
    );

    case.set_user_properties(Box::default());
    case.append_xml_attributes(Vec::new());
    case.append_xml_attributes(vec![
        ("assertions".to_owned(), "2".to_owned()),
        ("assertions".to_owned(), "3".to_owned()),
    ]);
    let mut expected = document;
    expected
        .as_object_mut()
        .expect("case document")
        .remove("user_properties");
    expected["xml_attributes"] = serde_json::json!([
        ["assertions", "1"],
        ["assertions", "2"],
        ["assertions", "3"]
    ]);
    assert_eq!(
        serde_json::to_value(&case).expect("serialize late metadata"),
        expected
    );

    case.set_user_properties(vec![("late".to_owned(), "value".to_owned())].into_boxed_slice());
    case.set_xml_attributes(Box::default());
    case.set_subtests(Box::default());
    assert_eq!(
        serde_json::to_value(&case).expect("serialize replacement properties"),
        serde_json::json!({
            "nodeid": "test_example.py::test_subtests",
            "duration_ms": 1.0,
            "phases": [],
            "subtests": [],
            "user_properties": [["late", "value"]]
        })
    );
    case.set_user_properties(Box::default());
    assert_eq!(
        serde_json::to_value(&case).expect("serialize cleared metadata"),
        serde_json::json!({
            "nodeid": "test_example.py::test_subtests",
            "duration_ms": 1.0,
            "phases": [],
            "subtests": []
        })
    );
}

#[test]
fn cloned_phase_payloads_share_until_mutated_and_roundtrip_independently() {
    let original = PhaseResult::new(
        ExecutionOutcome::Passed,
        ExecutionPhase::Call,
        1.25,
        PhasePayload {
            stdout: "captured output".to_owned(),
            report_sections: Some(vec![ReportSection {
                name: "plugin section".to_owned(),
                content: "original".to_owned(),
            }]),
            ..PhasePayload::default()
        },
    );
    let original_wire = serde_json::to_value(&original).unwrap();
    let mut changed = original.clone();
    changed.set_report_sections(original.report_sections.clone());
    changed.set_report_summary(original.report_summary.clone());
    match (&original.payload, &changed.payload) {
        (Some(first), Some(second)) => {
            assert!(std::sync::Arc::ptr_eq(first, second));
        }
        _ => panic!("captured phase must retain its payload"),
    }
    changed.set_report_sections(Some(vec![ReportSection {
        name: "plugin section".to_owned(),
        content: "changed".to_owned(),
    }]));
    changed.clear_capture();
    assert_eq!(serde_json::to_value(&original).unwrap(), original_wire);
    assert_eq!(original.stdout, "captured output");
    assert_eq!(
        original.report_sections.as_ref().unwrap()[0].content,
        "original"
    );
    assert!(changed.stdout.is_empty());
    assert_eq!(
        changed.report_sections.as_ref().unwrap()[0].content,
        "changed"
    );
    let changed_wire = serde_json::to_value(&changed).unwrap();
    let restored: PhaseResult = serde_json::from_value(changed_wire.clone()).unwrap();
    assert_eq!(serde_json::to_value(restored).unwrap(), changed_wire);
}
