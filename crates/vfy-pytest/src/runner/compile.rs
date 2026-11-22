//! Owns case preparation and suite compilation.

use super::fixture_lookup::{case_report_location, has_marker_named};
use super::model::{
    CachedFixtureInfo, CachedNames, CaseCallSpec, CompactCaseBindings, CompileOptions,
    CompiledFixtureDefinition, FixtureSchemas, ScopeOwnerKeys, SuiteBindings, TestBindings,
    TestFixtureBindings, XfailMode, asyncio_mode, optional_mapping_item, parse_fixture_scope,
    scope_owners_from_item, test_async_loop_scope,
};
use super::runtime::SharedRuntime;
use super::skipping_plugin_enabled;
use crate::UnsupportedFeatureError;
use crate::dense_reader::DenseCallSpecReader;
use crate::warnings::WarningService;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::{
    Bound, Py, PyAnyMethods, PyDictMethods, PyResult, PyTypeMethods, Python, pyfunction,
};
use pyo3::types::{PyAny, PyDict, PyList, PyListMethods};
use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use vfy_core::FixtureId;
use vfy_core::selection::TestOutcome;
use vfy_core::test_execution::CaseResult;
use vfy_core::test_plan::{FixtureOverride, TestBody, TestFixtureState, TestId, TestPlan};

pub(super) fn prepare_case_callable(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> PyResult<()> {
    let (item, originalname, reuse_template_instance) = {
        let state = runtime.lock();
        let test = &state.suite.test_bindings[test_index];
        let Some(compact) = &test.compact else {
            return Ok(());
        };
        if !compact.class_method || compact.class_instance.is_some() {
            return Ok(());
        }
        (
            test.item.clone_ref(py),
            compact.originalname.clone(),
            compact.reuse_template_instance,
        )
    };
    let parent = item.bind(py).getattr(intern!(py, "parent"))?;
    let instance = if reuse_template_instance {
        item.bind(py).getattr(intern!(py, "instance"))?
    } else {
        parent.call_method0(intern!(py, "newinstance"))?
    };
    let callable = if reuse_template_instance {
        item.bind(py).getattr(intern!(py, "obj"))?
    } else {
        instance.getattr(originalname.as_ref())?
    };
    let mut state = runtime.lock();
    let test = &mut state.suite.test_bindings[test_index];
    let compact = test
        .compact
        .as_mut()
        .ok_or_else(|| PyRuntimeError::new_err("native compact class binding disappeared"))?;
    let previous_instance = compact.class_instance.replace(instance.unbind());
    let previous_callable = std::mem::replace(&mut compact.callable, callable.unbind());
    drop(state);
    drop(previous_callable);
    drop(previous_instance);
    Ok(())
}

pub(crate) fn compact_case_markers<'py>(
    py: Python<'py>,
    template: &Bound<'py, PyAny>,
    parameter_marks: &[Py<PyAny>],
) -> PyResult<Bound<'py, PyList>> {
    let markers = PyList::empty(py);
    for marker in template
        .getattr(intern!(py, "_native_initial_markers"))?
        .try_iter()?
    {
        markers.append(marker?)?;
    }
    for marker in parameter_marks {
        markers.append(marker.bind(py))?;
    }
    for action in template
        .getattr(intern!(py, "_native_marker_actions"))?
        .try_iter()?
    {
        let action = action?;
        let marker = action.get_item(0)?;
        if action.get_item(1)?.extract::<bool>()? {
            markers.append(marker)?;
        } else {
            markers.insert(0, marker)?;
        }
    }
    Ok(markers)
}

pub(super) fn case_item(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
) -> PyResult<Py<PyAny>> {
    let (template, callspec, case_name, originalname, callable, instance) = {
        let state = runtime.lock();
        let test = &state.suite.test_bindings[test_index];
        let Some(compact) = &test.compact else {
            let item = test.item.clone_ref(py);
            drop(state);
            return Ok(bind_case_owner(py, runtime, test_index, item));
        };
        if let Some(item) = &compact.materialized_item {
            let item = item.clone_ref(py);
            drop(state);
            return Ok(bind_case_owner(py, runtime, test_index, item));
        }
        (
            test.item.clone_ref(py),
            compact.callspec.clone_ref(py),
            state.suite.tests()[test_index].nodeid[compact.case_name_start..].to_owned(),
            compact.originalname.clone(),
            compact.callable.clone_ref(py),
            compact
                .class_instance
                .as_ref()
                .map(|instance| instance.clone_ref(py)),
        )
    };
    let template = template.bind(py);
    let own_markers = compact_case_markers(py, template, &callspec.marks)?;
    let callspec = callspec.into_py(py)?;
    let kwargs = PyDict::new(py);
    kwargs.set_item(intern!(py, "name"), case_name)?;
    kwargs.set_item(intern!(py, "callspec"), callspec.bind(py))?;
    kwargs.set_item(
        intern!(py, "fixtureinfo"),
        template.getattr(intern!(py, "_fixtureinfo"))?,
    )?;
    kwargs.set_item(intern!(py, "originalname"), originalname.as_ref())?;
    kwargs.set_item(intern!(py, "callobj"), callable.bind(py))?;
    let keywords = PyDict::new(py);
    for marker in own_markers.try_iter()? {
        let marker = marker?;
        keywords.set_item(marker.getattr(intern!(py, "name"))?, marker)?;
    }
    keywords.set_item(callspec.bind(py).getattr(intern!(py, "id"))?, true)?;
    kwargs.set_item(intern!(py, "keywords"), keywords)?;
    let item = py
        .import("_pytest.python")?
        .getattr(intern!(py, "Function"))?
        .call_method(
            intern!(py, "from_parent"),
            (template.getattr(intern!(py, "parent"))?,),
            Some(&kwargs),
        )?;
    item.setattr(intern!(py, "own_markers"), own_markers)?;
    if let Some(instance) = instance {
        item.setattr(intern!(py, "_instance"), instance.bind(py))?;
    }
    let item = item.unbind();
    runtime.lock().suite.test_bindings[test_index]
        .compact
        .as_mut()
        .ok_or_else(|| PyRuntimeError::new_err("native compact binding disappeared"))?
        .materialized_item = Some(item.clone_ref(py));
    Ok(bind_case_owner(py, runtime, test_index, item))
}

fn bind_case_owner(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    item: Py<PyAny>,
) -> Py<PyAny> {
    let previous = {
        let mut state = runtime.lock();
        if state.active_function == Some(TestId::from_index(test_index)) {
            let owner = &state.suite.tests()[test_index].nodeid;
            if state
                .owner_nodes
                .get(owner)
                .is_some_and(|previous| previous.bind(py).is(item.bind(py)))
            {
                None
            } else {
                let owner = owner.clone();
                state.owner_nodes.insert(owner, item.clone_ref(py))
            }
        } else {
            None
        }
    };
    drop(previous);
    item
}

pub(super) fn case_user_properties(item: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    if !item
        .getattr("config")?
        .getattr("option")?
        .getattr("xmlpath")?
        .is_truthy()?
    {
        return Ok(Vec::new());
    }
    item.getattr("user_properties")?
        .try_iter()?
        .map(|property| {
            let property = property?;
            Ok((
                property.get_item(0)?.str()?.to_string(),
                property.get_item(1)?.str()?.to_string(),
            ))
        })
        .collect()
}

pub(super) fn case_xml_attributes(item: &Bound<'_, PyAny>) -> PyResult<Vec<(String, String)>> {
    if !item
        .getattr("config")?
        .getattr("option")?
        .getattr("xmlpath")?
        .is_truthy()?
    {
        return Ok(Vec::new());
    }
    item.py()
        .import("vfy._pytest.native_pytest")?
        .call_method1("case_xml_attributes", (item,))?
        .extract()
}

pub(super) fn prepare_builtin_teardown(
    py: Python<'_>,
    runtime: &SharedRuntime,
    test_index: usize,
    case: &CaseResult,
) -> PyResult<()> {
    {
        let state = runtime.lock();
        let test = &state.suite.tests()[test_index];
        if !test.contains_fixture("tmp_path") {
            return Ok(());
        }
    }
    let item = case_item(py, runtime, test_index)?;
    let key = py
        .import("_pytest.tmpdir")?
        .getattr(intern!(py, "tmppath_result_key"))?;
    let stash = item.bind(py).getattr(intern!(py, "stash"))?;
    let empty = PyDict::new(py);
    let outcomes = stash.call_method1(intern!(py, "setdefault"), (key, empty))?;
    for phase in &case.phases {
        outcomes.set_item(
            phase.phase.as_str(),
            phase.outcome.report_outcome() == TestOutcome::Passed,
        )?;
    }
    Ok(())
}

/// The pytest item classes whose test bodies native execution runs.
struct NativeItemTypes<'py> {
    function: Bound<'py, PyAny>,
    doctest: Bound<'py, PyAny>,
    unittest: Bound<'py, PyAny>,
}

impl<'py> NativeItemTypes<'py> {
    fn load(py: Python<'py>) -> PyResult<Self> {
        Ok(Self {
            function: py
                .import("_pytest.python")?
                .getattr(intern!(py, "Function"))?,
            doctest: py
                .import("_pytest.doctest")?
                .getattr(intern!(py, "DoctestItem"))?,
            unittest: py
                .import("_pytest.unittest")?
                .getattr(intern!(py, "TestCaseFunction"))?,
        })
    }

    /// Classify an item by the body native execution runs; `None` is unsupported.
    ///
    /// Subclasses of `Function` and `DoctestItem` can override `runtest`, which
    /// native execution does not call, so only the exact classes qualify.
    fn test_body(&self, item: &Bound<'py, PyAny>) -> PyResult<Option<TestBody>> {
        let item_type = item.get_type();
        Ok(if item.is_instance(&self.unittest)? {
            Some(TestBody::Unittest)
        } else if item_type.is(&self.function) {
            Some(TestBody::Function)
        } else if item_type.is(&self.doctest) {
            Some(TestBody::Doctest)
        } else {
            None
        })
    }
}

fn unsupported_item_description(item: &Bound<'_, PyAny>) -> PyResult<String> {
    let item_type = item.get_type();
    Ok(format!(
        "{} has type {}.{}",
        item.getattr(intern!(item.py(), "nodeid"))?
            .extract::<String>()?,
        item_type.module()?,
        item_type.name()?
    ))
}

/// Describe collected items whose type native execution does not run.
///
/// A collection-only run reports these before any test would execute.
#[pyfunction]
pub(crate) fn native_unsupported_items(items: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let item_types = NativeItemTypes::load(items.py())?;
    let mut unsupported = Vec::new();
    for item in items.try_iter()? {
        let item = item?;
        if item_types.test_body(&item)?.is_none() {
            unsupported.push(unsupported_item_description(&item)?);
        }
    }
    Ok(unsupported)
}

#[expect(
    clippy::too_many_lines,
    reason = "one pass shares fixture identity and binding state across collected items"
)]
pub(super) fn compile_suite(
    py: Python<'_>,
    items: &Bound<'_, PyAny>,
    warning_service: &mut WarningService,
    options: CompileOptions,
) -> PyResult<SuiteBindings> {
    let CompileOptions {
        track_vfy_coverage,
        retain_passing_report_locations,
    } = options;
    let callspec_reader = DenseCallSpecReader::load(py)?.map(Arc::new);
    let inspect = py.import("inspect")?;
    let pytest_python = py.import("_pytest.python")?;
    let item_types = NativeItemTypes::load(py)?;
    let direct_callable = pytest_python.getattr(intern!(py, "get_direct_param_fixture_func"))?;
    let mut mode = None;
    let mut fixtures = Vec::new();
    let mut fixture_bindings = Vec::new();
    let mut fixture_ids = HashMap::<usize, usize>::new();
    let mut fixture_info_cache = HashMap::<usize, CachedFixtureInfo>::new();
    let mut eager_names_cache = HashMap::<usize, CachedNames>::new();
    let mut fixture_schemas = FixtureSchemas::default();
    let mut scope_owner_keys = ScopeOwnerKeys::default();
    let mut xfail_mode = None;
    let initial_test_capacity = items.cast::<PyList>().map_or(0, PyListMethods::len);
    let mut tests = Vec::with_capacity(initial_test_capacity);
    let mut test_bindings = Vec::with_capacity(initial_test_capacity);
    let mut report_locations = retain_passing_report_locations.then(Vec::new);
    let mut no_cover_cases = track_vfy_coverage.then(HashSet::new);
    for item in items.try_iter()? {
        let item = item?;
        let config = item.getattr(intern!(py, "config"))?;
        let item_mode = asyncio_mode(&config)?;
        if mode.is_some_and(|mode| mode != item_mode) {
            return Err(PyRuntimeError::new_err(
                "native suite items disagree about asyncio mode",
            ));
        }
        mode = Some(item_mode);
        let Some(body) = item_types.test_body(&item)? else {
            return Err(UnsupportedFeatureError::new_err(format!(
                "native engine supports pytest Function, TestCaseFunction, and DoctestItem items; {}",
                unsupported_item_description(&item)?
            )));
        };
        let callable = match body {
            TestBody::Function => item.getattr(intern!(py, "obj"))?,
            TestBody::Unittest | TestBody::Doctest | TestBody::Item => {
                item.getattr(intern!(py, "runtest"))?
            }
        };
        let coroutine_test = body == TestBody::Function
            && inspect
                .call_method1(intern!(py, "iscoroutinefunction"), (&callable,))?
                .is_truthy()?;
        let mut nodeid = item.getattr(intern!(py, "nodeid"))?.extract::<String>()?;
        let current_xfail_mode = if let Some(mode) = xfail_mode {
            mode
        } else {
            let mode = if !skipping_plugin_enabled(&item)? {
                XfailMode::Disabled
            } else if item
                .getattr(intern!(py, "config"))?
                .getattr(intern!(py, "option"))?
                .getattr_opt(intern!(py, "runxfail"))?
                .map(|value| value.extract::<bool>())
                .transpose()?
                .unwrap_or(false)
            {
                XfailMode::Run
            } else {
                XfailMode::Evaluate
            };
            xfail_mode = Some(mode);
            mode
        };
        let fixture_info = item.getattr(intern!(py, "_fixtureinfo"))?;
        let eager_names = item.getattr(intern!(py, "fixturenames"))?;
        let eager_names_id = eager_names.as_ptr() as usize;
        if let Entry::Vacant(entry) = eager_names_cache.entry(eager_names_id) {
            entry.insert(CachedNames {
                _owner: eager_names.clone().unbind(),
                names: fixture_schemas.intern_names(eager_names.extract::<Vec<String>>()?),
            });
        }
        let eager_fixtures = eager_names_cache[&eager_names_id].names.clone();
        let fixture_info_id = fixture_info.as_ptr() as usize;
        if let Entry::Vacant(entry) = fixture_info_cache.entry(fixture_info_id) {
            let definitions = fixture_info
                .getattr(intern!(py, "name2fixturedefs"))?
                .cast_into::<PyDict>()?;
            let mut chain_rows = Vec::with_capacity(definitions.len());
            for (name, sequence) in definitions.iter() {
                let name = name.extract::<String>()?;
                let mut chain = Vec::new();
                for definition in sequence.try_iter()? {
                    let definition = definition?;
                    let identity = definition.as_ptr() as usize;
                    let fixture = if let Some(index) = fixture_ids.get(&identity) {
                        *index
                    } else {
                        let compiled = CompiledFixtureDefinition::from_py(
                            &config,
                            &inspect,
                            &direct_callable,
                            item_mode,
                            &definition,
                        )?;
                        let index = fixtures.len();
                        let (plan, binding) =
                            compiled.into_plan_and_binding(FixtureId::from_index(index));
                        fixtures.push(plan);
                        fixture_bindings.push(binding);
                        fixture_ids.insert(identity, index);
                        index
                    };
                    chain.push(FixtureId::from_index(fixture));
                }
                chain_rows.push((name, chain));
            }
            entry.insert(CachedFixtureInfo {
                _owner: fixture_info.clone().unbind(),
                argument_names: fixture_schemas.intern_names(
                    fixture_info
                        .getattr(intern!(py, "argnames"))?
                        .extract::<Vec<String>>()?,
                ),
                chains: fixture_schemas.intern_chains(chain_rows),
            });
        }
        let cached_info = &fixture_info_cache[&fixture_info_id];
        let argument_names = cached_info.argument_names.clone();
        let fixture_chains = cached_info.chains.clone();
        let family_callspecs = item.getattr(intern!(py, "_native_callspecs")).ok();
        let family_case = family_callspecs.is_some();
        let class_method = body == TestBody::Function
            && family_case
            && item
                .getattr(intern!(py, "parent"))?
                .is_instance(&pytest_python.getattr(intern!(py, "Class"))?)?;
        let originalname = {
            let value = match body {
                TestBody::Function | TestBody::Unittest => {
                    item.getattr(intern!(py, "originalname"))?
                }
                TestBody::Doctest | TestBody::Item => item.getattr(intern!(py, "name"))?,
            };
            let text = value.extract::<std::borrow::Cow<'_, str>>()?;
            if family_case {
                text.into_owned()
            } else {
                // Validate Unicode even when ordinary cases do not retain the name.
                String::new()
            }
        };
        let compact_originalname = family_case.then(|| Arc::<str>::from(originalname.as_str()));
        let original_owners = scope_owners_from_item(&item, &mut scope_owner_keys)?;
        let template_no_cover = if track_vfy_coverage {
            item.call_method1(intern!(py, "get_closest_marker"), ("no_cover",))?
                .is_truthy()?
                || eager_fixtures.iter().any(|name| name == "no_cover")
        } else {
            false
        };
        let callspecs: Vec<Option<Bound<'_, PyAny>>> = if let Some(family) = &family_callspecs {
            family
                .try_iter()?
                .map(|value| value.map(Some))
                .collect::<PyResult<_>>()?
        } else {
            vec![item.getattr(intern!(py, "callspec")).ok()]
        };
        let case_count = callspecs.len();
        if family_case && tests.is_empty() {
            tests.reserve_exact(case_count);
            test_bindings.reserve_exact(case_count);
        }
        for (case_index, callspec) in callspecs.into_iter().enumerate() {
            let async_loop_scope = if coroutine_test {
                test_async_loop_scope(
                    &item,
                    callspec.as_ref().filter(|_| family_case),
                    &config,
                    item_mode,
                )?
            } else {
                None
            };
            let case_name = if family_case {
                let callspec = callspec
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("native family has no callspec"))?;
                if callspec.getattr(intern!(py, "_idlist"))?.len()? == 0 {
                    originalname.clone()
                } else {
                    format!(
                        "{}[{}]",
                        originalname,
                        callspec.getattr(intern!(py, "id"))?.extract::<String>()?
                    )
                }
            } else {
                item.getattr(intern!(py, "name"))?
                    .extract::<std::borrow::Cow<'_, str>>()?;
                String::new()
            };
            let case_nodeid = if family_case {
                let prefix = nodeid.strip_suffix(&originalname).ok_or_else(|| {
                    PyRuntimeError::new_err(format!(
                        "native family node ID does not end in function name: {nodeid}"
                    ))
                })?;
                format!("{prefix}{case_name}")
            } else {
                std::mem::take(&mut nodeid)
            };
            if let Some(report_locations) = &mut report_locations {
                let family = family_case.then_some((originalname.as_str(), case_name.as_str()));
                report_locations.push((case_nodeid.clone(), case_report_location(&item, family)?));
            }
            let mut owners = original_owners.clone();
            if family_case && !class_method {
                owners.class = None;
            }
            let unused_params = argument_names
                .iter()
                .all(|name| fixture_chains.by_name.contains_key(name));
            let elide_params = match (&callspec_reader, &callspec) {
                (Some(reader), Some(callspec)) if unused_params => {
                    reader.unused_params_admitted(callspec)?
                }
                _ => false,
            };
            let callspec_params = callspec
                .as_ref()
                .filter(|_| !elide_params)
                .map(|value| value.getattr(intern!(py, "params")))
                .transpose()?;
            let callspec_scopes = callspec
                .as_ref()
                .map(|value| value.getattr(intern!(py, "_arg2scope")))
                .transpose()?;
            let mut fixture_state: Option<Arc<TestFixtureState>> = None;
            let mut fixture_binding_state: Option<Box<TestFixtureBindings>> = None;
            for name in &fixture_chains.ordered_names {
                let scope_override = callspec_scopes
                    .as_ref()
                    .map(|scopes| optional_mapping_item(scopes, name))
                    .transpose()?
                    .flatten()
                    .map(|scope| scope.getattr(intern!(py, "value"))?.extract::<String>())
                    .transpose()?
                    .map(|scope| parse_fixture_scope(&scope))
                    .transpose()?;
                if let Some(scope_override) = scope_override
                    && !fixture_chains.by_name.get(name).is_some_and(|chain| {
                        !chain.is_empty()
                            && chain.iter().all(|fixture| {
                                fixtures
                                    .get(fixture.index())
                                    .is_some_and(|plan| plan.declared_scope == scope_override)
                            })
                    })
                {
                    Arc::make_mut(fixture_state.get_or_insert_with(Arc::default))
                        .fixture_overrides
                        .insert(
                            name.clone(),
                            FixtureOverride {
                                cache_scope: Some(scope_override),
                            },
                        );
                }
            }
            let mut direct_parameters: Option<HashMap<String, Py<PyAny>>> = None;
            if let Some(params) = callspec_params {
                for name in argument_names.iter() {
                    if !fixture_chains.by_name.contains_key(name)
                        && let Some(value) = optional_mapping_item(&params, name)?
                    {
                        direct_parameters
                            .get_or_insert_with(HashMap::new)
                            .insert(name.clone(), value.unbind());
                    }
                }
            }
            if let Some(parameters) = direct_parameters {
                Arc::make_mut(fixture_state.get_or_insert_with(Arc::default))
                    .direct_parameter_names = parameters.keys().cloned().collect();
                if family_case {
                    fixture_binding_state
                        .get_or_insert_with(Box::default)
                        .direct_parameters = Some(Arc::new(parameters));
                }
            }
            let case_no_cover_mark = if track_vfy_coverage {
                callspec
                    .as_ref()
                    .map(|callspec| {
                        has_marker_named(&callspec.getattr(intern!(py, "marks"))?, "no_cover")
                    })
                    .transpose()?
                    .unwrap_or(false)
            } else {
                false
            };
            let warning_profile = warning_service.profile_for_case(
                py,
                &item,
                if family_case { callspec.as_ref() } else { None },
            )?;
            let test_index = tests.len();
            if (template_no_cover || case_no_cover_mark)
                && let Some(no_cover_cases) = &mut no_cover_cases
            {
                no_cover_cases.insert(test_index);
            }
            tests.push(TestPlan {
                id: TestId::from_index(test_index),
                collection_node: None,
                nodeid: case_nodeid,
                body,
                async_loop_scope,
                argument_names: argument_names.clone(),
                eager_fixtures: eager_fixtures.clone(),
                fixture_chains: fixture_chains.clone(),
                fixture_state,
                owners,
            });
            test_bindings.push(TestBindings {
                item: item.clone().unbind(),
                compact: if let Some(originalname) = &compact_originalname {
                    Some(Box::new(CompactCaseBindings {
                        materialized_item: None,
                        callable: callable.clone().unbind(),
                        callspec: CaseCallSpec::from_py(callspec.as_ref().ok_or_else(|| {
                            PyRuntimeError::new_err("native family has no callspec")
                        })?)?,
                        case_name_start: tests[test_index].nodeid.len() - case_name.len(),
                        originalname: originalname.clone(),
                        class_method,
                        reuse_template_instance: case_index == 0,
                        class_instance: None,
                    }))
                } else {
                    None
                },
                warning_profile,
                fixture_state: fixture_binding_state,
                xfail: None,
                xfail_mode: current_xfail_mode,
            });
        }
        if family_case {
            item.setattr(intern!(py, "_native_case_count"), case_count)?;
            item.delattr("_native_callspecs")?;
        }
    }
    let mut suite = SuiteBindings::new(
        tests,
        test_bindings,
        fixtures,
        fixture_bindings,
        fixture_ids,
        no_cover_cases,
        report_locations,
    )?;
    suite.callspec_reader = callspec_reader;
    Ok(suite)
}
