use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;
use std::hint::black_box;

use criterion::{BenchmarkId, Criterion, Throughput, criterion_group, criterion_main};
use sha2::{Digest, Sha256};
use vfy_core::analysis::{PythonSemantics, analyze_with_patterns};
use vfy_core::data::CoverageData;
use vfy_core::report::{
    PluginReportInput, ReportOptions, ReportRow, build_plugin_report_row, build_report_rows,
    render::render_json, report_totals, select_report_indices,
};
use vfy_core::semantic::{DecisionDefinition, Logic, SemanticData};
use vfy_core::{TraceStore, WatchSession};

#[path = "engine/analysis_cache.rs"]
mod analysis_cache;
#[path = "engine/line_persistence.rs"]
mod line_persistence;
#[path = "engine/path_major.rs"]
mod path_major;
#[path = "engine/semantic_reports.rs"]
mod semantic_reports;
#[path = "engine/test_execution.rs"]
mod test_execution;

fn benchmark_watch_idle_metadata(c: &mut Criterion) {
    const FILES: usize = 10_000;

    let root = tempfile::tempdir().expect("benchmark watch root");
    for directory_index in 0..100 {
        let directory = root.path().join(format!("package_{directory_index:03}"));
        std::fs::create_dir(&directory).expect("benchmark package");
        for file_index in 0..100 {
            #[expect(clippy::disallowed_methods, reason = "benchmark fixture input")]
            std::fs::write(
                directory.join(format!("module_{file_index:03}.py")),
                b"VALUE = 1\n",
            )
            .expect("benchmark source");
        }
    }
    let session = WatchSession::new(&[root.path().to_path_buf()], &[], None)
        .expect("benchmark watch session");

    let mut group = c.benchmark_group("watch_idle_metadata");
    group.throughput(Throughput::Elements(FILES as u64));
    group.bench_function(BenchmarkId::new("files", FILES), |bencher| {
        bencher.iter(|| {
            black_box(
                session
                    .input_metadata_identity()
                    .expect("benchmark metadata identity"),
            )
        });
    });
    group.finish();
}

fn report_row(index: usize, lines: usize) -> ReportRow {
    let executable: Vec<i32> = (1..=lines)
        .map(|line| i32::try_from(line).expect("benchmark line count fits i32"))
        .collect();
    build_plugin_report_row(PluginReportInput {
        file: (
            format!("package/module_{index:05}.py"),
            executable.clone(),
            Vec::new(),
            vec![("test_module".to_owned(), executable.clone())],
            Vec::new(),
        ),
        has_arcs: false,
        executable,
        excluded: Vec::new(),
        possible_arcs: Vec::new(),
        no_branch: Vec::new(),
        contexts: &[],
        source: "value = 1\n".repeat(lines),
    })
    .expect("benchmark report row is valid")
}

fn benchmark_report_selection(criterion: &mut Criterion) {
    let mut group = criterion.benchmark_group("report_selection");
    for file_count in [10, 1_000] {
        let rows: Vec<_> = (0..file_count)
            .rev()
            .map(|index| report_row(index, 100))
            .collect();
        let borrowed: Vec<_> = rows.iter().collect();
        group.throughput(Throughput::Elements(
            u64::try_from(file_count).expect("benchmark file count fits u64"),
        ));
        group.bench_with_input(
            BenchmarkId::new("cover", file_count),
            &borrowed,
            |bencher, rows| {
                bencher.iter(|| {
                    select_report_indices(rows, false, false, Some("cover"))
                        .expect("benchmark sort is valid")
                });
            },
        );
    }
    group.finish();
}

fn branch_report_row(index: usize, origins: i32) -> ReportRow {
    let partial_origins = i32::try_from(index + 1).expect("benchmark file count fits i32");
    let possible_arcs: Vec<_> = (1..=origins)
        .flat_map(|origin| [(origin, origin + 1), (origin, origin + 2)])
        .collect();
    let observed_arcs: Vec<_> = possible_arcs
        .iter()
        .copied()
        .filter(|(origin, destination)| *origin > partial_origins || *destination == *origin + 1)
        .collect();
    build_plugin_report_row(PluginReportInput {
        file: (
            format!("package/branch_{index:05}.py"),
            Vec::new(),
            observed_arcs.clone(),
            Vec::new(),
            vec![("test_module".to_owned(), observed_arcs)],
        ),
        has_arcs: true,
        executable: (1..=origins + 2).collect(),
        excluded: Vec::new(),
        possible_arcs,
        no_branch: Vec::new(),
        contexts: &[],
        source: "value = 1\n".repeat(usize::try_from(origins + 2).expect("benchmark size")),
    })
    .expect("benchmark branch row is valid")
}

fn benchmark_branch_report_counts(criterion: &mut Criterion) {
    let rows: Vec<_> = (0..100)
        .rev()
        .map(|index| branch_report_row(index, 100))
        .collect();
    let borrowed: Vec<_> = rows.iter().collect();
    let totals = report_totals(&borrowed);
    assert_eq!(totals.branches(), 20_000);
    assert_eq!(totals.partial_branches(), 5_050);
    assert_eq!(
        select_report_indices(&borrowed, false, false, Some("brpart"))
            .expect("benchmark branch sorting succeeds"),
        (0..100).rev().collect::<Vec<_>>()
    );
    criterion.bench_function("report_branch_counts/100_files_100_origins", |bencher| {
        bencher.iter(|| report_totals(&borrowed));
    });
    criterion.bench_function("report_branch_sort/100_files_100_origins", |bencher| {
        bencher.iter(|| {
            select_report_indices(&borrowed, false, false, Some("brpart"))
                .expect("benchmark branch sorting succeeds")
        });
    });
}

fn benchmark_source_analysis(criterion: &mut Criterion) {
    let source = (0..2_000).fold(String::new(), |mut source, index| {
        writeln!(
            source,
            "def function_{index}(value):\n    return value or {index}"
        )
        .expect("writing benchmark source cannot fail");
        source
    });
    criterion.bench_function("source_analysis/2000_functions", |bencher| {
        bencher.iter(|| {
            analyze_with_patterns(
                &source,
                &[],
                true,
                PythonSemantics {
                    class_bare_annotations_execute: true,
                    debug_enabled: true,
                },
            )
            .expect("benchmark source analysis succeeds")
        });
    });
}

fn benchmark_json_regions(criterion: &mut Criterion) {
    const FUNCTIONS: usize = 500;
    let source = (0..FUNCTIONS).fold(String::new(), |mut source, index| {
        writeln!(
            source,
            "def function_{index}(value):\n    if value:\n        return {index}\n    return -{index}"
        )
        .expect("writing benchmark source cannot fail");
        source
    });
    let analysis = analyze_with_patterns(
        &source,
        &[],
        true,
        PythonSemantics {
            class_bare_annotations_execute: true,
            debug_enabled: true,
        },
    )
    .expect("benchmark source analysis succeeds");
    let directory = tempfile::tempdir().expect("benchmark source directory");
    let path = directory.path().join("regions.py");
    #[expect(clippy::disallowed_methods, reason = "benchmark fixture input")]
    std::fs::write(&path, &source).expect("benchmark source file");
    let path = path.to_string_lossy().into_owned();
    let lines = (1..=source.lines().count())
        .map(|line| i32::try_from(line).expect("benchmark line count fits i32"))
        .collect();
    let rows = build_report_rows(
        vec![(
            path.clone(),
            lines,
            analysis.possible_arcs,
            Vec::new(),
            Vec::new(),
        )],
        ReportOptions {
            has_arcs: true,
            include: &[],
            omit: &[],
            excludes: &[],
            contexts: &[],
            partials: &[],
            source_roots: &[],
            path_semantics: vfy_core::report::ReportPathSemantics::Stored,
            requested_paths: None,
            context_detail: vfy_core::report::ReportContextDetail::Include,
            unicode_case_version: vfy_core::UnicodeCaseVersion::default(),
            python_semantics: PythonSemantics {
                class_bare_annotations_execute: true,
                debug_enabled: true,
            },
        },
    )
    .expect("benchmark report row succeeds");
    let borrowed: Vec<_> = rows.iter().collect();
    let rendered = render_json(&borrowed, false, false, 1).expect("benchmark JSON report");
    let value: serde_json::Value =
        serde_json::from_str(&rendered).expect("benchmark JSON report is valid");
    let regions = serde_json::to_vec(&value["files"][&path])
        .expect("benchmark region JSON serialization succeeds");
    let digest = Sha256::digest(&regions)
        .iter()
        .fold(String::new(), |mut digest, byte| {
            write!(digest, "{byte:02x}").expect("writing digest cannot fail");
            digest
        });
    assert_eq!(
        digest,
        "6f864186546ec723abc83d8a6cf62b2f762c1e426ac37068c8b59e1d9f5c3988"
    );
    criterion.bench_function("json_regions/500_branch_functions", |bencher| {
        bencher.iter(|| render_json(&borrowed, false, false, 1).expect("benchmark JSON report"));
    });
}

fn benchmark_json_regions_sparse_control(criterion: &mut Criterion) {
    benchmark_json_without_regions(
        criterion,
        "json_regions/no_regions_sparse_arcs",
        &[1, 5_000, 10_000],
        &[(1, 5_000), (5_000, 10_000), (10_000, -1)],
    );
    benchmark_json_without_regions(
        criterion,
        "json_regions/no_regions_dense_arcs",
        &(1..=5_000).collect::<Vec<_>>(),
        &(1..5_000)
            .map(|line| (line, line + 1))
            .chain([(5_000, -1)])
            .collect::<Vec<_>>(),
    );
}

fn benchmark_json_without_regions(
    criterion: &mut Criterion,
    name: &str,
    lines: &[i32],
    arcs: &[(i32, i32)],
) {
    let sparse_source = "value = 1\n".repeat(10_000);
    let sparse = build_plugin_report_row(PluginReportInput {
        file: (
            "package/sparse.py".to_owned(),
            lines.to_owned(),
            arcs.to_owned(),
            Vec::new(),
            Vec::new(),
        ),
        has_arcs: true,
        executable: lines.to_owned(),
        excluded: Vec::new(),
        possible_arcs: arcs.to_owned(),
        no_branch: Vec::new(),
        contexts: &[],
        source: sparse_source,
    })
    .expect("sparse benchmark row is valid");
    let sparse_rows = [&sparse];
    let sparse_rendered =
        render_json(&sparse_rows, false, false, 1).expect("sparse benchmark JSON report");
    let sparse_json: serde_json::Value =
        serde_json::from_str(&sparse_rendered).expect("sparse benchmark JSON is valid");
    let mut expected_file = serde_json::json!({
        "executed_lines": lines,
        "summary": {
            "covered_lines": lines.len(),
            "num_statements": lines.len(),
            "percent_covered": 100.0,
            "percent_covered_display": "100.0",
            "missing_lines": 0,
            "excluded_lines": 0,
            "percent_statements_covered": 100.0,
            "percent_statements_covered_display": "100.0",
            "num_branches": arcs.len(),
            "num_partial_branches": 0,
            "covered_branches": arcs.len(),
            "missing_branches": 0,
            "percent_branches_covered": 100.0,
            "percent_branches_covered_display": "100.0"
        },
        "missing_lines": [],
        "excluded_lines": [],
        "executed_branches": arcs,
        "missing_branches": []
    });
    let mut outside = expected_file.clone();
    outside["start_line"] = serde_json::json!(1);
    expected_file["functions"] = serde_json::json!({"": outside.clone()});
    expected_file["classes"] = serde_json::json!({"": outside});
    assert_eq!(sparse_json["files"]["package/sparse.py"], expected_file);
    criterion.bench_function(name, |bencher| {
        bencher.iter(|| render_json(&sparse_rows, false, false, 1).expect("benchmark JSON report"));
    });
}

fn benchmark_trace_updates(criterion: &mut Criterion) {
    criterion.bench_function("trace_updates/10000_lines", |bencher| {
        bencher.iter_batched(
            || TraceStore::new(false),
            |mut store| {
                for line in 1..=10_000 {
                    store.add_line("test_module", "package/module.py", line);
                }
                store
            },
            criterion::BatchSize::SmallInput,
        );
    });
}

fn coverage_data(file_start: usize, file_count: usize) -> CoverageData {
    let files = (file_start..file_start + file_count)
        .map(|index| {
            (
                format!("package/module_{index:05}.py"),
                (1..=100).collect(),
                Vec::new(),
                vec![("test_module".to_owned(), (1..=100).collect())],
                Vec::new(),
            )
        })
        .collect();
    CoverageData::from_materialized_records(false, files, BTreeMap::new(), BTreeMap::new(), None)
        .expect("benchmark coverage data is valid")
}

fn assert_merged_coverage_data(data: &CoverageData) {
    let expected_lines: BTreeSet<_> = (1..=100).collect();
    let expected_files: Vec<_> = (0..750)
        .map(|index| format!("package/module_{index:05}.py"))
        .collect();
    assert_eq!(data.measured_files(), expected_files);
    let measurements: Vec<_> = data.line_measurements().collect();
    assert_eq!(measurements.len(), 750);
    for (index, (context, path, lines)) in measurements.into_iter().enumerate() {
        assert_eq!(context, "test_module");
        assert_eq!(path, format!("package/module_{index:05}.py"));
        assert_eq!(lines, expected_lines);
    }
    assert!(data.arc_measurements().next().is_none());
}

fn benchmark_data_merge(criterion: &mut Criterion) {
    let left = coverage_data(0, 500);
    let right = coverage_data(250, 500);
    let mut validated = left.clone();
    validated
        .merge_from(right.clone(), false)
        .expect("benchmark coverage data merges");
    assert_merged_coverage_data(&validated);
    let mut group = criterion.benchmark_group("data_merge");
    group.sample_size(50);
    group.bench_function("500_plus_500_files", |bencher| {
        bencher.iter_batched(
            || (left.clone(), right.clone()),
            |(mut destination, source)| {
                destination
                    .merge_from(source, false)
                    .expect("benchmark coverage data merges");
                destination
            },
            criterion::BatchSize::SmallInput,
        );
    });
    group.finish();
}

fn benchmark_near_overlap_data_merge(criterion: &mut Criterion) {
    let paths = (0..128)
        .map(|index| format!("package/module_{index:05}.py"))
        .collect::<Vec<_>>();
    let make_lines = |first: i32, last: i32| {
        CoverageData::from_trace_records(
            false,
            paths
                .iter()
                .map(|path| {
                    (
                        "test_module".to_owned(),
                        path.clone(),
                        (first..=last).collect(),
                    )
                })
                .collect(),
            Vec::new(),
            Vec::new(),
            SemanticData::default(),
        )
    };
    let make_arcs = |first: i32, last: i32| {
        CoverageData::from_trace_records(
            true,
            Vec::new(),
            paths
                .iter()
                .map(|path| {
                    (
                        "test_module".to_owned(),
                        path.clone(),
                        (first..=last).map(|line| (line, line + 1)).collect(),
                    )
                })
                .collect(),
            Vec::new(),
            SemanticData::default(),
        )
    };
    let line_base = make_lines(2, 1_023);
    let line_added = make_lines(1, 1_022);
    let arc_base = make_arcs(1, 1_024);
    let arc_added = make_arcs(2, 1_025);
    let mut merged_lines = line_base.clone();
    merged_lines
        .merge_from(line_added.clone(), false)
        .expect("near-overlap line data merges");
    let line_measurements: Vec<_> = merged_lines.line_measurements().collect();
    assert_eq!(line_measurements.len(), paths.len());
    let expected_lines = (1..=1_023).collect::<BTreeSet<_>>();
    for ((context, path, lines), expected_path) in line_measurements.into_iter().zip(&paths) {
        assert_eq!(context, "test_module");
        assert_eq!(path, expected_path);
        assert_eq!(lines, expected_lines);
    }
    let mut merged_arcs = arc_base.clone();
    merged_arcs
        .merge_from(arc_added.clone(), false)
        .expect("near-overlap arc data merges");
    let arc_measurements: Vec<_> = merged_arcs.arc_measurements().collect();
    assert_eq!(arc_measurements.len(), paths.len());
    let expected_arcs = (1..=1_025).map(|line| (line, line + 1)).collect::<Vec<_>>();
    for ((context, path, arcs), expected_path) in arc_measurements.into_iter().zip(&paths) {
        assert_eq!(context, "test_module");
        assert_eq!(path, expected_path);
        assert_eq!(arcs, expected_arcs);
    }
    let mut group = criterion.benchmark_group("data_merge_near_overlap");
    group.sample_size(50);
    group.bench_function("128_near_overlap_line_partitions", |bencher| {
        bencher.iter_batched(
            || (line_base.clone(), line_added.clone()),
            |(mut destination, source)| {
                destination
                    .merge_from(source, false)
                    .expect("near-overlap line data merges");
                destination
            },
            criterion::BatchSize::SmallInput,
        );
    });
    group.bench_function("128_near_overlap_arc_partitions", |bencher| {
        bencher.iter_batched(
            || (arc_base.clone(), arc_added.clone()),
            |(mut destination, source)| {
                destination
                    .merge_from(source, false)
                    .expect("near-overlap arc data merges");
                destination
            },
            criterion::BatchSize::SmallInput,
        );
    });
    group.finish();
}

fn semantic_coverage_data(file_start: usize, file_count: usize) -> CoverageData {
    let mut semantic = SemanticData::default();
    let arcs = (file_start..file_start + file_count)
        .map(|index| {
            let path = format!("package/module_{index:05}.py");
            semantic
                .insert_definition(
                    DecisionDefinition::validated(
                        format!("decision_{index:05}"),
                        path.clone(),
                        1,
                        1,
                        "if".to_owned(),
                        format!("source_{index:05}"),
                        "condition".to_owned(),
                        Logic::Condition(0),
                        vec![1],
                        vec![(1, 0, 1, 9)],
                    )
                    .expect("benchmark semantic definition is valid"),
                )
                .expect("benchmark semantic key is unique");
            ("test_module".to_owned(), path, vec![(1, 2)])
        })
        .collect();
    CoverageData::from_trace_records(true, Vec::new(), arcs, Vec::new(), semantic)
}

fn benchmark_semantic_data_merge(criterion: &mut Criterion) {
    let left = semantic_coverage_data(0, 500);
    let right = semantic_coverage_data(250, 500);
    let mut validated = left.clone();
    validated
        .merge_from(right.clone(), false)
        .expect("benchmark semantic data merges");
    let expected: Vec<_> = (0..750)
        .map(|index| {
            (
                format!("decision_{index:05}"),
                format!("package/module_{index:05}.py"),
                1,
                1,
                "if".to_owned(),
                format!("source_{index:05}"),
                "condition".to_owned(),
                "c0".to_owned(),
            )
        })
        .collect();
    assert_eq!(validated.semantic_data().records(), expected);
    assert_eq!(validated.measured_files().len(), 750);
    let measurements: Vec<_> = validated.arc_measurements().collect();
    assert_eq!(measurements.len(), 750);
    for (index, (context, path, arcs)) in measurements.into_iter().enumerate() {
        assert_eq!(context, "test_module");
        assert_eq!(path, format!("package/module_{index:05}.py"));
        assert_eq!(arcs, vec![(1, 2)]);
    }

    let mut group = criterion.benchmark_group("semantic_data_merge");
    group.sample_size(50);
    group.bench_function("500_plus_500_files", |bencher| {
        bencher.iter_batched(
            || (left.clone(), right.clone()),
            |(mut destination, source)| {
                destination
                    .merge_from(source, false)
                    .expect("benchmark semantic data merges");
                destination
            },
            criterion::BatchSize::SmallInput,
        );
    });
    group.finish();
}

criterion_group!(
    engine,
    benchmark_report_selection,
    benchmark_branch_report_counts,
    analysis_cache::benchmark_analysis_cache_reports,
    line_persistence::benchmark_line_persistence,
    path_major::benchmark_path_major_queries,
    test_execution::benchmark_capture_reads,
    semantic_reports::benchmark_semantic_reports,
    semantic_reports::benchmark_semantic_report_build,
    benchmark_source_analysis,
    benchmark_json_regions,
    benchmark_json_regions_sparse_control,
    benchmark_trace_updates,
    benchmark_watch_idle_metadata,
    benchmark_data_merge,
    benchmark_near_overlap_data_merge,
    benchmark_semantic_data_merge
);
criterion_main!(engine);
