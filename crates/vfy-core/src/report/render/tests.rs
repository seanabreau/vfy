use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use super::{
    CodeRegion, ReportDisplay, ReportRow, UnicodeVersion, atomic_append, atomic_write,
    branch_statistics, colored_cell, display_percent, flat_rootname, format_line_ranges, ranges,
    render_annotation, render_html, render_json, render_lcov, render_markdown, render_terminal,
    render_total, render_xml, write_annotation_report, xml_escape,
};

#[test]
fn annotation_directory_keeps_same_basename_sources_and_beside_source_paths() {
    let directory = tempfile::tempdir().expect("temporary annotation directory");
    let mut first = row("a/__init__.py", false);
    let mut second = row("b/__init__.py", false);
    first.source_override = Some("x = 1\n".to_owned());
    second.source_override = Some("y = 2\n".to_owned());
    let root = directory
        .path()
        .to_str()
        .expect("UTF-8 temporary directory");
    assert_eq!(
        write_annotation_report(&[&first, &second], Some(root)).expect("annotations"),
        2
    );
    let contents: BTreeSet<String> = std::fs::read_dir(directory.path())
        .expect("annotation files")
        .map(|entry| {
            std::fs::read_to_string(entry.expect("annotation entry").path())
                .expect("annotation source")
        })
        .collect();
    assert_eq!(
        contents,
        BTreeSet::from(["> x = 1\n".to_owned(), "> y = 2\n".to_owned()])
    );
    assert!(
        directory
            .path()
            .join("z_80084bf2fba02475___init__.py,cover")
            .is_file()
    );
    assert!(
        directory
            .path()
            .join("z_b039179a8a4ce2c2___init__.py,cover")
            .is_file()
    );

    first.path = directory
        .path()
        .join("module.name.py")
        .to_string_lossy()
        .into_owned();
    write_annotation_report(&[&first], None).expect("beside-source annotation");
    assert_eq!(
        std::fs::read_to_string(directory.path().join("module.name.py,cover"))
            .expect("beside-source content"),
        "> x = 1\n"
    );
}

#[test]
fn annotation_source_failure_preserves_previous_outputs() {
    let directory = tempfile::tempdir().expect("temporary annotation directory");
    let first_source = directory.path().join("first.py");
    let second_source = directory.path().join("missing.py");
    let first_output = directory.path().join("first.py,cover");
    let second_output = directory.path().join("missing.py,cover");
    std::fs::write(&first_source, "x = 1\n").expect("first source");
    std::fs::write(&first_output, b"old first").expect("first annotation");
    std::fs::write(&second_output, b"old second").expect("second annotation");
    let mut first = row(first_source.to_str().expect("UTF-8 path"), false);
    let mut second = row(second_source.to_str().expect("UTF-8 path"), false);
    first.source_override = None;
    second.source_override = None;
    assert_eq!(
        render_annotation(&first, None).expect("render first"),
        "> x = 1\n"
    );

    let error = write_annotation_report(&[&first, &second], None)
        .expect_err("missing later source must fail");
    assert!(
        error
            .to_string()
            .contains(second_source.to_str().expect("UTF-8 path"))
    );
    assert_eq!(
        std::fs::read(first_output).expect("first output"),
        b"old first"
    );
    assert_eq!(
        std::fs::read(second_output).expect("second output"),
        b"old second"
    );
}

#[test]
fn flat_annotation_names_follow_coverage_py_directory_hashes() {
    for (filename, expected) in [
        ("a/b/c.py", "z_86bbcbe134d28fd2_c_py"),
        ("/module.py", "z_1236e9a3b6c8bbc8_module_py"),
        ("C:\\pkg\\module.py", "z_7aa78e97d4fdc3c8_module_py"),
        ("C:module.py", "z_2f94bfebc3e9a650_module_py"),
        ("module.name.py", "module_name_py"),
    ] {
        assert_eq!(flat_rootname(filename), expected);
    }
}

#[test]
fn lcov_fields_replace_nonprintable_characters_and_keep_printable_unicode() {
    let mut row = row(
        "injected\nDA:999,1\r\t\u{85}\u{a0}\u{ad}\u{200b}\u{2028}\u{2029}\u{e000}\u{378} é\u{301}😀.py",
        false,
    );
    row.regions[0].name = "café\nFNDA:999,forged".to_owned();
    let rendered = render_lcov(&[&row], UnicodeVersion::V16);
    assert_eq!(
        rendered.lines().find(|line| line.starts_with("SF:")),
        Some("SF:injected?DA:999,1?????????? é\u{301}😀.py")
    );
    assert!(!rendered.lines().any(|line| line == "DA:999,1"));
    assert!(
        rendered
            .lines()
            .any(|line| line == "FN:1,3,café?FNDA:999,forged")
    );
    assert!(!rendered.lines().any(|line| line == "FNDA:999,forged"));
}

#[test]
fn lcov_missing_arms_distinguish_taken_origins_from_origins_without_outgoing_hits() {
    let mut row = row("branch.py", true);
    row.possible_arcs.extend([(3, 4), (3, 5)]);
    let branches: Vec<String> = render_lcov(&[&row], UnicodeVersion::V16)
        .lines()
        .filter(|line| line.starts_with("BRDA:"))
        .map(str::to_owned)
        .collect();
    assert_eq!(
        branches,
        [
            "BRDA:2,0,jump to line 3,1",
            "BRDA:2,0,jump to line 5,0",
            "BRDA:3,0,jump to line 4,-",
            "BRDA:3,0,jump to line 5,-"
        ]
    );
}

#[test]
fn append_preserves_existing_bytes_and_creates_missing_destinations() {
    let directory = tempfile::tempdir().expect("temporary report directory");
    let path = directory.path().join("nested/report.md");
    atomic_append(&path, "first\n").expect("create report");
    atomic_append(&path, "second\n").expect("append report");
    assert_eq!(
        std::fs::read(&path).expect("read report"),
        b"first\nsecond\n"
    );
    std::fs::write(&path, b"\xff\n").expect("write opaque existing content");
    atomic_append(&path, "third\n").expect("append without decoding existing bytes");
    assert_eq!(std::fs::read(&path).expect("read report"), b"\xff\nthird\n");
}

#[test]
fn append_read_failure_preserves_destination() {
    let directory = tempfile::tempdir().expect("temporary report directory");
    let path = directory.path().join("report.md");
    std::fs::create_dir(&path).expect("directory destination");
    assert!(atomic_append(&path, "new content").is_err());
    assert!(path.is_dir());
    assert_eq!(
        std::fs::read_dir(directory.path())
            .expect("list parent")
            .count(),
        1
    );
}

fn row(path: &str, has_arcs: bool) -> ReportRow {
    ReportRow {
        path: path.to_owned(),
        report_file_paths: None,
        executable: BTreeSet::from([1, 2, 3, 5]),
        covered: BTreeSet::from([1, 2, 5]),
        excluded: BTreeSet::from([4]),
        possible_arcs: if has_arcs {
            BTreeSet::from([(2, 3), (2, 5)])
        } else {
            BTreeSet::new()
        },
        arc_exit_descriptions: BTreeMap::new(),
        covered_arcs: if has_arcs {
            BTreeSet::from([(2, 3)])
        } else {
            BTreeSet::new()
        },
        executed_arcs: if has_arcs {
            BTreeSet::from([(2, 3)])
        } else {
            BTreeSet::new()
        },
        contexts_by_line: BTreeMap::from([
            (1, vec!["test_a|run".to_owned()]),
            (2, vec!["test_b|run".to_owned(), "test_a|run".to_owned()]),
        ]),
        partial_override: None,
        source_override: Some("covered\nmissing\nbranch\nexcluded\ncovered\n".to_owned()),
        regions: vec![CodeRegion {
            kind: "function",
            name: "demo".to_owned(),
            start: 1,
            lines: BTreeSet::from([1, 2, 3]),
        }],
        source_line_count: 5,
        has_arcs,
        source_failure: None,
    }
}

fn with_row<T>(has_arcs: bool, callback: impl FnOnce(&[&ReportRow]) -> T) -> T {
    let row = row("pkg/module.py", has_arcs);
    callback(&[&row])
}

#[test]
fn display_filters_preserve_full_totals_and_branch_mode_when_all_rows_are_hidden() {
    let mut full = row("full.py", true);
    full.covered = full.executable.clone();
    full.covered_arcs = full.possible_arcs.clone();
    let mut half = row("half.py", true);
    half.executable = BTreeSet::from([1, 2]);
    half.covered = BTreeSet::from([1]);
    half.possible_arcs.clear();
    half.covered_arcs.clear();
    let mut empty = row("empty.py", true);
    empty.executable.clear();
    empty.covered.clear();
    empty.possible_arcs.clear();
    empty.covered_arcs.clear();

    let report = ReportDisplay::selected(&[&full, &half, &empty], true, true, Some("-cover"))
        .expect("filtered display");
    assert_eq!(
        report.rows.iter().map(|row| row.path()).collect::<Vec<_>>(),
        ["half.py"]
    );
    assert_eq!(report.totals.statements(), 6);
    assert_eq!(report.totals.missing(), 1);
    assert_eq!(report.totals.branches(), 2);
    let terminal = render_terminal(&report, 0, true, false);
    assert_eq!(
        terminal
            .lines()
            .find(|line| line.starts_with("TOTAL"))
            .expect("total row")
            .split_whitespace()
            .collect::<Vec<_>>(),
        ["TOTAL", "6", "1", "2", "0", "88%"]
    );
    assert_eq!(
        render_markdown(&report, 0, true)
            .lines()
            .find(|line| line.contains("**TOTAL**"))
            .expect("total row")
            .split_whitespace()
            .filter(|cell| *cell != "|")
            .collect::<Vec<_>>(),
        ["**TOTAL**", "**6**", "**1**", "**2**", "**0**", "**88%**"]
    );

    half.covered = half.executable.clone();
    let report = ReportDisplay::selected(&[&full, &half, &empty], true, true, None)
        .expect("fully hidden display");
    assert!(report.rows.is_empty());
    let terminal = render_terminal(&report, 0, true, false);
    assert!(terminal.lines().next().expect("header").contains("Branch"));
    assert_eq!(
        terminal
            .lines()
            .find(|line| line.starts_with("TOTAL"))
            .expect("total row")
            .split_whitespace()
            .collect::<Vec<_>>(),
        ["TOTAL", "6", "0", "2", "0", "100%"]
    );
}

fn scrub_json_timestamp(rendered: &str) -> String {
    let mut value: Value = serde_json::from_str(rendered).expect("valid JSON report");
    value["meta"]
        .as_object_mut()
        .expect("metadata object")
        .remove("timestamp");
    format!(
        "{}\n",
        serde_json::to_string_pretty(&value).expect("serialize JSON report")
    )
}

fn scrub_xml_environment(rendered: &str) -> String {
    let current = std::env::current_dir()
        .expect("current directory")
        .to_string_lossy()
        .into_owned();
    let normalized = current.replace('\\', "/");
    rendered
        .replace(&current, ".")
        .replace(&normalized, ".")
        .split_once(" timestamp=\"")
        .map_or_else(
            || rendered.to_owned(),
            |(prefix, rest)| {
                let (_, suffix) = rest.split_once('"').expect("XML timestamp");
                format!("{prefix} timestamp=\"0\"{suffix}")
            },
        )
}

#[test]
fn line_range_and_branch_helpers_are_deterministic() {
    let lines = BTreeSet::from([-2, 1, 2, 4, 5, 6, 10]);
    assert_eq!(ranges(&lines), "-2, 1-2, 4-6, 10");
    assert_eq!(format_line_ranges(vec![10, 1, 2, 4, 5, 6]), "1-2, 4-6, 10");
    assert_eq!(ranges(&BTreeSet::new()), "");
    assert_eq!(
        branch_statistics(vec![(1, 2), (1, 3), (4, 5)], vec![(1, 2)]),
        BTreeMap::from([(1, (2, 1))])
    );
}

#[test]
fn coverage_cells_and_escaping_cover_boundary_values() {
    assert_eq!(colored_cell(" 100.0%", 100.0, false), " 100.0%");
    assert_eq!(colored_cell("50%", 50.0, true), "\u{1b}[33m50%\u{1b}[0m");
    assert_eq!(colored_cell("50%", 49.9, true), "\u{1b}[31m50%\u{1b}[0m");
    assert_eq!(xml_escape("<&\"'"), "&lt;&amp;&quot;&apos;");
}

#[test]
fn xml_rates_match_coverage_four_significant_digits() {
    for (hit, total, expected) in [
        (0, 0, "1"),
        (0, 5, "0"),
        (5, 5, "1"),
        (1, 2, "0.5"),
        (2, 3, "0.6667"),
        (1, 8, "0.125"),
        (1, 9999, "0.0001"),
        (1, 10000, "0.0001"),
        (1, 10001, "9.999e-05"),
        (99995, 1_000_000_000, "9.999e-05"),
        (15185, 20000, "0.7592"),
        (99_995, 100_000, "1"),
        (99995, 1_000_000, "0.1"),
    ] {
        assert_eq!(super::xml_rate(hit, total), expected, "{hit}/{total}");
    }
}

#[test]
fn all_public_renderer_shapes_have_reviewed_snapshots() {
    insta::assert_snapshot!(
        "terminal",
        with_row(true, |rows| render_terminal(
            &ReportDisplay::all(rows),
            1,
            true,
            false
        ))
    );
    insta::assert_snapshot!(
        "markdown",
        with_row(true, |rows| render_markdown(
            &ReportDisplay::all(rows),
            1,
            true
        ))
    );
    insta::assert_snapshot!(
        "total",
        with_row(true, |rows| render_total(&ReportDisplay::all(rows), 1))
    );
    insta::assert_snapshot!(
        "json",
        with_row(true, |rows| scrub_json_timestamp(
            &render_json(rows, true, true, 1).expect("JSON report")
        ))
    );
    insta::assert_snapshot!(
        "lcov",
        with_row(true, |rows| render_lcov(rows, UnicodeVersion::V16))
    );
    insta::assert_snapshot!(
        "annotation",
        with_row(true, |rows| render_annotation(rows[0], None)
            .expect("annotation report"))
    );
    insta::assert_snapshot!(
        "html",
        with_row(true, |rows| {
            render_html(rows, "Demo <report>")
                .expect("HTML report")
                .into_iter()
                .map(|(name, content)| format!("--- {name}\n{content}"))
                .collect::<Vec<_>>()
                .join("\n")
        })
    );
    insta::assert_snapshot!(
        "xml",
        with_row(true, |rows| scrub_xml_environment(&render_xml(
            rows,
            Some(1)
        )))
    );
}

#[test]
fn json_region_counts_only_branch_origins_within_the_region() {
    let mut row = row("pkg/module.py", true);
    row.regions = vec![CodeRegion {
        kind: "function",
        name: "single".to_owned(),
        start: 3,
        lines: BTreeSet::from([3]),
    }];
    row.possible_arcs = BTreeSet::from([(2, 3), (3, -1), (3, 3), (3, 4), (4, 5)]);
    row.covered_arcs = BTreeSet::from([(2, 3), (3, 3), (4, 5)]);
    row.executed_arcs.clone_from(&row.covered_arcs);

    let rendered = render_json(&[&row], false, false, 1).expect("JSON report");
    let report: Value = serde_json::from_str(&rendered).expect("valid JSON report");
    let region = &report["files"]["pkg/module.py"]["functions"]["single"];
    assert_eq!(region["executed_branches"], serde_json::json!([[3, 3]]));
    assert_eq!(
        region["missing_branches"],
        serde_json::json!([[3, -1], [3, 4]])
    );
    assert_eq!(region["summary"]["num_branches"], 3);
    assert_eq!(region["summary"]["covered_branches"], 1);
}

#[test]
fn json_lists_observed_arcs_while_preserving_partial_branch_credit() {
    let mut row = row("pkg/module.py", true);
    row.covered_arcs.clone_from(&row.possible_arcs);
    let rendered = render_json(&[&row], false, false, 1).expect("JSON report");
    let report: Value = serde_json::from_str(&rendered).expect("valid JSON report");
    let file = &report["files"]["pkg/module.py"];

    for record in [file, &file["functions"]["demo"]] {
        assert_eq!(record["executed_branches"], serde_json::json!([[2, 3]]));
        assert_eq!(record["missing_branches"], serde_json::json!([]));
        assert_eq!(record["summary"]["covered_branches"], 2);
    }
    assert_eq!(report["totals"]["covered_branches"], 2);
}

#[test]
fn json_counts_missing_exits_when_the_origin_line_executed() {
    for origin_executed in [true, false] {
        let mut row = row("pkg/module.py", true);
        row.covered_arcs.clear();
        row.executed_arcs.clear();
        if !origin_executed {
            row.covered.remove(&2);
        }
        let rendered = render_json(&[&row], false, false, 1).expect("JSON report");
        let report: Value = serde_json::from_str(&rendered).expect("valid JSON report");
        let file = &report["files"]["pkg/module.py"];
        let expected = usize::from(origin_executed) * 2;
        for record in [file, &file["functions"]["demo"], &file["classes"][""]] {
            assert_eq!(record["summary"]["num_partial_branches"], expected);
            assert_eq!(record["summary"]["covered_branches"], 0);
            assert_eq!(record["executed_branches"], serde_json::json!([]));
            assert_eq!(
                record["missing_branches"],
                serde_json::json!([[2, 3], [2, 5]])
            );
        }
        assert_eq!(report["totals"]["num_partial_branches"], expected);
    }
}

#[test]
fn json_redefined_regions_keep_key_order_and_all_declarations_outside() {
    let mut row = row("pkg/module.py", true);
    row.regions = vec![
        CodeRegion {
            kind: "function",
            name: "redefined".to_owned(),
            start: 2,
            lines: BTreeSet::from([2, 3]),
        },
        CodeRegion {
            kind: "function",
            name: "between".to_owned(),
            start: 4,
            lines: BTreeSet::from([4]),
        },
        CodeRegion {
            kind: "function",
            name: "redefined".to_owned(),
            start: 5,
            lines: BTreeSet::from([5]),
        },
    ];
    for pretty in [false, true] {
        for show_contexts in [false, true] {
            let rendered = render_json(&[&row], pretty, show_contexts, 1).expect("JSON report");
            let report: Value = serde_json::from_str(&rendered).expect("valid JSON report");
            let file = &report["files"]["pkg/module.py"];
            let functions = file["functions"].as_object().expect("function records");
            assert_eq!(
                functions.keys().map(String::as_str).collect::<Vec<_>>(),
                ["redefined", "between", ""]
            );
            assert_eq!(functions["redefined"]["start_line"], 5);
            assert_eq!(
                functions["redefined"]["executed_lines"],
                serde_json::json!([5])
            );
            assert_eq!(functions[""]["executed_lines"], serde_json::json!([1]));
            assert_eq!(functions[""]["missing_lines"], serde_json::json!([]));
            assert_eq!(functions[""]["excluded_lines"], serde_json::json!([]));
            for region in functions.values() {
                if show_contexts {
                    assert_eq!(region["contexts"], file["contexts"]);
                } else {
                    assert!(region.get("contexts").is_none());
                }
            }
        }
    }
}

#[test]
fn json_duplicate_file_paths_keep_first_position_and_last_row() {
    let first = row("pkg/redefined.py", false);
    let between = row("pkg/between.py", false);
    let mut last = row("pkg/redefined.py", false);
    last.covered = BTreeSet::from([3]);
    let rendered = render_json(&[&first, &between, &last], false, false, 0).expect("JSON report");
    let report: Value = serde_json::from_str(&rendered).expect("valid JSON report");
    let files = report["files"].as_object().expect("file records");
    assert_eq!(
        files.keys().map(String::as_str).collect::<Vec<_>>(),
        ["pkg/redefined.py", "pkg/between.py"]
    );
    assert_eq!(
        files["pkg/redefined.py"]["executed_lines"],
        serde_json::json!([3])
    );
    assert_eq!(report["totals"]["num_statements"], 12);
}

#[test]
fn line_only_renderer_omits_branch_columns_and_branch_records() {
    let terminal = with_row(false, |rows| {
        render_terminal(&ReportDisplay::all(rows), 0, false, false)
    });
    assert!(terminal.contains("Stmts   Miss  Cover"));
    assert!(!terminal.contains("Branch"));
    assert!(!with_row(false, |rows| render_lcov(rows, UnicodeVersion::V16)).contains("BRF:"));
}

#[test]
fn atomic_write_replaces_existing_content_and_creates_parents() {
    let directory = tempfile::tempdir().expect("temporary report directory");
    let destination = directory.path().join("nested/report.txt");
    atomic_write(&destination, "first\n").expect("first atomic write");
    atomic_write(&destination, "second\n").expect("replacement atomic write");
    assert_eq!(
        std::fs::read_to_string(destination).expect("report content"),
        "second\n"
    );
}

#[test]
fn displayed_percentages_preserve_coverage_endpoints_at_each_precision() {
    for precision in 0..=6 {
        let increment = 10.0_f64.powi(-i32::try_from(precision).unwrap());
        let near_empty: f64 = display_percent(increment / 4.0, precision).parse().unwrap();
        let near_complete: f64 = display_percent(100.0 - increment / 4.0, precision)
            .parse()
            .unwrap();
        assert!(near_empty > 0.0);
        assert!(near_complete < 100.0);
        assert_eq!(
            display_percent(0.0, precision),
            format!("{:.*}", precision, 0.0)
        );
        assert_eq!(
            display_percent(100.0, precision),
            format!("{:.*}", precision, 100.0)
        );
    }
}

#[test]
fn markdown_matches_coverage_py_table_layout() {
    let first = row("pkg/my_module.py", true);
    let second = row("zeta.py", true);
    let report = ReportDisplay::selected(&[&first, &second], false, false, None).expect("display");
    // Expected text is Coverage.py 7.15.4's `SummaryReporter.report_markdown` for the same cells.
    assert_eq!(
        render_markdown(&report, 1, true),
        "\
| Name              |    Stmts |     Miss |   Branch |   BrPart |     Cover |   Missing |
|------------------ | -------: | -------: | -------: | -------: | --------: | --------: |
| pkg/my\\_module.py |        4 |        1 |        2 |        1 |     66.7% |  2-\\>5, 3 |
| zeta.py           |        4 |        1 |        2 |        1 |     66.7% |  2-\\>5, 3 |
| **TOTAL**         |    **8** |    **2** |    **4** |    **2** | **66.7%** |           |
"
    );
    assert_eq!(
        render_markdown(&report, 1, false),
        "\
| Name              |    Stmts |     Miss |   Branch |   BrPart |     Cover |
|------------------ | -------: | -------: | -------: | -------: | --------: |
| pkg/my\\_module.py |        4 |        1 |        2 |        1 |     66.7% |
| zeta.py           |        4 |        1 |        2 |        1 |     66.7% |
| **TOTAL**         |    **8** |    **2** |    **4** |    **2** | **66.7%** |
"
    );
    let mut covered = row("covered.py", true);
    covered.covered = covered.executable.clone();
    covered.covered_arcs = covered.possible_arcs.clone();
    let mut empty = row("empty.py", true);
    empty.executable.clear();
    empty.covered.clear();
    empty.possible_arcs.clear();
    empty.covered_arcs.clear();
    let report =
        ReportDisplay::selected(&[&first, &covered, &empty], true, true, None).expect("display");
    assert!(
        render_markdown(&report, 1, false)
            .ends_with("**83.3%** |\n\n2 files skipped due to complete coverage.\n")
    );
    let report = ReportDisplay::selected(&[&first, &empty], false, true, None).expect("display");
    assert!(render_markdown(&report, 1, false).ends_with("**66.7%** |\n\n1 empty file skipped.\n"));
    let plain = row("pkg/my_module.py", false);
    let report = ReportDisplay::selected(&[&plain], false, false, None).expect("display");
    assert_eq!(
        render_markdown(&report, 0, true),
        "\
| Name              |    Stmts |     Miss |   Cover |   Missing |
|------------------ | -------: | -------: | ------: | --------: |
| pkg/my\\_module.py |        4 |        1 |     75% |         3 |
| **TOTAL**         |    **4** |    **1** | **75%** |           |
"
    );
}
