use super::transport::{NativeRunResultTransportOwned, write_run_record_json};
use super::{NativeRunResultTransport, ReportLocation};
use std::fs;
use std::path::Path;
use vfy_core::test_execution::{
    CaseResult, CollectionOutcome, CollectionReport, Counts, ExecutionOutcome, ExecutionPhase,
    ExecutionRecordIdentity, PhasePayload, PhaseResult, RunRecord, WarningRecord, compute_counts,
    compute_run_counts,
};

mod collect_only;
mod demand;
mod execution_evidence;
mod gc;

#[test]
fn worker_xpass_projection_preserves_live_reason_and_legacy_detail() {
    for (exception, detail, expected) in [
        (Some("live hook reason"), "", "live hook reason"),
        (None, "legacy reason", "legacy reason"),
    ] {
        let phase = PhaseResult::new(
            ExecutionOutcome::XPassed,
            ExecutionPhase::Call,
            1.0,
            PhasePayload {
                exception: exception.map(Into::into),
                detail: detail.to_owned(),
                ..PhasePayload::default()
            },
        );
        let document = serde_json::to_string(&phase).expect("encode worker XPASS phase");
        let restored = serde_json::from_str(&document).expect("decode worker XPASS phase");
        assert_eq!(super::pytest_phase_xfail(&restored), Some(expected.into()));
    }
}

#[test]
fn internal_error_transport_and_rendering_preserve_real_test_counts() {
    let mut record = sample_record();
    record.internal_errors.push(PhasePayload {
        exception: Some("RuntimeError: loop exploded".into()),
        detail: "RuntimeError: loop exploded".to_owned(),
        stdout: "before hook failed\n".to_owned(),
        ..PhasePayload::default()
    });
    let document = serde_json::to_string(&record).expect("serialize internal error");
    let transport: NativeRunResultTransportOwned =
        serde_json::from_str(&document).expect("read native transport");
    let restored = transport.into_record();
    assert_eq!(restored.internal_errors.len(), 1);
    assert_eq!(restored.cases.len(), record.cases.len());
    assert_eq!(restored.counts, record.counts);
    let entries = super::capture::internal_failure_entries(
        &restored,
        &[super::capture::CaptureStream::Stdout],
    );
    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].0, "INTERNALERROR");
    assert_eq!(entries[0].1, "RuntimeError: loop exploded\n");
    assert_eq!(
        entries[0].2,
        vec![(
            "Captured stdout internal error".to_owned(),
            "before hook failed\n".to_owned()
        )]
    );
    assert!(
        super::capture::internal_failure_entries(&restored, &[])[0]
            .2
            .is_empty()
    );
}

fn sample_record() -> RunRecord {
    RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: Some(ExecutionRecordIdentity {
            producer_pid: 123,
            session_token: "a".repeat(64),
            observer_nonce: "b".repeat(64),
            session_depth: 0,
            rootdir: "/tmp/upstream".to_owned(),
        }),
        collected: 1,
        counts: Counts {
            failed: 1,
            ..Counts::default()
        },
        duration_ms: 1.25,
        warnings: vec![WarningRecord {
            message: "warning".to_owned(),
            nodeid: "test_example.py::test_case[param]".to_owned(),
            filename: "/tmp/upstream/test_example.py".to_owned(),
            lineno: 4,
            ..WarningRecord::default()
        }],
        cases: vec![CaseResult::new(
            "test_example.py::test_case[param]".to_owned(),
            0.75,
            vec![PhaseResult::new(
                ExecutionOutcome::Failed,
                ExecutionPhase::Call,
                0.5,
                PhasePayload {
                    exception: Some("AssertionError: assertion failed".into()),
                    stdout: "out\n".to_owned(),
                    stderr: "err\n".to_owned(),
                    log: "log".to_owned(),
                    detail: String::new(),
                    skip_location: None,
                    terminal_lead: String::new(),
                    report_sections: None,
                    report_summary: None,
                },
            )],
        )],
    }
}

fn plugin_sections(values: &[(&str, &str)]) -> Vec<vfy_core::test_execution::ReportSection> {
    values
        .iter()
        .map(|(name, content)| vfy_core::test_execution::ReportSection {
            name: (*name).to_owned(),
            content: (*content).to_owned(),
        })
        .collect()
}

#[test]
fn edited_report_sections_replace_raw_capture_and_preserve_order_and_empty_content() {
    let mut case = worker_case(
        "test_plugin.py::test_sections",
        3.0,
        ExecutionOutcome::Failed,
    );
    case.phases[0].set_capture("original setup".to_owned(), String::new(), String::new());
    case.phases[1].set_capture("original call".to_owned(), String::new(), String::new());
    case.phases[1].set_report_sections(Some(plugin_sections(&[
        ("plugin detail", ""),
        ("stdout plugin", "replacement"),
        ("plugin detail", "second"),
    ])));
    let result = super::partial_native_result(vec![case], Vec::new(), None, 1, 3.0);
    let failures = result.failure_sections("all", true, "failed", 1).unwrap();
    assert_eq!(
        failures[0].1[0].2,
        [
            ("plugin detail".to_owned(), String::new()),
            ("stdout plugin".to_owned(), "replacement".to_owned()),
            ("plugin detail".to_owned(), "second".to_owned()),
        ]
    );
    let stdout = result
        .failure_sections("stdout", true, "failed", 1)
        .unwrap();
    assert_eq!(
        stdout[0].1[0].2,
        [("stdout plugin".to_owned(), "replacement".to_owned())]
    );
    for mode in ["stderr", "log", "no"] {
        assert!(
            result.failure_sections(mode, true, "failed", 1).unwrap()[0].1[0]
                .2
                .is_empty()
        );
    }
}

#[test]
fn removed_sections_stay_removed_in_failure_and_passing_capture_projections() {
    let mut case = worker_case(
        "test_plugin.py::test_sections",
        3.0,
        ExecutionOutcome::Failed,
    );
    for phase in &mut case.phases {
        phase.set_capture("raw capture".to_owned(), String::new(), String::new());
        phase.set_report_sections(Some(Vec::new()));
    }
    let sections = super::capture::TerminalCaseSections::from_case(&case);
    assert!(sections.call.is_empty());
    assert!(sections.teardown.is_empty());
    let result = super::partial_native_result(vec![case], Vec::new(), None, 1, 3.0);
    assert!(
        result.failure_sections("all", true, "failed", 1).unwrap()[0].1[0]
            .2
            .is_empty()
    );
    pyo3::Python::initialize();
    pyo3::Python::attach(|py| {
        for report in result.pytest_reports(py).unwrap() {
            assert!(report.6.is_empty());
        }
    });
}

#[test]
fn passed_teardown_sections_follow_report_titles_without_recreating_earlier_capture() {
    let mut case = worker_case(
        "test_plugin.py::test_sections",
        3.0,
        ExecutionOutcome::Failed,
    );
    case.phases[0].set_capture("raw setup".to_owned(), String::new(), String::new());
    case.phases[1].set_report_sections(Some(plugin_sections(&[("call detail", "call")])));
    case.phases[2].set_report_sections(Some(plugin_sections(&[
        ("Captured stdout setup", "earlier"),
        ("plugin teardown detail", "cleanup"),
        ("stdout teardown detail", ""),
        ("plugin teardown detail", "duplicate"),
    ])));
    let sections = super::capture::TerminalCaseSections::from_case(&case);
    assert_eq!(
        sections.call,
        [("call detail".to_owned(), "call".to_owned())]
    );
    assert_eq!(
        sections.teardown,
        [
            ("plugin teardown detail".to_owned(), "cleanup".to_owned()),
            ("stdout teardown detail".to_owned(), String::new()),
            ("plugin teardown detail".to_owned(), "duplicate".to_owned()),
        ]
    );
    let result = super::partial_native_result(vec![case], Vec::new(), None, 1, 3.0);
    assert_eq!(
        result.failure_sections("all", true, "failed", 1).unwrap()[0].1[0]
            .2
            .len(),
        4
    );
    let stdout = result
        .failure_sections("stdout", true, "failed", 1)
        .unwrap();
    assert_eq!(
        stdout[0].1[0].2,
        [("stdout teardown detail".to_owned(), String::new())]
    );
}

#[test]
fn native_transport_preserves_independent_collection_metadata() {
    for populated in [false, true] {
        let mut record = sample_record();
        record.engine = "vfy-independent-v1".to_owned();
        record.execution_identity = None;
        if populated {
            record
                .deselected_nodeids
                .push("test_example.py::test_unselected".to_owned());
            record.collection_reports.push(CollectionReport {
                nodeid: "test_example.py".to_owned(),
                outcome: CollectionOutcome::Passed,
                duration_ms: 0.125,
                payload: PhasePayload::default(),
            });
        }
        let result = super::native_run_result(record, None);
        let json = result
            .to_json()
            .expect("serialize independent public result");
        assert!(json.contains("\"collection_reports\":"));
        assert!(json.contains("\"deselected_nodeids\":"));
        let decoded: NativeRunResultTransportOwned =
            serde_json::from_str(&json).expect("decode public result");
        let restored = super::native_run_result(decoded.into_record(), None);
        assert_eq!(restored.to_json().unwrap(), json);
        let transport_json =
            serde_json::to_string(&restored.transport()).expect("serialize worker transport");
        assert_eq!(transport_json, json);
        let decoded: NativeRunResultTransportOwned =
            serde_json::from_str(&transport_json).expect("decode worker result");
        assert_eq!(decoded.deselected_nodeids.len(), usize::from(populated));
        assert_eq!(decoded.collection_reports.len(), usize::from(populated));
    }
}

#[test]
fn native_transport_retains_collection_errors_and_capture() {
    let mut record = sample_record();
    record.execution_identity = None;
    record.collection_reports.push(CollectionReport {
        nodeid: "test_import.py".to_owned(),
        outcome: CollectionOutcome::Failed,
        duration_ms: 0.5,
        payload: PhasePayload {
            detail: "ImportError: missing symbol".to_owned(),
            stderr: "import stderr\n".to_owned(),
            ..PhasePayload::default()
        },
    });
    record.counts = compute_run_counts(&record.cases, &record.collection_reports);
    let json = serde_json::to_string(&record).expect("serialize collection record");
    let decoded: NativeRunResultTransportOwned = serde_json::from_str(&json).unwrap();
    let result = super::native_run_result(decoded.into_record(), None);
    assert_eq!(result.collection_errors(), 1);
    assert_eq!(result.collection_skipped(), 0);
    assert_eq!(result.to_json().unwrap(), json);
    let sections = result.failure_sections("all", true, "failed", 1).unwrap();
    assert!(sections.iter().any(|(name, failures)| {
        *name == "ERRORS"
            && failures.iter().any(|(title, detail, capture)| {
                title == "ERROR collecting test_import.py"
                    && detail == "ImportError: missing symbol\n"
                    && capture
                        == &[(
                            "Captured stderr collect".to_owned(),
                            "import stderr\n".to_owned(),
                        )]
            })
    }));
}

#[test]
fn worker_session_warnings_preserve_maximum_multiplicity_and_worker_order() {
    let warning = |message: &str| WarningRecord {
        message: message.to_owned(),
        filename: "conftest.py".to_owned(),
        lineno: 4,
        nodeid: String::new(),
        ..WarningRecord::default()
    };
    let controller = vec![warning("shared"), warning("shared")];
    let workers = vec![
        vec![
            warning("shared"),
            warning("worker-0"),
            warning("shared"),
            warning("shared"),
        ],
        vec![
            warning("shared"),
            warning("shared"),
            warning("worker-1"),
            warning("shared"),
            warning("shared"),
        ],
    ];
    let records = super::warnings::merge_session_warnings(&workers, &controller);
    assert_eq!(
        records
            .iter()
            .map(|record| record.message.as_str())
            .collect::<Vec<_>>(),
        ["worker-0", "shared", "worker-1", "shared"]
    );
}

#[test]
fn worker_warning_phases_merge_without_collapsing_test_occurrences() {
    let warning = |message: &str, nodeid: &str| WarningRecord {
        message: message.to_owned(),
        filename: "conftest.py".to_owned(),
        lineno: 4,
        nodeid: nodeid.to_owned(),
        ..WarningRecord::default()
    };
    let collection = vec![warning("collection", ""); 2];
    let finish = vec![warning("finish", ""); 2];
    let mut workers = Vec::new();
    for index in 0..2 {
        let mut record = sample_record();
        record.warnings = collection.clone();
        record
            .warnings
            .push(warning("call", &format!("test_{index}")));
        record.warnings.extend_from_slice(&finish);
        let mut worker = super::native_run_result(record, None);
        worker.partition_worker_warnings(2, 3).unwrap();
        assert_eq!(worker.record.warnings.len(), 1);
        workers.push(worker);
    }
    let mut combined = super::native_run_result(sample_record(), None);
    combined.record.warnings.clear();
    for worker in workers {
        combined.record.warnings.extend(worker.record.warnings);
        combined
            .worker_collection_warnings
            .extend(worker.worker_collection_warnings);
        combined
            .worker_finish_warnings
            .extend(worker.worker_finish_warnings);
    }
    let rows = combined.merged_worker_warnings(&collection, &finish);
    assert_eq!(
        rows.iter()
            .map(|row| row.message.as_str())
            .collect::<Vec<_>>(),
        [
            "collection",
            "collection",
            "call",
            "call",
            "finish",
            "finish"
        ]
    );
    assert_eq!(rows[2].nodeid, "test_0");
    assert_eq!(rows[3].nodeid, "test_1");
    assert_eq!(
        serde_json::to_value(combined.merged_worker_warnings(&collection, &finish)).unwrap(),
        serde_json::to_value(rows).unwrap()
    );
}

fn expected_public_json(record: &RunRecord) -> Vec<u8> {
    format!("{}\n", serde_json::to_string_pretty(record).unwrap()).into_bytes()
}

fn worker_case(nodeid: &str, duration_ms: f64, outcome: ExecutionOutcome) -> CaseResult {
    let phases: Vec<_> = [
        (ExecutionPhase::Setup, ExecutionOutcome::Passed),
        (ExecutionPhase::Call, outcome),
        (ExecutionPhase::Teardown, ExecutionOutcome::Passed),
    ]
    .into_iter()
    .map(|(phase, outcome)| {
        PhaseResult::new(
            outcome,
            phase,
            duration_ms / 3.0,
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
    })
    .collect();
    CaseResult::new(nodeid.to_owned(), duration_ms, phases)
}

fn worker_result_json(cases: Vec<CaseResult>, collected: usize) -> String {
    worker_result_json_with_metadata(cases, collected, &[])
}

fn worker_result_json_with_metadata(
    cases: Vec<CaseResult>,
    collected: usize,
    metadata: &[(String, String)],
) -> String {
    let record = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected,
        counts: compute_counts(&cases),
        duration_ms: 1.0,
        warnings: Vec::new(),
        cases,
    };
    serde_json::json!({
        "result": record,
        "junit_properties": {"global_properties": []},
        "metadata": metadata
    })
    .to_string()
}

#[test]
fn independent_parallel_merge_preserves_observations_and_deduplicates_collection() {
    let nodeids = ["a.py::test_a".to_owned(), "b.py::test_b".to_owned()];
    let mut workers = Vec::new();
    for (index, pid) in [(1, 222), (0, 111)] {
        let json = worker_result_json(
            vec![worker_case(&nodeids[index], 1.0, ExecutionOutcome::Passed)],
            1,
        );
        let mut worker: serde_json::Value = serde_json::from_str(&json).unwrap();
        let result = &mut worker["result"];
        result["engine"] = serde_json::json!("vfy-independent-v1");
        result["exitstatus"] = serde_json::json!(if index == 1 { 300 } else { 0 });
        result["configuration"] = serde_json::json!({
            "ini_path": null, "ini_sha256": null, "values": {},
            "strict_config": false, "strict_markers": false
        });
        result["case_markers"] =
            serde_json::json!([{"nodeid": nodeids[index], "names": ["observed"]}]);
        result["plugins"] = serde_json::json!([{
            "name": "conftest", "module": "conftest", "origin": null, "sha256": null,
            "hooks": ["pytest_runtest_call"], "hook_calls": {"pytest_runtest_call": 1}
        }]);
        result["execution_topology"] = serde_json::json!({
            "requested_workers": 2, "active_workers": 1, "worker_pids": [pid]
        });
        result["collection_reports"] = serde_json::json!([
            {"nodeid": "a.py", "outcome": "passed", "duration_ms": 0.0, "stdout": "", "stderr": "", "log": "", "detail": "", "terminal_lead": ""},
            {"nodeid": "b.py", "outcome": "passed", "duration_ms": 0.0, "stdout": "", "stderr": "", "log": "", "detail": "", "terminal_lead": ""}
        ]);
        result["deselected_nodeids"] = serde_json::json!(["a.py::test_deselected"]);
        workers.push(worker.to_string());
    }
    let merged = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![1], vec![0]],
        &nodeids,
        &[Some(44), Some(0)],
        0,
        0,
        2.0,
    )
    .unwrap();
    let result: serde_json::Value =
        serde_json::from_str(&merged.transport_json().unwrap()).unwrap();
    assert_eq!(result["engine"], "vfy-independent-v1");
    assert_eq!(result["exitstatus"], 300);
    assert_eq!(result["collection_reports"].as_array().unwrap().len(), 2);
    assert_eq!(
        result["deselected_nodeids"],
        serde_json::json!(["a.py::test_deselected"])
    );
    assert_eq!(result["case_markers"][0]["nodeid"], nodeids[0]);
    assert_eq!(result["case_markers"][1]["nodeid"], nodeids[1]);
    assert_eq!(
        result["execution_topology"],
        serde_json::json!({
            "requested_workers": 2, "active_workers": 2, "worker_pids": [111, 222]
        })
    );
    assert_eq!(result["plugins"][0]["hook_calls"]["pytest_runtest_call"], 2);
    assert_eq!(result["counts"]["passed"], 2);
}

#[test]
fn native_result_transport_accepts_public_records_and_preserves_locations() {
    let record_without_identity = RunRecord {
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

    for record in [record_without_identity, sample_record()] {
        // Portable children write the public record; file-group pipes append
        // private display locations without changing any public JSON bytes.
        let public_json = serde_json::to_string(&record).unwrap();
        let decoded_public: NativeRunResultTransportOwned =
            serde_json::from_str(&public_json).unwrap();
        assert!(decoded_public.report_locations.is_none());
        assert_eq!(decoded_public.engine, record.engine);
        assert_eq!(
            decoded_public.execution_identity.is_some(),
            record.execution_identity.is_some()
        );

        let no_locations = None;
        let serialized_public = serde_json::to_string(&NativeRunResultTransport {
            record: &record,
            report_locations: &no_locations,
            html_extras: &std::collections::BTreeMap::new(),
            html_errors: &std::collections::BTreeMap::new(),
        })
        .unwrap();
        assert_eq!(serialized_public, public_json);

        let report_locations: Option<Vec<(String, ReportLocation)>> = Some(Vec::new());
        let transport_json = serde_json::to_string(&NativeRunResultTransport {
            record: &record,
            report_locations: &report_locations,
            html_extras: &std::collections::BTreeMap::new(),
            html_errors: &std::collections::BTreeMap::new(),
        })
        .unwrap();
        assert_eq!(
            transport_json,
            format!(
                "{},\"report_locations\":[]}}",
                public_json.strip_suffix('}').unwrap()
            )
        );
        let decoded_transport: NativeRunResultTransportOwned =
            serde_json::from_str(&transport_json).unwrap();
        assert_eq!(decoded_transport.engine, record.engine);
        assert_eq!(decoded_transport.report_locations, Some(Vec::new()));
    }

    assert!(serde_json::from_str::<NativeRunResultTransportOwned>("{}").is_err());
}

#[test]
fn result_file_keeps_public_json_bytes_and_atomic_replacement() {
    let record = sample_record();
    let expected = expected_public_json(&record);
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("result.json");
    fs::write(&destination, b"old result").unwrap();

    write_run_record_json(&destination, &record).unwrap();

    assert_eq!(fs::read(&destination).unwrap(), expected);
    let json: serde_json::Value = serde_json::from_slice(&fs::read(&destination).unwrap()).unwrap();
    assert_eq!(json["producer_pid"], 123);
    assert_eq!(json["session_token"], "a".repeat(64));
    assert_eq!(json["observer_nonce"], "b".repeat(64));
    assert_eq!(json["session_depth"], 0);
    assert_eq!(json["rootdir"], "/tmp/upstream");
    assert_eq!(json["warnings"][0]["message"], "warning");
    assert_eq!(
        json["cases"][0]["phases"][0]["exception"],
        "AssertionError: assertion failed"
    );

    let occupied = directory.path().join("occupied");
    fs::create_dir(&occupied).unwrap();
    let marker = occupied.join("marker");
    fs::write(&marker, b"keep").unwrap();
    assert!(write_run_record_json(&occupied, &record).is_err());
    assert_eq!(fs::read(marker).unwrap(), b"keep");
}

#[test]
fn result_file_can_create_missing_parent_directories() {
    let record = sample_record();
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("nested").join("result.json");

    write_run_record_json(&destination, &record).unwrap();

    assert_eq!(
        fs::read(&destination).unwrap(),
        expected_public_json(&record)
    );
    assert!(Path::new(destination.parent().unwrap()).is_dir());
}

#[test]
fn parallel_result_merge_restores_parent_order_with_duplicate_nodeids() {
    let nodeids = [
        "a.py::test_same",
        "b.py::test_one",
        "b.py::test_two",
        "c.py::test_one",
        "d.py::test_one",
        "a.py::test_same",
    ]
    .map(str::to_owned);
    let assignments = [vec![0, 3, 5], vec![1, 2, 4]];
    let mut first = worker_case(&nodeids[0], 1.0, ExecutionOutcome::Passed);
    first.set_user_properties(
        vec![("occurrence".to_owned(), "first".to_owned())].into_boxed_slice(),
    );
    let mut repeated = worker_case(&nodeids[5], 6.0, ExecutionOutcome::Passed);
    repeated.set_user_properties(
        vec![("occurrence".to_owned(), "repeated".to_owned())].into_boxed_slice(),
    );
    let workers = vec![
        worker_result_json(
            vec![
                first,
                worker_case(&nodeids[3], 4.0, ExecutionOutcome::Passed),
                repeated,
            ],
            3,
        ),
        worker_result_json(
            vec![
                worker_case(&nodeids[1], 2.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[2], 3.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[4], 5.0, ExecutionOutcome::Passed),
            ],
            3,
        ),
    ];

    let result = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &assignments,
        &nodeids,
        &[Some(0), Some(0)],
        0,
        0,
        7.0,
    )
    .expect("valid worker results merge");

    assert_eq!(
        result.record.cases[0].user_properties(),
        [("occurrence".to_owned(), "first".to_owned())]
    );
    assert_eq!(
        result.record.cases[5].user_properties(),
        [("occurrence".to_owned(), "repeated".to_owned())]
    );
    assert_eq!(
        result
            .record
            .cases
            .iter()
            .map(|case| case.nodeid.as_str())
            .collect::<Vec<_>>(),
        nodeids.iter().map(String::as_str).collect::<Vec<_>>()
    );
    assert_eq!(
        result
            .record
            .cases
            .iter()
            .map(|case| case.duration_ms)
            .collect::<Vec<_>>(),
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    );
}

#[test]
fn parallel_result_merge_propagates_a_read_failure_after_a_valid_worker() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_one", "b.py::test_two"].map(str::to_owned);
    let workers = vec![
        Ok(worker_result_json(
            vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Passed)],
            1,
        )),
        Err(pyo3::exceptions::PyOSError::new_err(
            "worker 1 result could not be read",
        )),
    ];

    let Err(error) = super::merge_parallel_results(
        workers.into_iter(),
        &[vec![0], vec![1]],
        &nodeids,
        &[Some(0), Some(0)],
        0,
        0,
        2.0,
    ) else {
        panic!("a valid first worker must not hide a later worker read failure");
    };

    pyo3::Python::attach(|py| {
        assert!(error.is_instance_of::<pyo3::exceptions::PyOSError>(py));
    });
    assert!(
        error
            .to_string()
            .contains("worker 1 result could not be read")
    );
}

#[test]
fn native_parallel_merge_ignores_independent_headers_until_that_protocol_is_selected() {
    pyo3::Python::initialize();
    let nodeid = "test_example.py::test_case".to_owned();
    let json = worker_result_json(vec![worker_case(&nodeid, 1.0, ExecutionOutcome::Passed)], 1);
    for header in [serde_json::json!(17), serde_json::json!({"nonce": "other"})] {
        let mut worker: serde_json::Value = serde_json::from_str(&json).unwrap();
        worker["independent"] = header;
        let document = worker.to_string();
        let merged = super::merge_parallel_results(
            [Ok(document.clone())].into_iter(),
            &[vec![0]],
            std::slice::from_ref(&nodeid),
            &[Some(0)],
            0,
            0,
            2.0,
        )
        .expect("ordinary native transport ignores the independent extension");
        assert_eq!(merged.record().counts.passed, 1);
        assert!(super::worker_result_summary(&document).is_err());
    }
}

#[test]
fn parallel_result_merge_preserves_shared_record_validation() {
    pyo3::Python::initialize();
    let nodeid = "test_example.py::test_case".to_owned();
    let json = worker_result_json(vec![worker_case(&nodeid, 1.0, ExecutionOutcome::Passed)], 1);
    for corruption in ["counts", "duration", "phases", "identity"] {
        let mut worker: serde_json::Value = serde_json::from_str(&json).unwrap();
        let record = &mut worker["result"];
        let expected = match corruption {
            "counts" => {
                record["counts"]["passed"] = serde_json::json!(2);
                "counts do not match"
            }
            "duration" => {
                record["cases"][0]["phases"][0]["duration_ms"] = serde_json::json!(-1);
                "duration must be finite and nonnegative"
            }
            "phases" => {
                record["cases"][0]["phases"][0]["phase"] = serde_json::json!("teardown");
                "invalid phase sequence"
            }
            "identity" => {
                record["producer_pid"] = serde_json::json!(123);
                record["session_token"] = serde_json::json!("a".repeat(64));
                record["observer_nonce"] = serde_json::json!("b".repeat(64));
                record["session_depth"] = serde_json::json!(0);
                record["rootdir"] = serde_json::json!("/tmp");
                "outer execution identities cannot be merged"
            }
            _ => panic!("unknown corruption fixture"),
        };
        let Err(error) = super::merge_parallel_results(
            vec![worker.to_string()].into_iter().map(Ok),
            &[vec![0]],
            std::slice::from_ref(&nodeid),
            &[Some(0)],
            0,
            0,
            2.0,
        ) else {
            panic!("parallel aggregation accepted {corruption} corruption");
        };
        assert!(error.to_string().contains(expected), "{error}");
    }
}

#[test]
fn parallel_result_merge_rejects_missing_cases_before_maxfail_is_reached() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_one", "b.py::test_two"].map(str::to_owned);
    let workers = vec![
        worker_result_json(Vec::new(), 1),
        worker_result_json(
            vec![worker_case(&nodeids[1], 1.0, ExecutionOutcome::Passed)],
            1,
        ),
    ];

    let Err(error) = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![0], vec![1]],
        &nodeids,
        &[Some(0), Some(0)],
        2,
        0,
        2.0,
    ) else {
        panic!("a maxfail flag alone does not justify incomplete results");
    };

    assert!(
        error
            .to_string()
            .contains("omitted cases before reaching maxfail")
    );
}

#[test]
fn parallel_report_locations_validate_partial_and_empty_workers_after_maxfail() {
    pyo3::Python::initialize();
    let nodeids = [
        "a.py::test_fails",
        "a.py::test_omitted",
        "b.py::test_omitted",
    ]
    .map(str::to_owned);
    let mut failed: serde_json::Value = serde_json::from_str(&worker_result_json(
        vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Failed)],
        2,
    ))
    .unwrap();
    failed["result"]["report_locations"] =
        serde_json::json!([[nodeids[0], ["a.py", 0, "test_fails"]]]);
    let mut empty: serde_json::Value =
        serde_json::from_str(&worker_result_json(Vec::new(), 1)).unwrap();
    empty["result"]["report_locations"] = serde_json::json!([]);
    let merge = |failed: &serde_json::Value, empty: &serde_json::Value| {
        super::merge_parallel_results(
            vec![failed.to_string(), empty.to_string()]
                .into_iter()
                .map(Ok),
            &[vec![0, 1], vec![2]],
            &nodeids,
            &[Some(1), Some(0)],
            1,
            0,
            2.0,
        )
    };
    let result = merge(&failed, &empty).unwrap();
    assert_eq!(
        result.report_locations,
        Some(vec![(
            nodeids[0].clone(),
            ("a.py".to_owned(), Some(0), "test_fails".to_owned()),
        )])
    );

    for (worker, locations) in [
        (
            0,
            serde_json::json!([
                [nodeids[0], ["a.py", 0, "test_fails"]],
                [nodeids[1], ["a.py", 1, "test_omitted"]],
            ]),
        ),
        (
            0,
            serde_json::json!([[nodeids[1], ["a.py", 0, "test_fails"]]]),
        ),
        (
            1,
            serde_json::json!([[nodeids[2], ["b.py", 0, "test_omitted"]]]),
        ),
    ] {
        let mut corrupted = [failed.clone(), empty.clone()];
        corrupted[worker]["result"]["report_locations"] = locations;
        let error = merge(&corrupted[0], &corrupted[1]).err().unwrap();
        assert!(error.to_string().contains("misaligned report locations"));
    }
}

#[test]
fn parallel_result_merge_accepts_missing_suffix_after_maxfail_is_reached() {
    let nodeids = [
        "a.py::test_fails",
        "b.py::test_passes",
        "a.py::test_skipped_after_failure",
        "b.py::test_skipped_after_failure",
    ]
    .map(str::to_owned);
    let workers = vec![
        worker_result_json(
            vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Failed)],
            2,
        ),
        worker_result_json(
            vec![worker_case(&nodeids[1], 1.0, ExecutionOutcome::Passed)],
            2,
        ),
    ];

    let result = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![0, 2], vec![1, 3]],
        &nodeids,
        &[Some(1), Some(0)],
        1,
        0,
        2.0,
    )
    .expect("reaching maxfail justifies an omitted collection suffix");

    assert_eq!(result.record.collected, 4);
    assert_eq!(
        result
            .record
            .cases
            .iter()
            .map(|case| case.nodeid.as_str())
            .collect::<Vec<_>>(),
        [&nodeids[0], &nodeids[1]]
            .into_iter()
            .map(String::as_str)
            .collect::<Vec<_>>()
    );
    assert_eq!(result.record.counts.failed, 1);
}

#[test]
fn parallel_result_merge_applies_worker_metadata_in_worker_order() {
    let nodeids = ["a.py::test_alpha", "b.py::test_beta"].map(str::to_owned);
    let workers = vec![
        worker_result_json_with_metadata(
            vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Passed)],
            1,
            &[
                ("Region".to_owned(), "west".to_owned()),
                ("Worker one".to_owned(), "seen".to_owned()),
            ],
        ),
        worker_result_json_with_metadata(
            vec![worker_case(&nodeids[1], 1.0, ExecutionOutcome::Passed)],
            1,
            &[
                ("Region".to_owned(), "east".to_owned()),
                ("Worker two".to_owned(), "seen".to_owned()),
            ],
        ),
    ];

    let result = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![0], vec![1]],
        &nodeids,
        &[Some(0), Some(0)],
        0,
        0,
        2.0,
    )
    .expect("valid worker metadata merges");

    assert_eq!(
        result.worker_metadata_records(),
        [
            ("Region".to_owned(), "east".to_owned()),
            ("Worker one".to_owned(), "seen".to_owned()),
            ("Worker two".to_owned(), "seen".to_owned()),
        ]
    );
}

#[test]
fn parallel_result_merge_validates_each_nonzero_worker_exit() {
    pyo3::Python::initialize();
    let nodeids = ["a.py::test_fails", "b.py::test_passes"].map(str::to_owned);
    let workers = vec![
        worker_result_json(
            vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Failed)],
            1,
        ),
        worker_result_json(
            vec![worker_case(&nodeids[1], 1.0, ExecutionOutcome::Passed)],
            1,
        ),
    ];

    let Err(error) = super::merge_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![0], vec![1]],
        &nodeids,
        &[Some(1), Some(1)],
        0,
        0,
        2.0,
    ) else {
        panic!("an all-pass worker cannot explain its own exit status one");
    };

    assert!(error.to_string().contains("worker 1 exited 1"));
}

#[test]
fn parallel_warning_order_uses_first_assigned_occurrence_and_session_tail() {
    let nodeids = ["same", "middle", "same"].map(str::to_owned);
    let with_warnings = |cases, messages: &[(&str, &str)]| {
        let mut worker: serde_json::Value =
            serde_json::from_str(&worker_result_json(cases, 2)).unwrap();
        let record = &mut worker["result"];
        record["warnings"] = serde_json::to_value(
            messages
                .iter()
                .map(|(nodeid, message)| WarningRecord {
                    nodeid: (*nodeid).to_owned(),
                    message: (*message).to_owned(),
                    filename: "test_case.py".to_owned(),
                    lineno: 1,
                    ..WarningRecord::default()
                })
                .collect::<Vec<_>>(),
        )
        .unwrap();
        serde_json::to_string(&worker).unwrap()
    };
    let first = with_warnings(
        vec![
            worker_case("same", 1.0, ExecutionOutcome::Passed),
            worker_case("same", 1.0, ExecutionOutcome::Passed),
        ],
        &[
            ("", "session-first"),
            ("same", "first-duplicate"),
            ("same", "second-duplicate"),
            ("middle", "unassigned-first"),
        ],
    );
    let second = worker_result_json(
        vec![worker_case("middle", 1.0, ExecutionOutcome::Passed)],
        1,
    );
    let result = super::merge_parallel_results(
        vec![first, second].into_iter().map(Ok),
        &[vec![2, 0], vec![1]],
        &nodeids,
        &[Some(0), Some(0)],
        0,
        0,
        2.0,
    )
    .unwrap();
    assert_eq!(
        result
            .record
            .warnings
            .iter()
            .map(|warning| warning.message.as_str())
            .collect::<Vec<_>>(),
        [
            "first-duplicate",
            "second-duplicate",
            "session-first",
            "unassigned-first"
        ]
    );
}

#[test]
fn html_call_retains_subtest_failure_without_changing_phase_duration() {
    let mut case = worker_case(
        "test_example.py::test_subtest",
        3.0,
        ExecutionOutcome::Passed,
    );
    case.set_subtests(
        vec![vfy_core::test_execution::SubtestResult {
            report: None,
            outcome: ExecutionOutcome::Failed,
            context: "(value=2)".to_owned(),
            exception: Some("AssertionError".into()),
            detail: "subtest assertion evidence".to_owned(),
            skip_location: None,
            terminal_lead: String::new(),
        }]
        .into_boxed_slice(),
    );
    let result = super::partial_native_result(vec![case], Vec::new(), None, 1, 3.0);
    let html: serde_json::Value =
        serde_json::from_str(&result.html_results_json().unwrap()).unwrap();
    assert_eq!(html[0]["phases"][1]["outcome"], "failed");
    assert_eq!(html[0]["phases"][1]["duration"], 0.001);
    assert!(
        html[0]["phases"][1]["details"][0]
            .as_str()
            .unwrap()
            .contains("(value=2)")
    );
    assert!(
        html[0]["phases"][1]["details"][0]
            .as_str()
            .unwrap()
            .contains("subtest assertion evidence")
    );
}

#[test]
fn native_html_attachments_survive_transport_and_reject_unknown_cases() {
    pyo3::Python::initialize();
    let case = worker_case(
        "test_attachment.py::test_evidence",
        1.0,
        ExecutionOutcome::Passed,
    );
    let mut original = super::partial_native_result(vec![case], Vec::new(), None, 1, 1.0);
    original.html_extras.insert(
        "test_attachment.py::test_evidence".to_owned(),
        vec![vfy_core::PytestHtmlExtra {
            kind: "text".to_owned(),
            name: "proof".to_owned(),
            content: "attachment evidence".to_owned(),
        }],
    );
    let json = original.transport_json().unwrap();
    let merged = super::NativeRunResult::merge_json_results(vec![json.clone()]).unwrap();
    assert_eq!(
        merged.html_extras["test_attachment.py::test_evidence"][0].content,
        "attachment evidence"
    );
    let duplicate = super::NativeRunResult::merge_json_results(vec![json.clone(), json])
        .err()
        .unwrap();
    assert!(duplicate.to_string().contains("misaligned or duplicated"));
    let worker = serde_json::json!({"result": original.transport()}).to_string();
    let parallel = super::merge_parallel_results(
        vec![worker].into_iter().map(Ok),
        &[vec![0]],
        &["test_attachment.py::test_evidence".to_owned()],
        &[Some(0)],
        0,
        0,
        1.0,
    )
    .unwrap();
    assert_eq!(
        parallel.html_extras["test_attachment.py::test_evidence"][0].content,
        "attachment evidence"
    );

    let mut corrupt: serde_json::Value =
        serde_json::from_str(&original.transport_json().unwrap()).unwrap();
    let extras = corrupt["html_extras"]
        .as_object_mut()
        .unwrap()
        .remove("test_attachment.py::test_evidence")
        .unwrap();
    corrupt["html_extras"]["unknown"] = extras;
    let error = super::NativeRunResult::merge_json_results(vec![corrupt.to_string()])
        .err()
        .unwrap();
    assert!(error.to_string().contains("misaligned or duplicated"));
    let worker = serde_json::json!({"result": corrupt}).to_string();
    let error = super::merge_parallel_results(
        vec![worker].into_iter().map(Ok),
        &[vec![0]],
        &["test_attachment.py::test_evidence".to_owned()],
        &[Some(0)],
        0,
        0,
        1.0,
    )
    .err()
    .unwrap();
    assert!(error.to_string().contains("misaligned or duplicated"));
}

#[test]
fn native_html_validation_errors_survive_merge_and_prevent_publication() {
    pyo3::Python::initialize();
    let nodeid = "test_attachment.py::test_invalid";
    let case = worker_case(nodeid, 1.0, ExecutionOutcome::Passed);
    let mut original = super::partial_native_result(vec![case], Vec::new(), None, 1, 1.0);
    original.html_errors.insert(
        nodeid.to_owned(),
        "HTML attachment has no content".to_owned(),
    );
    let json = original.transport_json().unwrap();
    let serial = super::NativeRunResult::merge_json_results(vec![json]).unwrap();
    let worker = serde_json::json!({"result": original.transport()}).to_string();
    let parallel = super::merge_parallel_results(
        vec![worker].into_iter().map(Ok),
        &[vec![0]],
        &[nodeid.to_owned()],
        &[Some(0)],
        0,
        0,
        1.0,
    )
    .unwrap();
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("report.html");
    std::fs::write(&destination, "accepted report").unwrap();
    let mut writer = vfy_core::PytestHtmlReport::new(destination.clone());
    for result in [serial, parallel] {
        assert_eq!(result.record.counts.passed, 1);
        assert!(
            result
                .write_html(&mut writer, "{}")
                .unwrap_err()
                .to_string()
                .contains("HTML attachment has no content")
        );
        assert_eq!(
            std::fs::read_to_string(&destination).unwrap(),
            "accepted report"
        );
    }
    let mut corrupt: serde_json::Value =
        serde_json::from_str(&original.transport_json().unwrap()).unwrap();
    corrupt["html_errors"] = serde_json::json!({"unknown": "HTML attachment has no content"});
    let error = super::NativeRunResult::merge_json_results(vec![corrupt.to_string()])
        .err()
        .unwrap();
    assert!(error.to_string().contains("misaligned or duplicated"));
}

#[test]
fn upstream_phase_projection_keeps_parent_outcome_and_cumulative_capture() {
    pyo3::Python::initialize();
    let mut case = worker_case("test_subtests.py::test_case", 3.0, ExecutionOutcome::Passed);
    case.phases[0].set_capture("setup evidence\n".to_owned(), String::new(), String::new());
    case.phases[1].set_capture(String::new(), "call evidence\n".to_owned(), String::new());
    case.set_subtests(
        vec![vfy_core::test_execution::SubtestResult {
            report: None,
            outcome: ExecutionOutcome::Failed,
            context: "(value=2)".to_owned(),
            exception: Some("AssertionError".into()),
            detail: "subtest assertion evidence".to_owned(),
            skip_location: None,
            terminal_lead: String::new(),
        }]
        .into_boxed_slice(),
    );
    let result = super::partial_native_result(vec![case], Vec::new(), None, 1, 3.0);
    pyo3::Python::attach(|py| {
        let reports = result.pytest_reports(py).unwrap();
        assert_eq!(reports.len(), 3);
        assert_eq!(reports[1].1, "call");
        // Actual pytest SubtestReports already carry the subtest failure.
        assert_eq!(reports[1].2, "passed");
        assert!((reports[1].3 - 0.001).abs() < f64::EPSILON);
        assert_eq!(
            reports[0].6,
            [(
                "Captured stdout setup".to_owned(),
                "setup evidence\n".to_owned()
            )]
        );
        assert_eq!(
            reports[1].6,
            [
                (
                    "Captured stdout setup".to_owned(),
                    "setup evidence\n".to_owned()
                ),
                (
                    "Captured stderr call".to_owned(),
                    "call evidence\n".to_owned()
                ),
            ]
        );
        assert_eq!(reports[2].6, reports[1].6);
    });
}

#[test]
fn xunit2_resolves_only_missing_skip_locations_and_legacy_resolves_all() {
    let mut record = sample_record();
    record.cases = vec![
        worker_case("test_cases.py::test_pass", 0.0, ExecutionOutcome::Passed),
        worker_case(
            "test_cases.py::test_skip_missing",
            0.0,
            ExecutionOutcome::Skipped,
        ),
        worker_case(
            "test_cases.py::test_skip_recorded",
            0.0,
            ExecutionOutcome::Skipped,
        ),
        worker_case("test_cases.py::test_xfail", 0.0, ExecutionOutcome::XFailed),
    ];
    record.cases[2].phases[1].set_skip_location(Some(vfy_core::test_execution::SkipLocation {
        path: "skip-origin.py".to_owned(),
        line: 7,
    }));
    let locations = record
        .cases
        .iter()
        .map(|case| {
            (
                case.nodeid.clone(),
                (
                    "source.py".to_owned(),
                    Some(if case.nodeid.ends_with("test_skip_missing") {
                        -1
                    } else {
                        73
                    }),
                    "domain".to_owned(),
                ),
            )
        })
        .collect();
    let result = super::native_run_result(record, Some(locations));
    assert_eq!(
        result.required_junit_locations("xunit2").unwrap(),
        Some(vec!["test_cases.py::test_skip_missing".to_owned()])
    );
    assert_eq!(result.required_junit_locations("xunit1").unwrap(), None);
    assert_eq!(result.required_junit_locations("legacy").unwrap(), None);
    assert_eq!(result.junit_locations("legacy").unwrap().len(), 4);
    assert_eq!(result.junit_locations("xunit1").unwrap().len(), 4);
    assert_eq!(
        result.junit_locations("xunit2").unwrap(),
        std::collections::BTreeMap::from([(
            "test_cases.py::test_skip_missing".to_owned(),
            ("source.py".to_owned(), Some(-1))
        )])
    );
    let transported: NativeRunResultTransportOwned =
        serde_json::from_str(&result.transport_json().unwrap()).unwrap();
    assert_eq!(transported.report_locations, result.report_locations);
}

#[test]
fn empty_parallel_worker_exit_five_is_accepted_only_for_an_empty_result() {
    let empty = worker_result_json(Vec::new(), 0);
    let result = super::merge_parallel_results(
        vec![Ok(empty)].into_iter(),
        &[Vec::new()],
        &[],
        &[Some(5)],
        0,
        0,
        0.0,
    )
    .unwrap();
    assert_eq!(result.collected_count(), 0);
    let nodeid = "test_cases.py::test_one".to_owned();
    let populated =
        worker_result_json(vec![worker_case(&nodeid, 1.0, ExecutionOutcome::Passed)], 1);
    assert!(
        super::merge_parallel_results(
            vec![Ok(populated)].into_iter(),
            &[vec![0]],
            &[nodeid],
            &[Some(5)],
            0,
            0,
            0.0,
        )
        .is_err()
    );
}
#[test]
fn demand_ranges_restore_global_order_and_duplicate_nodeid_occurrences() {
    let nodeids = [
        "a.py::same",
        "a.py::one",
        "b.py::two",
        "b.py::three",
        "a.py::same",
    ]
    .map(str::to_owned);
    let workers = vec![
        worker_result_json(
            vec![
                worker_case(&nodeids[0], 1.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[1], 2.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[4], 5.0, ExecutionOutcome::Passed),
            ],
            3,
        ),
        worker_result_json(
            vec![
                worker_case(&nodeids[2], 3.0, ExecutionOutcome::Passed),
                worker_case(&nodeids[3], 4.0, ExecutionOutcome::Passed),
            ],
            2,
        ),
    ];
    let result = super::merge_demand_parallel_results(
        workers.into_iter().map(Ok),
        &[vec![0..2, 4..5], std::iter::once(2..4).collect()],
        &nodeids,
        &[Some(0), Some(0)],
        0,
        0,
        5.0,
    )
    .expect("noncontiguous leases merge in global collection order");
    assert_eq!(result.record.collected, 5);
    assert_eq!(
        result
            .record
            .cases
            .iter()
            .map(|case| case.nodeid.as_str())
            .collect::<Vec<_>>(),
        nodeids.iter().map(String::as_str).collect::<Vec<_>>()
    );
    assert_eq!(
        result
            .record
            .cases
            .iter()
            .map(|case| case.duration_ms)
            .collect::<Vec<_>>(),
        [1.0, 2.0, 3.0, 4.0, 5.0]
    );
}

#[test]
fn demand_unclaimed_and_lookahead_suffixes_require_observed_maxfail() {
    pyo3::Python::initialize();
    let nodeids = (0..6)
        .map(|index| format!("a.py::case[{index}]"))
        .collect::<Vec<_>>();
    for maxfail in [1, 2] {
        let workers = vec![
            worker_result_json(
                vec![worker_case(&nodeids[0], 1.0, ExecutionOutcome::Failed)],
                2,
            ),
            worker_result_json(
                vec![worker_case(&nodeids[2], 1.0, ExecutionOutcome::Passed)],
                2,
            ),
        ];
        let result = super::merge_demand_parallel_results(
            workers.into_iter().map(Ok),
            &[
                std::iter::once(0..2).collect(),
                std::iter::once(2..4).collect(),
            ],
            &nodeids,
            &[Some(1), Some(0)],
            maxfail,
            0,
            2.0,
        );
        if maxfail == 1 {
            let result =
                result.expect("observed failure permits unused leased and unclaimed suffixes");
            assert_eq!(result.record.collected, 6);
            assert_eq!(result.record.cases.len(), 2);
            assert_eq!(result.record.counts.failed, 1);
        } else {
            let error = result
                .err()
                .expect("merely enabling maxfail cannot hide omitted cases");
            assert!(error.to_string().contains("before reaching maxfail"));
        }
    }
}

#[test]
fn worker_report_locations_reject_misalignment_before_replacing_real_sources() {
    let mut result = super::native_run_result(sample_record(), None);
    let nodeid = result.record.cases[0].nodeid.clone();
    let location = (
        "actual/source.py".to_owned(),
        Some(37),
        "original_name".to_owned(),
    );
    result
        .set_report_locations(vec![(nodeid, location.clone())])
        .expect("aligned worker source");
    for invalid in [
        Vec::new(),
        vec![("different.py::test_case".to_owned(), location.clone())],
    ] {
        assert!(result.set_report_locations(invalid).is_err());
        assert_eq!(
            result
                .report_location(0)
                .expect("original worker source remains"),
            location
        );
    }
}
