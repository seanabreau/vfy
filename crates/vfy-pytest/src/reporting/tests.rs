use super::{
    JUnitMessageStyle, duration_summary, junit_identity, junit_message, terminal_summary,
    xml_escape,
};
use pyo3::Python;
use pyo3::types::{PyDict, PyDictMethods};
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, PythonText,
};

fn phase(phase: ExecutionPhase, duration_ms: f64) -> PhaseResult {
    PhaseResult::new(
        ExecutionOutcome::Passed,
        phase,
        duration_ms,
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

fn case(nodeid: &str, phases: Vec<PhaseResult>) -> CaseResult {
    CaseResult::new(
        nodeid.to_owned(),
        phases.iter().map(|phase| phase.duration_ms).sum(),
        phases,
    )
}

#[test]
fn duration_summary_sorts_phases_limits_rows_and_labels_hidden_durations() {
    // Pytest's setup, run, and teardown timings share one slowest-first list;
    // Call phases retain pytest's public label.
    let cases = [
        case(
            "test_fast.py::test_case",
            vec![
                phase(ExecutionPhase::Setup, 20.0),
                phase(ExecutionPhase::Call, 11.0),
                phase(ExecutionPhase::Teardown, 4.0),
            ],
        ),
        case(
            "test_slow.py::test_case",
            vec![
                phase(ExecutionPhase::Setup, 1.0),
                phase(ExecutionPhase::Call, 100.0),
                phase(ExecutionPhase::Teardown, 7.0),
            ],
        ),
    ];

    let (heading, lines) = duration_summary(&cases, Some(5), 0.005, false);

    assert_eq!(heading.as_deref(), Some("slowest 5 durations"));
    assert_eq!(
        lines,
        [
            "0.10s call     test_slow.py::test_case",
            "0.02s setup    test_fast.py::test_case",
            "0.01s call     test_fast.py::test_case",
            "0.01s teardown test_slow.py::test_case",
            "",
            "(1 durations < 0.005s hidden.  Use -vv to show these durations.)",
        ]
    );

    let (explicit_heading, explicit_lines) = duration_summary(&cases, Some(5), 0.005, true);

    assert_eq!(explicit_heading, heading);
    assert_eq!(
        explicit_lines.last().map(String::as_str),
        Some("(1 durations < 0.005s hidden.)")
    );
    assert_eq!(&explicit_lines[..5], &lines[..5]);
}

#[test]
fn junit_identity_normalizes_paths_and_preserves_class_parameters_and_prefix() {
    assert_eq!(
        junit_identity(
            r"pkg\test_widget.py::TestWidget::test_value[雪]",
            Some("ci"),
        ),
        (
            "ci.pkg.test_widget.TestWidget".to_owned(),
            "test_value[雪]".to_owned()
        )
    );
}

#[test]
fn junit_message_budget_counts_surrogates_before_xml_escaping() {
    let message = PythonText::from_codepoints(vec![0xd800; 501])
        .expect("surrogates are valid Python code points");
    assert_eq!(
        junit_message(Some(&message), "", "", "", JUnitMessageStyle::Bounded),
        format!(
            "{}...\n[truncated; full report in element text]",
            "#xD800".repeat(500)
        )
    );
    assert_eq!(
        junit_message(Some(&message), "", "", "", JUnitMessageStyle::Full),
        "#xD800".repeat(501)
    );
}

#[test]
fn xml_escape_uses_text_and_attribute_rules_for_quotes_unicode_and_controls() {
    let value = "A<&>\"'é\u{0001}\r\n\t";

    assert_eq!(xml_escape(value, false), "A&lt;&amp;&gt;\"'é#x01&#13;\n\t");
    assert_eq!(
        xml_escape(value, true),
        "A&lt;&amp;&gt;&quot;&apos;é#x01&#13;&#10;&#9;"
    );
}

#[test]
fn terminal_deselections_default_to_record_but_preserve_explicit_statistics() {
    let cases = vec![case(
        "test_selected.py::test_selected",
        vec![phase(ExecutionPhase::Call, 1.0)],
    )];
    let record = vfy_core::test_execution::RunRecord {
        counts: vfy_core::test_execution::compute_counts(&cases),
        cases,
        collection_reports: Vec::new(),
        deselected_nodeids: vec![
            "test_other.py::test_first".to_owned(),
            "test_other.py::test_second".to_owned(),
        ],
        internal_errors: Vec::new(),
        engine: "vfy-independent-v1".to_owned(),
        observations: None,
        exitstatus: Some(0),
        execution_identity: None,
        collected: 1,
        duration_ms: 1.0,
        warnings: Vec::new(),
    };
    Python::initialize();
    Python::attach(|py| {
        for (override_count, expected) in [
            (None, vec!["1 passed", "2 deselected"]),
            (Some(0), vec!["1 passed"]),
            (Some(7), vec!["1 passed", "7 deselected"]),
        ] {
            let options = PyDict::new(py);
            if let Some(count) = override_count {
                options
                    .set_item("deselected_count", count)
                    .expect("explicit terminal statistics");
            }
            let summary = terminal_summary(&record, options.as_any())
                .expect("terminal summary from the canonical record");
            assert_eq!(
                summary
                    .summary_parts
                    .iter()
                    .map(|(text, _, _)| text.as_str())
                    .collect::<Vec<_>>(),
                expected
            );
        }
    });
}
