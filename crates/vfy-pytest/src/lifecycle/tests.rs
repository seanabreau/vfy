use super::{HookStyle, ProtocolContext};
use crate::warnings::{WarningFilterOwnership, WarningService};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyModule};

#[test]
fn protocol_spill_unwinds_all_generators_in_lifo_order_after_cleanup_error() {
    Python::initialize();
    Python::attach(|py| {
        let module = PyModule::from_code(
            py,
            cr"events = []
def scope(name):
    events.append('enter ' + name)
    try:
        yield
    finally:
        events.append('exit ' + name)
        if name == 'middle':
            raise RuntimeError('middle cleanup failed')
",
            c"protocol_spill.py",
            c"protocol_spill",
        )
        .unwrap();
        let mut warnings = WarningService::new(
            py,
            PyList::empty(py).as_any(),
            WarningFilterOwnership::Native,
        )
        .unwrap();
        let mut context = ProtocolContext::default();
        for name in ["outer", "middle", "inner"] {
            let protocol = module.getattr("scope").unwrap().call1((name,)).unwrap();
            protocol.call_method0("__next__").unwrap();
            context.push(protocol.unbind(), HookStyle::Wrapper);
        }
        let error = context
            .finish(py, Ok(py.None()), &mut warnings)
            .unwrap_err();
        assert!(error.is_instance_of::<PyRuntimeError>(py));
        assert_eq!(
            error.value(py).str().unwrap().to_str().unwrap(),
            "middle cleanup failed"
        );
        context.finish(py, Ok(py.None()), &mut warnings).unwrap();
        drop(context);
        let mut active = ProtocolContext::default();
        for name in ["drop outer", "drop inner"] {
            let protocol = module.getattr("scope").unwrap().call1((name,)).unwrap();
            protocol.call_method0("__next__").unwrap();
            active.push(protocol.unbind(), HookStyle::Wrapper);
        }
        drop(active);
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            [
                "enter outer",
                "enter middle",
                "enter inner",
                "exit inner",
                "exit middle",
                "exit outer",
                "enter drop outer",
                "enter drop inner",
                "exit drop inner",
                "exit drop outer",
            ]
        );
    });
}

#[test]
fn legacy_protocol_wrappers_resume_past_their_yield_on_exit() {
    Python::initialize();
    Python::attach(|py| {
        // pytest-timeout cancels its timer after a bare `yield`; closing the
        // generator would skip that code.
        let module = PyModule::from_code(
            py,
            cr"events = []
def legacy(name):
    events.append('enter ' + name)
    yield
    events.append('exit ' + name)
def wrapper(name):
    events.append('enter ' + name)
    try:
        yield
    finally:
        events.append('exit ' + name)
",
            c"legacy_protocol.py",
            c"legacy_protocol",
        )
        .unwrap();
        let mut warnings = WarningService::new(
            py,
            PyList::empty(py).as_any(),
            WarningFilterOwnership::Native,
        )
        .unwrap();
        let mut context = ProtocolContext::default();
        for (factory, name, style) in [
            ("wrapper", "outer", HookStyle::Wrapper),
            ("legacy", "timeout", HookStyle::LegacyWrapper),
            ("wrapper", "inner", HookStyle::Wrapper),
        ] {
            let protocol = module.getattr(factory).unwrap().call1((name,)).unwrap();
            protocol.call_method0("__next__").unwrap();
            context.push(protocol.unbind(), style);
        }
        context.finish(py, Ok(py.None()), &mut warnings).unwrap();
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            [
                "enter outer",
                "enter timeout",
                "enter inner",
                "exit inner",
                "exit timeout",
                "exit outer",
            ]
        );
    });
}
