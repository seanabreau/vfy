use super::*;
use crate::results::partial_native_result;
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, PhasePayload, PhaseResult, SubtestResult,
};

#[test]
fn streamed_xdist_keys_borrow_the_first_python_nodeid_across_equal_reports() {
    Python::initialize();
    Python::attach(|py| {
        let directory = tempfile::tempdir().expect("cache directory");
        let mut session = cache_session(directory.path());
        session
            .select(Vec::new(), Vec::new(), Vec::new())
            .expect("controller selection");
        let value = "tests/test_cases.py::test_value[long-parameter-é]";
        let first = PyString::new(py, value);
        let later = PyString::new(py, value);
        assert_ne!(first.as_ptr(), later.as_ptr());
        let first_owner = first.as_ptr();
        let first_utf8 = first.to_str().expect("valid node ID").as_ptr();
        session.append_xdist_report(
            first.as_any(),
            PyString::new(py, "call").as_any(),
            PyString::new(py, "failed").as_any(),
        );
        session.append_xdist_report(
            later.as_any(),
            PyString::new(py, "call").as_any(),
            PyString::new(py, "passed").as_any(),
        );
        drop(first);
        drop(later);
        let (key, update) = session
            .xdist_last_failed_updates
            .get_key_value(value)
            .expect("coalesced node ID remains owned");
        assert_eq!(key.as_py_str().as_ptr(), first_owner);
        assert_eq!(key.as_str().as_ptr(), first_utf8);
        assert_eq!(key.as_str(), value);
        assert!(matches!(update, LastFailedUpdate::Cleared));
        session
            .finish_xdist_reports(py, "timestamp")
            .expect("borrowed node ID remains valid through completion");
    });
}

#[test]
fn streamed_xdist_cache_matches_batch_duplicates_and_keeps_selection_staged() {
    Python::initialize();
    Python::attach(|py| {
        let batch_dir = tempfile::tempdir().expect("batch cache directory");
        let stream_dir = tempfile::tempdir().expect("stream cache directory");
        for directory in [batch_dir.path(), stream_dir.path()] {
            seed_cache(
                directory,
                "cache/lastfailed",
                &serde_json::json!({"prior": true}),
            );
            seed_cache(
                directory,
                "cache/nodeids",
                &serde_json::json!(["historical"]),
            );
        }
        let mut batch = cache_session(batch_dir.path());
        let mut streamed = cache_session(stream_dir.path());
        for session in [&mut batch, &mut streamed] {
            session
                .select(Vec::new(), Vec::new(), Vec::new())
                .expect("controller selection");
        }
        let before = streamed
            .export_collection_snapshot()
            .expect("initial selection");
        let reports = [
            ("prior", "setup", "passed"),
            ("failed", "setup", "failed"),
            ("failed", "teardown", "passed"),
            ("subtests", "call", "failed"),
            ("subtests", "call", "skipped"),
            ("subtests", "call", "failed"),
            ("subtests", "call", "passed"),
            ("prior", "call", "passed"),
            ("crashed", "call", "failed"),
            ("skipped", "setup", "skipped"),
        ];
        for (nodeid, phase, outcome) in reports {
            streamed.append_xdist_report(
                PyString::new(py, nodeid).as_any(),
                PyString::new(py, phase).as_any(),
                PyString::new(py, outcome).as_any(),
            );
        }
        assert_eq!(
            streamed
                .export_collection_snapshot()
                .expect("staged selection"),
            before
        );
        assert_eq!(
            read_seeded_cache(stream_dir.path(), "cache/lastfailed"),
            serde_json::json!({"prior": true})
        );
        for session in [&mut batch, &mut streamed] {
            session
                .collect_report("late.py".to_owned(), "failed", Vec::new())
                .expect("late collection failure");
            session
                .collect_report("crashed".to_owned(), "passed", Vec::new())
                .expect("late collection success precedes all phases");
        }
        batch
            .finish(
                reports
                    .into_iter()
                    .map(|(nodeid, phase, outcome)| {
                        (nodeid.to_owned(), phase.to_owned(), outcome.to_owned())
                    })
                    .collect(),
                "2026-10-05T00:00:00",
            )
            .expect("batch finish");
        streamed
            .finish_xdist_reports(py, "2026-10-05T00:00:00")
            .expect("stream finish");
        for key in ["cache/lastfailed", "cache/nodeids"] {
            assert_eq!(
                read_seeded_cache(batch_dir.path(), key),
                read_seeded_cache(stream_dir.path(), key)
            );
        }
        assert_eq!(
            read_seeded_cache(stream_dir.path(), "cache/lastfailed"),
            serde_json::json!({"crashed": true, "failed": true, "late.py": true})
        );
    });
}

#[test]
fn streamed_xdist_errors_preserve_selection_and_conversion_priority_before_batch_retry() {
    Python::initialize();
    Python::attach(|py| {
        for conversion in [false, true] {
            let directory = tempfile::tempdir().expect("cache directory");
            seed_cache(
                directory.path(),
                "cache/lastfailed",
                &serde_json::json!({"prior": true}),
            );
            let mut session = cache_session(directory.path());
            session
                .select(Vec::new(), Vec::new(), Vec::new())
                .expect("controller selection");
            let before = session
                .export_collection_snapshot()
                .expect("initial selection");
            for (nodeid, phase) in [("partial", "call"), ("bad", "invalid")] {
                session.append_xdist_report(
                    PyString::new(py, nodeid).as_any(),
                    PyString::new(py, phase).as_any(),
                    PyString::new(py, "failed").as_any(),
                );
            }
            if conversion {
                let surrogate = py
                    .eval(c"chr(0xd800)", None, None)
                    .expect("surrogate Python string");
                session.append_xdist_report(
                    &surrogate,
                    PyString::new(py, "call").as_any(),
                    PyString::new(py, "passed").as_any(),
                );
            }
            let error = session
                .finish_xdist_reports(py, "timestamp")
                .expect_err("invalid stream should fail");
            if conversion {
                assert!(error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py));
            } else {
                assert!(error.is_instance_of::<PyValueError>(py));
                assert!(error.to_string().contains("unknown native cache phase"));
            }
            assert_eq!(
                session
                    .export_collection_snapshot()
                    .expect("rejected selection"),
                before
            );
            assert_eq!(
                read_seeded_cache(directory.path(), "cache/lastfailed"),
                serde_json::json!({"prior": true})
            );
            session
                .finish(
                    vec![("prior".to_owned(), "call".to_owned(), "passed".to_owned())],
                    "timestamp",
                )
                .expect("fresh batch can reuse unmodified selection");
            assert_eq!(
                read_seeded_cache(directory.path(), "cache/lastfailed"),
                serde_json::json!({})
            );
        }
    });
}

#[test]
fn streamed_xdist_valid_key_cycles_and_report_lifetimes_match_batch_owners() {
    let _gc_guard = crate::test_gc::lock();
    Python::initialize();
    Python::attach(|py| {
        let directory = tempfile::tempdir().expect("cache directory");
        let namespace = PyDict::new(py);
        namespace
            .set_item("CacheSession", py.get_type::<NativeCacheSession>())
            .expect("cache class");
        namespace
            .set_item("cachedir", directory.path().to_string_lossy().as_ref())
            .expect("cache root");
        py.run(
            c"
import gc
import weakref

released = []
class OwnedString(str):
    def __del__(self):
        released.append(str(self))

for streamed in (False, True):
    session = CacheSession(cachedir)
    session.select_items([], [], [])
    text = OwnedString('test_owned')
    owners = [(text, 'call', 'passed')]
    reference = weakref.ref(text)
    if streamed:
        session._append_xdist_report(*owners[0])
    del text
    gc.collect()
    assert reference() is not None and released == []
    if streamed:
        session._finish_xdist_reports('timestamp')
    else:
        session.finish(owners, 'timestamp')
    gc.collect()
    assert reference() is not None and released == []
    del owners
    gc.collect()
    assert reference() is None and released == ['test_owned']
    released.clear()

finalized = []
class CyclicString(str):
    def __del__(self):
        try:
            finalized.append(self.session.has_selection)
        except BaseException as error:
            finalized.append(type(error).__name__)

text = CyclicString('test_cycle')
text.session = CacheSession(cachedir)
text.session.select_items([], [], [])
text.session._append_xdist_report(text, 'call', 'passed')
reference = weakref.ref(text)
del text
gc.collect()
assert reference() is None
assert finalized == [True], finalized

resurrected = []
resurrection_errors = []
class ResurrectingString(str):
    def __del__(self):
        try:
            self.session._append_xdist_report('test_reentered', 'call', 'failed')
            resurrected.append(self)
        except BaseException as error:
            resurrection_errors.append((type(error).__name__, str(error)))

text = ResurrectingString('test_resurrected')
text.session = CacheSession(cachedir)
text.session.select_items([], [], [])
text.session._append_xdist_report(text, 'call', 'passed')
del text
gc.collect()
assert resurrection_errors == [], resurrection_errors
assert len(resurrected) == 1
rescued = resurrected.pop()
assert str(rescued) == 'test_resurrected' and rescued.session.has_selection
rescued.session._finish_xdist_reports('timestamp')
import json
from pathlib import Path
assert json.loads((Path(cachedir) / 'v/cache/lastfailed').read_text()) == {'test_reentered': True}
reference = weakref.ref(rescued)
del rescued
gc.collect()
assert reference() is None and resurrected == []
",
            Some(&namespace),
            Some(&namespace),
        )
        .expect("valid borrowed-key cycles and report-owner lifetimes");
    });
}

#[test]
fn streamed_xdist_defers_the_batch_conversion_errors_exactly() {
    let _gc_guard = crate::test_gc::lock();
    Python::initialize();
    Python::attach(|py| {
        let directory = tempfile::tempdir().expect("cache directory");
        let namespace = PyDict::new(py);
        namespace
            .set_item("CacheSession", py.get_type::<NativeCacheSession>())
            .expect("cache class");
        namespace
            .set_item("cachedir", directory.path().to_string_lossy().as_ref())
            .expect("cache root");
        py.run(
            c"
for reports in [
    [('partial', 'call', 'failed'), ('n', 'unknown', 'passed')],
    [('n', 'unknown', 'passed'), (chr(0xd800), 'call', 'passed')],
    [('n', 'call', 'unknown'), ('n', 3, 'passed')],
    [(3, 'call', 'passed'), (chr(0xd800), 'call', 'passed')],
    [('n', 'call', 3)],
]:
    batch = CacheSession(cachedir)
    streamed = CacheSession(cachedir)
    for session in (batch, streamed):
        session.select_items([], [], [])
    before = streamed.export_collection_snapshot()
    for report in reports:
        streamed._append_xdist_report(*report)
    errors = []
    for publish in (lambda: batch.finish(reports, 'timestamp'),
                    lambda: streamed._finish_xdist_reports('timestamp')):
        try:
            publish()
        except Exception as error:
            errors.append((type(error), error.args))
    assert len(errors) == 2 and errors[0] == errors[1], errors
    assert streamed.export_collection_snapshot() == before
    streamed.finish([('fresh', 'call', 'passed')], 'timestamp')

class Bad:
    pass
class Other:
    pass
value = Bad()
reports = [(value, 'call', 'passed')]
batch = CacheSession(cachedir)
streamed = CacheSession(cachedir)
for session in (batch, streamed):
    session.select_items([], [], [])
streamed._append_xdist_report(*reports[0])
value.__class__ = Other
errors = []
for publish in (lambda: batch.finish(reports, 'timestamp'),
                lambda: streamed._finish_xdist_reports('timestamp')):
    try:
        publish()
    except TypeError as error:
        errors.append(error.args)
assert len(errors) == 2 and errors[0] == errors[1], errors

import gc
import weakref
class OwnedString(str):
    pass
text = OwnedString(chr(0xd800))
text.session = CacheSession(cachedir)
text.session.select_items([], [], [])
text.session._append_xdist_report(text, 'call', 'passed')
reference = weakref.ref(text)
del text
gc.collect()
assert reference() is None
",
            Some(&namespace),
            Some(&namespace),
        )
        .expect("streaming conversion errors and cycles match the batch boundary");
    });
}

fn completion_case(nodeid: &str, phases: &[(ExecutionPhase, ExecutionOutcome)]) -> CaseResult {
    CaseResult::new(
        nodeid.to_owned(),
        0.0,
        phases
            .iter()
            .map(|(phase, outcome)| {
                PhaseResult::new(
                    *outcome,
                    *phase,
                    0.0,
                    PhasePayload {
                        exception: None,
                        stdout: String::new(),
                        stderr: String::new(),
                        log: String::new(),
                        detail: String::new(),
                        skip_location: None,
                        terminal_lead: String::new(),
                        report_sections: None,
                        report_summary: None,
                    },
                )
            })
            .collect::<Vec<_>>(),
    )
}

#[test]
fn typed_completion_preserves_tuple_outcomes_and_stepwise_phase_order() {
    use ExecutionOutcome::{Error, Failed, Passed, Skipped, XFailed, XPassed};
    use ExecutionPhase::{Call, Setup, Teardown};

    let mut subtest = completion_case("subtest", &[(Setup, Passed), (Call, Passed)]);
    subtest.set_subtests(
        vec![SubtestResult {
            report: None,
            outcome: Passed,
            context: "value=1".to_owned(),
            exception: None,
            detail: String::new(),
            skip_location: None,
            terminal_lead: String::new(),
        }]
        .into_boxed_slice(),
    );
    let cases = vec![
        completion_case(
            "passed",
            &[(Setup, Passed), (Call, Passed), (Teardown, Passed)],
        ),
        completion_case("skipped", &[(Setup, Skipped), (Teardown, Passed)]),
        completion_case("xfailed", &[(Call, XFailed)]),
        completion_case("xpassed", &[(Call, XPassed)]),
        completion_case("failed", &[(Call, Failed), (Teardown, Error)]),
        subtest,
    ];
    let nodeids: Vec<String> = cases.iter().map(|case| case.nodeid.clone()).collect();
    let result = partial_native_result(cases, Vec::new(), None, nodeids.len(), 0.0);
    let tuples: Vec<_> = result
        .phase_outcomes()
        .map(|(nodeid, phase, outcome, _)| {
            (
                nodeid.to_owned(),
                phase.as_str().to_owned(),
                outcome.report_outcome().as_str().to_owned(),
            )
        })
        .collect();
    for format in ["nodeid", "metadata"] {
        let tuple_dir = tempfile::tempdir().expect("tuple cache directory should be created");
        let typed_dir = tempfile::tempdir().expect("typed cache directory should be created");
        for directory in [tuple_dir.path(), typed_dir.path()] {
            seed_cache(
                directory,
                "cache/lastfailed",
                &serde_json::json!({
                    "passed": true, "skipped": true, "xfailed": true, "xpassed": true
                }),
            );
        }
        let mut tuple_session =
            NativeCacheSession::new(&tuple_dir.path().to_string_lossy(), format)
                .expect("tuple session should initialize");
        let mut typed_session =
            NativeCacheSession::new(&typed_dir.path().to_string_lossy(), format)
                .expect("typed session should initialize");
        for session in [&mut tuple_session, &mut typed_session] {
            session
                .select(
                    nodeids.clone(),
                    Vec::new(),
                    vec!["stepwise".to_owned(), "stepwise-skip".to_owned()],
                )
                .expect("completion cases should be selected");
        }
        tuple_session
            .finish(tuples.clone(), "2026-10-02T12:00:00")
            .expect("tuple completion should succeed");
        typed_session
            .finish_result(&result, "2026-10-02T12:00:00")
            .expect("typed completion should succeed");
        for key in ["cache/nodeids", "cache/lastfailed", "cache/stepwise"] {
            assert_eq!(
                read_seeded_cache(tuple_dir.path(), key),
                read_seeded_cache(typed_dir.path(), key)
            );
        }
        assert_eq!(
            read_seeded_cache(typed_dir.path(), "cache/lastfailed"),
            serde_json::json!({"failed": true})
        );
        let stepwise = read_seeded_cache(typed_dir.path(), "cache/stepwise");
        assert_eq!(
            if format == "nodeid" {
                &stepwise
            } else {
                &stepwise["last_failed"]
            },
            "failed"
        );
    }
}

#[test]
fn malformed_tuple_completion_preserves_selection_before_typed_retry() {
    Python::initialize();
    for (phase, outcome) in [("unknown", "passed"), ("call", "unknown")] {
        let directory = tempfile::tempdir().expect("cache directory should be created");
        let mut session = cache_session(directory.path());
        seed_cache(
            directory.path(),
            "cache/lastfailed",
            &serde_json::json!({"test_a": true}),
        );
        session
            .select(vec!["test_a".to_owned()], Vec::new(), Vec::new())
            .expect("test should be selected");
        let error = session
            .finish(
                vec![
                    ("test_a".to_owned(), "call".to_owned(), "passed".to_owned()),
                    ("test_a".to_owned(), phase.to_owned(), outcome.to_owned()),
                ],
                "2026-10-02T12:00:00",
            )
            .expect_err("malformed suffix should fail");
        Python::attach(|py| assert!(error.is_instance_of::<PyValueError>(py)));
        assert!(session.state.is_some());
        assert_eq!(
            read_seeded_cache(directory.path(), "cache/lastfailed"),
            serde_json::json!({"test_a": true})
        );
        assert!(!directory.path().join("v/cache/nodeids").exists());
        let result = partial_native_result(
            vec![completion_case(
                "test_a",
                &[(ExecutionPhase::Call, ExecutionOutcome::Passed)],
            )],
            Vec::new(),
            None,
            1,
            0.0,
        );
        session
            .finish_result(&result, "2026-10-02T12:00:00")
            .expect("typed completion should reuse the preserved selection");
        assert_eq!(
            read_seeded_cache(directory.path(), "cache/lastfailed"),
            serde_json::json!({})
        );
    }
}

#[test]
fn empty_typed_completion_requires_and_consumes_selection() {
    Python::initialize();
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let mut session = cache_session(directory.path());
    let result = partial_native_result(Vec::new(), Vec::new(), None, 0, 0.0);
    let error = session
        .finish_result(&result, "2026-10-02T12:00:00")
        .expect_err("unselected completion should fail");
    Python::attach(|py| assert!(error.is_instance_of::<PyRuntimeError>(py)));
    session
        .select(Vec::new(), Vec::new(), Vec::new())
        .expect("empty collection should be selectable");
    session
        .finish_result(&result, "2026-10-02T12:00:00")
        .expect("empty selected completion should persist");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/nodeids"),
        serde_json::json!([])
    );
    let error = session
        .finish_result(&result, "2026-10-02T12:00:00")
        .expect_err("completion should consume selection only once");
    Python::attach(|py| assert!(error.is_instance_of::<PyRuntimeError>(py)));
}

#[test]
fn subtest_completion_clears_parent_failure_after_consuming_stepwise_skip() {
    for format in ["nodeid", "metadata"] {
        for (skip_first, failures) in [(false, 1), (true, 1), (true, 2)] {
            let directory = tempfile::tempdir().expect("cache directory should be created");
            let mut session = NativeCacheSession::new(&directory.path().to_string_lossy(), format)
                .expect("stepwise schema should initialize");
            let mut flags = vec!["stepwise".to_owned()];
            if skip_first {
                flags.push("stepwise-skip".to_owned());
            }
            session
                .select(
                    vec![
                        "subtest".to_owned(),
                        "ordinary".to_owned(),
                        "later".to_owned(),
                    ],
                    Vec::new(),
                    flags,
                )
                .expect("stepwise suite should select");
            let mut case = completion_case(
                "subtest",
                &[(ExecutionPhase::Call, ExecutionOutcome::Passed)],
            );
            case.set_subtests(
                (0..failures)
                    .map(|index| SubtestResult {
                        report: None,
                        outcome: ExecutionOutcome::Failed,
                        context: index.to_string(),
                        exception: None,
                        detail: String::new(),
                        skip_location: None,
                        terminal_lead: String::new(),
                    })
                    .collect(),
            );
            let reaches_ordinary = skip_first && failures == 1;
            let mut cases = vec![case];
            if reaches_ordinary {
                cases.push(completion_case(
                    "ordinary",
                    &[(ExecutionPhase::Call, ExecutionOutcome::Failed)],
                ));
            }
            let result = partial_native_result(cases, Vec::new(), None, 3, 0.0);
            session
                .finish_result(&result, "2026-10-02T12:00:00")
                .expect("subtest reports and parent should persist");
            let failed_path = directory.path().join("v/cache/lastfailed");
            let last_failed = if failed_path.exists() {
                read_seeded_cache(directory.path(), "cache/lastfailed")
            } else {
                serde_json::json!({})
            };
            assert_eq!(
                last_failed,
                if reaches_ordinary {
                    serde_json::json!({"ordinary": true})
                } else {
                    serde_json::json!({})
                }
            );
            let stepwise = read_seeded_cache(directory.path(), "cache/stepwise");
            let resume = if format == "nodeid" {
                &stepwise
            } else {
                &stepwise["last_failed"]
            };
            assert_eq!(
                resume,
                &if reaches_ordinary {
                    serde_json::json!("ordinary")
                } else {
                    Value::Null
                }
            );
        }
    }
}

fn seed_cache(root: &Path, key: &str, value: &Value) {
    let path = root.join("v").join(key);
    fs::create_dir_all(path.parent().expect("cache key has a parent"))
        .expect("cache parent should be created");
    fs::write(
        &path,
        serde_json::to_vec(value).expect("cache value should serialize"),
    )
    .expect("cache state should be seeded");
}

fn read_seeded_cache(root: &Path, key: &str) -> Value {
    let path = root.join("v").join(key);
    serde_json::from_slice(&fs::read(path).expect("cache value should exist"))
        .expect("cache value should parse")
}

fn cache_session(root: &Path) -> NativeCacheSession {
    NativeCacheSession::new(&root.to_string_lossy(), "metadata")
        .expect("cache session should accept the modern pytest schema")
}

#[test]
fn legacy_stepwise_cache_reads_and_writes_the_nodeid_schema() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/stepwise",
        &serde_json::json!("test_b"),
    );
    let mut session = NativeCacheSession::new(&directory.path().to_string_lossy(), "nodeid")
        .expect("legacy cache schema should be accepted");
    assert_eq!(
        session
            .select(
                vec!["test_a".to_owned(), "test_b".to_owned()],
                Vec::new(),
                vec!["stepwise".to_owned()]
            )
            .expect("legacy failure should resume"),
        [1]
    );
    session
        .finish(
            vec![("test_b".to_owned(), "call".to_owned(), "passed".to_owned())],
            "2026-09-25T12:00:00",
        )
        .expect("legacy recovery should persist");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/stepwise"),
        Value::Null
    );
}

#[test]
fn staged_stepwise_preserves_original_count_after_wrappers_reorder_items() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/stepwise",
        &serde_json::json!({
            "last_failed": "test_b", "last_test_count": 3, "last_cache_date_str": "2026-09-25T12:00:00"
        }),
    );
    seed_cache(
        directory.path(),
        "cache/lastfailed",
        &serde_json::json!({"test_b": true}),
    );
    let mut session = cache_session(directory.path());
    let flags = vec!["stepwise".to_owned(), "failed-first".to_owned()];
    assert_eq!(
        session
            .select_stepwise(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                flags.clone()
            )
            .expect("stepwise should precede wrappers"),
        [1, 2]
    );
    assert_eq!(
        session
            .select(
                vec!["test_c".to_owned(), "test_b".to_owned()],
                Vec::new(),
                flags
            )
            .expect("failed-first should follow wrappers"),
        [1, 0]
    );
    session
        .finish(Vec::new(), "2026-09-25T12:01:00")
        .expect("staged cache should persist");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/stepwise")["last_test_count"],
        3
    );
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/nodeids"),
        serde_json::json!(["test_b", "test_c"])
    );
}

#[test]
fn cache_parser_preserves_literal_tokens_and_decodes_nonfinite_numbers() {
    let mut bytes =
        br#"{"literal":"NaN Infinity -Infinity","values":[NaN,Infinity,-Infinity]}"#.to_vec();

    let (value, markers) = parse_cache_json(&mut bytes).expect("pytest cache JSON should parse");
    let Value::Object(value) = value else {
        panic!("expected an object cache value");
    };
    let markers = markers
        .numbers
        .expect("raw non-finite numbers need markers");
    assert_eq!(value["literal"], "NaN Infinity -Infinity");
    let Value::Array(values) = &value["values"] else {
        panic!("expected non-finite values");
    };
    assert_eq!(
        markers.matches(values[0].as_str().unwrap()),
        Some(NonFiniteFloat::NaN)
    );
    assert_eq!(
        markers.matches(values[1].as_str().unwrap()),
        Some(NonFiniteFloat::PositiveInfinity)
    );
    assert_eq!(
        markers.matches(values[2].as_str().unwrap()),
        Some(NonFiniteFloat::NegativeInfinity)
    );
}

#[test]
fn cache_parser_rejects_malformed_bare_nonfinite_tokens() {
    for bytes in [
        br#"{"value":NaNish}"#.as_slice(),
        br#"{"value":Infinity0}"#.as_slice(),
        br"{NaN: 1}".as_slice(),
        br#"{"valid": 1, Infinity: 2}"#.as_slice(),
    ] {
        assert!(parse_cache_json(&mut bytes.to_vec()).is_err());
    }
}

#[test]
fn cache_parser_retains_surrogate_string_bytes_in_values_and_keys() {
    let mut bytes = br#"{"\udcff":["prefix\ud800suffix","\ud800\udc00","\\udcff",NaN]}"#.to_vec();
    let (value, markers) =
        parse_cache_json(&mut bytes).expect("surrogate cache strings should parse");
    let object = value.as_object().expect("cache value should be an object");
    let (key, values) = object
        .iter()
        .next()
        .expect("cache object should have a key");
    assert_eq!(markers.strings[key], b"\xed\xb3\xbf");
    let values = values
        .as_array()
        .expect("cache value should contain an array");
    assert_eq!(
        markers.strings[values[0].as_str().unwrap()],
        b"prefix\xed\xa0\x80suffix"
    );
    assert_eq!(values[1], "\u{10000}");
    assert_eq!(values[2], "\\udcff");
    assert_eq!(
        markers
            .numbers
            .expect("NaN needs a number marker")
            .matches(values[3].as_str().unwrap()),
        Some(NonFiniteFloat::NaN)
    );
}

#[test]
fn surrogate_cache_strings_do_not_hide_invalid_json_or_bypass_depth_limits() {
    for bytes in [
        br#""\ud800\uZZZZ""#.as_slice(),
        b"\"\\ud800\n\"".as_slice(),
        br#"{"\udcff":1,}"#.as_slice(),
        br#""\udcff" trailing"#.as_slice(),
        b"\"\\udcff\xff\"".as_slice(),
    ] {
        assert!(
            parse_cache_json(&mut bytes.to_vec()).is_err(),
            "invalid JSON {bytes:?}"
        );
    }
    let mut bytes = vec![b'['; MAX_CACHE_JSON_DEPTH];
    bytes.extend_from_slice(br#""\udcff""#);
    bytes.extend(std::iter::repeat_n(b']', MAX_CACHE_JSON_DEPTH));
    assert!(parse_cache_json(&mut bytes.clone()).is_ok());
    bytes.insert(0, b'[');
    bytes.push(b']');
    assert!(parse_cache_json(&mut bytes).is_err());
}

#[test]
fn last_failed_collection_prioritizes_paths_and_enables_skipping_after_a_real_failure() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let failed = directory.path().join("test_z.py");
    let unrelated = directory.path().join("test_a.py");
    fs::write(&failed, "").expect("failed file should exist");
    fs::write(&unrelated, "").expect("unrelated file should exist");
    seed_cache(
        directory.path(),
        "cache/lastfailed",
        &serde_json::json!({"test_z.py::test_failed": true}),
    );
    let mut session = cache_session(directory.path());
    session.configure_last_failed_collection(&directory.path().to_string_lossy());
    assert!(!session.skip_collection_file(&unrelated.to_string_lossy()));
    let paths = vec![
        (
            "test_a.py".to_owned(),
            unrelated.to_string_lossy().into_owned(),
            true,
            false,
        ),
        (
            "test_z.py".to_owned(),
            failed.to_string_lossy().into_owned(),
            true,
            false,
        ),
    ];
    assert_eq!(
        session
            .collection_report_indices("directory", "", paths)
            .unwrap(),
        (vec![1, 0], false)
    );
    let children = vec![
        (
            "test_z.py::test_passed".to_owned(),
            failed.to_string_lossy().into_owned(),
            false,
            false,
        ),
        (
            "test_z.py::test_failed".to_owned(),
            failed.to_string_lossy().into_owned(),
            false,
            false,
        ),
    ];
    assert_eq!(
        session
            .collection_report_indices("file", &failed.to_string_lossy(), children)
            .unwrap(),
        (vec![1], true)
    );
    assert!(session.skip_collection_file(&unrelated.to_string_lossy()));
    assert!(!session.skip_collection_file(&failed.to_string_lossy()));
}

#[test]
fn cache_depth_gate_accepts_512_levels_and_rejects_513() {
    let nested = |depth: usize| {
        let mut bytes = vec![b'['; depth];
        bytes.extend_from_slice(b"null");
        bytes.extend(std::iter::repeat_n(b']', depth));
        bytes
    };
    let mut at_limit = nested(MAX_CACHE_JSON_DEPTH);
    let mut over_limit = nested(MAX_CACHE_JSON_DEPTH + 1);

    assert!(cache_json_within_depth(&at_limit));
    assert!(parse_cache_json(&mut at_limit).is_ok());
    assert!(!cache_json_within_depth(&over_limit));
    assert!(parse_cache_json(&mut over_limit).is_err());
}

#[test]
fn completed_passing_cases_do_not_drop_collection_failures() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let mut session = cache_session(directory.path());
    session
        .collect_report("test_broken.py".to_owned(), "failed", Vec::new())
        .expect("collection error should be retained");
    session
        .select(
            vec!["test_ok.py::test_ok".to_owned()],
            Vec::new(),
            Vec::new(),
        )
        .expect("other collected tests should be selected");
    session
        .finish(
            vec![(
                "test_ok.py::test_ok".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
            )],
            "2026-09-30T12:00:00",
        )
        .expect("collection and execution outcomes should persist together");

    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({"test_broken.py": true})
    );
}

#[test]
fn nodeid_cache_completion_compares_order_duplicates_and_value_types() {
    for (seed, unchanged) in [
        (r#"[ "test_a", "test_b" ]"#, true),
        (r#"["test_b", "test_a"]"#, false),
        (r#"["test_a", "test_a", "test_b"]"#, false),
        (r#"["test_a", "test_b", null]"#, false),
        (r#"["test_a", "test_b", 1]"#, false),
        (r#"{"test_a": true}"#, false),
        ("null", false),
    ] {
        let directory = tempfile::tempdir().expect("cache directory should be created");
        let path = directory.path().join("v/cache/nodeids");
        fs::create_dir_all(path.parent().expect("cache file should have a parent"))
            .expect("cache parent should be created");
        fs::write(&path, seed).expect("cache seed should be written");
        let mut session = cache_session(directory.path());
        session
            .select(
                vec!["test_a".to_owned(), "test_b".to_owned()],
                Vec::new(),
                Vec::new(),
            )
            .expect("tests should be selected");
        session
            .finish(Vec::new(), "2026-10-02T12:00:00")
            .expect("node IDs should be persisted");
        assert_eq!(
            read_seeded_cache(directory.path(), "cache/nodeids"),
            serde_json::json!(["test_a", "test_b"])
        );
        assert_eq!(
            fs::read_to_string(&path).expect("cache should be readable") == seed,
            unchanged
        );
    }
}

#[test]
fn cache_selectors_consume_seeded_state_and_persist_completed_outcomes() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/lastfailed",
        &serde_json::json!({"test_b": true}),
    );
    seed_cache(
        directory.path(),
        "cache/nodeids",
        &serde_json::json!(["test_a", "test_b"]),
    );

    let mut last_failed = cache_session(directory.path());
    assert_eq!(
        last_failed
            .select(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                Vec::new(),
                vec!["last-failed".to_owned()],
            )
            .expect("last-failed selection should succeed"),
        [1]
    );
    last_failed
        .finish(
            vec![("test_b".to_owned(), "call".to_owned(), "passed".to_owned())],
            "2026-09-25T12:00:00",
        )
        .expect("completed cache state should persist");

    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({})
    );
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/nodeids"),
        serde_json::json!(["test_a", "test_b"])
    );

    let mut failed_first = cache_session(directory.path());
    assert_eq!(
        failed_first
            .select(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                Vec::new(),
                vec!["failed-first".to_owned()],
            )
            .expect("failed-first selection should succeed"),
        [0, 1, 2]
    );

    let mut new_first = cache_session(directory.path());
    assert_eq!(
        new_first
            .select(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                vec![10, 30, 20],
                vec!["new-first".to_owned()],
            )
            .expect("new-first selection should succeed"),
        [2, 1, 0]
    );
}

#[test]
fn failure_selectors_apply_empty_cache_policy_without_hiding_outside_failures() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    for selector in ["last-failed", "failed-first"] {
        assert_eq!(
            cache_session(directory.path())
                .select(
                    vec!["test_present".to_owned()],
                    Vec::new(),
                    vec![
                        selector.to_owned(),
                        "last-failed-no-failures-none".to_owned()
                    ],
                )
                .expect("empty failure cache should honor the requested policy"),
            Vec::<usize>::new()
        );
    }
    seed_cache(
        directory.path(),
        "cache/lastfailed",
        &serde_json::json!({"test_outside_selection": true}),
    );
    for selector in ["last-failed", "failed-first"] {
        assert_eq!(
            cache_session(directory.path())
                .select(
                    vec!["test_present".to_owned()],
                    Vec::new(),
                    vec![
                        selector.to_owned(),
                        "last-failed-no-failures-none".to_owned()
                    ],
                )
                .expect("unselected cached failures should retain the requested tests"),
            [0]
        );
    }
}

#[test]
fn new_first_keeps_last_duplicate_item_at_first_nodeid_position() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/nodeids",
        &serde_json::json!(["test_old"]),
    );
    assert_eq!(
        cache_session(directory.path())
            .select(
                vec![
                    "test_old".to_owned(),
                    "test_new_a".to_owned(),
                    "test_old".to_owned(),
                    "test_new_b".to_owned(),
                    "test_new_a".to_owned(),
                ],
                vec![10, 40, 50, 30, 20],
                vec!["new-first".to_owned()],
            )
            .expect("duplicate node IDs should be coalesced before sorting"),
        [3, 4, 2]
    );
}

#[test]
fn stepwise_seed_is_rejected_when_collection_size_changes_then_resumes() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/stepwise",
        &serde_json::json!({
            "last_failed": "test_b",
            "last_test_count": 4,
            "last_cache_date_str": "2026-09-25T12:00:00"
        }),
    );
    let nodeids = vec![
        "test_a".to_owned(),
        "test_b".to_owned(),
        "test_c".to_owned(),
    ];

    let mut stale = cache_session(directory.path());
    assert_eq!(
        stale
            .select(nodeids.clone(), Vec::new(), vec!["stepwise".to_owned()],)
            .expect("stale stepwise state should be ignored"),
        [0, 1, 2]
    );
    stale
        .finish(Vec::new(), "2026-09-25T12:01:00")
        .expect("stepwise state should refresh");

    let mut resumed = cache_session(directory.path());
    assert_eq!(
        resumed
            .select(nodeids, Vec::new(), vec!["stepwise".to_owned()])
            .expect("fresh stepwise state should load"),
        [0, 1, 2]
    );
    resumed
        .finish(
            vec![("test_b".to_owned(), "call".to_owned(), "failed".to_owned())],
            "2026-09-25T12:02:00",
        )
        .expect("failed test should become the resume point");

    let mut next_run = cache_session(directory.path());
    assert_eq!(
        next_run
            .select(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                Vec::new(),
                vec!["stepwise".to_owned()],
            )
            .expect("stepwise resume should select the failure and following tests"),
        [1, 2]
    );
}

#[test]
fn stepwise_skip_ignores_one_failure_and_stops_at_the_next() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let nodeids = vec![
        "test_steps.py::test_a".to_owned(),
        "test_steps.py::test_b".to_owned(),
        "test_steps.py::test_c".to_owned(),
    ];

    let mut skipped_failure = cache_session(directory.path());
    assert_eq!(
        skipped_failure
            .select(
                nodeids.clone(),
                Vec::new(),
                vec!["stepwise".to_owned(), "stepwise-skip".to_owned()],
            )
            .expect("stepwise-skip should select the suite"),
        [0, 1, 2]
    );
    skipped_failure
        .finish(
            vec![
                (
                    "test_steps.py::test_a".to_owned(),
                    "call".to_owned(),
                    "passed".to_owned(),
                ),
                (
                    "test_steps.py::test_b".to_owned(),
                    "call".to_owned(),
                    "failed".to_owned(),
                ),
                (
                    "test_steps.py::test_c".to_owned(),
                    "call".to_owned(),
                    "failed".to_owned(),
                ),
            ],
            "2026-09-25T12:00:00",
        )
        .expect("the second failure should become the resume point");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/stepwise")["last_failed"],
        "test_steps.py::test_c"
    );

    let mut resumed = cache_session(directory.path());
    assert_eq!(
        resumed
            .select(nodeids, Vec::new(), vec!["stepwise".to_owned()])
            .expect("the next run should resume at the second failure"),
        [2]
    );
}

#[test]
fn collection_reports_persist_failures_and_migrate_recovered_file_nodes() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let mut failed_collection = cache_session(directory.path());
    failed_collection
        .collect_report("test_broken.py".to_owned(), "failed", Vec::new())
        .expect("collection failure should be recorded");
    failed_collection
        .finish_collection()
        .expect("collection failure should persist without an executed test session");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({"test_broken.py": true})
    );

    let mut recovered_collection = cache_session(directory.path());
    recovered_collection
        .collect_report(
            "test_broken.py".to_owned(),
            "passed",
            vec!["test_broken.py::test_fixed".to_owned()],
        )
        .expect("recovered collection should be recorded");
    assert_eq!(
        recovered_collection
            .select(
                vec!["test_broken.py::test_fixed".to_owned()],
                Vec::new(),
                vec!["last-failed".to_owned()],
            )
            .expect("recovered child should inherit its failed file selection"),
        [0]
    );
    recovered_collection
        .finish_collection()
        .expect("the recovered collection state should persist");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({"test_broken.py::test_fixed": true})
    );
}

#[test]
fn stepwise_and_lastfailed_retain_phase_order_and_setup_skip_resume_point() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let nodeid = "test_steps.py::test_a";
    let mut session = cache_session(directory.path());
    session
        .select(
            vec![nodeid.to_owned()],
            Vec::new(),
            vec!["stepwise".to_owned(), "stepwise-skip".to_owned()],
        )
        .expect("stepwise-skip should select the test");
    session
        .finish(
            vec![
                (nodeid.to_owned(), "setup".to_owned(), "passed".to_owned()),
                (nodeid.to_owned(), "call".to_owned(), "failed".to_owned()),
                (
                    nodeid.to_owned(),
                    "teardown".to_owned(),
                    "failed".to_owned(),
                ),
            ],
            "2026-09-25T12:00:00",
        )
        .expect("teardown should become the second failure");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/stepwise")["last_failed"],
        nodeid
    );

    let mut resumed = cache_session(directory.path());
    resumed
        .select(
            vec![nodeid.to_owned()],
            Vec::new(),
            vec!["stepwise".to_owned()],
        )
        .expect("stepwise should resume at the test");
    resumed
        .finish(
            vec![
                (nodeid.to_owned(), "setup".to_owned(), "skipped".to_owned()),
                (
                    nodeid.to_owned(),
                    "teardown".to_owned(),
                    "passed".to_owned(),
                ),
            ],
            "2026-09-25T12:01:00",
        )
        .expect("skipped setup and passing teardown should persist");
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/stepwise")["last_failed"],
        nodeid
    );
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({})
    );
}

#[test]
fn controller_cache_snapshot_overwrites_later_worker_cache_writes() {
    for initial_failures in [
        serde_json::json!({}),
        serde_json::json!({"prior.py::test_case": true}),
    ] {
        let directory = tempfile::tempdir().expect("cache directory should be created");
        seed_cache(directory.path(), "cache/lastfailed", &initial_failures);
        seed_cache(
            directory.path(),
            "cache/nodeids",
            &serde_json::json!(["prior.py::test_case"]),
        );
        let mut session = cache_session(directory.path());
        assert!(!session.has_selection());
        session.select(Vec::new(), Vec::new(), Vec::new()).unwrap();
        assert!(session.has_selection());
        // A worker's public cache fixture can write these shared keys while the
        // controller keeps its own previously selected cache state.
        seed_cache(
            directory.path(),
            "cache/lastfailed",
            &serde_json::json!({"injected.py::test_case": true}),
        );
        seed_cache(
            directory.path(),
            "cache/nodeids",
            &serde_json::json!(["injected.py::test_case"]),
        );
        session
            .finish(
                vec![(
                    "current.py::test_case".to_owned(),
                    "call".to_owned(),
                    "passed".to_owned(),
                )],
                "2026-10-01T23:00:00",
            )
            .unwrap();
        assert!(!session.has_selection());
        assert_eq!(
            read_seeded_cache(directory.path(), "cache/lastfailed"),
            initial_failures
        );
        assert_eq!(
            read_seeded_cache(directory.path(), "cache/nodeids"),
            serde_json::json!(["prior.py::test_case"])
        );
    }
}

#[test]
fn controller_collection_reports_update_an_already_selected_cache_snapshot() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    seed_cache(
        directory.path(),
        "cache/lastfailed",
        &serde_json::json!({"recovered.py": true}),
    );
    let mut session = cache_session(directory.path());
    session.select(Vec::new(), Vec::new(), Vec::new()).unwrap();
    session
        .collect_report(
            "recovered.py".to_owned(),
            "passed",
            vec!["recovered.py::test_case".to_owned()],
        )
        .unwrap();
    session
        .collect_report("broken.py".to_owned(), "failed", Vec::new())
        .unwrap();
    session
        .finish(
            vec![(
                "recovered.py::test_case".to_owned(),
                "call".to_owned(),
                "passed".to_owned(),
            )],
            "2026-10-01T23:00:00",
        )
        .unwrap();
    assert_eq!(
        read_seeded_cache(directory.path(), "cache/lastfailed"),
        serde_json::json!({"broken.py": true})
    );
}

#[test]
fn collection_snapshot_preserves_worker_selection_history_and_report_recovery() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let root = directory.path();
    seed_cache(
        root,
        "cache/nodeids",
        &serde_json::json!(["historical", "test_a", "test_b"]),
    );
    let failures = serde_json::json!({"test_b": true, "broken.py": true});
    seed_cache(root, "cache/lastfailed", &failures);
    let mut worker = cache_session(root);
    worker.configure_last_failed_collection(&root.to_string_lossy());
    worker
        .collect_report(
            "broken.py".to_owned(),
            "passed",
            vec!["broken.py::recovered".to_owned()],
        )
        .expect("collection recovery should be recorded");
    assert_eq!(
        worker
            .select(
                vec![
                    "test_a".to_owned(),
                    "test_b".to_owned(),
                    "test_c".to_owned()
                ],
                vec![10, 20, 30],
                vec!["failed-first".to_owned(), "new-first".to_owned()],
            )
            .expect("worker selection should succeed"),
        [2, 1, 0]
    );
    let snapshot = worker
        .export_collection_snapshot()
        .expect("worker state should serialize");
    let mut controller = cache_session(root);
    controller
        .adopt_collection_snapshot(&snapshot)
        .expect("fresh controller should adopt worker state");
    assert!(controller.has_selection());
    assert_eq!(
        controller
            .export_collection_snapshot()
            .expect("adopted state should serialize"),
        snapshot
    );
    assert_eq!(read_seeded_cache(root, "cache/lastfailed"), failures);
    controller
        .finish(
            vec![("test_b".to_owned(), "call".to_owned(), "passed".to_owned())],
            "2026-10-03T12:00:00",
        )
        .expect("controller should persist collected history and outcomes");
    assert_eq!(
        read_seeded_cache(root, "cache/nodeids"),
        serde_json::json!(["historical", "test_a", "test_b", "test_c"])
    );
    assert_eq!(
        read_seeded_cache(root, "cache/lastfailed"),
        serde_json::json!({"broken.py::recovered": true})
    );
}

#[test]
fn collection_snapshot_preserves_pending_stepwise_and_unselected_collection_errors() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let root = directory.path();
    seed_cache(
        root,
        "cache/stepwise",
        &serde_json::json!({
            "last_failed": "test_b", "last_test_count": 2, "last_cache_date_str": "2026-10-02T12:00:00"
        }),
    );
    for pending in [false, true] {
        let mut worker = cache_session(root);
        worker
            .collect_report("broken.py".to_owned(), "failed", Vec::new())
            .expect("collection failure should be recorded");
        if pending {
            assert_eq!(
                worker
                    .select_stepwise(
                        vec!["test_a".to_owned(), "test_b".to_owned()],
                        vec!["stepwise".to_owned()],
                    )
                    .expect("stepwise selection should succeed"),
                [1]
            );
        }
        let snapshot = worker
            .export_collection_snapshot()
            .expect("pending collection should serialize");
        let mut controller = cache_session(root);
        controller
            .collect_report("parent-only.py".to_owned(), "failed", Vec::new())
            .expect("controller startup report should be recorded");
        controller
            .adopt_collection_snapshot(&snapshot)
            .expect("pending collection should transfer");
        assert_eq!(
            controller
                .export_collection_snapshot()
                .expect("adopted collection should serialize"),
            snapshot
        );
        assert!(!controller.has_selection());
        controller
            .finish_collection()
            .expect("collection-only completion should persist failures");
        assert_eq!(
            read_seeded_cache(root, "cache/lastfailed"),
            serde_json::json!({"broken.py": true})
        );
        assert!(controller.adopt_collection_snapshot(&snapshot).is_err());
    }
}

#[test]
fn invalid_collection_snapshot_preserves_controller_state_before_successful_adoption() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let root = directory.path();
    let mut worker = cache_session(root);
    worker
        .select(vec!["test_a".to_owned()], Vec::new(), Vec::new())
        .expect("worker selection should succeed");
    let snapshot = worker
        .export_collection_snapshot()
        .expect("worker should serialize");
    let valid: Value = serde_json::from_str(&snapshot).expect("snapshot should be JSON");
    let mut invalid = vec!["{}".to_owned(), format!("{snapshot} null")];
    for (field, replacement) in [
        ("version", serde_json::json!(2)),
        ("cachedir", serde_json::json!("another-cache-directory")),
        ("stepwise_cache_format", serde_json::json!("nodeid")),
        ("unexpected", serde_json::json!(true)),
        (
            "pending_stepwise",
            serde_json::json!({"last_failed": null, "mode": "disabled", "test_count": 1}),
        ),
        (
            "collection_reports",
            serde_json::json!([{"nodeid": "bad.py", "outcome": "unknown", "child_nodeids": []}]),
        ),
    ] {
        let mut changed = valid.clone();
        changed[field] = replacement;
        invalid.push(serde_json::to_string(&changed).expect("malformed snapshot should serialize"));
    }
    let mut controller = cache_session(root);
    controller
        .collect_report("controller.py".to_owned(), "failed", Vec::new())
        .expect("controller report should be recorded");
    let original = controller
        .export_collection_snapshot()
        .expect("controller should serialize");
    for input in invalid {
        assert!(controller.adopt_collection_snapshot(&input).is_err());
        assert_eq!(
            controller
                .export_collection_snapshot()
                .expect("failed adoption should leave state serializable"),
            original
        );
    }
    controller
        .adopt_collection_snapshot(&snapshot)
        .expect("validation errors must not prevent later valid adoption");
    assert_eq!(
        controller
            .export_collection_snapshot()
            .expect("controller should serialize"),
        snapshot
    );
}

#[test]
fn published_worker_cache_inventory_releases_without_losing_controller_history() {
    let directory = tempfile::tempdir().expect("cache directory should be created");
    let root = directory.path();
    seed_cache(root, "cache/nodeids", &serde_json::json!(["historical"]));
    let mut worker = cache_session(root);
    worker.configure_last_failed_collection(&root.to_string_lossy());
    worker
        .collect_report(
            "module".to_owned(),
            "passed",
            vec!["test_a".to_owned(), "test_b".to_owned()],
        )
        .expect("complete module children should be recorded");
    worker
        .select(
            vec!["test_a".to_owned(), "test_b".to_owned()],
            Vec::new(),
            Vec::new(),
        )
        .expect("worker selection should succeed");
    let snapshot = worker
        .export_collection_snapshot()
        .expect("worker history should serialize");
    worker
        .release_worker_collection()
        .expect("published worker inventory should release");
    assert!(!worker.has_selection());
    assert!(worker.collection_reports.is_empty());
    assert!(worker.last_failed_collection.is_none());
    assert!(worker.pending_stepwise.is_none());
    assert_eq!(
        read_seeded_cache(root, "cache/nodeids"),
        serde_json::json!(["historical"])
    );
    let mut controller = cache_session(root);
    controller
        .adopt_collection_snapshot(&snapshot)
        .expect("published history should remain adoptable");
    assert!(controller.release_worker_collection().is_err());
    assert_eq!(
        controller
            .export_collection_snapshot()
            .expect("rejected release should preserve controller state"),
        snapshot
    );
    controller
        .finish(Vec::new(), "2026-10-03T12:00:00")
        .expect("controller should retain selected history");
    assert_eq!(
        read_seeded_cache(root, "cache/nodeids"),
        serde_json::json!(["historical", "test_a", "test_b"])
    );
}

#[test]
fn cache_values_take_open_w_permissions_and_keep_existing_modes() {
    let directory = tempfile::tempdir().expect("cache directory");
    let session = cache_session(directory.path());
    let service = &session.cache;
    let fresh = directory.path().join("v/cache/fresh");
    service
        .write_json_value("cache/fresh", &serde_json::json!({"a": 1}))
        .expect("fresh value");
    let ordinary = File::create(fresh.with_file_name("ordinary")).expect("open reference file");
    let fresh_mode = fs::metadata(&fresh).expect("fresh").permissions().mode();
    let ordinary_mode = ordinary.metadata().expect("ordinary").permissions().mode();
    assert_eq!(fresh_mode & 0o7777, ordinary_mode & 0o7777);
    let existing = directory.path().join("v/cache/existing");
    fs::write(&existing, "{}").expect("seed");
    fs::set_permissions(&existing, fs::Permissions::from_mode(0o640)).expect("chmod");
    service
        .write_json_value("cache/existing", &serde_json::json!({"a": 2}))
        .expect("existing value");
    assert_eq!(
        fs::metadata(&existing)
            .expect("existing")
            .permissions()
            .mode()
            & 0o7777,
        0o640
    );
}
