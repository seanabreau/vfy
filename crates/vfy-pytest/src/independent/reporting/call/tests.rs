use super::{CallStage, NativeIndependentCallInfo, PhaseClock, PhaseTiming};
use pyo3::prelude::*;
use pyo3::types::PyModule;
use vfy_core::test_execution::{ExecutionPhase, PhasePayload};

#[test]
fn unpublished_call_and_published_call_use_the_same_phase_state() {
    Python::initialize();
    Python::attach(|py| {
        let (call, capture) = PhaseClock::start()
            .complete(ExecutionPhase::Call, Ok(()))
            .into_call_state(py)
            .unwrap();
        let raw = call
            .phase_result_state(py, capture, |_, _, _| unreachable!())
            .unwrap();
        let call = Py::new(py, call).unwrap();
        call.bind(py).setattr("duration", 2.0).unwrap();
        let published = NativeIndependentCallInfo::phase_result(
            call.bind(py),
            PhasePayload::default(),
            |_, _, _| unreachable!(),
        )
        .unwrap();
        assert_eq!(raw.phase, published.phase);
        assert_eq!(raw.outcome, published.outcome);
        assert_eq!(published.duration_ms.to_bits(), 2000.0_f64.to_bits());
    });
}

#[test]
fn completion_retains_the_actual_exception_and_traceback_until_report_creation() {
    Python::initialize();
    Python::attach(|py| {
        // Import the package before its submodules: concurrent test threads that
        // import a submodule first can deadlock on CPython module locks.
        py.import("vfy").unwrap();
        let module = PyModule::from_code(
            py,
            c"def fail():\n    value = object()\n    raise RuntimeError('phase witness')\n",
            c"phase_witness.py",
            c"phase_witness",
        )
        .unwrap();
        let error = module.getattr("fail").unwrap().call0().unwrap_err();
        let original = error.clone_ref(py);
        let (call, capture) = PhaseClock::start()
            .complete(ExecutionPhase::Call, Err(error))
            .into_call(py)
            .unwrap();
        let call = call.bind(py);
        let info = call.getattr("excinfo").unwrap();
        assert!(info.getattr("value").unwrap().is(original.value(py)));
        assert!(
            info.getattr("tb")
                .unwrap()
                .is(original.traceback(py).unwrap())
        );
        assert_eq!(call.borrow().phase().unwrap(), ExecutionPhase::Call);
        assert!(call.borrow().start() > 0.0);
        assert!(call.borrow().stop() >= call.borrow().start());
        assert!(call.borrow().duration() >= 0.0);
        assert!(capture.exception.is_none());
        assert!(call.getattr("result").is_err());
        let recovered = NativeIndependentCallInfo::error(call).unwrap().unwrap();
        assert!(recovered.value(py).is(original.value(py)));

        call.setattr("extra", "live hook metadata").unwrap();
        call.setattr("excinfo", py.None()).unwrap();
        assert!(call.getattr("result").unwrap().is_none());
        assert!(NativeIndependentCallInfo::error(call).unwrap().is_none());
    });
}

#[test]
fn a_collection_call_cannot_be_converted_into_an_execution_result() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let call = Py::new(
            py,
            NativeIndependentCallInfo {
                when: CallStage::Collect,
                timing: PhaseTiming {
                    start: 1.0,
                    stop: 2.0,
                    duration: 1.0,
                },
                excinfo: None,
                result: Some(py.None()),
                pending_report: None,
            },
        )
        .unwrap();
        let formatted = std::cell::Cell::new(false);
        let result = NativeIndependentCallInfo::phase_result(
            call.bind(py),
            PhasePayload::default(),
            |_, _, _| {
                formatted.set(true);
                Ok(())
            },
        );
        assert!(result.is_err());
        assert!(!formatted.get(), "collection is rejected before formatting");
    });
}

#[test]
fn success_result_identity_survives_excinfo_mutation_and_reentrant_repr() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let module = PyModule::from_code(
            py,
            c"call = None\nclass Result:\n    def __repr__(self):\n        call.duration = 42.0\n        return 'retained result'\nvalue = Result()\ndef success():\n    return value\n",
            c"call_result.py",
            c"call_result",
        )
        .unwrap();
        let call = py
            .get_type::<NativeIndependentCallInfo>()
            .getattr("from_call")
            .unwrap()
            .call1((module.getattr("success").unwrap(), "collect"))
            .unwrap();
        module.setattr("call", &call).unwrap();
        let value = module.getattr("value").unwrap();
        assert!(call.getattr("result").unwrap().is(&value));
        assert_eq!(
            call.repr().unwrap().to_str().unwrap(),
            "<CallInfo when='collect' result: retained result>"
        );
        assert_eq!(
            call.getattr("duration")
                .unwrap()
                .extract::<f64>()
                .unwrap()
                .to_bits(),
            42.0_f64.to_bits()
        );
        call.setattr("excinfo", false).unwrap();
        assert!(
            call.getattr("result").is_err(),
            "only None permits result access"
        );
        call.setattr("excinfo", py.None()).unwrap();
        assert!(call.getattr("result").unwrap().is(&value));
        module.setattr("call", py.None()).unwrap();
    });
}
