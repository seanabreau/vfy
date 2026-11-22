use super::*;
use crate::independent::reporting::{serialization, terminal};
use pyo3::types::PyList;
use vfy_core::test_execution::ExecutionOutcome;
use vfy_core::test_plan::TestId;

fn new_history(py: Python<'_>) -> Bound<'_, NativeSessionReports> {
    Py::new(
        py,
        NativeSessionReports {
            session: None,
            registry: None,
            reports: Vec::new(),
            rows: Vec::new(),
            owners: super::super::rows::ReportOwners::default(),
            retired_rows: std::sync::Arc::new(std::sync::Mutex::new(Vec::new())),
            terminal: terminal::TerminalStatusIndex::new(py),
            summary: terminal::session::SummarySession::new(),
            generation: 1,
            worker: false,
        },
    )
    .unwrap()
    .into_bound(py)
}

fn report(py: Python<'_>, test: usize) -> Bound<'_, NativeIndependentReport> {
    let data = PyDict::new(py);
    for (name, value) in [
        ("nodeid", "same.py::test_same"),
        ("when", "call"),
        ("outcome", "passed"),
    ] {
        data.set_item(name, value).unwrap();
    }
    data.set_item("location", ("same.py", 1, "test_same"))
        .unwrap();
    data.set_item("longrepr", py.None()).unwrap();
    data.set_item("sections", PyList::empty(py)).unwrap();
    data.set_item("user_properties", PyList::empty(py)).unwrap();
    for name in ["duration", "start", "stop"] {
        data.set_item(name, 0.0).unwrap();
    }
    let mut fields = serialization::from_json_fields(&data).unwrap();
    fields.origin = Some(ReportOrigin::new(TestId::from_index(test), 1));
    Py::new(py, fields).unwrap().into_bound(py)
}

fn logged(history: &Bound<'_, NativeSessionReports>, report: &Bound<'_, NativeIndependentReport>) {
    NativeSessionReports::retain_report(history, report);
}

#[test]
fn attached_copy_keeps_public_edits_and_binds_to_its_test() {
    Python::initialize();
    Python::attach(|py| {
        let source = report(py, 9);
        let data = serialization::to_json(&source).unwrap();
        let decoded = serialization::from_json(data.bind(py)).unwrap();
        let decoded = decoded.bind(py);
        decoded.setattr("outcome", "failed").unwrap();
        decoded
            .setattr("user_properties", [("actual", "edited")])
            .unwrap();
        decoded
            .getattr("sections")
            .unwrap()
            .call_method1("append", (("Captured stdout call", "edited output"),))
            .unwrap();
        attach(decoded, ReportOrigin::new(TestId::from_index(4), 1)).unwrap();
        assert_eq!(decoded.borrow().phase.outcome, ExecutionOutcome::Failed);
        assert_eq!(
            decoded
                .borrow()
                .origin
                .as_ref()
                .unwrap()
                .observed_test()
                .index(),
            4
        );
        assert_eq!(
            decoded
                .getattr("user_properties")
                .unwrap()
                .extract::<Vec<(String, String)>>()
                .unwrap(),
            [("actual".to_owned(), "edited".to_owned())]
        );
        assert_eq!(
            decoded
                .getattr("capstdout")
                .unwrap()
                .extract::<String>()
                .unwrap(),
            "edited output"
        );
        assert!(decoded.borrow().pending_copy.is_none());
    });
}

#[test]
fn release_drops_payloads_of_logged_issuers_in_issuance_order() {
    Python::initialize();
    Python::attach(|py| {
        let history = new_history(py);
        let first = report(py, 4);
        let second = report(py, 9);
        logged(&history, &first);
        logged(&history, &second);
        let namespace = PyDict::new(py);
        py.run(c"events = []\nclass Payload:\n def __init__(self, label, source):\n  self.label, self.source = label, source\n def __del__(self):\n  self.source.outcome = 'skipped'\n  assert type(self.source._to_json()) is dict\n  events.append(self.label)", Some(&namespace), None).unwrap();
        let payload = namespace.get_item("Payload").unwrap().unwrap();
        for (label, source) in [("first", &first), ("second", &second), ("third", &first)] {
            let data = serialization::to_json(source).unwrap();
            data.bind(py)
                .set_item("payload", payload.call1((label, source)).unwrap())
                .unwrap();
        }
        release(&history);
        assert_eq!(
            namespace
                .get_item("events")
                .unwrap()
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            ["first", "third", "second"]
        );
    });
}
