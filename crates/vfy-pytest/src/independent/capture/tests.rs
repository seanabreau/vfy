use super::{
    LiveLogSettings, LogCaptureSettings, LogPhase, NativeLiveStream, NativeWarningRecorder,
    TestCapture, WarningScope, WarningSettings,
};
use crate::hooks::NativeHookRegistry;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::cell::RefCell;
use std::ffi::CString;
use std::rc::Rc;
use std::sync::Mutex;
use vfy_core::test_execution::ExecutionPhase;

// CPython can release its GIL during imports and thread.join(). These cases
// own the same process-wide logging handlers and warning filters.
static PYTHON_STATE: Mutex<()> = Mutex::new(());

fn with_python(case: impl FnOnce(Python<'_>)) {
    let _state = PYTHON_STATE.lock().unwrap();
    Python::initialize();
    Python::attach(|py| {
        // Import the package before its submodules: concurrent test threads that
        // import a submodule first can deadlock on CPython module locks.
        py.import("vfy").unwrap();
        case(py);
    });
}

fn settings(level: i32, format: &str, auto_indent: i32) -> LogCaptureSettings {
    LogCaptureSettings {
        shared_live: None,
        level: Some(level),
        format: format.to_owned(),
        date_format: None,
        auto_indent,
        live: None,
    }
}

fn live_scope(
    py: Python<'_>,
    stream: &Bound<'_, PyAny>,
    auto_indent: i32,
) -> (Py<NativeLiveStream>, LogPhase) {
    let live = NativeLiveStream::create(
        py,
        &LiveLogSettings {
            capture: None,
            level: Some(20),
            format: "%(message)s".to_owned(),
            date_format: None,
            stream: stream.clone().unbind(),
        },
        auto_indent,
    )
    .unwrap();
    let scope = LogPhase::protocol(py, Some(&live)).unwrap();
    (live, scope)
}

fn run(py: Python<'_>, source: &str, globals: &Bound<'_, PyDict>) {
    py.run(&CString::new(source).unwrap(), Some(globals), Some(globals))
        .unwrap();
}

#[test]
fn mutable_fixture_records_do_not_erase_phase_report_logs() {
    with_python(|py| {
        let capture = TestCapture::new(py, &settings(20, "%(message)s", 0)).unwrap();
        let setup = capture.begin_phase(py, ExecutionPhase::Setup).unwrap();
        py.import("logging")
            .unwrap()
            .call_method1("warning", ("setup",))
            .unwrap();
        assert_eq!(setup.finish(py).unwrap(), "setup");
        let call = capture.begin_phase(py, ExecutionPhase::Call).unwrap();
        let (fixture, cleanup) = capture.caplog(py).unwrap();
        let globals = PyDict::new(py);
        globals.set_item("caplog", fixture).unwrap();
        run(
            py,
            "import logging\nrecords = caplog.records\nlogging.warning('before clear')\ncaplog.clear()\nassert records is caplog.records and records == []\nlogging.warning('after clear')\nassert caplog.messages == ['after clear']\nassert [r.getMessage() for r in caplog.get_records('setup')] == ['setup']",
            &globals,
        );
        cleanup.bind(py).call0().unwrap();
        assert_eq!(call.finish(py).unwrap(), "before clear\nafter clear");
    });
}

#[test]
fn multiline_and_live_logs_use_the_existing_phase_boundary() {
    with_python(|py| {
        let stream = py.import("io").unwrap().call_method0("StringIO").unwrap();
        let (live, _loop_scope) = live_scope(py, &stream, -1);
        let capture = TestCapture::new(
            py,
            &LogCaptureSettings {
                shared_live: Some(live),
                ..settings(20, "%(levelname)s %(message)s", -1)
            },
        )
        .unwrap();
        let phase = capture.begin_phase(py, ExecutionPhase::Call).unwrap();
        py.import("logging")
            .unwrap()
            .call_method1("warning", ("first\nsecond",))
            .unwrap();
        assert_eq!(phase.finish(py).unwrap(), "WARNING first\n        second");
        let live = stream
            .call_method0("getvalue")
            .unwrap()
            .extract::<String>()
            .unwrap();
        assert!(live.contains("live log call"));
        assert!(live.ends_with("first\nsecond\n"));
    });
}

#[test]
fn warning_filters_capture_original_objects_and_restore_python_state() {
    with_python(|py| {
        let warnings = py.import("warnings").unwrap();
        let filters = warnings.getattr("filters").unwrap();
        let settings = WarningSettings {
            config_filters: PyList::new(py, ["error"]).unwrap().into_any().unbind(),
            command_filters: PyList::new(py, ["ignore:a.b"]).unwrap().into_any().unbind(),
        };
        let globals = PyDict::new(py);
        run(
            py,
            "class Mark:\n    args = ('always:axb',)\nclass Item:\n    def iter_markers(self, name):\n        return iter((Mark(),))\nitem = Item()",
            &globals,
        );
        let item = globals.get_item("item").unwrap().unwrap();
        let scope = WarningScope::start(py, &settings, Some(&item)).unwrap();
        warnings.call_method1("warn", ("axb",)).unwrap();
        warnings.call_method1("warn", ("a.b",)).unwrap();
        let originals = PyList::empty(py);
        let records = Rc::new(RefCell::new(Vec::new()));
        let recorder = NativeWarningRecorder::new(Rc::clone(&records));
        scope
            .dispatch(py, "test.py::test_warning", |message, nodeid| {
                originals.append(message)?;
                recorder.record(py, message, nodeid)
            })
            .unwrap();
        let records = records.borrow();
        assert_eq!(originals.len(), 1);
        assert_eq!(records.len(), 1);
        assert!(records[0].message.contains("UserWarning: axb"));
        assert_eq!(records[0].nodeid, "test.py::test_warning");
        assert!(filters.is(warnings.getattr("filters").unwrap()));
    });
}

#[test]
fn invalid_warning_category_restores_the_enclosing_filter_scope() {
    with_python(|py| {
        let warnings = py.import("warnings").unwrap();
        let previous = warnings.getattr("filters").unwrap();
        let settings = WarningSettings {
            config_filters: PyList::new(py, ["ignore::MissingCategory"])
                .unwrap()
                .into_any()
                .unbind(),
            command_filters: PyList::empty(py).into_any().unbind(),
        };
        let error = WarningScope::start(py, &settings, None).err().unwrap();
        // The Python parser calls the installed extension, not this test executable.
        let usage_error = py
            .import("vfy.native")
            .unwrap()
            .getattr("_pytest")
            .unwrap()
            .getattr("IndependentUsageError")
            .unwrap();
        assert!(error.is_instance(py, &usage_error));
        assert!(previous.is(warnings.getattr("filters").unwrap()));
    });
}

#[test]
fn failed_handler_reset_restores_root_level_and_attached_handlers() {
    with_python(|py| {
        let capture = TestCapture::new(py, &settings(10, "%(message)s", 0)).unwrap();
        let globals = PyDict::new(py);
        globals
            .set_item("handler", capture.handler.bind(py))
            .unwrap();
        run(
            py,
            "import logging\nroot = logging.getLogger()\nprevious_level = root.level\nprevious_handlers = root.handlers[:]\ndef fail_reset():\n    raise RuntimeError('reset failed')\nhandler._reset_stream = fail_reset",
            &globals,
        );
        let error = capture
            .begin_phase(py, ExecutionPhase::Setup)
            .err()
            .unwrap();
        assert!(error.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py));
        run(
            py,
            "assert root.level == previous_level\nassert root.handlers == previous_handlers",
            &globals,
        );
    });
}

#[test]
fn logging_records_and_live_stream_accept_background_python_threads() {
    with_python(|py| {
        let stream = py.import("io").unwrap().call_method0("StringIO").unwrap();
        let (live, _loop_scope) = live_scope(py, &stream, 0);
        let capture = TestCapture::new(
            py,
            &LogCaptureSettings {
                shared_live: Some(live),
                ..settings(20, "%(message)s", 0)
            },
        )
        .unwrap();
        let phase = capture.begin_phase(py, ExecutionPhase::Call).unwrap();
        let globals = PyDict::new(py);
        run(
            py,
            "import logging, threading\nthread = threading.Thread(target=logging.getLogger('native.thread').info, args=('thread sentinel',))\nthread.start()\nthread.join()",
            &globals,
        );
        assert_eq!(phase.finish(py).unwrap(), "thread sentinel");
        assert!(
            stream
                .call_method0("getvalue")
                .unwrap()
                .extract::<String>()
                .unwrap()
                .contains("thread sentinel")
        );
    });
}

#[test]
fn warning_dispatch_observes_messages_appended_by_callbacks() {
    with_python(|py| {
        let warnings = py.import("warnings").unwrap();
        let settings = WarningSettings {
            config_filters: PyList::new(py, ["always"]).unwrap().into_any().unbind(),
            command_filters: PyList::empty(py).into_any().unbind(),
        };
        let scope = WarningScope::start(py, &settings, None).unwrap();
        warnings.call_method1("warn", ("first",)).unwrap();
        let mut seen = Vec::new();
        scope
            .dispatch(py, "", |message, _nodeid| {
                let text = message.getattr("message")?.str()?.to_str()?.to_owned();
                if text == "first" {
                    warnings.call_method1("warn", ("second",))?;
                }
                seen.push(text);
                Ok(())
            })
            .unwrap();
        assert_eq!(seen, ["first", "second"]);
    });
}

#[test]
fn warning_recording_formats_at_the_registered_hook_priority() {
    with_python(|py| {
        for tryfirst in [false, true] {
            let records = Rc::new(RefCell::new(Vec::new()));
            let recorder = Py::new(py, NativeWarningRecorder::new(Rc::clone(&records))).unwrap();
            let globals = PyDict::new(py);
            globals
                .set_item("record", recorder.bind(py).getattr("record").unwrap())
                .unwrap();
            globals.set_item("tryfirst", tryfirst).unwrap();
            run(
                py,
                r"
seen = []
class User:
    def pytest_warning_recorded(self, warning_message):
        warning_message.message = UserWarning('changed by hook')
        seen.append(str(warning_message.message))
    pytest_warning_recorded.__vfy_hookimpl__ = {'tryfirst': tryfirst}
class Builtin:
    def pytest_warning_recorded(self, warning_message, nodeid):
        record(warning_message, nodeid)
user = User()
builtin = Builtin()
",
                &globals,
            );
            let registry = Py::new(py, NativeHookRegistry::new()).unwrap();
            for name in ["user", "builtin"] {
                NativeHookRegistry::register(
                    registry.bind(py),
                    py,
                    globals.get_item(name).unwrap().unwrap().unbind(),
                    Some(name.to_owned()),
                )
                .unwrap();
            }
            let scope = WarningScope::start(
                py,
                &WarningSettings {
                    config_filters: PyList::new(py, ["always"]).unwrap().into_any().unbind(),
                    command_filters: PyList::empty(py).into_any().unbind(),
                },
                None,
            )
            .unwrap();
            py.import("warnings")
                .unwrap()
                .call_method1("warn", ("body",))
                .unwrap();
            scope
                .dispatch(py, "test.py::test_warning", |message, nodeid| {
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("warning_message", message)?;
                    kwargs.set_item("nodeid", nodeid)?;
                    NativeHookRegistry::call(
                        registry.bind(py),
                        py,
                        "pytest_warning_recorded",
                        &kwargs,
                        false,
                    )?;
                    Ok(())
                })
                .unwrap();
            let expected = if tryfirst {
                "UserWarning: changed by hook"
            } else {
                "UserWarning: body"
            };
            assert_eq!(records.borrow().len(), 1);
            assert!(records.borrow()[0].message.contains(expected));
            assert_eq!(
                globals
                    .get_item("seen")
                    .unwrap()
                    .unwrap()
                    .extract::<Vec<String>>()
                    .unwrap(),
                ["changed by hook"]
            );
            globals.clear();
        }
    });
}

#[test]
fn interrupted_logging_scope_restores_handlers_and_preserves_the_pending_exception() {
    with_python(|py| {
        let root = py
            .import("logging")
            .unwrap()
            .call_method0("getLogger")
            .unwrap();
        let handlers = root.getattr("handlers").unwrap();
        let before = handlers.call_method0("copy").unwrap();
        let scope = LogPhase::protocol(py, None).unwrap();
        assert_ne!(handlers.len().unwrap(), before.len().unwrap());

        let interrupt = pyo3::exceptions::PyKeyboardInterrupt::new_err("peer stopped");
        let original = interrupt.value(py).clone();
        interrupt.restore(py);
        drop(scope);
        let pending =
            PyErr::take(py).expect("dropping a logging scope must preserve the interrupt");
        assert!(pending.value(py).is(&original));
        assert!(handlers.eq(before).unwrap());
    });
}
