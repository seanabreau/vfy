use super::SummarySession;
use crate::results::native_run_result;
use vfy_core::test_execution::{Counts, RunRecord, WarningRecord};

#[test]
fn controller_terminal_warning_appends_after_identical_worker_projection() {
    let warning = |message: &str| WarningRecord {
        message: message.to_owned(),
        filename: "conftest.py".to_owned(),
        lineno: 4,
        nodeid: String::new(),
        ..WarningRecord::default()
    };
    let mut result = native_run_result(
        RunRecord {
            internal_errors: Vec::new(),
            exitstatus: Some(0),
            observations: None,
            engine: "vfy-independent-v1".to_owned(),
            execution_identity: None,
            collected: 0,
            counts: Counts::default(),
            duration_ms: 0.0,
            warnings: vec![warning("session"), warning("terminal")],
            cases: Vec::new(),
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
        },
        None,
    );
    result.partition_worker_warnings(0, 0).unwrap();
    let mut summary = SummarySession::new();
    summary.terminal_warning_start = Some(1);
    let initial = summary
        .project_warnings(&result, &[warning("session")], 0)
        .unwrap();
    let final_rows = summary
        .project_warnings(&result, &[warning("session"), warning("terminal")], 0)
        .unwrap();
    let messages = |rows: &[WarningRecord]| {
        rows.iter()
            .map(|row| row.message.clone())
            .collect::<Vec<_>>()
    };
    assert_eq!(messages(&initial), ["terminal", "session"]);
    assert_eq!(messages(&final_rows), ["terminal", "session", "terminal"]);
    assert_eq!(
        serde_json::to_value(&initial).unwrap(),
        serde_json::to_value(&final_rows[..initial.len()]).unwrap()
    );
}
