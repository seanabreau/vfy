use super::{
    FailedPhase, TestAttempt, TestOutcome, TestRun, normalize_test_results,
    record_test_execution_artifact, record_test_run, record_test_run_artifact, write_test_phases,
    write_test_reports, write_test_run_result,
};

#[test]
fn history_write_preserves_sorted_custom_fields_and_array_order() {
    let directory = tempfile::tempdir().expect("result directory");
    let path = directory.path().join("result.json");
    std::fs::write(&path, r#"{"z":[{"z":2,"a":"é\n"},1],"a":true}"#).expect("unsorted nested JSON");
    record_test_run(
        &path,
        &TestRun {
            infrastructure_failed: false,
            tests: Vec::new(),
        },
        "run",
        TestAttempt::Primary,
    )
    .expect("atomic history write");

    assert_eq!(
        std::fs::read_to_string(path).expect("result bytes"),
        "{\n  \"a\": true,\n  \"tests\": {},\n  \"z\": [\n    {\n      \"a\": \"é\\n\",\n      \"z\": 2\n    },\n    1\n  ]\n}"
    );
}

#[test]
fn history_entry_field_merge_matches_canonical_json_with_custom_values() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.json");
    let initial = serde_json::json!({
        "z": {"z": false, "a": [null, 3]},
        "tests": {
            "unchanged": [true, {"z": 2, "a": 1}],
            "existing": {
                "z": [null, {"z": 2, "a": 1}],
                "observations": [null, 3, false, {"z": 2, "a": 1}],
                "observations_after": "after observations",
                "middle": "between replacements",
                "duration_ms": {"custom": true},
                "duration_ms_before": "after duration",
                "a": "before replacements"
            }
        },
        "version": {"discard": true}
    });
    let mut compact = initial.clone();
    compact["tests"]["unchanged"] = serde_json::json!({"custom": [true, {"z": 2, "a": 1}]});
    compact["tests"]["existing"]["duration_ms"] = serde_json::json!(1.25);
    compact["tests"]["existing"]["observations"] = serde_json::json!([{
        "attempt": "primary", "duration_ms": 1.0, "failed_phase": "setup",
        "outcome": "failed", "run_id": "previous", "custom": [null, {"z": 2, "a": 1}]
    }]);
    for (initial, is_compact) in [(initial, false), (compact, true)] {
        std::fs::write(&path, initial.to_string()).expect("initial history");
        assert_eq!(
            matches!(
                super::history::read_history(&path),
                super::History::Compact(_)
            ),
            is_compact
        );
        for attempt in [TestAttempt::Primary, TestAttempt::Confirmation] {
            for call_duration_ms in [None, Some(2.0)] {
                std::fs::write(&path, initial.to_string()).expect("initial history");
                let run = TestRun {
                    infrastructure_failed: true,
                    tests: ["existing", "new"]
                        .map(|nodeid| super::TestReport {
                            nodeid: nodeid.to_owned(),
                            outcome: TestOutcome::Failed,
                            failed_phase: Some(FailedPhase::Teardown),
                            duration_ms: 3.0,
                            call_duration_ms,
                        })
                        .to_vec(),
                };
                let mut expected = initial.clone();
                expected
                    .as_object_mut()
                    .expect("history object")
                    .remove("version");
                for nodeid in ["existing", "new"] {
                    let tests = expected["tests"].as_object_mut().expect("tests object");
                    let entry = tests.entry(nodeid).or_insert_with(|| serde_json::json!({}));
                    if attempt == TestAttempt::Primary
                        && let Some(duration) = call_duration_ms
                    {
                        entry["duration_ms"] = serde_json::json!(duration);
                    }
                    if entry.get("observations").is_none() {
                        entry["observations"] = serde_json::json!([]);
                    }
                    entry["observations"].as_array_mut().expect("observations").push(
                        serde_json::json!({
                            "attempt": if attempt == TestAttempt::Primary { "primary" } else { "confirmation" },
                            "duration_ms": 3.0,
                            "failed_phase": "teardown",
                            "outcome": "failed",
                            "run_id": "é\n"
                        }),
                    );
                }
                expected.sort_all_objects();

                let recorded =
                    record_test_run(&path, &run, "é\n", attempt).expect("record history");

                assert_eq!(recorded.failed, ["existing", "new"]);
                assert!(recorded.infrastructure_failed);
                assert_eq!(
                    std::fs::read(&path).expect("history bytes"),
                    serde_json::to_vec_pretty(&expected).expect("canonical JSON bytes")
                );
            }
        }
    }
}

#[test]
fn history_fallback_retains_canonical_values_and_reset_behavior() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.json");
    for text in [
        r#"{"tests":{"test":{"duration_ms":null,"observations":[]}}}"#,
        r#"{"tests":{"test":{"observations":[{"custom":true}]}}}"#,
        r#"{"tests":{"test":{"observations":[{"attempt":"primary","duration_ms":1,"failed_phase":null,"outcome":"passed","run_id":"old"}]}}}"#,
        r#"{"tests":{"test":{"duration_ms":1,"duration_ms":3,"observations":[]}}}"#,
        r#"{"tests":[],"tests":{"test":{"duration_ms":2}}}"#,
        r#"{"$serde_json::private::Number":"4","tests":{}}"#,
        r#"{"tests":{"test":{"$serde_json::private::Number":"4","duration_ms":3}}}"#,
        r#"{"tests":{"$serde_json::private::Number":{"duration_ms":2,"observations":[]}},"keep":true}"#,
        r#"{"tests":{},"custom":{"$serde_json::private::RawValue":"null","z":2}}"#,
        r#"{"tests":{"test":{"duration_ms":1e999}},"custom":1e999}"#,
        "{invalid",
        "null",
        "[]",
    ] {
        std::fs::write(&path, text).expect("history fixture");
        let mut expected = serde_json::from_str::<serde_json::Value>(text)
            .ok()
            .filter(serde_json::Value::is_object)
            .unwrap_or_else(|| serde_json::json!({}));
        let root = expected.as_object_mut().expect("canonical root");
        root.remove("version");
        root.entry("tests").or_insert_with(|| serde_json::json!({}));
        expected.sort_all_objects();

        record_test_run(
            &path,
            &TestRun {
                infrastructure_failed: false,
                tests: Vec::new(),
            },
            "run",
            TestAttempt::Primary,
        )
        .expect("canonical history record");

        assert_eq!(
            std::fs::read(&path).expect("history bytes"),
            serde_json::to_vec_pretty(&expected).expect("canonical bytes"),
            "{text}"
        );
    }
}

#[test]
fn test_run_write_matches_sorted_json_bytes() {
    let directory = tempfile::tempdir().expect("result directory");
    let path = directory.path().join("result.json");
    for reports in [
        Vec::new(),
        vec![
            ("z::passed", "setup", "passed", 0.0),
            ("z::passed", "call", "passed", 2.25),
            ("a::skipped", "setup", "skipped", 1.0),
            ("é::failed[\"\\\n]", "call", "failed", 0.125),
            ("z::passed", "teardown", "failed", 3.0),
        ],
    ] {
        for infrastructure_failed in [false, true] {
            let run = normalize_test_results(
                reports.iter().map(|&(nodeid, phase, outcome, duration)| {
                    (
                        nodeid.to_owned(),
                        phase.to_owned(),
                        outcome.to_owned(),
                        duration,
                    )
                }),
                infrastructure_failed,
            )
            .expect("normalized reports");
            let mut value = serde_json::to_value(&run).expect("JSON reference");
            value.sort_all_objects();
            let expected = serde_json::to_vec_pretty(&value).expect("reference bytes");

            write_test_run_result(&path, &run).expect("direct result write");

            assert_eq!(std::fs::read(&path).expect("result bytes"), expected);
        }
    }
}

#[test]
fn typed_phase_write_matches_owned_reports() {
    let directory = tempfile::tempdir().expect("result directory");
    let typed_path = directory.path().join("typed.json");
    let owned_path = directory.path().join("owned.json");
    let reports = [
        ("z", FailedPhase::Setup, TestOutcome::Passed, 1.0),
        ("a", FailedPhase::Setup, TestOutcome::Skipped, 0.5),
        ("é", FailedPhase::Setup, TestOutcome::Failed, 0.125),
        ("z", FailedPhase::Call, TestOutcome::Passed, 2.0),
        ("é", FailedPhase::Call, TestOutcome::Failed, 0.25),
        ("a", FailedPhase::Teardown, TestOutcome::Passed, 0.5),
        ("z", FailedPhase::Call, TestOutcome::Passed, 3.0),
        ("é", FailedPhase::Teardown, TestOutcome::Failed, 0.5),
    ];
    for count in [0, reports.len()] {
        for infrastructure_failed in [false, true] {
            write_test_phases(
                &typed_path,
                reports[..count].iter().copied(),
                infrastructure_failed,
            )
            .expect("typed write");
            write_test_reports(
                &owned_path,
                reports[..count]
                    .iter()
                    .map(|&(nodeid, phase, outcome, duration)| {
                        (
                            nodeid.to_owned(),
                            phase.as_str().to_owned(),
                            outcome.as_str().to_owned(),
                            duration,
                        )
                    }),
                infrastructure_failed,
            )
            .expect("owned write");

            assert_eq!(
                std::fs::read(&typed_path).expect("typed bytes"),
                std::fs::read(&owned_path).expect("owned bytes")
            );
        }
    }
}

#[test]
fn invalid_typed_report_preserves_destination_and_owned_diagnostic() {
    let directory = tempfile::tempdir().expect("result directory");
    let typed_path = directory.path().join("typed.json");
    let owned_path = directory.path().join("owned.json");
    for (nodeid, duration) in [
        ("", 1.0),
        ("bad", -1.0),
        ("bad", f64::NAN),
        ("bad", f64::INFINITY),
    ] {
        std::fs::write(&typed_path, b"previous").expect("typed prior result");
        std::fs::write(&owned_path, b"previous").expect("owned prior result");
        let reports = [
            ("valid", FailedPhase::Call, TestOutcome::Passed, 1.0),
            (nodeid, FailedPhase::Call, TestOutcome::Passed, duration),
        ];
        let typed_error =
            write_test_phases(&typed_path, reports, false).expect_err("invalid typed suffix");
        let owned_error = write_test_reports(
            &owned_path,
            reports
                .into_iter()
                .map(|(nodeid, phase, outcome, duration)| {
                    (
                        nodeid.to_owned(),
                        phase.as_str().to_owned(),
                        outcome.as_str().to_owned(),
                        duration,
                    )
                }),
            false,
        )
        .expect_err("invalid owned suffix");

        assert_eq!(typed_error.to_string(), owned_error.to_string());
        assert_eq!(
            std::fs::read(&typed_path).expect("typed prior bytes"),
            b"previous"
        );
        assert_eq!(
            std::fs::read(&owned_path).expect("owned prior bytes"),
            b"previous"
        );
    }
}

#[test]
fn controller_projection_preserves_history_and_confirmation_outcomes() {
    let directory = tempfile::tempdir().expect("result directory");
    let result = directory.path().join("result.json");
    let full_history = directory.path().join("full.json");
    let controller_history = directory.path().join("controller.json");
    for infrastructure_failed in [false, true] {
        write_test_reports(
            &result,
            [
                ("z::passed", "call", "passed", 1.25),
                ("a::failed", "call", "failed", 2.5),
                ("m::skipped", "setup", "skipped", 0.0),
                ("b::failed", "teardown", "failed", 0.5),
            ]
            .into_iter()
            .map(|(nodeid, phase, outcome, duration)| {
                (
                    nodeid.to_owned(),
                    phase.to_owned(),
                    outcome.to_owned(),
                    duration,
                )
            }),
            infrastructure_failed,
        )
        .expect("result artifact");
        for attempt in [TestAttempt::Primary, TestAttempt::Confirmation] {
            let full = record_test_run_artifact(&full_history, &result, "run", attempt)
                .expect("public history record");
            let projected =
                record_test_execution_artifact(&controller_history, &result, "run", attempt)
                    .expect("controller history record");
            assert_eq!(projected.failed, full.failed);
            assert_eq!(projected.infrastructure_failed, infrastructure_failed);
            assert_eq!(full.outcomes.len(), 4);
            match attempt {
                TestAttempt::Primary => assert!(projected.outcomes.is_empty()),
                TestAttempt::Confirmation => assert_eq!(projected.outcomes, full.outcomes),
            }
            assert_eq!(
                std::fs::read(&controller_history).expect("controller history"),
                std::fs::read(&full_history).expect("public history"),
            );
        }
    }
    let before = std::fs::read(&controller_history).expect("recorded history");
    std::fs::write(&result, b"{invalid").expect("invalid result");
    assert!(
        record_test_execution_artifact(
            &controller_history,
            &result,
            "rejected",
            TestAttempt::Primary,
        )
        .is_err()
    );
    assert_eq!(
        std::fs::read(&controller_history).expect("preserved history"),
        before
    );
}
#[test]
fn ordinary_result_artifacts_use_per_report_parsing_with_canonical_values() {
    for reports in [
        Vec::new(),
        vec![
            ("é::test", "setup", "passed", 0.25),
            ("é::test", "call", "failed", 1.25),
            ("other", "setup", "skipped", 0.0),
        ],
    ] {
        for infrastructure_failed in [false, true] {
            let run = normalize_test_results(
                reports.iter().map(|&(nodeid, phase, outcome, duration)| {
                    (
                        nodeid.to_owned(),
                        phase.to_owned(),
                        outcome.to_owned(),
                        duration,
                    )
                }),
                infrastructure_failed,
            )
            .expect("normalized reports");
            let text = serde_json::to_string(&run).expect("result JSON");
            let parsed = serde_json::from_str::<super::ParsedTestRun>(&text)
                .expect("ordinary artifact uses bounded report parsing")
                .0;
            assert_eq!(
                parsed,
                super::parse_test_run(&text).expect("canonical result")
            );
            assert_eq!(parsed, run);
        }
    }
}

#[test]
fn incremental_reports_preserve_duplicate_phase_reduction_and_retry() {
    let directory = tempfile::tempdir().expect("result directory");
    let path = directory.path().join("missing/result.json");
    let batch = directory.path().join("batch.json");
    let reports = [
        ("z", "setup", "passed", 1.0),
        ("z", "call", "skipped", 2.0),
        ("z", "call", "passed", 3.0),
        ("a", "teardown", "failed", 4.0),
        ("a", "setup", "failed", 5.0),
        ("only-setup", "setup", "passed", 6.0),
    ];
    let mut accumulator = super::TestReportAccumulator::default();
    for &(nodeid, phase, outcome, duration) in &reports {
        accumulator.append(nodeid, phase, outcome, duration);
    }
    assert!(accumulator.write(directory.path(), false).is_err());
    for infrastructure_failed in [false, true] {
        accumulator
            .write(&path, infrastructure_failed)
            .expect("retry write");
        write_test_reports(
            &batch,
            reports.iter().map(|&(nodeid, phase, outcome, duration)| {
                (
                    nodeid.to_owned(),
                    phase.to_owned(),
                    outcome.to_owned(),
                    duration,
                )
            }),
            infrastructure_failed,
        )
        .expect("batch reference");
        assert_eq!(
            std::fs::read(&path).unwrap(),
            std::fs::read(&batch).unwrap()
        );
        let run = super::read_test_run(&path).expect("read normalized result");
        assert_eq!(run.tests.len(), 2);
        assert_eq!(run.tests[0].nodeid, "a");
        assert_eq!(run.tests[0].failed_phase, Some(FailedPhase::Teardown));
        assert!((run.tests[0].duration_ms - 9.0).abs() < f64::EPSILON);
        assert_eq!(run.tests[1].outcome, TestOutcome::Passed);
        assert!(
            run.tests[1]
                .call_duration_ms
                .is_some_and(|duration| (duration - 3.0).abs() < f64::EPSILON)
        );
        assert!((run.tests[1].duration_ms - 6.0).abs() < f64::EPSILON);
    }
}

#[test]
fn incremental_invalid_reports_defer_first_error_and_preserve_destination() {
    let directory = tempfile::tempdir().expect("result directory");
    let path = directory.path().join("result.json");
    let batch = directory.path().join("batch.json");
    for invalid in [
        ("", "call", "passed", 0.0),
        ("node", "invalid", "passed", 0.0),
        ("node", "call", "invalid", 0.0),
        ("node", "call", "passed", f64::NAN),
        ("node", "call", "passed", f64::INFINITY),
        ("node", "call", "passed", -1.0),
    ] {
        std::fs::write(&path, "previous result").expect("seed result");
        let reports = [
            ("good", "call", "passed", 1.0),
            invalid,
            ("", "bad", "bad", -2.0),
        ];
        let mut accumulator = super::TestReportAccumulator::default();
        for &(nodeid, phase, outcome, duration) in &reports {
            accumulator.append(nodeid, phase, outcome, duration);
        }
        let expected = write_test_reports(
            &batch,
            reports.iter().map(|&(nodeid, phase, outcome, duration)| {
                (
                    nodeid.to_owned(),
                    phase.to_owned(),
                    outcome.to_owned(),
                    duration,
                )
            }),
            false,
        )
        .expect_err("invalid batch")
        .to_string();
        for _ in 0..2 {
            assert_eq!(
                accumulator
                    .write(&path, false)
                    .expect_err("deferred invalid report")
                    .to_string(),
                expected
            );
            assert_eq!(std::fs::read_to_string(&path).unwrap(), "previous result");
        }
    }
}

#[test]
fn typed_report_parser_matches_canonical_values_and_falls_back_for_other_shapes() {
    let directory = tempfile::tempdir().expect("result directory");
    let path = directory.path().join("result.json");
    let reports = [
        (
            r#"{"nodeid":"a","outcome":"passed","duration_ms":-0,"call_duration_ms":0.0}"#,
            true,
        ),
        (
            r#"{"nodeid":"λ","outcome":"failed","failed_phase":"setup","duration_ms":18446744073709551615,"call_duration_ms":1e-20}"#,
            true,
        ),
        (
            r#"{"nodeid":"a","outcome":"passed","duration_ms":1,"extra":true}"#,
            false,
        ),
        (
            r#"{"nodeid":"old","nodeid":"new","outcome":"passed","duration_ms":1}"#,
            false,
        ),
        (
            r#"{"nodeid":"a","outcome":"passed","duration_ms":1,"failed_phase":null}"#,
            false,
        ),
        (
            r#"{"nodeid":"a","outcome":"passed","duration_ms":1,"call_duration_ms":null}"#,
            false,
        ),
        (
            r#"{"nodeid":"a","outcome":"failed","duration_ms":1}"#,
            false,
        ),
        (r#"{"nodeid":"","outcome":"passed","duration_ms":1}"#, false),
        (
            r#"{"nodeid":"a","outcome":"passed","duration_ms":-1}"#,
            false,
        ),
        (r#"["a","passed",null,1]"#, false),
    ];
    for (report, typed) in reports {
        let text = format!(r#"{{"infrastructure_failed":false,"tests":[{report}]}}"#);
        let fast = serde_json::from_str::<super::ParsedTestRun>(&text);
        assert_eq!(fast.is_ok(), typed, "{text}");
        let expected = super::parse_test_run(&text).map_err(|error| error.to_string());
        if let Ok(fast) = fast {
            let canonical = expected.as_ref().expect("valid canonical record");
            assert_eq!(&fast.0, canonical, "{text}");
            assert_eq!(
                fast.0.tests[0].duration_ms.to_bits(),
                canonical.tests[0].duration_ms.to_bits(),
                "{text}"
            );
        }
        std::fs::write(&path, &text).expect("result artifact");
        assert_eq!(
            super::read_test_run(&path).map_err(|error| error.to_string()),
            expected,
            "{text}"
        );
    }
}

fn record_artifact(
    directory: &std::path::Path,
    artifact: &[u8],
) -> (
    std::path::PathBuf,
    Result<super::RecordedTestRun, crate::CoreError>,
) {
    let result = directory.join("result.json");
    let history = directory.join("history.json");
    std::fs::write(&result, artifact).expect("result artifact");
    let recorded = record_test_run_artifact(&history, &result, "run", TestAttempt::Primary);
    (history, recorded)
}

#[test]
fn result_reader_preserves_order_duplicates_and_unknown_fields() {
    let artifacts = [
        r#"{"infrastructure_failed":false,"tests":[{"nodeid":"test_é","outcome":"passed","duration_ms":2}]}"#,
        r#"{"tests":[{"duration_ms":2,"outcome":"passed","nodeid":"test_é"}],"infrastructure_failed":false}"#,
        r#"{"infrastructure_failed":null,"tests":null,"infrastructure_failed":false,"tests":[{"nodeid":"test_é","outcome":"passed","duration_ms":2}]}"#,
        r#"{"infrastructure_failed":false,"tests":[{"nodeid":null,"nodeid":"test_é","outcome":"failed","outcome":"passed","duration_ms":null,"duration_ms":2}]}"#,
        r#"{"custom":{"nested":[null,true]},"infrastructure_failed":false,"tests":[{"custom":{"nested":[null,true]},"nodeid":"test_é","outcome":"passed","duration_ms":2}]}"#,
    ];
    for artifact in artifacts {
        let directory = tempfile::tempdir().expect("result directory");
        let (history, recorded) = record_artifact(directory.path(), artifact.as_bytes());
        let recorded = recorded.expect("valid result artifact");
        assert!(recorded.failed.is_empty(), "{artifact}");
        assert!(!recorded.infrastructure_failed, "{artifact}");
        assert_eq!(
            recorded.outcomes.keys().collect::<Vec<_>>(),
            ["test_é"],
            "{artifact}"
        );
        let stored: serde_json::Value =
            serde_json::from_slice(&std::fs::read(history).expect("history bytes"))
                .expect("history JSON");
        assert_eq!(
            stored["tests"]["test_é"],
            serde_json::json!({
                "observations": [
                    {"run_id": "run", "attempt": "primary", "outcome": "passed", "duration_ms": 2.0}
                ]
            }),
            "{artifact}"
        );
    }
}

#[test]
fn result_reader_reports_field_errors_before_history_mutation() {
    let cases = [
        ("[false, []]", "test result artifact must be an object"),
        (
            "{}",
            "test result artifact has invalid infrastructure status",
        ),
        (
            r#"{"tests":[null],"infrastructure_failed":null}"#,
            "test result artifact has invalid infrastructure status",
        ),
        (
            r#"{"infrastructure_failed":false}"#,
            "test result artifact has invalid tests",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":null}"#,
            "test result artifact has invalid tests",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[{}]}"#,
            "test result entry has invalid nodeid",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[{"nodeid":"test","outcome":"passed","failed_phase":null,"duration_ms":0}]}"#,
            "test result entry has invalid phase",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[{"nodeid":"test","outcome":"passed","duration_ms":0,"call_duration_ms":null}]}"#,
            "test result entry has invalid call duration",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[{"nodeid":"test","outcome":"failed","duration_ms":0}]}"#,
            "failed test result entry requires a phase",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[],"infrastructure_failed":null}"#,
            "test result artifact has invalid infrastructure status",
        ),
        (
            r#"{"infrastructure_failed":false,"tests":[{}]} trailing"#,
            "invalid test result artifact: trailing characters",
        ),
    ];
    for (artifact, message) in cases {
        let directory = tempfile::tempdir().expect("result directory");
        let history = directory.path().join("history.json");
        std::fs::write(&history, br#"{"tests":{}}"#).expect("existing history");
        let result = directory.path().join("result.json");
        std::fs::write(&result, artifact).expect("result artifact");
        let error = record_test_run_artifact(&history, &result, "run", TestAttempt::Primary)
            .expect_err(artifact);
        assert!(
            error.to_string().starts_with(message),
            "{artifact}: {error}"
        );
        assert_eq!(
            std::fs::read(&history).expect("history bytes"),
            br#"{"tests":{}}"#,
            "{artifact}"
        );
    }
}

#[test]
fn result_reader_checks_utf8_before_json_and_preserves_io_errors() {
    let directory = tempfile::tempdir().expect("result directory");
    let (history, recorded) = record_artifact(
        directory.path(),
        b"{\"infrastructure_failed\":null,\"tests\":[]} trailing\xff",
    );
    let error = recorded.expect_err("invalid UTF-8");
    assert!(
        matches!(error, crate::CoreError::ArtifactIo { .. }),
        "{error}"
    );
    assert!(error.to_string().contains("read test result"), "{error}");
    std::fs::remove_file(directory.path().join("result.json")).expect("remove result");
    let error = record_test_run_artifact(
        &history,
        &directory.path().join("result.json"),
        "run",
        TestAttempt::Primary,
    )
    .expect_err("missing artifact");
    assert!(error.to_string().contains("read test result"), "{error}");
    assert!(!history.exists());
}
