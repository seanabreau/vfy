use std::collections::BTreeSet;
use std::fmt::Write;

use criterion::{BatchSize, Bencher, Criterion};
use vfy_core::analysis::{PythonSemantics, analyze_with_patterns, clear_analysis_cache};
use vfy_core::report::{
    PluginReportInput, ReportOptions, build_plugin_report_row, build_report_rows,
};

type Arc = (i32, i32);
type FileInput = (
    String,
    Vec<i32>,
    Vec<Arc>,
    Vec<(String, Vec<i32>)>,
    Vec<(String, Vec<Arc>)>,
);

fn build_rows(input: FileInput) -> Vec<vfy_core::report::ReportRow> {
    build_report_rows(
        vec![input],
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
    .expect("benchmark report rows are valid")
}

fn measure_report(bencher: &mut Bencher<'_>, input: &FileInput, cold: bool) {
    if !cold {
        let _ = build_rows(input.clone());
    }
    bencher.iter_batched(
        || {
            if cold {
                clear_analysis_cache();
            }
            input.clone()
        },
        build_rows,
        if cold {
            BatchSize::PerIteration
        } else {
            BatchSize::SmallInput
        },
    );
}

fn plugin_input(
    source: &str,
    executable: &[i32],
    possible_arcs: &[Arc],
) -> PluginReportInput<'static> {
    PluginReportInput {
        file: (
            "branch_dense_plugin.py".to_owned(),
            executable.to_vec(),
            possible_arcs.to_vec(),
            Vec::new(),
            Vec::new(),
        ),
        has_arcs: true,
        executable: executable.to_vec(),
        excluded: Vec::new(),
        possible_arcs: possible_arcs.to_vec(),
        no_branch: Vec::new(),
        contexts: &[],
        source: source.to_owned(),
    }
}

pub(super) fn benchmark_analysis_cache_reports(criterion: &mut Criterion) {
    const FUNCTIONS: usize = 2_000;
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
    let path = directory.path().join("branch_dense.py");
    #[expect(clippy::disallowed_methods, reason = "benchmark fixture input")]
    std::fs::write(&path, &source).expect("benchmark source file");
    let input = (
        path.to_string_lossy().into_owned(),
        analysis.executable.clone(),
        analysis.possible_arcs.clone(),
        Vec::new(),
        Vec::new(),
    );

    clear_analysis_cache();
    let rows = build_rows(input.clone());
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    let executable: BTreeSet<_> = analysis.executable.into_iter().collect();
    let excluded: BTreeSet<_> = analysis.report_excluded.into_iter().collect();
    let possible_arcs: BTreeSet<_> = analysis.possible_arcs.into_iter().collect();
    assert_eq!(row.executable_lines(), executable);
    assert_eq!(row.covered_lines(), executable);
    assert_eq!(row.excluded_lines(), excluded);
    assert_eq!(row.possible_arcs(), possible_arcs);
    assert_eq!(row.covered_arcs(), possible_arcs);
    assert!(row.contexts_by_line().is_empty());
    assert!(!row.source_missing());
    assert_eq!(row.statements(), 8_000);
    assert_eq!(row.branches(), 4_000);

    criterion.bench_function(
        "analysis_cache_report/2000_branch_functions/plugin_owned_control",
        |bencher| {
            bencher.iter_batched(
                || plugin_input(&source, &input.1, &input.2),
                |input| {
                    build_plugin_report_row(input).expect("benchmark plug-in report row is valid")
                },
                BatchSize::PerIteration,
            );
        },
    );

    criterion.bench_function(
        "analysis_cache_report/2000_branch_functions/cold",
        |bencher| {
            measure_report(bencher, &input, true);
        },
    );
    criterion.bench_function(
        "analysis_cache_report/2000_branch_functions/reused",
        |bencher| measure_report(bencher, &input, false),
    );
}
