use super::NativeIndependentCollectReport;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyModule, PyTuple};
use vfy_core::test_execution::{CollectionOutcome, ReportSection};

fn new_report(
    py: Python<'_>,
    nodeid: &str,
    outcome: &str,
    result: Py<PyAny>,
    sections: Py<PyAny>,
) -> Py<NativeIndependentCollectReport> {
    NativeIndependentCollectReport::new(
        py,
        nodeid.to_owned(),
        outcome,
        py.None(),
        result,
        sections,
        None,
    )
    .unwrap()
}

#[test]
fn nonempty_results_alias_the_original_list_but_empty_results_are_replaced() {
    Python::initialize();
    Python::attach(|py| {
        // Import the package before its submodules: concurrent test threads that
        // import a submodule first can deadlock on CPython module locks.
        py.import("vfy").unwrap();
        let result = PyList::new(py, ["first", "second"]).unwrap();
        let sections = PyList::new(py, [("Captured stdout", "collection output")]).unwrap();
        let report = new_report(
            py,
            "module.py",
            "passed",
            result.clone().into_any().unbind(),
            sections.clone().into_any().unbind(),
        );
        let report = report.bind(py);
        assert!(report.getattr("result").unwrap().is(&result));
        assert!(!report.getattr("sections").unwrap().is(&sections));
        result.append("third").unwrap();
        assert_eq!(report.getattr("result").unwrap().len().unwrap(), 3);

        let empty = PyList::empty(py);
        let empty_report = new_report(
            py,
            "empty.py",
            "passed",
            empty.clone().into_any().unbind(),
            PyTuple::empty(py).into_any().unbind(),
        );
        assert!(!empty_report.bind(py).getattr("result").unwrap().is(&empty));
    });
}

#[test]
fn python_constructor_defaults_sections_but_rejects_explicit_none() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let constructor = py.get_type::<NativeIndependentCollectReport>();
        let report = constructor
            .call1(("module.py", "passed", py.None(), py.None()))
            .unwrap();
        let sections = report.getattr("sections").unwrap();
        assert!(sections.cast::<PyList>().unwrap().is_empty());
        let error = constructor
            .call1(("module.py", "passed", py.None(), py.None(), py.None()))
            .unwrap_err();
        assert!(error.is_instance_of::<PyTypeError>(py));
    });
}

#[test]
fn sections_begin_iteration_lazily_and_keep_the_started_iterator() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let report = new_report(
            py,
            "module.py",
            "passed",
            py.None(),
            PyTuple::empty(py).into_any().unbind(),
        );
        let report = report.bind(py);
        let iterator = report
            .call_method1("get_sections", ("Captured stdout",))
            .unwrap();
        let active = PyList::new(
            py,
            [
                ("Captured stdout first", "first"),
                ("Captured stdout second", "second"),
            ],
        )
        .unwrap();
        report.setattr("sections", &active).unwrap();
        let first = iterator
            .call_method0("__next__")
            .unwrap()
            .extract::<(String, String)>()
            .unwrap();
        assert_eq!(first, ("Captured stdout".to_owned(), "first".to_owned()));
        report.setattr("sections", PyList::empty(py)).unwrap();
        active
            .append(("Captured stdout appended", "third"))
            .unwrap();
        let remainder = iterator
            .try_iter()
            .unwrap()
            .map(|section| section.unwrap().extract::<(String, String)>().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(
            remainder,
            vec![
                ("Captured stdout".to_owned(), "second".to_owned()),
                ("Captured stdout".to_owned(), "third".to_owned()),
            ]
        );
    });
}

#[test]
fn snapshot_observes_mutations_from_the_actual_terminal_representation() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let module = PyModule::from_code(
            py,
            c"report = None\nclass Representation:\n    def __str__(self):\n        return 'not the terminal diagnostic'\n    def toterminal(self, out):\n        report.outcome = 'skipped'\n        report.nodeid = 'edited.py::child'\n        report.sections.append(('Captured stdout', 'from renderer'))\n        out.line('  terminal diagnostic  ')\nrepresentation = Representation()\n",
            c"collection_representation.py",
            c"collection_representation",
        )
        .unwrap();
        let report = NativeIndependentCollectReport::new(
            py,
            "original.py".to_owned(),
            "failed",
            module.getattr("representation").unwrap().unbind(),
            py.None(),
            PyTuple::empty(py).into_any().unbind(),
            None,
        )
        .unwrap();
        module.setattr("report", &report).unwrap();
        let snapshot = NativeIndependentCollectReport::snapshot(report.bind(py))
            .expect("terminal formatting can mutate the report without a borrow conflict");
        assert_eq!(snapshot.nodeid, "edited.py::child");
        assert_eq!(snapshot.outcome, CollectionOutcome::Skipped);
        assert_eq!(snapshot.payload.detail, "terminal diagnostic");
        assert_eq!(
            snapshot
                .payload
                .exception
                .as_ref()
                .and_then(vfy_core::test_execution::PythonText::as_scalar),
            Some("terminal diagnostic")
        );
        let sections = snapshot.payload.report_sections.unwrap();
        assert!(
            matches!(sections.as_slice(), [ReportSection { name, content }]
            if name == "Captured stdout" && content == "from renderer")
        );
        module.setattr("report", py.None()).unwrap();
    });
}
