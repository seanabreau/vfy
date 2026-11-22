use super::model::{
    FixtureBindings, FixtureFinalizerErrors, ScopeOwnerKeys, SuiteBindings, TestBindings, XfailMode,
};
use super::runtime::{Runtime, SharedRuntime};
use super::teardown::teardown_all;
use super::test_call::invoke_test_body;
use super::test_setup::{resolve_test_arguments, test_execution_plan};
use pyo3::prelude::{PyAnyMethods, PyDictMethods};
use std::collections::HashMap;
use std::sync::Arc;
use vfy_core::test_plan::{
    FixtureChains, FixtureExecution, FixturePlan, ScopeOwners, TestBody, TestId, TestPlan,
};
use vfy_core::{FixtureGeneration, FixtureId, FixtureScope};

struct PlainLifecycleObjects {
    events: pyo3::Py<pyo3::types::PyAny>,
    fixture_definition: pyo3::Py<pyo3::types::PyAny>,
    item: pyo3::Py<pyo3::types::PyAny>,
}

fn plain_lifecycle_objects(py: pyo3::Python<'_>) -> PlainLifecycleObjects {
    let globals = pyo3::types::PyDict::new(py);
    py.import("builtins")
        .expect("builtins module")
        .getattr("exec")
        .expect("exec builtin")
        .call1((
            concat!(
                "from types import SimpleNamespace\n",
                "events = []\n",
                "def resource(request):\n",
                "    events.append('fixture setup')\n",
                "    yield 'resource value'\n",
                "    events.append('fixture teardown')\n",
                "fixture_definition = SimpleNamespace(\n",
                "    params=None, func=resource, argnames=('request',),\n",
                ")\n",
                "def test_body(resource):\n",
                "    events.append('test call: ' + resource)\n",
                "item = SimpleNamespace(\n",
                "    nodeid='test_lifecycle.py::test_body', obj=test_body,\n",
                "    config=SimpleNamespace(stash={}, getoption=lambda name: 'strict'), stash={},\n",
                "    get_closest_marker=lambda name: None,\n",
                "    funcargs={},\n",
                ")\n",
            ),
            &globals,
            &globals,
        ))
        .expect("define plain fixture, item, and test objects");
    let get = |name| {
        globals
            .get_item(name)
            .expect("Python namespace lookup")
            .expect("plain Python object")
            .unbind()
    };
    PlainLifecycleObjects {
        events: get("events"),
        fixture_definition: get("fixture_definition"),
        item: get("item"),
    }
}

fn plain_lifecycle_test_plan() -> TestPlan {
    TestPlan {
        id: TestId::from_index(0),
        collection_node: None,
        nodeid: "test_lifecycle.py::test_body".to_owned(),
        body: TestBody::Function,
        async_loop_scope: None,
        argument_names: Arc::from(["resource".to_owned()]),
        eager_fixtures: Arc::from(["resource".to_owned()]),
        fixture_chains: Arc::new(FixtureChains {
            ordered_names: vec!["resource".to_owned()],
            by_name: HashMap::from([("resource".to_owned(), vec![FixtureId::from_index(0)])]),
        }),
        fixture_state: None,
        owners: ScopeOwners {
            class: None,
            module: Arc::from("test_lifecycle.py"),
        },
    }
}

fn plain_lifecycle_fixture_plan() -> FixturePlan {
    FixturePlan {
        id: FixtureId::from_index(0),
        name: "resource".to_owned(),
        package_baseid: String::new(),
        dependencies: vec!["request".to_owned()],
        declared_scope: FixtureScope::Function,
        async_loop_scope: None,
        execution: FixtureExecution::Generator,
        parameterized: false,
    }
}

fn plain_lifecycle_runtime(py: pyo3::Python<'_>, objects: &PlainLifecycleObjects) -> SharedRuntime {
    let fixture_callable = super::model::FixtureCallable::from_definition(
        &objects
            .item
            .bind(py)
            .getattr("config")
            .expect("item config"),
        &py.import("inspect").expect("inspect module"),
        &py.import("_pytest.python")
            .expect("pytest Python module")
            .getattr("get_direct_param_fixture_func")
            .expect("direct parameter fixture"),
        vfy_core::AsyncioMode::Strict,
        objects.fixture_definition.bind(py),
        &["request".to_owned()],
    )
    .expect("classify the plain generator fixture");
    let suite = SuiteBindings::new(
        vec![plain_lifecycle_test_plan()],
        vec![TestBindings {
            item: objects.item.clone_ref(py),
            compact: None,
            warning_profile: 0,
            fixture_state: None,
            xfail: None,
            xfail_mode: XfailMode::Evaluate,
        }],
        vec![plain_lifecycle_fixture_plan()],
        vec![FixtureBindings {
            definition: objects.fixture_definition.clone_ref(py),
            callable: fixture_callable,
        }],
        HashMap::new(),
        None,
        None,
    )
    .expect("aligned semantic plans and plain-Python bindings");
    SharedRuntime::new(Runtime {
        suite,
        compatibility: super::model::PytestCompatibilityPolicy {
            fixturedefs_use_nodeid: false,
            legacy_fixture_order: false,
            fixture_finalizer_errors: FixtureFinalizerErrors::Group,
            unittest_wide_scope_instance: false,
            parameter_comparison: super::model::FixtureParameterComparison::Equality,
            scope_override_lookup: super::model::FixtureScopeOverrideLookup::Required,
            cached_fixture_error_traceback: super::model::CachedFixtureErrorTraceback::Restore,
            scope_checks: super::model::FixtureScopeChecks::Declared,
            unhandled_async_fixture: super::model::UnhandledAsyncFixture::Error,
            returned_test_value: super::model::ReturnedTestValue::WarnType,
            dynamic_fixture_teardown: super::model::DynamicFixtureTeardown::Allow,
        },
        cache: HashMap::new(),
        active: Vec::new(),
        active_by_fixture: HashMap::new(),
        pending_finalizers: HashMap::new(),
        owner_finalizers: HashMap::new(),
        active_nodes: vec!["test_lifecycle.py::test_body".to_owned()],
        owner_nodes: HashMap::from([(
            "test_lifecycle.py::test_body".to_owned(),
            objects.item.clone_ref(py),
        )]),
        package_setups: HashMap::new(),
        active_function: Some(TestId::from_index(0)),
        resolved_fixture_keys: HashMap::new(),
        retained_request_counts: HashMap::new(),
        resolving: Vec::new(),
        next_cache_generation: FixtureGeneration::default(),
    })
}

fn load_project_adapter_dependencies(py: pyo3::Python<'_>) {
    // The canonical Rust test script embeds the base interpreter for libpython.
    // Load the configured venv's real dependencies and editable .pth entries.
    let interpreter =
        std::env::var_os("PYO3_PYTHON").expect("run PyO3 tests through scripts/cargo-test.sh");
    let output = std::process::Command::new(interpreter)
        .args([
            "-c",
            "import json, site; print(json.dumps(site.getsitepackages()))",
        ])
        .output()
        .expect("query the configured project interpreter");
    assert!(
        output.status.success(),
        "project site-packages lookup failed: {}",
        String::from_utf8_lossy(&output.stderr),
    );
    let directories: Vec<String> = serde_json::from_slice(&output.stdout)
        .expect("project interpreter returns site-packages paths");
    let site = py.import("site").expect("Python site module");
    for directory in directories {
        site.call_method1("addsitedir", (directory,))
            .expect("load the project interpreter's real packages");
    }
}

#[test]
fn control_teardown_exception_is_propagated_by_identity() {
    pyo3::Python::initialize();
    pyo3::Python::attach(|py| {
        load_project_adapter_dependencies(py);
        let error = pyo3::exceptions::PyKeyboardInterrupt::new_err("cleanup interrupted");
        let original = error.value(py);
        let failure = super::runtime::TeardownFailure::from_pyerr(py, &error);
        assert!(failure.control.as_ref().unwrap().value(py).is(original));
        assert!(failure.exception.as_ref().unwrap().value(py).is(original));
        assert!(failure.into_pyerr(py).value(py).is(original));
    });
}

#[test]
fn aligned_bindings_run_plain_generator_fixture_through_native_lifecycle() {
    pyo3::Python::initialize();
    pyo3::Python::attach(|py| {
        load_project_adapter_dependencies(py);
        let objects = plain_lifecycle_objects(py);
        let runtime = plain_lifecycle_runtime(py, &objects);

        let value = super::fixture_setup::resolve_named_fixture(
            py,
            &runtime,
            0,
            "resource",
            None,
            None,
            super::fixture_setup::FixtureResolution::DECLARED,
        )
        .expect("named fixture resolves through the native plan");
        assert_eq!(
            value.bind(py).extract::<String>().expect("fixture value"),
            "resource value"
        );

        let plan = test_execution_plan(py, &runtime, 0);
        let kwargs = resolve_test_arguments(py, &runtime, 0, objects.item.bind(py), &plan)
            .expect("fixture arguments resolve through the native plan");
        let mut subtests = Vec::new();
        invoke_test_body(
            py,
            &runtime,
            0,
            objects.item.bind(py),
            kwargs.as_ref().map(|kwargs| kwargs.bind(py)),
            &plan,
            &mut subtests,
        )
        .expect("plain test function executes through the native adapter");
        assert!(subtests.is_empty());
        assert_eq!(
            objects
                .events
                .bind(py)
                .extract::<Vec<String>>()
                .expect("fixture lifecycle events"),
            ["fixture setup", "test call: resource value"]
        );

        assert!(teardown_all(py, &runtime).is_none());
        assert_eq!(
            objects
                .events
                .bind(py)
                .extract::<Vec<String>>()
                .expect("complete fixture lifecycle events"),
            [
                "fixture setup",
                "test call: resource value",
                "fixture teardown"
            ]
        );
    });
}

#[test]
fn suite_owner_keys_share_equal_collectors_and_outlive_the_compile_pool() {
    let mut keys = ScopeOwnerKeys::default();
    let first = keys.intern("tests/test_module.py");
    let same = keys.intern(&["tests/", "test_module.py"].concat());
    let other = keys.intern("tests/test_other.py");
    assert!(Arc::ptr_eq(&first, &same));
    assert_ne!(first, other);
    drop(keys);
    assert_eq!(&*first, "tests/test_module.py");
    assert_eq!(&*same, "tests/test_module.py");
    assert_eq!(&*other, "tests/test_other.py");
}

#[test]
fn interrupted_dispatch_snapshot_keeps_case_locations_and_retained_evidence() {
    use std::collections::BTreeMap;
    use std::time::Instant;
    use vfy_core::test_execution::{CaseResult, ExecutionOutcome, ExecutionPhase, PhaseResult};

    let case = |nodeid: &str| {
        CaseResult::new(
            nodeid.to_owned(),
            1.0,
            vec![PhaseResult::new(
                ExecutionOutcome::Passed,
                ExecutionPhase::Call,
                1.0,
                vfy_core::test_execution::PhasePayload {
                    exception: None,
                    stdout: "retained output".to_owned(),
                    stderr: String::new(),
                    log: String::new(),
                    detail: String::new(),
                    skip_location: None,
                    terminal_lead: String::new(),
                    report_sections: None,
                    report_summary: None,
                },
            )],
        )
    };
    let cases = vec![case("third.py::test_case"), case("first.py::test_case")];
    let indices = [2, 0];
    let locations = [
        (
            "first.py::test_case".to_owned(),
            ("first.py".to_owned(), Some(10), "test_case".to_owned()),
        ),
        (
            "second.py::test_case".to_owned(),
            ("second.py".to_owned(), Some(20), "test_case".to_owned()),
        ),
        (
            "third.py::test_case".to_owned(),
            ("third.py".to_owned(), Some(30), "test_case".to_owned()),
        ),
    ];
    let extras = BTreeMap::from([(
        "third.py::test_case".to_owned(),
        vec![vfy_core::PytestHtmlExtra {
            kind: "text".to_owned(),
            name: "proof".to_owned(),
            content: "retained attachment".to_owned(),
        }],
    )]);
    let errors = BTreeMap::new();
    let result = super::native_session::native_execution_snapshot(
        super::native_session::PartialReportSnapshot {
            cases: &cases,
            case_indices: &indices,
            warnings: &[],
            report_locations: Some(&locations),
            summary_locations: &BTreeMap::new(),
            html_extras: &extras,
            html_errors: &errors,
            collected: 3,
            started: Instant::now(),
        },
    );
    let json: serde_json::Value = serde_json::from_str(&result.transport_json().unwrap()).unwrap();
    assert_eq!(
        json["report_locations"],
        serde_json::json!([
            ["third.py::test_case", ["third.py", 30, "test_case"]],
            ["first.py::test_case", ["first.py", 10, "test_case"]],
        ])
    );
    assert_eq!(json["cases"][0]["nodeid"], "third.py::test_case");
    assert_eq!(json["cases"][1]["nodeid"], "first.py::test_case");
    assert_eq!(json["cases"][0]["phases"][0]["stdout"], "retained output");
    assert_eq!(
        json["html_extras"]["third.py::test_case"][0]["content"],
        "retained attachment"
    );
}
