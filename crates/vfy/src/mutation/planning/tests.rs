use super::{FileExecution, MutationTestScope, parse_mutation_job_kind, planned_test_ids};
use vfy_core::mutation::MutationPhase;

#[test]
fn definition_phase_uses_full_suite_even_when_line_contexts_exist() {
    let plan = planned_test_ids(
        MutationPhase::Definition,
        FileExecution::Executed,
        vec![("tests/test_reload.py::test_reload".to_owned(), Some(1.0))],
    );

    assert_eq!(plan, (MutationTestScope::FullSuite, Vec::new()));
}

#[test]
fn body_phase_uses_ranked_contexts_or_reports_unreached() {
    assert_eq!(
        planned_test_ids(
            MutationPhase::Body,
            FileExecution::Executed,
            vec![("tests/test_value.py::test_value".to_owned(), Some(1.0))],
        ),
        (
            MutationTestScope::Selected,
            vec!["tests/test_value.py::test_value".to_owned()]
        )
    );
    assert_eq!(
        planned_test_ids(MutationPhase::Body, FileExecution::Executed, Vec::new()),
        (MutationTestScope::Unreached, Vec::new())
    );
}

#[test]
fn definition_phase_in_a_never_executed_file_is_unreached() {
    assert_eq!(
        planned_test_ids(
            MutationPhase::Definition,
            FileExecution::NeverExecuted,
            Vec::new()
        ),
        (MutationTestScope::Unreached, Vec::new())
    );
}

#[test]
fn mutation_job_kind_rejects_unknown_values() {
    assert!(parse_mutation_job_kind("fast").is_err());
}

#[test]
fn collection_guard_rejects_unpaired_evidence_and_a_different_catalog() {
    use super::collection_executed_mutations;
    use vfy_core::mutation::{ExecutionOwner, MutationIdentity, MutationInput};

    let data = vfy_core::data::CoverageData::empty();
    assert!(collection_executed_mutations(&[], Some(&data), None).is_err());
    assert!(collection_executed_mutations(&[], None, Some("[]")).is_err());
    let mut input = MutationInput {
        id: String::new(),
        path: "/project/app.py".to_owned(),
        source_hash: "a".repeat(64),
        kind: "value".to_owned(),
        operator: "integer_plus_one".to_owned(),
        original: "1".to_owned(),
        replacement: "2".to_owned(),
        start: 24,
        end: 25,
        line: 2,
        column: 12,
        owner: ExecutionOwner::default(),
    };
    input.id = MutationIdentity {
        path: &input.path,
        digest: &input.source_hash,
        kind: &input.kind,
        operator: &input.operator,
        start: input.start,
        end: input.end,
        replacement: &input.replacement,
    }
    .key();
    let catalog = vec![(input.id.clone(), input.path.clone(), 3, input.owner.phase)];
    let inputs = serde_json::to_string(&vec![input]).expect("canonical input");
    pyo3::Python::initialize();
    let error = collection_executed_mutations(&catalog, Some(&data), Some(&inputs))
        .expect_err("a different line is not the bound catalog");
    assert!(
        error
            .to_string()
            .contains("disagree with the planning catalog")
    );
}

#[test]
fn collection_guard_keeps_each_owner_isolation_when_sources_are_grouped() {
    use super::collection_executed_mutations;
    use vfy_core::mutation::{
        ExecutionOwner, MutationIdentity, MutationInput, OwnerKind, source_hash,
    };

    let directory = tempfile::tempdir().expect("real grouped mutation sources");
    let source = "def first():\n    return 1\n\ndef second():\n    return 2\n";
    let paths = [directory.path().join("a.py"), directory.path().join("b.py")];
    for path in &paths {
        std::fs::write(path, source).expect("source bytes match the mutation owners");
    }
    let paths = paths.map(|path| vfy_core::glob_path(&vfy_core::absolute_path(&path)));
    let byte_offset = |value| u32::try_from(value).expect("fixture offsets fit the catalog");
    let make_input = |path: &str, function: &str, original: &str, replacement: &str, line| {
        let start = source
            .find(&format!("def {function}():"))
            .expect("owner header");
        let tail = &source[start..];
        let body_start = start + tail.find("return").expect("owner body");
        let end = start + tail.find("\n\n").unwrap_or(tail.len());
        let offset = source.find(original).expect("mutation literal");
        let mut input = MutationInput {
            id: String::new(),
            path: path.to_owned(),
            source_hash: source_hash(source),
            kind: "value".to_owned(),
            operator: "integer_plus_one".to_owned(),
            original: original.to_owned(),
            replacement: replacement.to_owned(),
            start: byte_offset(offset),
            end: byte_offset(offset + original.len()),
            line,
            column: 12,
            owner: ExecutionOwner {
                kind: OwnerKind::Function,
                qualname: function.to_owned(),
                start: byte_offset(start),
                end: byte_offset(end),
                body_start: Some(byte_offset(body_start)),
                fast_eligible: false,
                phase: MutationPhase::Body,
            },
        };
        input.id = MutationIdentity {
            path: &input.path,
            digest: &input.source_hash,
            kind: &input.kind,
            operator: &input.operator,
            start: input.start,
            end: input.end,
            replacement: &input.replacement,
        }
        .key();
        input
    };
    let inputs = vec![
        make_input(&paths[0], "first", "1", "2", 2),
        make_input(&paths[1], "first", "1", "2", 2),
        make_input(&paths[0], "second", "2", "3", 5),
        make_input(&paths[1], "second", "2", "3", 5),
    ];
    let catalog = inputs
        .iter()
        .map(|input| {
            (
                input.id.clone(),
                input.path.clone(),
                input.line,
                input.owner.phase,
            )
        })
        .collect::<Vec<_>>();
    let mut data = vfy_core::data::CoverageData::empty();
    data.add_lines("", &paths[0], vec![1, 4, 5]);
    data.add_lines("", &paths[1], vec![1, 2, 4]);
    let encoded = serde_json::to_string(&inputs).expect("canonical grouped mutation inputs");
    assert_eq!(
        collection_executed_mutations(&catalog, Some(&data), Some(&encoded))
            .expect("each function retains its own collection evidence"),
        [inputs[1].id.clone(), inputs[2].id.clone()].into(),
    );
}

#[test]
fn missing_measurement_requires_full_suite_but_inventoried_empty_files_are_unreached() {
    use super::plan_mutation_tests;
    use pyo3::prelude::*;
    use std::collections::{BTreeMap, BTreeSet};
    use vfy_core::mutation::MutationInput;

    let directory = tempfile::tempdir().expect("real mutation scope sources");
    let path = directory.path().join("app.py");
    let source = "def value(default=1):\n    return default + 1\n";
    std::fs::write(&path, source).expect("source includes definition and body mutations");
    let path = vfy_core::glob_path(&vfy_core::absolute_path(&path));
    let inputs = super::super::analyze_source(&path, source)
        .into_iter()
        .filter(|record| record.4 == "integer_plus_one")
        .map(|record| MutationInput {
            id: record.0,
            path: record.1,
            source_hash: record.2,
            kind: record.3,
            operator: record.4,
            original: record.5,
            replacement: record.6,
            start: record.7,
            end: record.8,
            line: record.9,
            column: record.10,
            owner: serde_json::from_str(&record.11).expect("analyzer supplies the execution owner"),
        })
        .collect::<Vec<_>>();
    assert_eq!(
        inputs.len(),
        2,
        "definition and body literals are both planned"
    );
    let catalog = inputs
        .iter()
        .map(|input| {
            (
                input.id.clone(),
                input.path.clone(),
                input.line,
                match input.owner.phase {
                    MutationPhase::Definition => "definition",
                    MutationPhase::Body => "body",
                }
                .to_owned(),
            )
        })
        .collect::<Vec<_>>();
    let encoded = serde_json::to_string(&inputs).expect("canonical analyzer inputs");
    let relative = vfy_core::glob_path(&vfy_core::relative_path(
        &std::env::current_dir().expect("test working directory"),
        std::path::Path::new(&path),
    ));
    let unrelated = directory
        .path()
        .join("other.py")
        .to_string_lossy()
        .into_owned();
    let history = directory.path().join("history.sqlite3");
    Python::initialize();
    Python::attach(|py| {
        for (measured, expected) in [
            (None, "full_suite"),
            (Some(unrelated.as_str()), "full_suite"),
            (Some(relative.as_str()), "unreached"),
        ] {
            let mut data = vfy_core::data::CoverageData::empty();
            if let Some(measured) = measured {
                data.touch_file(measured);
            }
            let data = Py::new(py, crate::data::CoverageData::from(data))
                .expect("coverage inventory handle");
            let plans = plan_mutation_tests(
                catalog.clone(),
                BTreeMap::new(),
                BTreeSet::new(),
                history.to_str().expect("temporary history path is UTF-8"),
                Vec::new(),
                Some(data.borrow(py)),
                Some(&encoded),
            )
            .expect("valid coverage inventory and mutation catalog");
            assert_eq!(plans.len(), inputs.len());
            assert!(
                plans
                    .values()
                    .all(|(scope, tests)| scope == expected && tests.is_empty()),
                "measured={measured:?}, plans={plans:?}",
            );
        }
    });
}
