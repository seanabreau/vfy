use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;

enum RetainedReference {
    WarningCategory,
    PhaseRepresentation,
}

#[test]
fn result_cycles_release_actual_warning_categories_and_phase_representations() {
    let _gc_guard = crate::test_gc::lock();
    Python::initialize();
    Python::attach(|py| {
        for field in [
            RetainedReference::WarningCategory,
            RetainedReference::PhaseRepresentation,
        ] {
            let target = match field {
                RetainedReference::WarningCategory => {
                    py.eval(c"type('WarningCategory', (Warning,), {})", None, None)
                }
                RetainedReference::PhaseRepresentation => {
                    py.eval(c"type('PhaseRepresentation', (), {})()", None, None)
                }
            }
            .expect("create actual Python cycle member");
            let result = Py::new(
                py,
                crate::results::native_run_result(super::sample_record(), None),
            )
            .expect("create native result");
            target
                .setattr("result", &result)
                .expect("retain result from Python object");
            let weak = py
                .import("weakref")
                .expect("import weakref")
                .call_method1("ref", (&target,))
                .expect("observe cycle member");
            match field {
                RetainedReference::WarningCategory => result
                    .borrow_mut(py)
                    .set_warning_messages(vec![("warning".to_owned(), target.clone().unbind())]),
                RetainedReference::PhaseRepresentation => result
                    .borrow_mut(py)
                    .set_last_phase_representation(Some(target.clone().unbind())),
            }
            drop(target);
            drop(result);
            py.import("gc")
                .expect("import gc")
                .call_method0("collect")
                .expect("collect native/Python cycle");
            assert!(weak.call0().expect("read weak reference").is_none());
        }
    });
}
