//! Owns fixture lookup, scope checks, fixture error diagnostics, and the item
//! state that setup reads: report locations, xfail evaluation, and unittest and
//! package owner binding.

use super::compile::case_item;
use super::fixture_setup::{FixtureRequestOrigin, FixtureSetupPlan, fixture_request};
use super::model::{
    CachedFixtureErrorTraceback, FixtureStack, FixtureUse, TestBindings, XfailMode, XfailPlan,
};
use super::request::NativeRequest;
use super::runtime::{FixtureOutcome, PackageSetup, SharedRuntime, register_node_finalizer};
use crate::lifecycle::LifecycleService;
use crate::results::ReportLocation;
use pyo3::exceptions::{PyAttributeError, PyRuntimeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyDictMethods, PyErr, PyResult, PyTypeMethods, Python,
};
use pyo3::types::{PyAny, PyDict};
use vfy_core::FixtureScope;
use vfy_core::test_execution::ExecutionPhase;
use vfy_core::test_plan::{FixturePlan, TestPlan};

/// Pytest's report location for one case. Compact family cases share their
/// template item, so its domain is renamed from the function to the case.
pub(super) fn case_report_location(
    item: &Bound<'_, PyAny>,
    family: Option<(&str, &str)>,
) -> PyResult<ReportLocation> {
    let mut location = item
        .getattr(intern!(item.py(), "location"))?
        .extract::<ReportLocation>()?;
    if let Some((originalname, case_name)) = family {
        let domain_prefix = location.2.strip_suffix(originalname).ok_or_else(|| {
            PyRuntimeError::new_err(format!(
                "native family location does not end in test name: {}",
                location.2
            ))
        })?;
        location.2 = format!("{domain_prefix}{case_name}");
    }
    Ok(location)
}

/// Resolve a completed case's location from its retained template binding.
pub(super) fn executed_case_report_location(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> PyResult<ReportLocation> {
    let (item, family) = {
        let state = runtime.lock();
        let bindings = &state.suite.test_bindings[test_index];
        let family = bindings.compact.as_ref().map(|compact| {
            (
                compact.originalname.clone(),
                state.suite.tests()[test_index].nodeid[compact.case_name_start..].to_owned(),
            )
        });
        (bindings.item.clone_ref(py), family)
    };
    case_report_location(
        item.bind(py),
        family
            .as_ref()
            .map(|(originalname, case_name)| (&**originalname, case_name.as_str())),
    )
}

pub(super) fn has_marker_named(marks: &Bound<'_, PyAny>, expected: &str) -> PyResult<bool> {
    for mark in marks.try_iter()? {
        if mark?.getattr("name")?.extract::<String>()? == expected {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(super) fn refresh_xfail(
    runtime: &SharedRuntime,
    test_index: usize,
    item: &Bound<'_, PyAny>,
    lifecycle: &LifecycleService,
    phase: ExecutionPhase,
) -> PyResult<Option<XfailPlan>> {
    let py = item.py();
    let (current, runxfail) = {
        let state = runtime.lock();
        let bindings = &state.suite.test_bindings[test_index];
        if bindings.xfail_mode == XfailMode::Disabled {
            return Ok(None);
        }
        (
            bindings.xfail.as_ref().map(|value| value.clone_ref(py)),
            bindings.xfail_mode == XfailMode::Run,
        )
    };
    if current.is_some() {
        return Ok(if runxfail { None } else { current });
    }
    let value = lifecycle.evaluate_xfail_marks(item, phase)?;
    if value.is_none() {
        return Ok(None);
    }
    let raises = value.getattr(intern!(py, "raises"))?;
    let xfail = XfailPlan {
        reason: value.getattr(intern!(py, "reason"))?.extract()?,
        run: value.getattr(intern!(py, "run"))?.extract()?,
        strict: value.getattr(intern!(py, "strict"))?.extract()?,
        raises: (!raises.is_none()).then(|| raises.unbind()),
    };
    runtime.lock().suite.test_bindings[test_index].xfail = Some(Box::new(xfail.clone_ref(py)));
    Ok((!runxfail).then_some(xfail))
}

pub(super) fn select_fixture_use(
    py: Python<'_>,
    test: &TestPlan,
    bindings: &TestBindings,
    fixtures: &[FixturePlan],
    name: &str,
    same_name_depth: usize,
) -> PyResult<Option<FixtureUse>> {
    let Some(resolution) = test.resolve_fixture(fixtures, name, same_name_depth) else {
        return Ok(None);
    };
    let param = if resolution.source == vfy_core::test_plan::FixtureChainSource::Dynamic {
        let params = bindings
            .fixture_state
            .as_ref()
            .and_then(|state| state.dynamic_fixture_params.get(name))
            .ok_or_else(|| PyRuntimeError::new_err("dynamic fixture parameters are missing"))?;
        let planned_uses = test
            .fixture_state
            .as_ref()
            .and_then(|state| state.dynamic_fixture_uses.get(name))
            .ok_or_else(|| PyRuntimeError::new_err("dynamic fixture plan chain is missing"))?;
        if params.len() != planned_uses.len() {
            return Err(PyRuntimeError::new_err(
                "dynamic fixture parameter slots do not align with the resolved fixture chain",
            ));
        }
        params
            .get(resolution.chain_index)
            .ok_or_else(|| PyRuntimeError::new_err("dynamic fixture parameter slot is missing"))?
            .as_ref()
    } else {
        bindings.compact.as_ref().and_then(|compact| {
            compact
                .callspec
                .params
                .iter()
                .find(|(candidate, _)| candidate == name)
                .map(|(_, value)| value)
        })
    };
    let use_ = resolution.use_;
    Ok(Some(FixtureUse {
        fixture: use_.fixture,
        owner: test
            .scope_owner(use_.cache_scope)
            .unwrap_or_default()
            .to_owned(),
        cache_scope: use_.cache_scope,
        param: param.map(|value| value.clone_ref(py)),
    }))
}

pub(super) fn fixture_lookup_error(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    name: &str,
    fixture_stack: &FixtureStack,
) -> PyResult<PyErr> {
    let fixtures = py.import("_pytest.fixtures")?;
    let item = case_item(py, runtime, test_index)?;
    let request = Py::new(
        py,
        NativeRequest::for_test(runtime, item, test_index, fixture_stack.clone()),
    )?;
    let error = fixtures
        .getattr("FixtureLookupError")?
        .call1((name, request))?;
    Ok(PyErr::from_value(error))
}

pub(super) fn missing_dynamic_fixture_parameter_error(
    py: Python<'_>,
    item: &Bound<'_, PyAny>,
    name: &str,
    definition: &Py<PyAny>,
    origin: FixtureRequestOrigin,
    fixture_stack: &FixtureStack,
) -> PyResult<PyErr> {
    let has_params = !definition.bind(py).getattr("params")?.is_none();
    if has_params {
        let nofuncargs = match item.getattr("nofuncargs") {
            Ok(value) => value.is_truthy()?,
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => false,
            Err(error) => return Err(error),
        };
        if nofuncargs {
            let message = format!(
                "{} does not support fixtures, maybe unittest.TestCase subclass?\nNode id: {}\nFunction type: {}",
                item.getattr("name")?.extract::<String>()?,
                item.getattr("nodeid")?.extract::<String>()?,
                item.get_type().name()?
            );
            return pytest_failed_error(py, message);
        }
    }
    let config = item.getattr("config")?;
    let rootpath = config.getattr("rootpath")?;
    let location = py
        .import("_pytest.compat")?
        .getattr("getlocation")?
        .call1((definition.bind(py).getattr("func")?, &rootpath))?
        .extract::<String>()?;
    let (filename, line) = missing_fixture_request_location(py, origin, fixture_stack)?;
    let absolute = py
        .import("_pytest.pathlib")?
        .getattr("absolutepath")?
        .call1((filename.bind(py),))?;
    let requested_path = match absolute.call_method1("relative_to", (&rootpath,)) {
        Ok(path) => path.str()?.to_string(),
        Err(error) if error.is_instance_of::<PyValueError>(py) => absolute.str()?.to_string(),
        Err(error) => return Err(error),
    };
    let nodeid = item.getattr("nodeid")?.extract::<String>()?;
    let message = format!(
        "The requested fixture has no parameter defined for test:\n    {nodeid}\n\nRequested fixture '{name}' defined in:\n{location}\n\nRequested here:\n{requested_path}:{line}"
    );
    pytest_failed_error(py, message)
}

fn missing_fixture_request_location(
    py: Python<'_>,
    origin: FixtureRequestOrigin,
    fixture_stack: &FixtureStack,
) -> PyResult<(Py<PyAny>, usize)> {
    if origin == FixtureRequestOrigin::Declared {
        let (class, method, callee) = if fixture_stack.0.is_none() {
            ("TopRequest", "_fillfixtures", "getfixturevalue")
        } else {
            ("FixtureDef", "execute", "getfixturevalue")
        };
        if let Some(location) = fixture_method_call_location(py, class, method, callee)? {
            return Ok(location);
        }
        // Older pytest constructs the SubRequest in a separate compute frame.
        if fixture_stack.0.is_some()
            && let Some(location) = fixture_method_call_location(
                py,
                "FixtureRequest",
                "_compute_fixture_value",
                "execute",
            )?
        {
            return Ok(location);
        }
        // Newer pytest resolves dependencies without the getfixturevalue frame.
        if fixture_stack.0.is_some()
            && let Some(location) = fixture_method_call_location(
                py,
                "FixtureRequest",
                "_get_active_fixturedef",
                "execute",
            )?
        {
            return Ok(location);
        }
    }
    let frame = py.import("sys")?.call_method0("_getframe")?;
    Ok((
        frame.getattr("f_code")?.getattr("co_filename")?.unbind(),
        frame.getattr("f_lineno")?.extract()?,
    ))
}

fn fixture_method_call_location(
    py: Python<'_>,
    class: &str,
    method: &str,
    callee: &str,
) -> PyResult<Option<(Py<PyAny>, usize)>> {
    let Some(method) = py
        .import("_pytest.fixtures")?
        .getattr(class)?
        .getattr_opt(method)?
    else {
        return Ok(None);
    };
    let instructions = py
        .import("dis")?
        .call_method1("get_instructions", (&method,))?;
    let mut found_lookup = false;
    for instruction in instructions.try_iter()? {
        let instruction = instruction?;
        if instruction
            .getattr("argval")?
            .extract::<String>()
            .ok()
            .as_deref()
            == Some(callee)
        {
            found_lookup = true;
        } else if found_lookup
            && matches!(
                instruction.getattr("opname")?.extract::<String>()?.as_str(),
                "CALL" | "CALL_KW"
            )
        {
            return Ok(Some((
                method.getattr("__code__")?.getattr("co_filename")?.unbind(),
                instruction
                    .getattr("positions")?
                    .getattr("lineno")?
                    .extract()?,
            )));
        }
    }
    Ok(None)
}

fn pytest_failed_error(py: Python<'_>, message: String) -> PyResult<PyErr> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    let failed = py
        .import("_pytest.outcomes")?
        .getattr("Failed")?
        .call((message,), Some(&kwargs))?;
    Ok(PyErr::from_value(failed))
}

#[derive(Clone, Copy)]
pub(super) struct FixtureScopeMismatch<'a> {
    pub(super) name: &'a str,
    pub(super) requesting_scope: FixtureScope,
    pub(super) fixture_scope: FixtureScope,
    pub(super) fixture_definition: &'a Py<PyAny>,
    pub(super) fixture_stack: &'a FixtureStack,
}

pub(super) fn fixture_scope_mismatch(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    mismatch: FixtureScopeMismatch<'_>,
) -> PyResult<PyErr> {
    let FixtureScopeMismatch {
        name,
        requesting_scope,
        fixture_scope,
        fixture_definition,
        fixture_stack,
    } = mismatch;
    let request = Py::new(
        py,
        NativeRequest {
            runtime: runtime.downgrade(),
            resolved_fixture_keys: runtime.fixture_keys(test_index),
            item: Some(case_item(py, runtime, test_index)?),
            package_baseid: None,
            test_index,
            target: None,
            requesting_scope,
            fixture_name: Some(name.to_owned()),
            param: None,
            fixture_stack: fixture_stack.clone(),
        },
    )?;
    let message = py
        .import("vfy._pytest.native_pytest")?
        .getattr("fixture_scope_mismatch_message")?
        .call1((
            request.bind(py),
            name,
            requesting_scope.as_str(),
            fixture_scope.as_str(),
            fixture_definition.bind(py),
        ))?
        .extract::<String>()?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    match py
        .import("_pytest.outcomes")?
        .getattr("fail")?
        .call((message,), Some(&kwargs))
    {
        Err(error) => Ok(error),
        Ok(_) => Err(PyRuntimeError::new_err(
            "pytest's fail helper returned unexpectedly",
        )),
    }
}

fn unhandled_async_fixture_error(
    py: Python<'_>,
    item_name: &str,
    fixture_name: &str,
    autouse: bool,
) -> PyResult<PyErr> {
    let autouse_text = if autouse { " with autouse=True" } else { "" };
    let repr = py.import("builtins")?.getattr("repr")?;
    let item_name = repr.call1((item_name,))?.extract::<String>()?;
    let fixture_name = repr.call1((fixture_name,))?.extract::<String>()?;
    let message = format!(
        "{item_name} requested an async fixture {fixture_name}{autouse_text}, with no plugin or hook that handled it. This is an error, as pytest does not natively support it.\nSee: https://docs.pytest.org/en/stable/deprecations.html#sync-test-depending-on-async-fixture"
    );
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    match py
        .import("_pytest.outcomes")?
        .getattr("fail")?
        .call((message,), Some(&kwargs))
    {
        Err(error) => Ok(error),
        Ok(_) => Err(PyRuntimeError::new_err(
            "pytest's fail helper returned unexpectedly",
        )),
    }
}

pub(super) fn unhandled_async_fixture_error_for_item(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    fixture_name: &str,
    definition: &Py<PyAny>,
) -> PyResult<PyErr> {
    let item = case_item(py, runtime, test_index)?;
    let item_name = item.bind(py).getattr("name")?.extract::<String>()?;
    let autouse = definition.bind(py).getattr("_autouse")?.extract::<bool>()?;
    unhandled_async_fixture_error(py, &item_name, fixture_name, autouse)
}

pub(super) fn warn_unhandled_async_fixture_for_item(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    fixture_name: &str,
    definition: &Py<PyAny>,
) -> PyResult<()> {
    let item = case_item(py, runtime, test_index)?;
    let item_name = item.bind(py).getattr("name")?.extract::<String>()?;
    let autouse = definition.bind(py).getattr("_autouse")?.extract::<bool>()?;
    let autouse_text = if autouse { " with autouse=True" } else { "" };
    let repr = py.import("builtins")?.getattr("repr")?;
    let item_name = repr.call1((item_name,))?.extract::<String>()?;
    let fixture_name = repr.call1((fixture_name,))?.extract::<String>()?;
    let message = format!(
        "{item_name} requested an async fixture {fixture_name}{autouse_text}, with no plugin or hook that handled it. This is usually an error, as pytest does not natively support it. This will turn into an error in pytest 9.\nSee: https://docs.pytest.org/en/stable/deprecations.html#sync-test-depending-on-async-fixture"
    );
    let warning = py
        .import("_pytest.warning_types")?
        .getattr("PytestRemovedIn9Warning")?
        .call1((message,))?;
    py.import("warnings")?.getattr("warn")?.call1((warning,))?;
    Ok(())
}

pub(super) fn bound_fixture_callable(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    plan: &FixtureSetupPlan,
    callable: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    // Module functions need no instance binding. Bound fixture methods must use
    // the current test instance, not the discovery instance retained by pytest.
    let definition = plan.definition.bind(py);
    let unittest_fixture = definition.hasattr(intern!(py, "unittest"))?
        && definition
            .getattr(intern!(py, "unittest"))?
            .extract::<bool>()?;
    if !callable.bind(py).hasattr(intern!(py, "__self__"))? && !unittest_fixture {
        return Ok(callable.clone_ref(py));
    }
    let request = match kwargs.get_item("request")? {
        Some(request) => request.unbind(),
        None => fixture_request(py, runtime, test_index, plan)?,
    };
    py.import("_pytest.fixtures")?
        .getattr(intern!(py, "resolve_fixture_function"))?
        .call1((plan.definition.bind(py), request.bind(py)))
        .map(Bound::unbind)
}

pub(super) fn prepare_unittest_item(py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<()> {
    // Pytest 8.0 binds the TestCase in item.setup(), which the native runner bypasses.
    // Bind it here so request.instance, xunit fixtures, and runtest share one instance.
    item.setattr("_explicit_tearDown", py.None())?;
    if !item.getattr("instance")?.is_none() {
        return Ok(());
    }
    let name = item.getattr("name")?.extract::<String>()?;
    let testcase = item.getattr("parent")?.getattr("obj")?.call1((&name,))?;
    let test_method = testcase.getattr(name.as_str())?;
    item.setattr("_testcase", &testcase)?;
    item.setattr("_obj", test_method)?;
    Ok(())
}

pub(super) fn setup_package_owners(
    py: Python<'_>,
    runtime: &SharedRuntime,
    item: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let package_type = py.import("_pytest.python")?.getattr("Package")?;
    for node in item.call_method0("listchain")?.try_iter()? {
        let node = node?;
        if !node.is_instance(&package_type)? {
            continue;
        }
        let owner = node.getattr("nodeid")?.extract::<String>()?;
        let cached = {
            let state = runtime.lock();
            state.package_setups.get(&owner).and_then(|setup| {
                setup
                    .node
                    .bind(py)
                    .is(&node)
                    .then(|| setup.outcome.clone_ref(py))
            })
        };
        if let Some(outcome) = cached {
            outcome.clone_result(py, CachedFixtureErrorTraceback::Restore)?;
            continue;
        }
        // Package xunit hooks are node setup, unlike module and class xunit fixtures.
        register_node_finalizer(py, runtime, &node, node.getattr("teardown")?.unbind())?;
        let setup = node.call_method0("setup").map(|_| (py.None(), None));
        let (outcome, _) = FixtureOutcome::from_setup(py, setup);
        let result = outcome.clone_result(py, CachedFixtureErrorTraceback::Preserve);
        let previous = runtime.lock().package_setups.insert(
            owner,
            PackageSetup {
                node: node.unbind(),
                outcome,
            },
        );
        drop(previous);
        result?;
    }
    Ok(())
}
