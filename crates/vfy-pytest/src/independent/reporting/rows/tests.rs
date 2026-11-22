use super::*;
use pyo3::types::{PyDict, PyDictMethods, PyList, PyListMethods};

fn history(py: Python<'_>) -> Bound<'_, NativeSessionReports> {
    Py::new(
        py,
        NativeSessionReports {
            session: None,
            registry: None,
            reports: Vec::new(),
            rows: Vec::new(),
            owners: super::super::rows::ReportOwners::default(),
            retired_rows: Arc::new(Mutex::new(Vec::new())),
            terminal: terminal::TerminalStatusIndex::new(py),
            summary: terminal::session::SummarySession::new(),
            generation: 1,
            worker: false,
        },
    )
    .unwrap()
    .into_bound(py)
}

fn tracked_report<'py>(
    history: &Bound<'py, NativeSessionReports>,
) -> (ReportHandle, Bound<'py, PyAny>, Bound<'py, PyList>) {
    let py = history.py();
    let namespace = PyDict::new(py);
    let events = PyList::empty(py);
    namespace.set_item("history", history).unwrap();
    namespace.set_item("events", &events).unwrap();
    py.run(c"class Payload:\n    def __del__(self):\n        try:\n            events.append(f'released:{len(history.reports())}')\n        except BaseException as error:\n            events.append(f'error:{type(error).__name__}')\n", Some(&namespace), None).unwrap();
    let payload = namespace
        .get_item("Payload")
        .unwrap()
        .unwrap()
        .call0()
        .unwrap();
    let weak = py
        .import("weakref")
        .unwrap()
        .getattr("ref")
        .unwrap()
        .call1((&payload,))
        .unwrap();
    let mut report = NativeIndependentReport::empty_shell();
    report.longrepr = Some(payload.unbind());
    let handle = NativeSessionReports::prepare_native(history, report).unwrap();
    (handle, weak, events)
}

#[test]
fn cloned_raw_handles_release_payload_only_after_the_last_owner() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (handle, weak, events) = tracked_report(&history);
        let other = handle.clone_ref(py);
        drop(handle);
        assert!(!weak.call0().unwrap().is_none());
        assert!(events.is_empty());
        drop(other);
        assert!(weak.call0().unwrap().is_none());
        assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}

#[test]
fn terminal_replacement_keeps_pinned_unlogged_rows_alive() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (handle, weak, events) = tracked_report(&history);
        terminal::append_handle(py, &handle, "passed").unwrap();
        terminal::append_handle(py, &handle, "passed").unwrap();
        let pins = NativeSessionReports::pin_rows(&history, &[handle.id()]);
        drop(handle);
        terminal::replace_stats(&history, PyDict::new(py).as_any());
        assert!(!weak.call0().unwrap().is_none());
        assert!(events.is_empty());
        drop(pins);
        assert!(weak.call0().unwrap().is_none());
        assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}

#[test]
fn repeated_session_retention_releases_one_owner_when_history_clears() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (handle, weak, events) = tracked_report(&history);
        handle.retain_only(py);
        handle.retain_only(py);
        drop(handle);
        assert!(!weak.call0().unwrap().is_none());
        NativeSessionReports::__clear__(&history);
        assert!(weak.call0().unwrap().is_none());
        assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}

#[test]
fn repeated_report_delivery_keeps_order_and_rescue_does_not_duplicate_it() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (first, first_weak, first_events) = tracked_report(&history);
        let (second, second_weak, second_events) = tracked_report(&history);
        NativeSessionReports::record_native(&first, py).unwrap();
        NativeSessionReports::record_native(&second, py).unwrap();
        NativeSessionReports::record_native(&first, py).unwrap();
        first.retain_only(py);
        second.retain_only(py);
        let reports = NativeSessionReports::reports(&history).unwrap();
        let reports = reports.bind(py);
        assert_eq!(reports.len(), 3);
        assert!(
            reports
                .get_item(0)
                .unwrap()
                .is(reports.get_item(2).unwrap())
        );
        assert!(
            !reports
                .get_item(0)
                .unwrap()
                .is(reports.get_item(1).unwrap())
        );
        drop(first);
        drop(second);
        assert!(!first_weak.call0().unwrap().is_none());
        assert!(!second_weak.call0().unwrap().is_none());
        reports
            .set_slice(0, reports.len(), &PyList::empty(py))
            .unwrap();
        NativeSessionReports::__clear__(&history);
        assert!(first_weak.call0().unwrap().is_none());
        assert!(second_weak.call0().unwrap().is_none());
        assert_eq!(
            first_events.extract::<Vec<String>>().unwrap(),
            ["released:0"]
        );
        assert_eq!(
            second_events.extract::<Vec<String>>().unwrap(),
            ["released:0"]
        );
    });
}

#[test]
fn last_owner_defers_payload_finalization_until_history_borrow_ends() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (handle, weak, events) = tracked_report(&history);
        let borrowed = history.borrow();
        drop(handle);
        assert!(!weak.call0().unwrap().is_none());
        assert!(events.is_empty());
        drop(borrowed);
        NativeSessionReports::flush_retired_rows(&history);
        assert!(weak.call0().unwrap().is_none());
        assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}

#[test]
fn final_raw_handle_can_retire_its_payload_on_another_thread() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (handle, weak, events) = tracked_report(&history);
        let worker = std::thread::spawn(move || drop(handle));
        py.detach(move || worker.join().unwrap());
        assert!(weak.call0().unwrap().is_none());
        assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}

#[test]
fn counter_column_growth_preserves_selected_unlogged_payloads_until_pins_release() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let count = COUNTERS_PER_CHUNK * 3 + 7;
        let selected = [0, count / 3, count * 2 / 3, count - 1];
        let mut observations = Vec::new();
        let mut pins = Vec::new();
        for index in 0..count {
            let (handle, weak, events) = tracked_report(&history);
            if selected.contains(&index) {
                pins.extend(NativeSessionReports::pin_rows(&history, &[handle.id()]));
            }
            drop(handle);
            observations.push((weak, events));
        }
        for (index, (weak, events)) in observations.iter().enumerate() {
            assert_eq!(!weak.call0().unwrap().is_none(), selected.contains(&index));
            assert_eq!(events.is_empty(), selected.contains(&index));
        }
        drop(pins);
        for (weak, events) in observations {
            assert!(weak.call0().unwrap().is_none());
            assert_eq!(events.extract::<Vec<String>>().unwrap(), ["released:0"]);
        }
    });
}

#[test]
fn clearing_history_keeps_stale_handle_counters_separate_from_new_rows() {
    Python::initialize();
    Python::attach(|py| {
        let history = history(py);
        let (old, old_weak, old_events) = tracked_report(&history);
        NativeSessionReports::__clear__(&history);
        assert!(old_weak.call0().unwrap().is_none());
        assert_eq!(old_events.extract::<Vec<String>>().unwrap(), ["released:0"]);
        let (new, new_weak, new_events) = tracked_report(&history);
        drop(old);
        assert!(!new_weak.call0().unwrap().is_none());
        assert!(new_events.is_empty());
        drop(new);
        assert!(new_weak.call0().unwrap().is_none());
        assert_eq!(new_events.extract::<Vec<String>>().unwrap(), ["released:0"]);
    });
}
