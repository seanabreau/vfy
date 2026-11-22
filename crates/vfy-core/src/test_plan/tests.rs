use super::{
    DynamicFixturePlanEntry, FixtureChainSource, FixtureChains, FixtureExecution, FixtureOverride,
    FixturePlan, FixtureUse, ScopeOwners, SuitePlan, TestBody, TestFixtureState, TestId, TestPlan,
};
use crate::{AsyncioLoopScope, FixtureId, FixtureScope};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

fn fixture(id: usize, name: &str, scope: FixtureScope) -> FixturePlan {
    FixturePlan {
        id: FixtureId::from_index(id),
        name: name.to_owned(),
        package_baseid: String::new(),
        dependencies: Vec::new(),
        declared_scope: scope,
        parameterized: false,
        async_loop_scope: None,
        execution: FixtureExecution::Callable,
    }
}

fn test_plan(chains: FixtureChains) -> TestPlan {
    TestPlan {
        id: TestId::from_index(0),
        collection_node: None,
        nodeid: "test_case".to_owned(),
        body: TestBody::Function,
        async_loop_scope: Some(AsyncioLoopScope::Function),
        argument_names: Arc::from(["resource".to_owned()]),
        eager_fixtures: Arc::from(["resource".to_owned()]),
        fixture_chains: Arc::new(chains),
        fixture_state: Some(Arc::new(TestFixtureState {
            fixture_overrides: HashMap::from([(
                "resource".to_owned(),
                FixtureOverride {
                    cache_scope: Some(FixtureScope::Module),
                },
            )]),
            dynamic_fixture_uses: HashMap::new(),
            direct_parameter_names: HashSet::new(),
        })),
        owners: ScopeOwners {
            class: None,
            module: Arc::from("test_module.py"),
        },
    }
}

#[test]
fn fixture_selection_keeps_shadow_depth_and_scope_override_in_plan() {
    let fixture_plans = vec![
        fixture(0, "resource", FixtureScope::Session),
        fixture(1, "resource", FixtureScope::Function),
    ];
    let test = test_plan(FixtureChains {
        ordered_names: vec!["resource".to_owned()],
        by_name: HashMap::from([(
            "resource".to_owned(),
            vec![FixtureId::from_index(0), FixtureId::from_index(1)],
        )]),
    });

    assert_eq!(
        test.resolve_fixture(&fixture_plans, "resource", 0),
        Some(super::FixtureResolution {
            use_: FixtureUse {
                fixture: FixtureId::from_index(1),
                cache_scope: FixtureScope::Module,
            },
            source: FixtureChainSource::Collected,
            chain_index: 1,
        })
    );
    assert_eq!(
        test.resolve_fixture(&fixture_plans, "resource", 1),
        Some(super::FixtureResolution {
            use_: FixtureUse {
                fixture: FixtureId::from_index(0),
                cache_scope: FixtureScope::Module,
            },
            source: FixtureChainSource::Collected,
            chain_index: 0,
        })
    );
    assert_eq!(test.resolve_fixture(&fixture_plans, "resource", 2), None);
}

#[test]
fn static_fixture_query_follows_overrides_and_excludes_direct_values() {
    let mut fixtures = vec![
        fixture(0, "resource", FixtureScope::Function),
        fixture(1, "resource", FixtureScope::Function),
        fixture(2, "left", FixtureScope::Function),
        fixture(3, "right", FixtureScope::Function),
        fixture(4, "direct", FixtureScope::Function),
    ];
    fixtures[0].dependencies = vec!["left".to_owned()];
    fixtures[1].dependencies = vec!["resource".to_owned(), "right".to_owned()];
    fixtures[3].dependencies = vec!["left".to_owned()];
    fixtures[4].dependencies = vec!["ignored".to_owned()];
    let mut test = test_plan(FixtureChains {
        ordered_names: vec!["resource", "left", "right", "direct"]
            .into_iter()
            .map(str::to_owned)
            .collect(),
        by_name: HashMap::from([
            (
                "resource".to_owned(),
                vec![FixtureId::from_index(0), FixtureId::from_index(1)],
            ),
            ("left".to_owned(), vec![FixtureId::from_index(2)]),
            ("right".to_owned(), vec![FixtureId::from_index(3)]),
            ("direct".to_owned(), vec![FixtureId::from_index(4)]),
        ]),
    });
    test.argument_names = Arc::from(["right".to_owned(), "direct".to_owned()]);
    Arc::make_mut(test.fixture_state.as_mut().unwrap())
        .direct_parameter_names
        .insert("direct".to_owned());
    assert_eq!(
        test.static_fixture_names(&fixtures),
        ["resource", "left", "right"]
    );
    assert_eq!(
        test.static_fixture_closure(
            &fixtures,
            super::FixtureClosureMode::IncludeDirectParameters
        ),
        ["resource", "left", "right", "direct"]
    );
}

#[test]
fn dynamic_resolution_chain_overrides_collected_chain_at_same_name_depth() {
    let fixture_plans = vec![
        fixture(0, "resource", FixtureScope::Function),
        fixture(1, "resource", FixtureScope::Session),
    ];
    let mut test = test_plan(FixtureChains {
        ordered_names: vec!["resource".to_owned()],
        by_name: HashMap::from([("resource".to_owned(), vec![FixtureId::from_index(0)])]),
    });
    Arc::make_mut(test.fixture_state.get_or_insert_with(Arc::default))
        .dynamic_fixture_uses
        .insert(
            "resource".to_owned(),
            vec![FixtureUse {
                fixture: FixtureId::from_index(1),
                cache_scope: FixtureScope::Session,
            }],
        );
    assert_eq!(
        test.resolve_fixture(&fixture_plans, "resource", 0),
        Some(super::FixtureResolution {
            use_: FixtureUse {
                fixture: FixtureId::from_index(1),
                cache_scope: FixtureScope::Session,
            },
            source: FixtureChainSource::Dynamic,
            chain_index: 0,
        })
    );
}

#[test]
fn suite_validation_rejects_misaligned_handles_and_fixture_references() {
    let mut test = test_plan(FixtureChains {
        ordered_names: vec!["missing".to_owned()],
        by_name: HashMap::new(),
    });
    test.id = TestId::from_index(1);
    let error = SuitePlan::new(vec![test], Vec::new(), None).unwrap_err();
    assert!(error.contains("test at index 0 has identity 1"));

    let error = SuitePlan::new(
        vec![test_plan(FixtureChains {
            ordered_names: vec!["missing".to_owned()],
            by_name: HashMap::new(),
        })],
        Vec::new(),
        None,
    )
    .unwrap_err();
    assert!(error.contains("ordered fixture names and fixture chains have different lengths"));

    let fixture_plans = vec![
        fixture(0, "value", FixtureScope::Function),
        fixture(1, "value", FixtureScope::Function),
        fixture(2, "dependency", FixtureScope::Function),
    ];
    let missing_index = SuitePlan::new(
        vec![test_plan(FixtureChains {
            ordered_names: vec!["value".to_owned()],
            by_name: HashMap::from([("value".to_owned(), vec![FixtureId::from_index(3)])]),
        })],
        fixture_plans.clone(),
        None,
    )
    .unwrap_err();
    assert!(missing_index.contains("fixture chain \"value\" references missing index 3"));

    let misaligned_name = SuitePlan::new(
        vec![test_plan(FixtureChains {
            ordered_names: vec!["value".to_owned()],
            by_name: HashMap::from([("value".to_owned(), vec![FixtureId::from_index(2)])]),
        })],
        fixture_plans,
        None,
    )
    .unwrap_err();
    assert!(
        misaligned_name
            .contains("fixture chain \"value\" references fixture named \"dependency\" at index 2")
    );
}

#[test]
fn failed_dynamic_chain_registration_leaves_the_suite_unchanged() {
    let mut test = test_plan(FixtureChains::default());
    test.fixture_state = None;
    let mut suite =
        SuitePlan::new(vec![test], Vec::new(), None).expect("empty collected plan is valid");
    let original = suite.clone();
    let error = suite
        .register_dynamic_fixture_chain(
            TestId::from_index(0),
            "resource",
            vec![
                DynamicFixturePlanEntry::New {
                    fixture: fixture(0, "resource", FixtureScope::Function),
                    cache_scope: FixtureScope::Function,
                },
                DynamicFixturePlanEntry::Existing {
                    fixture: FixtureId::from_index(99),
                    cache_scope: FixtureScope::Function,
                },
            ],
        )
        .expect_err("missing existing fixture invalidates the entire registration");

    assert!(error.contains("references missing fixture 99"));
    assert!(suite.fixtures().is_empty());
    assert!(!suite.tests()[0].contains_fixture("resource"));
    assert_eq!(suite, original);
}

#[test]
fn dynamic_chain_registration_adds_fixture_and_resolves_its_typed_handle() {
    let mut test = test_plan(FixtureChains::default());
    test.fixture_state = None;
    let mut suite =
        SuitePlan::new(vec![test], Vec::new(), None).expect("empty collected plan is valid");
    let ids = suite
        .register_dynamic_fixture_chain(
            TestId::from_index(0),
            "resource",
            vec![
                DynamicFixturePlanEntry::New {
                    fixture: fixture(123, "resource", FixtureScope::Session),
                    cache_scope: FixtureScope::Session,
                },
                DynamicFixturePlanEntry::Pending {
                    fixture: FixtureId::from_index(0),
                    cache_scope: FixtureScope::Session,
                },
            ],
        )
        .expect("valid dynamic chain is registered");

    assert_eq!(
        ids,
        vec![FixtureId::from_index(0), FixtureId::from_index(0)]
    );
    assert_eq!(suite.fixtures().len(), 1);
    assert_eq!(
        suite.tests()[0].resolve_fixture(suite.fixtures(), "resource", 1),
        Some(super::FixtureResolution {
            use_: FixtureUse {
                fixture: FixtureId::from_index(0),
                cache_scope: FixtureScope::Session,
            },
            source: FixtureChainSource::Dynamic,
            chain_index: 0,
        })
    );
}

#[test]
fn compact_cases_derive_function_and_no_class_owners_from_each_case_id() {
    let mut first = test_plan(FixtureChains::default());
    first.nodeid = "test_module.py::test_case[first]".to_owned();
    let mut second = first.clone();
    second.nodeid = "test_module.py::test_case[second]".to_owned();

    for test in [&first, &second] {
        assert_eq!(
            test.scope_owner(FixtureScope::Function),
            Some(test.nodeid.as_str())
        );
        assert_eq!(
            test.scope_owner(FixtureScope::Class),
            Some(test.nodeid.as_str())
        );
        assert_eq!(
            test.scope_owner(FixtureScope::Module),
            Some("test_module.py")
        );
        assert_eq!(test.scope_owner(FixtureScope::Package), None);
        assert_eq!(test.scope_owner(FixtureScope::Session), None);
    }
    assert!(Arc::ptr_eq(&first.owners.module, &second.owners.module));

    let class = Arc::<str>::from("test_module.py::TestCases");
    first.owners.class = Some(Arc::clone(&class));
    second.owners.class = Some(class);
    assert_eq!(
        first.scope_owner(FixtureScope::Class),
        second.scope_owner(FixtureScope::Class)
    );
    assert_ne!(
        first.scope_owner(FixtureScope::Function),
        second.scope_owner(FixtureScope::Function)
    );
}

#[test]
fn selection_moves_reorders_duplicates_and_remaps_coverage_atomically() {
    let mut first = test_plan(FixtureChains::default());
    first.fixture_state = None;
    first.nodeid = "first".to_owned();
    first.argument_names = Arc::from([]);
    first.eager_fixtures = Arc::from([]);
    let mut second = first.clone();
    second.id = TestId::from_index(1);
    second.nodeid = "second".to_owned();
    let mut third = first.clone();
    third.id = TestId::from_index(2);
    third.nodeid = "third".to_owned();
    let mut plan = SuitePlan::new(
        vec![first, second, third],
        Vec::new(),
        Some(HashSet::from([1])),
    )
    .expect("valid plan");
    let original = plan.clone();
    assert!(plan.select_tests(&[1, 3, 0]).is_err());
    assert_eq!(plan, original);
    plan.select_tests(&[1, 0, 1]).expect("valid selection");
    assert_eq!(
        plan.tests()
            .iter()
            .map(|test| test.nodeid.as_str())
            .collect::<Vec<_>>(),
        vec!["second", "first", "second"]
    );
    assert_eq!(
        plan.tests()
            .iter()
            .map(|test| test.id.index())
            .collect::<Vec<_>>(),
        vec![0, 1, 2]
    );
    assert_eq!(plan.no_cover_cases(), Some(&HashSet::from([0, 2])));
    plan.validate().expect("selected plan remains valid");
}

#[test]
fn shared_fixture_state_detaches_only_selected_test_after_reordering() {
    let first = test_plan(FixtureChains {
        ordered_names: vec!["resource".to_owned()],
        by_name: HashMap::from([("resource".to_owned(), vec![FixtureId::from_index(0)])]),
    });
    let mut second = first.clone();
    second.id = TestId::from_index(1);
    second.nodeid = "second".to_owned();
    let mut plan = SuitePlan::new(
        vec![first, second],
        vec![fixture(0, "resource", FixtureScope::Function)],
        None,
    )
    .expect("valid plan");
    plan.select_tests(&[1, 0, 1]).expect("valid selection");
    let state = |index: usize| plan.tests()[index].fixture_state.as_ref().unwrap();
    assert!(Arc::ptr_eq(state(0), state(1)));
    assert!(Arc::ptr_eq(state(1), state(2)));
    let before = plan.clone();
    assert!(
        plan.register_dynamic_fixture_chain(
            TestId::from_index(1),
            "resource",
            vec![DynamicFixturePlanEntry::Existing {
                fixture: FixtureId::from_index(99),
                cache_scope: FixtureScope::Function,
            }],
        )
        .is_err()
    );
    assert_eq!(plan, before);
    plan.register_dynamic_fixture_chain(
        TestId::from_index(1),
        "resource",
        vec![DynamicFixturePlanEntry::Existing {
            fixture: FixtureId::from_index(0),
            cache_scope: FixtureScope::Session,
        }],
    )
    .expect("valid dynamic fixture");
    let state = |index: usize| plan.tests()[index].fixture_state.as_ref().unwrap();
    assert!(Arc::ptr_eq(state(0), state(2)));
    assert!(!Arc::ptr_eq(state(0), state(1)));
    assert!(state(0).dynamic_fixture_uses.is_empty());
    assert!(state(2).dynamic_fixture_uses.is_empty());
    assert_eq!(
        state(1).dynamic_fixture_uses["resource"][0].cache_scope,
        FixtureScope::Session
    );
    assert_eq!(state(1).fixture_overrides, state(0).fixture_overrides);
    assert_eq!(
        state(1).direct_parameter_names,
        state(0).direct_parameter_names
    );
    plan.validate().expect("plan remains valid");
}
