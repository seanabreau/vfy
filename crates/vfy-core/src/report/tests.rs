use std::collections::{BTreeMap, BTreeSet};

use fancy_regex::Regex;

use crate::analysis::{WithHeaderExit, WithJump, partial_lines};
use crate::data::CoverageData;

use super::{
    PluginReportInput, ReportCounts, ReportDisplay, ReportOptions, ReportPathSemantics, ReportRow,
    SourceAnalysis, build_plugin_report_row, build_report_rows, build_report_rows_from_data,
    build_row, covered_branch_arcs, fix_with_jumps, percentage, select_plugin_report_paths,
    select_report_indices, unavailable_analysis,
};

#[test]
fn custom_reporter_filters_precede_measurement_key_resolution() {
    let reporter_path = std::env::current_dir()
        .expect("report test working directory")
        .join("virtual.tpl.py")
        .to_string_lossy()
        .into_owned();
    let paths = vec![reporter_path.clone()];
    for semantics in [
        ReportPathSemantics::Stored,
        ReportPathSemantics::Canonical,
        ReportPathSemantics::Relative,
    ] {
        let data_path = if semantics == ReportPathSemantics::Relative {
            "virtual.tpl.py".to_owned()
        } else {
            reporter_path.clone()
        };
        assert_eq!(
            select_plugin_report_paths(
                &paths,
                std::slice::from_ref(&reporter_path),
                &[],
                semantics
            )
            .expect("matching custom reporter filter"),
            vec![(reporter_path.clone(), data_path)],
        );
        assert!(
            select_plugin_report_paths(&paths, &["*.tpl".to_owned()], &[], semantics)
                .expect("rejecting custom reporter filter")
                .is_empty()
        );
    }
}

#[test]
fn normalizes_cpython_with_cleanup_arcs() {
    let observed = BTreeSet::from([(131, 128), (128, 135), (131, 132)]);
    let jumps = [WithJump {
        header: 128,
        body_start: 131,
        body_end: 134,
        after: 135,
        header_exit: WithHeaderExit::Removed,
    }];

    assert_eq!(
        fix_with_jumps(observed, &jumps),
        BTreeSet::from([(131, 132), (131, 135)])
    );
}

#[test]
fn chained_with_cleanup_keeps_retained_header_exits() {
    // with a:            # 1
    //     with b:        # 2
    //         with c:    # 3
    //             with d:  # 4
    //                 x()  # 5
    // y()                # 6
    let jump = |header, header_exit| WithJump {
        header,
        body_start: header + 1,
        body_end: 5,
        after: 6,
        header_exit,
    };
    let jumps = [
        jump(1, WithHeaderExit::Removed),
        jump(2, WithHeaderExit::Removed),
        jump(3, WithHeaderExit::Retained),
        jump(4, WithHeaderExit::Retained),
    ];
    let observed = BTreeSet::from([
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 5),
        (5, 4),
        (4, 3),
        (3, 2),
        (2, 1),
        (1, 6),
    ]);

    assert_eq!(
        fix_with_jumps(observed, &jumps),
        BTreeSet::from([(1, 2), (2, 3), (3, 4), (3, 6), (4, 5), (4, 6), (5, 6)])
    );
}

#[test]
fn partial_origins_are_counted_as_covered() {
    let possible = BTreeSet::from([(2, 3), (2, 5), (8, 9)]);
    let observed = BTreeSet::from([(2, 3)]);
    let no_branch = BTreeSet::from([2]);
    assert_eq!(
        covered_branch_arcs(&possible, &observed, &no_branch),
        BTreeSet::from([(2, 3), (2, 5)])
    );
}

#[test]
fn partial_branch_credit_does_not_invent_executed_arcs() {
    let analysis = SourceAnalysis {
        executable: vec![1, 2, 3],
        excluded: Vec::new(),
        possible_arcs: vec![(1, 2), (1, 3)],
        arc_exit_descriptions: BTreeMap::new(),
        no_branch: vec![1],
        source_failure: None,
        regions: Vec::new(),
        source_line_count: 3,
        line_map: BTreeMap::new(),
        report_excluded: Vec::new(),
        with_jumps: Vec::new(),
    };
    let row = build_row(
        (
            "module.py".to_owned(),
            vec![1, 2],
            vec![(1, 2)],
            Vec::new(),
            Vec::new(),
        ),
        analysis,
        true,
        &[],
    )
    .expect("partial branch row");

    assert_eq!(row.executed_arcs, BTreeSet::from([(1, 2)]));
    assert_eq!(row.covered_arcs, BTreeSet::from([(1, 2), (1, 3)]));
    assert_eq!(ReportCounts::from(&row).covered_branches, 2);
}

#[test]
fn custom_partial_patterns_return_source_lines() {
    let patterns = [Regex::new("project: partial").unwrap()];
    let lines = partial_lines("first\n# project: partial\nlast\n", &patterns)
        .expect("partial pattern matches");
    assert_eq!(lines, vec![2]);
}

fn row_for_totals() -> ReportRow {
    ReportRow {
        path: "module.py".to_owned(),
        report_file_paths: None,
        executable: BTreeSet::from([1, 2, 3]),
        covered: BTreeSet::from([1, 2]),
        excluded: BTreeSet::from([4]),
        possible_arcs: BTreeSet::from([(2, 3), (2, 4)]),
        arc_exit_descriptions: BTreeMap::new(),
        covered_arcs: BTreeSet::from([(2, 3)]),
        executed_arcs: BTreeSet::from([(2, 3)]),
        contexts_by_line: BTreeMap::new(),
        partial_override: None,
        source_override: None,
        regions: Vec::new(),
        source_line_count: 4,
        has_arcs: true,
        source_failure: None,
    }
}

#[test]
fn report_selection_preserves_input_positions_and_deterministic_order() {
    let mut alpha = row_for_totals();
    alpha.path = "alpha.py".to_owned();
    let mut beta = row_for_totals();
    beta.path = "beta.py".to_owned();
    let rows = [&beta, &alpha];

    let selected = select_report_indices(&rows, false, false, Some("name"))
        .expect("report rows can be selected");

    assert_eq!(selected, [1, 0]);

    let descending = select_report_indices(&rows, false, false, Some("-cover"))
        .expect("report rows can be sorted descending");
    assert_eq!(descending, [0, 1]);

    let repeated = [&beta, &alpha, &beta];
    assert_eq!(
        select_report_indices(&repeated, false, false, Some("name"))
            .expect("duplicate rows retain their input positions"),
        [1, 0, 2]
    );
}

#[test]
fn default_report_order_uses_numeric_filename_components() {
    let mut second = row_for_totals();
    second.path = "pkg/item2.py".to_owned();
    let mut tenth = row_for_totals();
    tenth.path = "pkg/item10.py".to_owned();
    let rows = [&tenth, &second];

    let selected = ReportDisplay::selected(&rows, false, false, None).expect("default order");
    assert_eq!(
        selected
            .rows
            .iter()
            .map(|row| row.path())
            .collect::<Vec<_>>(),
        ["pkg/item2.py", "pkg/item10.py"]
    );
}

#[test]
fn default_report_order_compares_displayed_relative_and_external_names() {
    let current_dir = std::env::current_dir().expect("report test working directory");
    let mut inside = row_for_totals();
    inside.path = current_dir.join("a.py").to_string_lossy().into_owned();
    let mut external = row_for_totals();
    external.path = current_dir
        .parent()
        .expect("test directory has a parent")
        .join("zz.py")
        .to_string_lossy()
        .into_owned();

    let selected =
        ReportDisplay::selected(&[&inside, &external], false, false, None).expect("default order");
    assert_eq!(
        selected
            .rows
            .iter()
            .map(|row| row.path())
            .collect::<Vec<_>>(),
        [external.path(), inside.path()]
    );
    let tied_statements =
        ReportDisplay::selected(&[&inside, &external], false, false, Some("stmts"))
            .expect("statement sort");
    assert_eq!(
        tied_statements
            .rows
            .iter()
            .map(|row| row.path())
            .collect::<Vec<_>>(),
        [external.path(), inside.path()]
    );
}

#[test]
fn report_row_aggregation_handles_empty_totals_and_partial_branches() {
    let row = row_for_totals();
    assert_eq!(row.statement_count(), 3);
    assert_eq!(row.covered_count(), 2);
    assert_eq!(row.missing_set(), BTreeSet::from([3]));
    assert_eq!(row.missing_arcs_set(), BTreeSet::from([(2, 4)]));
    assert!((row.line_percent_value() - 200.0 / 3.0).abs() < 1e-12);
    assert!((row.branch_percent_value() - 50.0).abs() < 1e-12);
    assert!((row.percent_value() - 60.0).abs() < 1e-12);
    assert_eq!(row.partial_count(), 1);
    assert!((percentage(0, 0) - 100.0).abs() < 1e-12);
}

#[test]
fn partial_exits_require_executed_origins_and_ignore_unrelated_arcs() {
    let mut row = row_for_totals();
    row.possible_arcs = BTreeSet::from([(1, 2), (1, 3), (2, 3), (2, 4), (3, 4), (3, 5)]);
    row.covered_arcs = BTreeSet::from([(1, 2), (2, 3), (2, 4), (4, 5)]);
    assert_eq!(row.partial_count(), 1);

    row.covered_arcs.insert((3, 99));
    assert_eq!(row.partial_count(), 1);
    row.covered.insert(3);
    assert_eq!(row.partial_count(), 3);
    row.possible_arcs.clear();
    assert_eq!(row.partial_count(), 0);
}

#[test]
fn report_counts_addition_obeys_identity_and_associativity() {
    let first = ReportCounts::from(&row_for_totals());
    let second = ReportCounts {
        statements: 5,
        covered: 4,
        missing: 1,
        excluded: 2,
        branches: 6,
        covered_branches: 3,
        missing_branches: 3,
        partial_branches: 2,
    };
    let third = ReportCounts {
        statements: 7,
        covered: 1,
        missing: 6,
        excluded: 3,
        branches: 4,
        covered_branches: 1,
        missing_branches: 3,
        partial_branches: 1,
    };

    assert_eq!(ReportCounts::default() + first, first);
    assert_eq!(first + ReportCounts::default(), first);
    assert_eq!((first + second) + third, first + (second + third));
}

#[test]
fn report_counts_add_component_wise() {
    let counts = ReportCounts::from(&row_for_totals())
        + ReportCounts {
            statements: 5,
            covered: 4,
            missing: 1,
            excluded: 2,
            branches: 6,
            covered_branches: 3,
            missing_branches: 3,
            partial_branches: 2,
        };

    assert_eq!(
        counts,
        ReportCounts {
            statements: 8,
            covered: 6,
            missing: 2,
            excluded: 3,
            branches: 8,
            covered_branches: 4,
            missing_branches: 4,
            partial_branches: 3,
        }
    );
}

#[test]
fn missing_source_rows_do_not_invent_executable_lines() {
    let row = build_row(
        (
            "missing.py".to_owned(),
            vec![7, 8],
            Vec::new(),
            vec![("unit".to_owned(), vec![7, 8])],
            Vec::new(),
        ),
        unavailable_analysis(&std::io::Error::from(std::io::ErrorKind::NotFound)),
        false,
        &[],
    )
    .expect("missing source row");
    assert!(row.executable.is_empty());
    assert!(row.covered.is_empty());
}

#[test]
fn context_filtering_is_applied_before_line_and_arc_aggregation() {
    let analysis = SourceAnalysis {
        executable: vec![1, 2, 3],
        excluded: Vec::new(),
        possible_arcs: vec![(1, 2), (1, 3)],
        arc_exit_descriptions: BTreeMap::new(),
        no_branch: Vec::new(),
        source_failure: None,
        regions: Vec::new(),
        source_line_count: 3,
        line_map: BTreeMap::new(),
        report_excluded: Vec::new(),
        with_jumps: Vec::new(),
    };
    let context = [Regex::new("^selected$").expect("context regex")];
    let row = build_row(
        (
            "module.py".to_owned(),
            Vec::new(),
            vec![(1, 2), (1, 3)],
            vec![
                ("selected".to_owned(), vec![1, 2]),
                ("other".to_owned(), vec![3]),
            ],
            vec![
                ("selected".to_owned(), vec![(1, 2)]),
                ("other".to_owned(), vec![(1, 3)]),
            ],
        ),
        analysis,
        true,
        &context,
    )
    .expect("context-filtered row");
    assert_eq!(row.covered, BTreeSet::from([1, 2]));
    assert_eq!(row.covered_arcs, BTreeSet::from([(1, 2)]));
    assert_eq!(
        row.contexts_by_line.get(&1),
        Some(&vec!["selected".to_owned()])
    );
}

#[test]
fn plugin_report_rejects_context_runtime_match_failures() {
    let context = format!("{}c", "a".repeat(32));
    let Err(error) = build_plugin_report_row(PluginReportInput {
        file: (
            "module.py".to_owned(),
            vec![1],
            Vec::new(),
            vec![(context, vec![1])],
            Vec::new(),
        ),
        has_arcs: false,
        executable: vec![1],
        excluded: Vec::new(),
        possible_arcs: Vec::new(),
        no_branch: Vec::new(),
        contexts: &[r"^(a|aa)+\1$".to_owned()],
        source: "value = 1\n".to_owned(),
    }) else {
        panic!("context matching must fail");
    };
    assert!(error.to_string().contains("context pattern"));
    let message = error.to_string();
    assert!(message.contains("module.py"), "{message}");
    assert!(
        message.to_ascii_lowercase().contains("backtrack"),
        "{message}"
    );
}

#[test]
fn native_and_materialized_reports_return_the_same_context_error_before_analysis() {
    let directory = tempfile::tempdir().expect("source directory");
    let path = directory.path().join("module.py");
    std::fs::write(&path, format!("# {}c\nvalue = 1\n", "a".repeat(32))).expect("source fixture");
    let path = path.to_string_lossy().into_owned();
    let context = format!("{}c", "a".repeat(32));
    let file = (
        path.clone(),
        vec![2],
        Vec::new(),
        vec![(context, vec![2])],
        Vec::new(),
    );
    let data = CoverageData::from_materialized_records(
        false,
        vec![file.clone()],
        BTreeMap::new(),
        BTreeMap::new(),
        None,
    )
    .expect("valid native data");
    let excludes = [r"^# (a|aa)+\1$".to_owned()];
    let contexts = [r"^(a|aa)+\1$".to_owned()];
    let options = || ReportOptions {
        has_arcs: false,
        include: &[],
        omit: &[],
        excludes: &excludes,
        contexts: &contexts,
        partials: &[],
        source_roots: &[],
        path_semantics: super::ReportPathSemantics::Stored,
        requested_paths: None,
        context_detail: super::ReportContextDetail::Include,
        unicode_case_version: crate::path::UnicodeCaseVersion::default(),
        python_semantics: crate::analysis::PythonSemantics {
            class_bare_annotations_execute: false,
            debug_enabled: true,
        },
    };

    let Err(materialized) = build_report_rows(vec![file], options()) else {
        panic!("materialized context matching must fail");
    };
    let materialized = materialized.to_string();
    let Err(native) = build_report_rows_from_data(&data, options()) else {
        panic!("native context matching must fail");
    };
    let native = native.to_string();

    assert_eq!(native, materialized);
    assert!(native.contains(&path), "{native}");
    assert!(native.contains("context pattern"), "{native}");
    assert!(!native.contains("failed to analyze"), "{native}");
}

#[test]
fn parallel_report_analysis_returns_the_first_path_failure() {
    let directory = tempfile::tempdir().expect("source directory");
    let first = directory.path().join("first.py");
    let second = directory.path().join("second.py");
    let source = format!("# {}c\nvalue = 1\n", "a".repeat(32));
    std::fs::write(&first, &source).expect("first source");
    std::fs::write(&second, source).expect("second source");
    let files = vec![
        (
            second.to_string_lossy().into_owned(),
            vec![2],
            Vec::new(),
            Vec::new(),
            Vec::new(),
        ),
        (
            first.to_string_lossy().into_owned(),
            vec![2],
            Vec::new(),
            Vec::new(),
            Vec::new(),
        ),
    ];
    let excludes = [r"^# (a|aa)+\1$".to_owned()];
    for _ in 0..4 {
        let Err(error) = build_report_rows(
            files.clone(),
            ReportOptions {
                has_arcs: false,
                include: &[],
                omit: &[],
                excludes: &excludes,
                contexts: &[],
                partials: &[],
                source_roots: &[],
                path_semantics: super::ReportPathSemantics::Stored,
                requested_paths: None,
                context_detail: super::ReportContextDetail::Include,
                unicode_case_version: crate::path::UnicodeCaseVersion::default(),
                python_semantics: crate::analysis::PythonSemantics {
                    class_bare_annotations_execute: false,
                    debug_enabled: true,
                },
            },
        ) else {
            panic!("analysis must fail");
        };
        let message = error.to_string();
        assert!(
            message.contains(first.to_string_lossy().as_ref()),
            "{message}"
        );
        assert!(
            !message.contains(second.to_string_lossy().as_ref()),
            "{message}"
        );
    }
}

#[test]
fn alias_reporters_preserve_occurrences_and_query_the_configured_data_key() {
    let current = std::env::current_dir().expect("working directory");
    let directory = tempfile::tempdir_in(&current).expect("source directory");
    let source = directory.path().join("sample.py");
    std::fs::write(&source, "x = 1\ny = 2\nz = 3\n").expect("source file");
    let absolute = source.to_string_lossy().into_owned();
    let relative = source
        .strip_prefix(&current)
        .expect("relative source")
        .to_string_lossy()
        .into_owned();
    let paths = [absolute.clone(), relative.clone()];
    let contexts = ["chosen".to_owned()];
    for (semantics, covered) in [
        (super::ReportPathSemantics::Canonical, 1),
        (super::ReportPathSemantics::Relative, 2),
    ] {
        let files = vec![
            (
                absolute.clone(),
                vec![1],
                Vec::new(),
                vec![("chosen".to_owned(), vec![1])],
                Vec::new(),
            ),
            (
                relative.clone(),
                vec![2, 3],
                Vec::new(),
                vec![("chosen".to_owned(), vec![2, 3])],
                Vec::new(),
            ),
        ];
        let rows = build_report_rows(
            files,
            ReportOptions {
                has_arcs: false,
                include: &[],
                omit: &[],
                excludes: &[],
                contexts: &contexts,
                partials: &[],
                source_roots: &[],
                path_semantics: semantics,
                requested_paths: Some(&paths),
                context_detail: super::ReportContextDetail::Include,
                unicode_case_version: crate::path::UnicodeCaseVersion::default(),
                python_semantics: crate::analysis::PythonSemantics {
                    class_bare_annotations_execute: false,
                    debug_enabled: true,
                },
            },
        )
        .expect("alias reports");
        assert_eq!(rows.len(), 2);
        for row in &rows {
            assert_eq!(row.covered(), covered);
            assert_eq!(row.statements(), 3);
            assert_eq!(
                row.data_filename(),
                if semantics == super::ReportPathSemantics::Relative {
                    &relative
                } else {
                    &absolute
                }
            );
        }
        let json: serde_json::Value = serde_json::from_str(
            &super::render::render_json(&rows.iter().collect::<Vec<_>>(), false, true, 0)
                .expect("render report"),
        )
        .expect("JSON report");
        assert_eq!(json["files"].as_object().expect("file map").len(), 1);
        assert_eq!(json["totals"]["covered_lines"], covered * 2);
    }
}

#[test]
fn omitting_line_contexts_preserves_measurements_and_context_filters() {
    let directory = tempfile::tempdir().expect("source directory");
    let path = directory.path().join("module.py");
    std::fs::write(
        &path,
        "def choose(flag):\n    if flag:\n        return 1\n    return 2\n",
    )
    .expect("source fixture");
    let path = path.to_string_lossy().into_owned();
    let data = CoverageData::from_materialized_records(
        true,
        vec![(
            path,
            Vec::new(),
            Vec::new(),
            Vec::new(),
            vec![
                (String::new(), vec![(-1, 1), (1, -1)]),
                ("test_true".to_owned(), vec![(-1, 2), (2, 3), (3, -1)]),
                ("test_false".to_owned(), vec![(-1, 2), (2, 4), (4, -1)]),
            ],
        )],
        BTreeMap::new(),
        BTreeMap::new(),
        None,
    )
    .expect("valid native data");
    for contexts in [Vec::new(), vec!["^test_true$".to_owned()]] {
        let rows = |context_detail| {
            build_report_rows_from_data(
                &data,
                ReportOptions {
                    has_arcs: true,
                    include: &[],
                    omit: &[],
                    excludes: &[],
                    contexts: &contexts,
                    partials: &[],
                    source_roots: &[],
                    path_semantics: super::ReportPathSemantics::Stored,
                    requested_paths: None,
                    context_detail,
                    unicode_case_version: crate::path::UnicodeCaseVersion::default(),
                    python_semantics: crate::analysis::PythonSemantics {
                        class_bare_annotations_execute: false,
                        debug_enabled: true,
                    },
                },
            )
            .expect("report rows")
        };
        let detailed = rows(super::ReportContextDetail::Include);
        let aggregate = rows(super::ReportContextDetail::Omit);
        assert_eq!(detailed.len(), 1);
        assert_eq!(aggregate.len(), 1);
        let (detailed, aggregate) = (&detailed[0], &aggregate[0]);
        assert_eq!(aggregate.executable, detailed.executable);
        assert_eq!(aggregate.covered, detailed.covered);
        assert_eq!(aggregate.possible_arcs, detailed.possible_arcs);
        assert_eq!(aggregate.covered_arcs, detailed.covered_arcs);
        assert_eq!(aggregate.executed_arcs, detailed.executed_arcs);
        assert!(!detailed.contexts_by_line.is_empty(), "{contexts:?}");
        assert!(aggregate.contexts_by_line.is_empty(), "{contexts:?}");
    }
}
