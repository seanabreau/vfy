use criterion::Criterion;
use vfy_core::semantic::report::{SemanticReportOptions, build_semantic_report};
use vfy_core::semantic::{
    DecisionDefinition, Logic, PathDecisionDefinition, PathDecisionKind, PathOutcomeDefinition,
    PathOutcomeRole, SemanticData, SemanticReportRecord,
};

const MCDC_CONTEXTS: usize = 48;

fn mcdc_definition(key: &str, logic: Logic) -> DecisionDefinition {
    let conditions = logic.condition_count();
    DecisionDefinition::validated(
        key.to_owned(),
        "source.py".to_owned(),
        1,
        1,
        "if".to_owned(),
        "source-hash".to_owned(),
        key.to_owned(),
        logic,
        vec![1; conditions],
        vec![(1, 0, 1, 1); conditions],
    )
    .expect("valid benchmark decision")
}

fn insert_mcdc_observation(data: &mut SemanticData, context: usize, vector: &str, outcome: bool) {
    data.insert_observation(
        format!("test_{context:02}|run"),
        "mcdc".to_owned(),
        vector.to_owned(),
        outcome,
    )
    .expect("valid benchmark observation");
}

fn duplicate_mcdc_fixture(witness_context: Option<usize>) -> SemanticData {
    let mut data = SemanticData::default();
    data.insert_definition(mcdc_definition(
        "mcdc",
        Logic::And((0..4).map(Logic::Condition).collect()),
    ))
    .expect("unique benchmark decision");
    for context in 0..MCDC_CONTEXTS {
        for vector in ["0---", "10--", "110-", "1110"] {
            insert_mcdc_observation(&mut data, context, vector, false);
        }
        if witness_context == Some(context) {
            insert_mcdc_observation(&mut data, context, "1111", true);
        }
    }
    data
}

fn mostly_unique_mcdc_fixture(only_true: bool) -> SemanticData {
    const PAIRS: usize = 6;
    let mut data = SemanticData::default();
    let logic = Logic::And(
        (0..PAIRS)
            .map(|pair| {
                Logic::Or(vec![
                    Logic::Condition(pair * 2),
                    Logic::Condition(pair * 2 + 1),
                ])
            })
            .collect(),
    );
    data.insert_definition(mcdc_definition("mcdc", logic))
        .expect("unique benchmark decision");

    // Trace each Boolean assignment through left-to-right short circuiting.
    // The set removes assignments that lead to the same observed vector.
    let mut observations = std::collections::BTreeSet::new();
    for bits in 0..(1 << (PAIRS * 2)) {
        let mut vector = vec!['-'; PAIRS * 2];
        let mut outcome = true;
        for pair in 0..PAIRS {
            let left = bits & (1 << (pair * 2)) != 0;
            vector[pair * 2] = if left { '1' } else { '0' };
            if !left {
                let right = bits & (1 << (pair * 2 + 1)) != 0;
                vector[pair * 2 + 1] = if right { '1' } else { '0' };
                if !right {
                    outcome = false;
                    break;
                }
            }
        }
        if !only_true || outcome {
            observations.insert((vector.into_iter().collect::<String>(), outcome));
        }
    }
    assert_eq!(observations.len(), if only_true { 64 } else { 127 });
    for (vector, outcome) in observations {
        insert_mcdc_observation(&mut data, 0, &vector, outcome);
    }
    data
}

fn expected_mcdc_record(conditions: usize, witness: bool) -> SemanticReportRecord {
    let missing = if witness {
        Vec::new()
    } else {
        let mut missing = vec![
            "decision=true".to_owned(),
            format!("condition {conditions}=true"),
        ];
        missing.extend(
            (1..=conditions).map(|index| format!("condition {index} lacks independent effect")),
        );
        missing
    };
    (
        "mcdc".to_owned(),
        "source.py".to_owned(),
        1,
        "mcdc".to_owned(),
        "if".to_owned(),
        usize::from(witness) + 1,
        2,
        conditions * 2 - usize::from(!witness),
        conditions * 2,
        usize::from(witness) * conditions,
        conditions,
        missing,
    )
}

fn expected_unique_no_witness_record() -> SemanticReportRecord {
    let mut expected = expected_mcdc_record(12, true);
    expected.5 = 1;
    expected.7 = 18;
    expected.9 = 0;
    expected.11 = vec!["decision=false".to_owned()];
    expected
        .11
        .extend((1..=6).map(|pair| format!("condition {}=false", pair * 2)));
    expected
        .11
        .extend((1..=12).map(|index| format!("condition {index} lacks independent effect")));
    expected
}

pub(super) const MCDC_CASES: [&str; 5] = [
    "duplicate_contexts_no_witness",
    "duplicate_contexts_early_witness",
    "duplicate_contexts_late_witness",
    "mostly_unique_vectors",
    "mostly_unique_no_witness",
];

pub(super) fn mcdc_case(name: &str) -> Option<(SemanticData, SemanticReportRecord)> {
    Some(match name {
        "duplicate_contexts_no_witness" => {
            (duplicate_mcdc_fixture(None), expected_mcdc_record(4, false))
        }
        "duplicate_contexts_early_witness" => (
            duplicate_mcdc_fixture(Some(0)),
            expected_mcdc_record(4, true),
        ),
        "duplicate_contexts_late_witness" => (
            duplicate_mcdc_fixture(Some(MCDC_CONTEXTS - 1)),
            expected_mcdc_record(4, true),
        ),
        "mostly_unique_vectors" => (
            mostly_unique_mcdc_fixture(false),
            expected_mcdc_record(12, true),
        ),
        "mostly_unique_no_witness" => (
            mostly_unique_mcdc_fixture(true),
            expected_unique_no_witness_record(),
        ),
        _ => return None,
    })
}

fn benchmark_mcdc_proof_inputs(criterion: &mut Criterion) {
    for name in MCDC_CASES {
        let (data, expected) = mcdc_case(name).expect("listed benchmark case");
        assert_eq!(data.report_records(&[], &[]), vec![expected]);
        criterion.bench_function(&format!("semantic_mcdc_proof/{name}"), |bencher| {
            bencher.iter(|| data.report_records(&[], &[]));
        });
    }
}

pub(super) fn report_fixture() -> SemanticData {
    let mut data = SemanticData::default();
    for index in 0..1_024 {
        let key = format!("decision_{index:04}");
        data.insert_definition(
            DecisionDefinition::validated(
                key.clone(),
                "source.py".to_owned(),
                1,
                1,
                "if".to_owned(),
                "source-hash".to_owned(),
                "value".to_owned(),
                Logic::Condition(0),
                vec![1],
                vec![(1, 0, 1, 5)],
            )
            .expect("valid benchmark decision"),
        )
        .expect("unique benchmark decision");
        data.insert_path_definition(
            PathDecisionDefinition::validated(
                key.clone(),
                "source.py".to_owned(),
                1,
                4,
                PathDecisionKind::Try,
                "source-hash".to_owned(),
                "try".to_owned(),
                2,
                2,
                (1, 0, 1, 3),
                vec![
                    PathOutcomeDefinition::validated(
                        "normal".to_owned(),
                        (1, 0, 1, 3),
                        PathOutcomeRole::Normal,
                    )
                    .expect("valid normal outcome"),
                    PathOutcomeDefinition::validated(
                        "handler".to_owned(),
                        (3, 0, 3, 6),
                        PathOutcomeRole::Handler { body: (4, 4) },
                    )
                    .expect("valid handler outcome"),
                ],
            )
            .expect("valid benchmark path decision"),
        )
        .expect("unique benchmark path decision");
        for context in 0..8 {
            let context = format!("test_{context:02}|run");
            for outcome in [false, true] {
                data.insert_observation(
                    context.clone(),
                    key.clone(),
                    u8::from(outcome).to_string(),
                    outcome,
                )
                .expect("valid benchmark observation");
                data.insert_path_observation(context.clone(), key.clone(), usize::from(outcome))
                    .expect("valid benchmark path observation");
            }
        }
    }
    data
}

pub(super) fn expected_report(index: usize) -> SemanticReportRecord {
    (
        format!("decision_{index:04}"),
        "source.py".to_owned(),
        1,
        "value".to_owned(),
        "if".to_owned(),
        2,
        2,
        2,
        2,
        1,
        1,
        Vec::new(),
    )
}

pub(super) fn benchmark_semantic_reports(criterion: &mut Criterion) {
    benchmark_mcdc_proof_inputs(criterion);
    let data = report_fixture();
    let all: Vec<_> = (0..1_024).map(expected_report).collect();
    let cases = [
        ("all", Vec::new(), Vec::new(), all.clone()),
        (
            "selected_decision",
            vec!["decision_0512".to_owned()],
            Vec::new(),
            vec![expected_report(512)],
        ),
        (
            "selected_context",
            Vec::new(),
            vec!["test_04|run".to_owned()],
            all,
        ),
    ];
    for (name, selected, contexts, expected) in cases {
        assert_eq!(data.report_records(&selected, &contexts), expected);
        criterion.bench_function(&format!("semantic_reports/{name}"), |bencher| {
            bencher.iter(|| data.report_records(&selected, &contexts));
        });
    }
    let expected: Vec<_> = (0..1_024)
        .map(|index| {
            (
                format!("decision_{index:04}"),
                "source.py".to_owned(),
                1,
                "try".to_owned(),
                "try".to_owned(),
                2,
                2,
                Vec::<String>::new(),
            )
        })
        .collect();
    assert_eq!(data.path_report_records(&[], &[]), expected);
    criterion.bench_function("semantic_reports/paths", |bencher| {
        bencher.iter(|| data.path_report_records(&[], &[]));
    });
}

pub(super) fn benchmark_semantic_report_build(criterion: &mut Criterion) {
    let data = report_fixture();
    let options = SemanticReportOptions::default();
    let report = build_semantic_report(data.clone(), &options).expect("valid semantic report");
    assert_eq!(report.rows().len(), 2_048);
    assert_eq!(
        (report.decisions_covered(), report.decisions_total()),
        (4_096, 4_096)
    );
    assert_eq!(
        (report.conditions_covered(), report.conditions_total()),
        (2_048, 2_048)
    );
    assert_eq!((report.mcdc_covered(), report.mcdc_total()), (1_024, 1_024));
    let vectors: Vec<_> = (0..8)
        .flat_map(|context| {
            [false, true].map(move |outcome| {
                (
                    format!("test_{context:02}|run"),
                    u8::from(outcome).to_string(),
                    outcome,
                )
            })
        })
        .collect();
    let contexts: Vec<_> = (0..8)
        .map(|context| format!("test_{context:02}|run"))
        .collect();
    for (index, pair) in report.rows().chunks_exact(2).enumerate() {
        for row in pair {
            assert_eq!(row.key(), format!("decision_{index:04}"));
            assert_eq!(row.path(), "source.py");
            assert_eq!(row.line(), 1);
            assert_eq!(row.source_hash(), "source-hash");
            assert!(row.missing().is_empty());
            assert_eq!((row.decisions_covered(), row.decisions_total()), (2, 2));
        }
        assert_eq!(pair[0].kind(), "if");
        assert_eq!(pair[0].expression(), "value");
        assert_eq!(pair[0].vectors(), vectors);
        assert_eq!(pair[0].logic(), "c0");
        assert_eq!(pair[0].condition_lines(), [1]);
        assert_eq!(pair[0].condition_ranges(), [(1, 0, 1, 5)]);
        assert_eq!(pair[1].kind(), "try");
        assert_eq!(
            pair[1].outcomes(),
            [
                ("normal".to_owned(), true, contexts.clone()),
                ("handler".to_owned(), true, contexts.clone()),
            ]
        );
    }
    criterion.bench_function("semantic_reports/build", |bencher| {
        bencher.iter_batched(
            || data.clone(),
            |data| build_semantic_report(data, &options).expect("valid semantic report"),
            criterion::BatchSize::SmallInput,
        );
    });
}
