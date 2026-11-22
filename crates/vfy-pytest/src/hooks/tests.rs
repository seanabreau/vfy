use super::{NativeHookDefault, NativeHookRegistry};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule, PyTuple};

#[test]
fn private_default_preserves_first_and_multi_result_contracts() {
    Python::initialize();
    Python::attach(|py| {
        let registry = Py::new(py, NativeHookRegistry::default()).unwrap();
        let kwargs = PyDict::new(py);
        let removed = PyTuple::empty(py);
        for (firstresult, value, expected) in [
            (false, None, Vec::new()),
            (false, Some(7_i32), vec![7]),
            (true, None, Vec::new()),
            (true, Some(7), vec![7]),
        ] {
            let result = NativeHookRegistry::call_subset_with_lazy_default(
                registry.bind(py),
                "pytest_unobserved",
                &kwargs,
                removed.as_any(),
                firstresult,
                "runner",
                NativeHookDefault {
                    execute: || {
                        value.map_or_else(
                            || Ok(py.None()),
                            |value| Ok(value.into_pyobject(py)?.into_any().unbind()),
                        )
                    },
                    materialize: || panic!("unobserved default was materialized"),
                },
            )
            .unwrap();
            if firstresult {
                if expected.is_empty() {
                    assert!(result.is_none(py));
                } else {
                    assert_eq!(vec![result.extract::<i32>(py).unwrap()], expected);
                }
            } else {
                assert_eq!(result.extract::<Vec<i32>>(py).unwrap(), expected);
            }
        }
    });
}

const MIXED_WRAPPERS: &std::ffi::CStr = cr"
events = []
def impl(**options):
    def decorate(fn):
        fn.__vfy_hookimpl__ = options
        return fn
    return decorate
class Plain:
    def __init__(self, label): self.label = label
    def pytest_hook(self, value):
        events.append(self.label)
        return value + self.label
class First:
    @impl(tryfirst=True)
    def pytest_hook(self):
        events.append('first')
class Last:
    @impl(trylast=True)
    def pytest_hook(self):
        events.append('last')
class Outer:
    @impl(wrapper=True, tryfirst=True)
    def pytest_hook(self):
        events.append('outer enter')
        result = yield
        events.append('outer exit')
        return result
class Legacy:
    @impl(hookwrapper=True)
    def pytest_hook(self):
        events.append('legacy enter')
        result = yield
        events.append('legacy exit')
        result.force_result(['changed', *result.get_result()])
";

#[test]
fn registry_order_and_mixed_wrappers_match_pluggy() {
    Python::initialize();
    Python::attach(|py| {
        // Import the package before its submodules: concurrent test threads that
        // import a submodule first can deadlock on CPython module locks.
        py.import("vfy").unwrap();
        let module = PyModule::from_code(py, MIXED_WRAPPERS, c"registry.py", c"registry").unwrap();
        let registry = Py::new(py, NativeHookRegistry::default()).unwrap();
        for (class, args) in [
            ("Plain", Some("a")),
            ("Plain", Some("b")),
            ("Last", None),
            ("First", None),
            ("Outer", None),
            ("Legacy", None),
        ] {
            let class = module.getattr(class).unwrap();
            let plugin = match args {
                Some(label) => class.call1((label,)),
                None => class.call0(),
            }
            .unwrap();
            NativeHookRegistry::register(registry.bind(py), py, plugin.unbind(), None).unwrap();
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("value", "value ").unwrap();
        let result =
            NativeHookRegistry::call(registry.bind(py), py, "pytest_hook", &kwargs, false).unwrap();
        assert_eq!(
            result.extract::<Vec<String>>(py).unwrap(),
            ["changed", "value b", "value a"]
        );
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            [
                "outer enter",
                "legacy enter",
                "first",
                "b",
                "a",
                "last",
                "legacy exit",
                "outer exit"
            ]
        );
        module
            .getattr("events")
            .unwrap()
            .call_method0("clear")
            .unwrap();
        let result =
            NativeHookRegistry::call(registry.bind(py), py, "pytest_hook", &kwargs, true).unwrap();
        assert_eq!(
            result.extract::<Vec<String>>(py).unwrap(),
            ["changed", "v", "a", "l", "u", "e", " ", "b"]
        );
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            [
                "outer enter",
                "legacy enter",
                "first",
                "b",
                "legacy exit",
                "outer exit"
            ]
        );
    });
}

#[test]
fn callbacks_can_mutate_registration_without_changing_active_dispatch() {
    Python::initialize();
    Python::attach(|py| {
        py.import("vfy").unwrap();
        let module = PyModule::from_code(
            py,
            cr"
events = []
class Added:
    def pytest_hook(self): events.append('added')
class Mutating:
    def pytest_hook(self):
        events.append('mutating')
        registry.register(Added(), 'added')
        registry.unregister(name='mutating')
",
            c"registration.py",
            c"registration",
        )
        .unwrap();
        let registry = Py::new(py, NativeHookRegistry::default()).unwrap();
        module.setattr("registry", &registry).unwrap();
        let plugin = module.getattr("Mutating").unwrap().call0().unwrap();
        NativeHookRegistry::register(
            registry.bind(py),
            py,
            plugin.unbind(),
            Some("mutating".into()),
        )
        .unwrap();
        let kwargs = PyDict::new(py);
        NativeHookRegistry::call(registry.bind(py), py, "pytest_hook", &kwargs, false).unwrap();
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            ["mutating"]
        );
        NativeHookRegistry::call(registry.bind(py), py, "pytest_hook", &kwargs, false).unwrap();
        assert_eq!(
            module
                .getattr("events")
                .unwrap()
                .extract::<Vec<String>>()
                .unwrap(),
            ["mutating", "added"]
        );
        module.delattr("registry").unwrap();
    });
}
