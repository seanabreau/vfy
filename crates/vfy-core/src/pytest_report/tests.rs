use super::*;

#[test]
fn image_decoding_preserves_strict_padding_and_python_trailing_bits() {
    for content in [
        "AA=", "AA===", "====", "AA==\n", "AA==AAAA", "AA-_", "YWJj====",
    ] {
        let mut assets = Vec::new();
        let rendered = render_extra(
            &PytestHtmlExtra {
                kind: "image/png".to_owned(),
                name: "Image".to_owned(),
                content: content.to_owned(),
            },
            false,
            &mut assets,
            "proof",
        );
        assert_eq!(rendered, format!("<img alt=\"Image\" src=\"{content}\">"));
        assert!(assets.is_empty());
    }
    for (content, bytes) in [("AB==", vec![0]), ("AAB=", vec![0, 0])] {
        let mut assets = Vec::new();
        let rendered = render_extra(
            &PytestHtmlExtra {
                kind: "image/png".to_owned(),
                name: "Image".to_owned(),
                content: content.to_owned(),
            },
            false,
            &mut assets,
            "proof",
        );
        assert_eq!(rendered, "<img alt=\"Image\" src=\"assets/proof.png\">");
        assert_eq!(assets, [(PathBuf::from("proof.png"), bytes)]);
    }
}

#[test]
fn base64_images_use_their_mime_types_and_asset_extensions() {
    for (kind, extension) in [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/svg+xml", "svg"),
    ] {
        let extra = PytestHtmlExtra {
            kind: kind.to_owned(),
            name: "Image".to_owned(),
            content: "AA==".to_owned(),
        };
        let mut assets = Vec::new();
        assert_eq!(
            render_extra(&extra, false, &mut assets, "proof"),
            format!("<img alt=\"Image\" src=\"assets/proof.{extension}\">")
        );
        assert_eq!(
            assets,
            [(PathBuf::from(format!("proof.{extension}")), vec![0])]
        );

        assets.clear();
        assert_eq!(
            render_extra(&extra, true, &mut assets, "proof"),
            format!("<img alt=\"Image\" src=\"data:{kind};base64,AA==\">")
        );
        assert!(assets.is_empty());
    }
}

#[test]
fn writes_redacted_asset_backed_report() {
    let directory = tempfile::tempdir().unwrap();
    let report = directory.path().join("report.html");
    let payload = r#"{"title":"Results <now>","metadata":[["SECRET","value"]],"redact":["^SECRET$"],"results":[{"nodeid":"test_a.py::test_ok","phases":[{"when":"call","outcome":"passed","duration":0.1,"wasxfail":false,"details":[]}],"extras":[{"kind":"text","name":"proof","content":"ok"}],"cells":[]}],"css":[],"self_contained":false,"prefix":[],"summary":[],"postfix":[],"headers":[]}"#;
    write_pytest_html_report(payload, &report).unwrap();
    let html = std::fs::read_to_string(report).unwrap();
    assert!(html.contains("Results &lt;now&gt;"));
    assert!(html.contains("••••••"));
    assert!(!html.contains(">value<"));
    assert!(directory.path().join("assets/style.css").is_file());
    let attachments = std::fs::read_dir(directory.path().join("assets"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "txt"))
        .map(|path| std::fs::read_to_string(path).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(attachments, ["ok"]);
}

#[test]
fn asset_preflight_preserves_the_complete_previous_report() {
    let directory = tempfile::tempdir().unwrap();
    let report = directory.path().join("report.html");
    let stylesheet = directory.path().join("assets/style.css");
    std::fs::create_dir_all(&stylesheet).unwrap();
    std::fs::write(&report, "accepted report").unwrap();
    let payload = r#"{"title":"new","metadata":[],"redact":[],"results":[],"css":[],"self_contained":false,"prefix":[],"summary":[],"postfix":[],"headers":[]}"#;

    assert!(write_pytest_html_report(payload, &report).is_err());
    assert_eq!(std::fs::read_to_string(report).unwrap(), "accepted report");
    assert!(stylesheet.is_dir());
}

#[test]
fn redaction_match_failure_preserves_existing_report() {
    let directory = tempfile::tempdir().unwrap();
    let report = directory.path().join("report.html");
    std::fs::write(&report, "accepted report").unwrap();
    let payload = serde_json::json!({
        "title": "results", "metadata": [[format!("{}b", "a".repeat(40)), "secret"]],
        "redact": [r"^(a|aa)+\1$"], "results": [], "css": [],
        "self_contained": true, "prefix": [], "summary": [], "postfix": [], "headers": []
    });
    let error = write_pytest_html_report(&payload.to_string(), &report).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("failed to evaluate metadata redaction")
    );
    assert_eq!(std::fs::read_to_string(report).unwrap(), "accepted report");
}

#[test]
fn overlapping_outputs_are_rejected_before_creating_asset_directories() {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("assets");
    let payload = serde_json::json!({
        "title": "results", "metadata": [], "redact": [], "results": [],
        "css": [], "self_contained": false,
        "prefix": [], "summary": [], "postfix": [], "headers": []
    });
    let error = write_pytest_html_report(&payload.to_string(), &destination).unwrap_err();
    assert!(error.to_string().contains("overlapping pytest HTML output"));
    assert!(!destination.exists());
}

#[cfg(unix)]
#[test]
fn output_aliases_are_rejected_before_publication() {
    let directory = tempfile::tempdir().unwrap();
    let original = directory.path().join("original.txt");
    let alias = directory.path().join("alias.txt");
    std::fs::write(&original, "accepted evidence").unwrap();
    std::os::unix::fs::symlink(&original, &alias).unwrap();
    assert!(
        commit_outputs(&[
            (original.clone(), b"first replacement".to_vec()),
            (alias, b"second replacement".to_vec()),
        ])
        .is_err()
    );
    assert_eq!(
        std::fs::read_to_string(original).unwrap(),
        "accepted evidence"
    );
}

#[test]
fn failed_publication_does_not_cache_unpublished_results() {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("report.html");
    let mut writer = PytestHtmlReport::new(destination.clone());
    let mut payload = serde_json::json!({
        "title": "results", "metadata": [], "redact": [], "css": [],
        "self_contained": false, "prefix": [], "summary": [], "postfix": [], "headers": [],
        "results": [{"nodeid": "accepted", "phases": [{"when": "call", "outcome": "passed",
            "duration": 0.1, "wasxfail": false, "details": []}], "cells": [], "extras": []}]
    });
    writer.write(&payload.to_string()).unwrap();
    let accepted = std::fs::read(&destination).unwrap();
    let assets = directory.path().join("assets");
    let backup = directory.path().join("accepted-assets");
    std::fs::rename(&assets, &backup).unwrap();
    std::fs::write(&assets, "blocked output directory").unwrap();
    payload["results"][0]["nodeid"] = serde_json::json!("unpublished");
    payload["results"][0]["extras"] = serde_json::json!([
        {"kind": "text", "name": "evidence", "content": "unpublished evidence"}
    ]);
    assert!(writer.write(&payload.to_string()).is_err());
    assert_eq!(std::fs::read(&destination).unwrap(), accepted);
    std::fs::remove_file(&assets).unwrap();
    std::fs::rename(&backup, &assets).unwrap();
    payload["results"] = serde_json::json!([]);
    writer.write(&payload.to_string()).unwrap();
    assert_eq!(std::fs::read(destination).unwrap(), accepted);
}

#[test]
fn typed_native_rows_share_renderer_and_preserve_publication_on_validation_failure() {
    use crate::test_execution::{
        CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, RunRecord,
        compute_counts,
    };
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("report.html");
    let case = CaseResult::new(
        "test_native.py::test_evidence".to_owned(),
        2.0,
        vec![PhaseResult::new(
            ExecutionOutcome::Failed,
            ExecutionPhase::Call,
            2.0,
            PhasePayload {
                exception: None,
                stdout: "capture <proof>".to_owned(),
                stderr: String::new(),
                log: String::new(),
                detail: "assertion evidence".to_owned(),
                skip_location: None,
                terminal_lead: String::new(),
                report_sections: None,
                report_summary: None,
            },
        )],
    );
    let cases = vec![case];
    let record = RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 1,
        counts: compute_counts(&cases),
        duration_ms: 2.0,
        warnings: Vec::new(),
        cases,
    };
    let extras = BTreeMap::from([(
        "test_native.py::test_evidence".to_owned(),
        vec![PytestHtmlExtra {
            kind: "text".to_owned(),
            name: "proof".to_owned(),
            content: "attachment <proof>".to_owned(),
        }],
    )]);
    let mut payload = serde_json::json!({"title":"results","metadata":[],"redact":[],"results":[],"css":[],"self_contained":true,"prefix":[],"summary":[],"postfix":[],"headers":[]});
    let mut writer = PytestHtmlReport::new(destination.clone());
    writer
        .write_native(&payload.to_string(), &record, &extras)
        .unwrap();
    let typed = std::fs::read(&destination).unwrap();
    payload["results"] =
        serde_json::from_str(&native_pytest_html_results_json(&record, &extras).unwrap()).unwrap();
    let reference = directory.path().join("reference.html");
    write_pytest_html_report(&payload.to_string(), &reference).unwrap();
    assert_eq!(typed, std::fs::read(reference).unwrap());
    let html = String::from_utf8(typed.clone()).unwrap();
    assert!(html.contains("1 Failed,"));
    assert!(html.contains("capture &lt;proof&gt;"));
    assert!(html.contains("attachment &lt;proof&gt;"));

    let duplicate = writer
        .write_native(&payload.to_string(), &record, &extras)
        .unwrap_err();
    assert!(duplicate.to_string().contains("duplicate pytest result"));
    assert_eq!(typed, std::fs::read(&destination).unwrap());

    payload["results"] = serde_json::json!([]);
    let unknown = BTreeMap::from([("unknown".to_owned(), Vec::new())]);
    assert!(
        writer
            .write_native(&payload.to_string(), &record, &unknown)
            .is_err()
    );
    assert_eq!(typed, std::fs::read(destination).unwrap());
}

fn subtest_record(nodeid: &str) -> RunRecord {
    use crate::test_execution::{
        CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, SubtestResult,
        compute_counts,
    };
    let cases = vec![{
        let mut case = CaseResult::new(
            nodeid.to_owned(),
            3.0,
            [
                ExecutionPhase::Setup,
                ExecutionPhase::Call,
                ExecutionPhase::Teardown,
            ]
            .into_iter()
            .map(|phase| {
                PhaseResult::new(
                    ExecutionOutcome::Passed,
                    phase,
                    1.0,
                    PhasePayload {
                        exception: None,
                        stdout: "stdout <proof>".to_owned(),
                        stderr: "stderr proof".to_owned(),
                        log: "log proof".to_owned(),
                        detail: String::new(),
                        skip_location: None,
                        terminal_lead: String::new(),
                        report_sections: None,
                        report_summary: None,
                    },
                )
            }),
        );
        case.set_subtests(
            vec![
                SubtestResult {
                    report: None,
                    outcome: ExecutionOutcome::Passed,
                    context: "(value=1)".to_owned(),
                    exception: None,
                    detail: "passed subtest".to_owned(),
                    skip_location: None,
                    terminal_lead: String::new(),
                },
                SubtestResult {
                    report: None,
                    outcome: ExecutionOutcome::Failed,
                    context: "(value=2)".to_owned(),
                    exception: Some("AssertionError".into()),
                    detail: "subtest <assertion>".to_owned(),
                    skip_location: None,
                    terminal_lead: String::new(),
                },
            ]
            .into_boxed_slice(),
        );
        case
    }];
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
        duration_ms: 3.0,
        warnings: Vec::new(),
        cases,
    }
}

#[test]
fn native_subtest_rows_preserve_json_capture_and_asset_evidence() {
    let directory = tempfile::tempdir().unwrap();
    let nodeid = "test_native.py::test_subtests";
    let record = subtest_record(nodeid);
    let extras = BTreeMap::from([(
        nodeid.to_owned(),
        vec![PytestHtmlExtra {
            kind: "text".to_owned(),
            name: "proof".to_owned(),
            content: "attachment <proof>".to_owned(),
        }],
    )]);
    let expected = serde_json::json!([{
        "nodeid": nodeid,
        "phases": [
            {"when":"setup","outcome":"passed","duration":0.001,"wasxfail":false,
             "details":["","stdout <proof>","stderr proof","log proof"]},
            {"when":"call","outcome":"failed","duration":0.001,"wasxfail":false,
             "details":["\nSubtest (value=2) (failed):\nsubtest <assertion>","stdout <proof>","stderr proof","log proof"]},
            {"when":"teardown","outcome":"passed","duration":0.001,"wasxfail":false,
             "details":["","stdout <proof>","stderr proof","log proof"]}
        ],
        "extras":[{"kind":"text","name":"proof","content":"attachment <proof>"}],
        "cells":[]
    }]);
    let diagnostic: serde_json::Value =
        serde_json::from_str(&native_pytest_html_results_json(&record, &extras).unwrap()).unwrap();
    assert_eq!(diagnostic, expected);

    for self_contained in [false, true] {
        let native = directory
            .path()
            .join(format!("native-{self_contained}.html"));
        let reference = directory
            .path()
            .join(format!("reference-{self_contained}.html"));
        let mut payload = serde_json::json!({"title":"results","metadata":[],"redact":[],"results":[],"css":[],"self_contained":self_contained,"prefix":[],"summary":[],"postfix":[],"headers":[]});
        PytestHtmlReport::new(native.clone())
            .write_native(&payload.to_string(), &record, &extras)
            .unwrap();
        let native_html = std::fs::read(&native).unwrap();
        let native_assets = if self_contained {
            BTreeMap::new()
        } else {
            std::fs::read_dir(directory.path().join("assets"))
                .unwrap()
                .map(|entry| {
                    let path = entry.unwrap().path();
                    (
                        path.file_name().unwrap().to_owned(),
                        std::fs::read(&path).unwrap(),
                    )
                })
                .collect::<BTreeMap<_, _>>()
        };
        payload["results"] = expected.clone();
        write_pytest_html_report(&payload.to_string(), &reference).unwrap();
        assert_eq!(native_html, std::fs::read(reference).unwrap());
        for (filename, content) in native_assets {
            assert_eq!(
                content,
                std::fs::read(directory.path().join("assets").join(filename)).unwrap()
            );
        }
        let html = String::from_utf8(native_html).unwrap();
        assert!(html.contains("1 Failed,"));
        assert!(html.contains("0.003s"));
        assert_eq!(html.matches("stdout &lt;proof&gt;").count(), 1);
        assert!(html.contains("subtest &lt;assertion&gt;"));
        assert!(!html.contains("passed subtest"));
    }
}

#[test]
fn native_html_aggregates_failed_builtin_subtests_without_changing_parent_reports() {
    use crate::test_execution::{
        CaseReportEvent, ExecutionOutcome, SubtestContext, SubtestReportMetadata,
        validate_run_record,
    };

    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("report.html");
    let mut record = subtest_record("test_case.py::TestCase::test_values");
    let case = &mut record.cases[0];
    let subtests = case
        .subtests()
        .iter()
        .cloned()
        .map(|mut subtest| {
            subtest.report = Some(Box::new(SubtestReportMetadata {
                context: SubtestContext {
                    msg: None,
                    kwargs: Vec::new(),
                },
                duration_ms: 0.0,
                start: 0.0,
                stop: 0.0,
                stdout: String::new(),
                stderr: String::new(),
                log: String::new(),
                report_sections: None,
                report_summary: None,
            }));
            subtest
        })
        .collect();
    case.set_subtests(subtests);
    case.set_report_events(
        vec![
            CaseReportEvent::Phase(0),
            CaseReportEvent::Subtest(0),
            CaseReportEvent::Subtest(1),
            CaseReportEvent::Phase(1),
            CaseReportEvent::Phase(2),
        ]
        .into_boxed_slice(),
    );
    assert_eq!(
        case.report_phase_outcome(&case.phases[1]),
        ExecutionOutcome::Passed
    );
    validate_run_record(&record).unwrap();
    let payload = serde_json::json!({"title":"results","metadata":[],"redact":[],"results":[],"css":[],"self_contained":true,"prefix":[],"summary":[],"postfix":[],"headers":[]});

    PytestHtmlReport::new(destination.clone())
        .write_native(&payload.to_string(), &record, &BTreeMap::new())
        .unwrap();

    let document = std::fs::read_to_string(destination).unwrap();
    assert!(document.contains("1 Failed,"));
    assert!(document.contains("subtest &lt;assertion&gt;"));
    assert!(!document.contains("passed subtest"));
    assert_eq!(record.cases[0].phases[1].outcome, ExecutionOutcome::Passed);
}

#[test]
fn distributed_test_occurrences_keep_distinct_rows_and_attachment_assets() {
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("report.html");
    let mut writer = PytestHtmlReport::new(destination.clone());
    let result = |worker: &str, outcome: &str| {
        serde_json::json!({
            "nodeid": "test_shared.py::test_evidence", "worker_id": worker,
            "phases": [{"when": "call", "outcome": outcome, "duration": 0.1,
                "wasxfail": false, "details": []}], "cells": [],
            "extras": [{"kind": "text", "name": "proof", "content": worker}]
        })
    };
    let mut payload = serde_json::json!({
        "title": "results", "metadata": [], "redact": [], "css": [],
        "self_contained": false, "prefix": [], "summary": [], "postfix": [], "headers": [],
        "results": [result("gw0", "passed"), result("gw1", "failed")]
    });
    writer.write(&payload.to_string()).unwrap();
    let accepted = std::fs::read(&destination).unwrap();
    let html = String::from_utf8(accepted.clone()).unwrap();
    assert_eq!(html.matches("test_shared.py::test_evidence").count(), 2);
    assert!(html.contains("1 Passed, 1 Failed,"));
    let mut attachments = std::fs::read_dir(directory.path().join("assets"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "txt"))
        .map(|path| std::fs::read_to_string(path).unwrap())
        .collect::<Vec<_>>();
    attachments.sort();
    assert_eq!(attachments, ["gw0", "gw1"]);

    payload["results"] = serde_json::json!([result("gw0", "passed"), result("gw0", "failed")]);
    assert!(writer.write(&payload.to_string()).is_err());
    assert_eq!(std::fs::read(&destination).unwrap(), accepted);

    payload["results"] = serde_json::json!([result("gw0", "failed")]);
    writer.write(&payload.to_string()).unwrap();
    let updated = std::fs::read_to_string(destination).unwrap();
    assert!(updated.contains("0 Passed, 2 Failed,"));
    assert_eq!(updated.matches("test_shared.py::test_evidence").count(), 2);
}

#[test]
fn native_html_preserves_collection_failures_without_creating_cases() {
    use crate::test_execution::{
        CollectionOutcome, CollectionReport, PhasePayload, compute_run_counts,
    };

    let mut record = subtest_record("test_unused.py::test_unused");
    record.cases.clear();
    record.collected = 0;
    record.collection_reports = [
        (
            "test_bad.py",
            CollectionOutcome::Failed,
            "collection <failure>",
        ),
        (
            "test_optional.py",
            CollectionOutcome::Skipped,
            "optional dependency",
        ),
        ("test_imported.py", CollectionOutcome::Passed, ""),
    ]
    .into_iter()
    .map(|(nodeid, outcome, detail)| CollectionReport {
        nodeid: nodeid.to_owned(),
        outcome,
        duration_ms: 1.0,
        payload: PhasePayload {
            detail: detail.to_owned(),
            ..PhasePayload::default()
        },
    })
    .collect();
    record.counts = compute_run_counts(&record.cases, &record.collection_reports);
    let payload = serde_json::json!({"title":"results","metadata":[],"redact":[],"results":[],"css":[],"self_contained":true,"prefix":[],"summary":[],"postfix":[],"headers":[]});
    let directory = tempfile::tempdir().unwrap();
    let destination = directory.path().join("collection.html");
    let mut writer = PytestHtmlReport::new(destination.clone());
    writer
        .write_native(&payload.to_string(), &record, &BTreeMap::new())
        .unwrap();
    let html = std::fs::read_to_string(destination).unwrap();
    assert!(html.contains("1 Skipped,"));
    assert!(html.contains("1 Error,"));
    assert!(html.contains("collection &lt;failure&gt;"));
    assert!(!html.contains("test_imported.py"));
    assert!(record.cases.is_empty());
}
