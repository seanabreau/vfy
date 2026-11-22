use std::collections::BTreeSet;

use criterion::{Criterion, Throughput};
use vfy_core::data::{Arc, CoverageData, ReportInput};
use vfy_core::report::report_inputs_for_contexts;
use vfy_core::semantic::SemanticData;

const FILES: usize = 512;
const CONTEXTS: usize = 64;
const ARCS_PER_CONTEXT: i32 = 4;

fn path(index: usize) -> String {
    format!("package/module_{index:05}.py")
}

fn context(index: usize) -> String {
    format!("test_{index:04}|run")
}

fn context_arcs(index: usize) -> Vec<Arc> {
    let start = i32::try_from(index).expect("benchmark context fits i32") * 10 + 1;
    (start..start + ARCS_PER_CONTEXT)
        .map(|line| (line, line + 1))
        .collect()
}

fn coverage_data() -> CoverageData {
    let arcs = (0..CONTEXTS)
        .flat_map(|context_index| {
            (0..FILES).map(move |file_index| {
                (
                    context(context_index),
                    path(file_index),
                    context_arcs(context_index),
                )
            })
        })
        .collect();
    CoverageData::from_trace_records(true, Vec::new(), arcs, Vec::new(), SemanticData::default())
}

fn coverage_data_with_unique_contexts() -> CoverageData {
    let arcs = (0..CONTEXTS)
        .flat_map(|context_index| {
            (0..FILES).map(move |file_index| {
                (
                    format!("file_{file_index:05}_context_{context_index:04}|run"),
                    path(file_index),
                    context_arcs(context_index),
                )
            })
        })
        .collect();
    CoverageData::from_trace_records(true, Vec::new(), arcs, Vec::new(), SemanticData::default())
}

fn expected_arc_records(unique_contexts: bool) -> Vec<(String, String, Vec<Arc>)> {
    let mut records: Vec<_> = (0..CONTEXTS)
        .flat_map(|context_index| {
            (0..FILES).map(move |file_index| {
                let context = if unique_contexts {
                    format!("file_{file_index:05}_context_{context_index:04}|run")
                } else {
                    context(context_index)
                };
                (context, path(file_index), context_arcs(context_index))
            })
        })
        .collect();
    records.sort_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    records
}

fn expected_input(file_index: usize, contexts: impl Iterator<Item = usize>) -> ReportInput {
    let context_arcs: Vec<_> = contexts
        .map(|index| (context(index), context_arcs(index)))
        .collect();
    let arcs: BTreeSet<_> = context_arcs
        .iter()
        .flat_map(|(_, arcs)| arcs.iter().copied())
        .collect();
    let context_lines: Vec<_> = context_arcs
        .iter()
        .map(|(context, arcs)| {
            (
                context.clone(),
                arcs.iter()
                    .flat_map(|&(start, end)| [start, end])
                    .collect::<BTreeSet<_>>()
                    .into_iter()
                    .collect::<Vec<_>>(),
            )
        })
        .collect();
    let lines = context_lines
        .iter()
        .flat_map(|(_, lines)| lines.iter().copied())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    (
        path(file_index),
        lines,
        arcs.into_iter().collect(),
        context_lines,
        context_arcs,
    )
}

pub(super) fn benchmark_path_major_queries(criterion: &mut Criterion) {
    let data = coverage_data();
    let selected_paths = BTreeSet::from([path(0)]);
    let context_patterns = vec!["^test_000[123]\\|run$".to_owned()];
    let selected = report_inputs_for_contexts(&data, &context_patterns, &selected_paths)
        .expect("selected report input succeeds");
    assert_eq!(selected, vec![expected_input(0, 1..4)]);

    let all = data.report_inputs();
    assert_eq!(all.len(), FILES);
    for (index, input) in all.into_iter().enumerate() {
        assert_eq!(input, expected_input(index, 0..CONTEXTS));
    }

    let expected_lines = expected_input(0, 0..CONTEXTS).1;
    let expected_arcs = expected_input(0, 0..CONTEXTS).2;
    for index in 0..FILES {
        assert_eq!(data.lines_for(&path(index)), Some(expected_lines.clone()));
        assert_eq!(data.arcs_for(&path(index)), Some(expected_arcs.clone()));
    }
    assert_eq!(data.arc_records(), expected_arc_records(false));

    let mut group = criterion.benchmark_group("coverage_data_path_queries");
    group.sample_size(20);
    group.throughput(Throughput::Elements(
        u64::try_from(FILES * CONTEXTS).expect("benchmark size fits u64"),
    ));
    group.bench_function("one_of_512_files_three_of_64_contexts", |bencher| {
        bencher.iter(|| {
            report_inputs_for_contexts(&data, &context_patterns, &selected_paths)
                .expect("selected report input succeeds")
        });
    });
    group.bench_function("query_lines_and_arcs_for_all_512_files", |bencher| {
        bencher.iter(|| {
            (0..FILES)
                .map(|index| {
                    let path = path(index);
                    (data.lines_for(&path), data.arcs_for(&path))
                })
                .collect::<Vec<_>>()
        });
    });
    group.bench_function("report_inputs_all_512_files", |bencher| {
        bencher.iter(|| data.report_inputs());
    });
    group.bench_function("arc_records_all_512_files", |bencher| {
        bencher.iter(|| data.arc_records());
    });
    let unique_context_data = coverage_data_with_unique_contexts();
    assert_eq!(
        unique_context_data.arc_records(),
        expected_arc_records(true)
    );
    group.bench_function("arc_records_32768_unique_contexts", |bencher| {
        bencher.iter(|| unique_context_data.arc_records());
    });
    group.finish();
}
