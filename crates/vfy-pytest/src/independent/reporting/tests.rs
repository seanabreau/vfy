use super::{NativeIndependentReport, PhasePayload, PhaseResult, PhaseTiming, ReportSections};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use vfy_core::test_execution::{ExecutionOutcome, ExecutionPhase};

fn empty_report(py: Python<'_>) -> Bound<'_, NativeIndependentReport> {
    let namespace = PyDict::new(py);
    py.run(
        c"from types import SimpleNamespace\nitem = SimpleNamespace(nodeid='case.py::test_empty', user_properties=[], keywords={}, config=SimpleNamespace(option=SimpleNamespace(workers=1)))",
        Some(&namespace),
        None,
    )
    .unwrap();
    NativeIndependentReport::create(
        py,
        &std::sync::Arc::new(("case.py".to_owned(), Some(0), "test_empty".to_owned())),
        PhaseResult::new(
            ExecutionOutcome::Passed,
            ExecutionPhase::Call,
            0.0,
            PhasePayload::default(),
        ),
        Vec::new(),
        &namespace.get_item("item").unwrap().unwrap(),
        PhaseTiming {
            start: 0.0,
            stop: 0.0,
            duration: 0.0,
        },
        None,
    )
    .unwrap()
    .into_bound(py)
}

#[test]
fn empty_native_sections_stay_unallocated_through_internal_reads() {
    Python::initialize();
    Python::attach(|py| {
        let report = empty_report(py);
        assert!(matches!(report.borrow().sections, ReportSections::Empty));
        assert!(
            NativeIndependentReport::strings(&report, "Captured stdout")
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            NativeIndependentReport::snapshot_sections(&report).unwrap(),
            Some(Vec::new())
        );
        assert!(matches!(report.borrow().sections, ReportSections::Empty));
        let first = report.getattr("sections").unwrap();
        assert!(first.cast::<PyList>().unwrap().is_empty());
        assert!(report.getattr("sections").unwrap().is(&first));
    });
}

#[test]
fn cleared_sections_stay_distinct_until_an_explicit_setter_restores_them() {
    Python::initialize();
    Python::attach(|py| {
        let report = empty_report(py);
        NativeIndependentReport::__clear__(&report);
        assert!(
            NativeIndependentReport::snapshot_sections(&report)
                .unwrap()
                .is_none()
        );
        let error = report.getattr("sections").unwrap_err();
        assert!(error.is_instance_of::<PyRuntimeError>(py));
        let replacement = PyList::empty(py);
        report.setattr("sections", &replacement).unwrap();
        assert!(report.getattr("sections").unwrap().is(&replacement));
        replacement.append(("custom", "restored")).unwrap();
        assert_eq!(
            NativeIndependentReport::snapshot_sections(&report)
                .unwrap()
                .unwrap()[0]
                .content,
            "restored"
        );
    });
}
