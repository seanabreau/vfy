use super::{InvocationState, Rc, callback};
use crate::parallel::independent::InvocationPurpose;
use pyo3::prelude::*;

#[test]
fn authenticated_children_skip_automatic_ids_before_touching_output_operands() {
    Python::initialize();
    Python::attach(|py| {
        for purpose in [InvocationPurpose::Parallel, InvocationPurpose::FreshSerial] {
            let invocation = Rc::new(InvocationState::new());
            invocation.mark_worker(purpose);
            let consumer = callback(py, &invocation).expect("bound native collection consumer");
            // None has neither writer nor config: child admission must precede both.
            consumer
                .bind(py)
                .call1((py.None(), py.None()))
                .expect("child automatic output is suppressed");
        }
    });
}

#[test]
fn expired_invocation_cannot_use_retained_collection_consumer() {
    Python::initialize();
    Python::attach(|py| {
        let consumer = {
            let invocation = Rc::new(InvocationState::new());
            callback(py, &invocation).expect("bound native collection consumer")
        };
        let error = consumer
            .bind(py)
            .call1((py.None(), py.None()))
            .expect_err("expired authority must fail before reading operands");
        assert!(error.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py));
        assert_eq!(
            error
                .value(py)
                .str()
                .expect("error message")
                .to_str()
                .expect("UTF-8 message"),
            "terminal collection invocation is no longer available"
        );
    });
}
