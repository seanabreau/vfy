use super::{
    AnalysisCache, AnalysisKey, CacheLimits, CachedAnalysis, ProcessAnalysisCache, PythonSemantics,
    SourceFailure, analysis_key, analyze, analyze_with_patterns, clear_analysis_cache,
    compile_patterns, failed_analysis, multiline_map, partial_lines,
};
use fancy_regex::{Regex, RegexBuilder};
use serde::Deserialize;

fn normal_python_semantics(class_bare_annotations_execute: bool) -> PythonSemantics {
    PythonSemantics {
        class_bare_annotations_execute,
        debug_enabled: true,
    }
}

#[test]
fn exclusion_union_searches_long_ordinary_source_before_matching_candidates() {
    let excludes = [
        r"# pragma: !=3\.12 cover\b",
        r"# pragma: <3\.12 cover\b",
        r"# pragma: <=?([0-2])\.\d+ cover\b",
        r"# pragma: <=?3\.([0-0]\d|1[0-1]|\d{1,1}) cover\b",
        r"# pragma: ==(?!3\.12)\d+\.\d+ cover\b",
        r"# pragma: >3\.12 cover\b",
        r"# pragma: >=?([4-9]|\d{2,})\.\d+ cover\b",
        r"# pragma: >=?3\.([2-9]\d|1[3-9]|\d{3,}) cover\b",
        r"# pragma: cpython no cover\b",
        r"# pragma: cygwin cover\b",
        r"# pragma: darwin cover\b",
        r"# pragma: linux no cover\b",
        r"# pragma: msys cover\b",
        r"# pragma: no cover\b",
        r"# pragma: nt cover\b",
        r"# pragma: posix no cover\b",
        r"# pragma: pypy cover\b",
        r"# pragma: win32 cover\b",
        r#"-> [\'"]?NoReturn[\'"]?:"#,
        r": \.\.\.(\s*#.*)?$",
        r"^ +\.\.\.$",
        r"^\s*assert_never\b",
        r"^\s*if (False|TYPE_CHECKING):",
        r"^\s*raise AssertionError\b",
        r"^\s*raise NotImplementedError\b",
        r"^\s*raise$",
        r"^\s*return NotImplemented\b",
        r#"^if __name__ == [\'"]__main__[\'"]:$"#,
    ]
    .map(str::to_owned);
    let patterns = compile_patterns(&excludes).expect("ordinary exclusion patterns compile");
    let source = format!(
        "excluded = 1 # pragma: no cover\n{}",
        "retained_value = 0\n".repeat(3000)
    );
    assert_eq!(
        partial_lines(&source, &patterns).expect("long source search remains bounded"),
        vec![1]
    );
}

#[test]
fn logical_lines_cover_decorators_headers_and_comprehensions_without_absorbing_comments() {
    let source = "@decorator(\n    True,\n)\ndef choose(\n    values,\n):\n    # comment\n    if (\n        values\n    ):\n        return [\n            value\n            for value in values\n        ]\n    return []\n";
    let expected = [(1..=3, 1), (4..=6, 4), (8..=10, 8), (11..=14, 11)]
        .into_iter()
        .flat_map(|(lines, start)| lines.map(move |line| (line, start)))
        .collect();
    assert_eq!(multiline_map(source), expected);
}

#[derive(Clone, Debug, Deserialize)]
struct ReviewedRegion {
    kind: String,
    name: String,
    start: i32,
    lines: Vec<i32>,
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ReviewedOverrides {
    executable: Option<Vec<i32>>,
    excluded: Option<Vec<i32>>,
    possible_arcs: Option<Vec<[i32; 2]>>,
    no_branch: Option<Vec<i32>>,
    source_missing: Option<bool>,
    collect_arcs: Option<bool>,
    class_annotations_before_314: Option<bool>,
    regions: Option<Vec<ReviewedRegion>>,
}

#[derive(Clone, Debug, Deserialize)]
struct ReviewedAnalysis {
    executable: Vec<i32>,
    excluded: Vec<i32>,
    possible_arcs: Vec<[i32; 2]>,
    no_branch: Vec<i32>,
    #[serde(default)]
    source_missing: bool,
    #[serde(default = "default_collect_arcs")]
    collect_arcs: bool,
    #[serde(default)]
    class_annotations_before_314: bool,
    #[serde(default)]
    excludes: Vec<String>,
    #[serde(default)]
    regions: Vec<ReviewedRegion>,
    #[serde(default)]
    before_314: Option<ReviewedOverrides>,
}

const fn default_collect_arcs() -> bool {
    true
}

fn markdown_block(markdown: &str, language: &str) -> String {
    let markdown = markdown.replace("\r\n", "\n");
    let marker = format!("```{language}\n");
    let (_, body) = markdown
        .split_once(&marker)
        .unwrap_or_else(|| panic!("missing {language} block"));
    body.split_once("\n```").map_or_else(
        || panic!("unterminated {language} block"),
        |(block, _)| block.to_owned(),
    )
}

fn assert_reviewed_fixture(name: &str, markdown: &str, before_314: bool) {
    let source = markdown_block(markdown, "python");
    let mut expected: ReviewedAnalysis = serde_json::from_str(&markdown_block(markdown, "json"))
        .unwrap_or_else(|error| panic!("{name}: invalid fixture oracle: {error}"));
    if before_314 && let Some(overrides) = expected.before_314.take() {
        if let Some(value) = overrides.executable {
            expected.executable = value;
        }
        if let Some(value) = overrides.excluded {
            expected.excluded = value;
        }
        if let Some(value) = overrides.possible_arcs {
            expected.possible_arcs = value;
        }
        if let Some(value) = overrides.no_branch {
            expected.no_branch = value;
        }
        if let Some(value) = overrides.source_missing {
            expected.source_missing = value;
        }
        if let Some(value) = overrides.collect_arcs {
            expected.collect_arcs = value;
        }
        if let Some(value) = overrides.class_annotations_before_314 {
            expected.class_annotations_before_314 = value;
        }
        if let Some(value) = overrides.regions {
            expected.regions = value;
        }
    }
    let patterns: Vec<String> = std::iter::once(r"(?i)#\s*pragma[:\s]?\s*no\s*cover".to_owned())
        .chain(expected.excludes.iter().cloned())
        .collect();
    let mut actual = analyze(
        &source,
        &patterns,
        expected.collect_arcs,
        normal_python_semantics(expected.class_annotations_before_314),
    )
    .unwrap_or_else(|error| panic!("{name}: analysis failed: {error}"));
    let partial_patterns = compile_patterns(&["pragma: no branch".to_owned()]).unwrap();
    actual
        .no_branch
        .extend(partial_lines(&source, &partial_patterns).expect("partial patterns"));
    actual.no_branch.sort_unstable();
    actual.no_branch.dedup();
    let expected_arcs = expected
        .possible_arcs
        .into_iter()
        .map(|[start, end]| (start, end))
        .collect::<Vec<_>>();
    assert_eq!(actual.executable, expected.executable, "{name}: executable");
    assert_eq!(actual.excluded, expected.excluded, "{name}: excluded");
    assert_eq!(actual.possible_arcs, expected_arcs, "{name}: possible arcs");
    assert_eq!(actual.no_branch, expected.no_branch, "{name}: no branch");
    assert_eq!(
        actual.source_missing(),
        expected.source_missing,
        "{name}: source missing"
    );
    let actual_regions: Vec<_> = actual
        .regions
        .iter()
        .map(|region| {
            (
                region.kind,
                region.name.as_str(),
                region.start,
                region.lines.iter().copied().collect::<Vec<_>>(),
            )
        })
        .collect();
    let expected_regions: Vec<_> = expected
        .regions
        .iter()
        .map(|region| {
            (
                region.kind.as_str(),
                region.name.as_str(),
                region.start,
                region.lines.clone(),
            )
        })
        .collect();
    assert_eq!(actual_regions, expected_regions, "{name}: code regions");
}

#[test]
fn literate_analysis_fixtures_match_reviewed_truth_tables() {
    let fixture_directory =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/analysis");
    let mut fixtures: Vec<_> = std::fs::read_dir(&fixture_directory)
        .expect("analysis fixture directory")
        .map(|entry| {
            let entry = entry.expect("analysis fixture entry");
            let path = entry.path();
            let name = path
                .file_name()
                .and_then(std::ffi::OsStr::to_str)
                .expect("UTF-8 analysis fixture name")
                .to_owned();
            (name, path)
        })
        .filter(|(name, path)| {
            std::path::Path::new(name)
                .extension()
                .is_some_and(|extension| extension.eq_ignore_ascii_case("md"))
                && path.is_file()
        })
        .collect();
    fixtures.sort_by(|left, right| left.0.cmp(&right.0));
    for (name, path) in fixtures {
        let fixture = std::fs::read_to_string(path)
            .unwrap_or_else(|error| panic!("{name}: analysis fixture contents: {error}"));
        for before_314 in [true, false] {
            assert_reviewed_fixture(&name, &fixture, before_314);
        }
    }
}

#[test]
fn malformed_source_is_reported_without_partial_analysis() {
    let result = analyze("def broken(:\n", &[], true, normal_python_semantics(false))
        .expect("analysis returns a result");
    assert!(result.source_missing());
    assert!(matches!(
        result.source_failure,
        Some(SourceFailure::NotPython { .. })
    ));
    assert!(result.executable.is_empty());
    assert!(result.excluded.is_empty());
    assert!(result.possible_arcs.is_empty());
    assert!(result.regions.is_empty());
}

#[test]
fn exclusion_and_partial_runtime_match_failures_are_errors() {
    let source = format!("# {}c\nvalue = 1\n", "a".repeat(32));
    let expensive = r"^# (a|aa)+\1$".to_owned();
    for excludes in [vec![expensive.clone()], vec![expensive, "^#".to_owned()]] {
        let patterns = compile_patterns(&excludes).expect("patterns compile");
        let Err(exclusion) =
            analyze_with_patterns(&source, &patterns, false, normal_python_semantics(false))
        else {
            panic!("exclusion matching must fail");
        };
        assert!(exclusion.to_string().contains("exclusion pattern"));
        assert!(exclusion.to_string().contains("matching source"));
        let exclusion_message = exclusion.to_string();
        assert!(
            exclusion_message.to_ascii_lowercase().contains("backtrack"),
            "{exclusion_message}"
        );

        let partial = partial_lines(&source, &patterns).expect_err("partial matching must fail");
        assert!(partial.to_string().contains("partial branch pattern"));
        assert!(partial.to_string().contains("matching source"));
        let partial_message = partial.to_string();
        assert!(
            partial_message.to_ascii_lowercase().contains("backtrack"),
            "{partial_message}"
        );
    }
}

#[test]
fn disabling_arc_collection_keeps_statement_analysis() {
    let result = analyze(
        "if value:\n    selected = 1\nelse:\n    selected = 2\n",
        &[],
        false,
        normal_python_semantics(false),
    )
    .expect("analysis returns a result");
    assert_eq!(result.executable, vec![1, 2, 4]);
    assert!(result.possible_arcs.is_empty());
}

#[test]
fn analyzes_exclusions_and_branches_without_python_ast() {
    let result = analyze(
        "if enabled:\n    covered = 1\nelse:\n    missed = 2  # pragma: no cover\n",
        &["pragma: no cover".to_owned()],
        true,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 2]);
    assert!(result.excluded.contains(&4));
    assert!(result.possible_arcs.is_empty());
}

#[test]
fn excluded_if_keeps_unexcluded_alternate_branches() {
    let source = "def choose(platform, flag):\n    if platform == \"jupyter\":  # pragma: no cover\n        show()\n    elif platform == \"terminal\":\n        if flag:\n            color()\n        else:\n            plain()\n    else:\n        if flag:\n            write()\n        else:\n            discard()\n";
    let result = analyze(
        source,
        &["pragma: no cover".to_owned()],
        true,
        normal_python_semantics(false),
    )
    .unwrap();

    assert_eq!(
        result.possible_arcs,
        vec![(4, 5), (4, 10), (5, 6), (5, 8), (10, 11), (10, 13)]
    );
}

#[test]
fn excluded_alternate_scopes_still_filter_their_branch_arcs() {
    let source = "if outer:  # pragma: no cover\n    ignored()\nelif alternate:  # pragma: no cover\n    if nested:\n        ignored_too()\nelse:  # pragma: no cover\n    if fallback:\n        ignored_as_well()\n";
    let result = analyze(
        source,
        &["pragma: no cover".to_owned()],
        true,
        normal_python_semantics(false),
    )
    .unwrap();

    assert!(result.possible_arcs.is_empty());
}

#[test]
fn excluded_with_headers_do_not_register_cleanup_arc_rewrites() {
    let source =
        "if platform:  # pragma: no cover\n    with manager:\n        work()\nelse:\n    done()\n";
    let result = analyze(
        source,
        &["pragma: no cover".to_owned()],
        true,
        normal_python_semantics(false),
    )
    .unwrap();

    assert!(result.with_jumps.is_empty());
}

#[test]
fn ellipsis_gap_expansion_requires_a_direct_pattern_match() {
    let source = "class Contract:\n    def render(\n        self,\n    ) -> str:  # pragma: no cover\n        ...\n\n# adjacent comment\nvalue = 1\n";
    let pragma = analyze(
        source,
        &["pragma: no cover".to_owned()],
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(pragma.excluded, vec![2, 3, 4, 5]);
    assert_eq!(pragma.report_excluded, vec![2, 3, 4, 5]);

    let ellipsis = analyze(
        source,
        &[r"^\s*\.\.\.\s*$".to_owned()],
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(ellipsis.excluded, vec![5, 6]);
}

#[test]
fn multiline_exclusion_patterns_cover_every_matched_statement() {
    let source = "first = 1  # project: begin\nmiddle = 2\nlast = 3  # project: end\n";
    let pattern = r"(?s)# project: begin.*?# project: end".to_owned();
    let result = analyze(
        source,
        std::slice::from_ref(&pattern),
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(result.excluded, vec![1, 2, 3]);
    assert_eq!(
        partial_lines(source, &compile_patterns(&[pattern]).unwrap()).unwrap(),
        vec![1, 2, 3]
    );
}

#[test]
fn literal_exclusion_unions_preserve_full_source_vm_match_ranges() {
    let source = "first = 1  # begin\nmiddle = 2\nlast = 3  # end\nlabel = 'café'\n";
    for configured in [
        vec!["begin", "begin\nmiddle = 2\nlast = 3  # end"],
        vec!["begin\nmiddle = 2\nlast = 3  # end", "begin"],
        vec!["café", "begin", "begin"],
        vec!["absent", "missing"],
        vec!["", "begin"],
        vec![r"(?s:begin.*end)", "begin"],
    ] {
        let configured: Vec<_> = configured.into_iter().map(str::to_owned).collect();
        let alternation = configured
            .iter()
            .map(|pattern| format!("(?:{pattern})"))
            .collect::<Vec<_>>()
            .join("|");
        let original = RegexBuilder::new(&format!("(?=)(?:{alternation})"))
            .multi_line(true)
            .build()
            .expect("original configured union compiles");
        let compiled = compile_patterns(&configured).expect("configured union compiles");
        let ranges = |pattern: &Regex| {
            pattern
                .find_iter(source)
                .map(|found| {
                    let found = found.expect("fixture matching succeeds");
                    (found.start(), found.end())
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(ranges(&compiled[0]), ranges(&original), "{configured:?}");
    }
}

#[test]
fn exclusion_patterns_preserve_order_for_overlapping_matches() {
    let source = "first = 1  # begin\nmiddle = 2\nlast = 3  # end\n";
    let short = "begin".to_owned();
    let long = r"(?s:begin.*end)".to_owned();

    let short_first = analyze(
        source,
        &[short.clone(), long.clone()],
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    let long_first = analyze(
        source,
        &[long, short],
        false,
        normal_python_semantics(false),
    )
    .unwrap();

    assert_eq!(short_first.excluded, vec![1]);
    assert_eq!(long_first.excluded, vec![1, 2, 3]);
}

#[test]
fn rejects_invalid_custom_exclusion_regex() {
    assert!(
        analyze(
            "value = 1\n",
            &["(".to_owned()],
            false,
            normal_python_semantics(false)
        )
        .is_err()
    );
}

#[test]
fn excludes_bare_annotations_inside_function_and_class_scopes() {
    let result = analyze(
        "class Model:\n    field: int\n\ndef function():\n    local: int\n",
        &[],
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 4]);
}

#[test]
fn counts_class_bare_annotations_with_future_annotations() {
    let result = analyze(
        "from __future__ import annotations\n\nclass Model:\n    field: int\n\ndef function():\n    local: int\n",
        &[],
        false, normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 3, 4, 6]);
}

#[test]
fn counts_class_bare_annotations_before_python_314() {
    let result = analyze(
        "class Model:\n    field: int\n\ndef function():\n    local: int\n",
        &[],
        false,
        normal_python_semantics(true),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 2, 4]);
}

#[test]
fn annotation_evaluator_body_has_no_statement_or_branch_obligations() {
    let result = analyze(
        "def __annotate__(value):\n    if value:\n        return 1\n    return 0\nresult = __annotate__(True)\n",
        &[],
        true, normal_python_semantics(true),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 5]);
    assert!(result.possible_arcs.is_empty());
}

#[test]
fn annotation_evaluator_keeps_independently_compiled_child_obligations() {
    let result = analyze(
        "def __annotate__(value):\n    def helper(value):\n        if value:\n            return 1\n        return 0\n    return helper(value)\nresult = __annotate__(True)\n",
        &[],
        true, normal_python_semantics(true),
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 2, 3, 4, 5, 7]);
    assert_eq!(result.possible_arcs, vec![(3, 4), (3, 5)]);
}

#[test]
fn single_line_child_code_keeps_parent_and_child_exits() {
    for source in [
        "def helper(): return 1\nresult = 2\n",
        "async def helper(): return 1\nresult = 2\n",
        "class Model: value = 1\nresult = 2\n",
    ] {
        let result = analyze(source, &[], true, normal_python_semantics(true)).unwrap();
        assert_eq!(result.possible_arcs, vec![(1, -1), (1, 2)]);
    }
}

#[test]
fn analysis_cache_keys_every_semantic_input() {
    let first_patterns = vec!["pragma: no cover".to_owned()];
    let second_patterns = vec!["project: omit".to_owned()];
    let source = "value = 1\n";
    let baseline = analysis_key(
        source,
        &first_patterns,
        &[],
        false,
        normal_python_semantics(false),
    );

    assert_ne!(
        baseline,
        analysis_key(
            "value = 2\n",
            &first_patterns,
            &[],
            false,
            normal_python_semantics(false)
        )
    );
    assert_ne!(
        baseline,
        analysis_key(
            source,
            &second_patterns,
            &[],
            false,
            normal_python_semantics(false)
        )
    );
    assert_ne!(
        baseline,
        analysis_key(
            source,
            &first_patterns,
            &second_patterns,
            false,
            normal_python_semantics(false)
        )
    );
    assert_ne!(
        baseline,
        analysis_key(
            source,
            &first_patterns,
            &[],
            true,
            normal_python_semantics(false)
        )
    );
    assert_ne!(
        baseline,
        analysis_key(
            source,
            &first_patterns,
            &[],
            false,
            normal_python_semantics(true)
        )
    );
}

#[test]
fn cached_analysis_returns_an_isolated_result() {
    let source = "def uniquely_named_cache_fixture():\n    return 1\n";
    let mut first = analyze(source, &[], false, normal_python_semantics(false)).unwrap();
    first.executable.clear();

    let second = analyze(source, &[], false, normal_python_semantics(false)).unwrap();
    assert_eq!(second.executable, vec![1, 2]);
}

#[test]
fn inherited_process_never_acquires_the_parent_cache_lock() {
    let cache = ProcessAnalysisCache::new();
    let owner_pid = std::process::id();
    let parent = cache
        .for_process(owner_pid)
        .expect("owner process uses its cache");
    let _locked_by_vanished_thread = parent.lock().expect("cache lock");

    assert!(cache.for_process(owner_pid.wrapping_add(1)).is_none());
}

#[test]
fn externally_compiled_regex_options_cannot_alias_cached_analysis() {
    let source = "value = 1 # OMIT\n";
    let sensitive = Regex::new("omit").unwrap();
    let insensitive = RegexBuilder::new("omit")
        .case_insensitive(true)
        .build()
        .unwrap();
    clear_analysis_cache();

    let sensitive_result =
        analyze_with_patterns(source, &[sensitive], false, normal_python_semantics(false)).unwrap();
    let insensitive_result = analyze_with_patterns(
        source,
        &[insensitive],
        false,
        normal_python_semantics(false),
    )
    .unwrap();

    assert_eq!(sensitive_result.executable, vec![1]);
    assert!(insensitive_result.executable.is_empty());
    let owned_excludes = vec!["OMIT".to_owned()];
    let cached = analyze(
        source,
        &owned_excludes,
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    let uncached_patterns = compile_patterns(&owned_excludes).unwrap();
    let uncached = analyze_with_patterns(
        source,
        &uncached_patterns,
        false,
        normal_python_semantics(false),
    )
    .unwrap();
    assert_eq!(cached.executable, uncached.executable);
    assert_eq!(cached.excluded, uncached.excluded);
}

#[test]
fn analysis_cache_evicts_by_retained_bytes_and_rejects_oversized_entries() {
    let mut analysis = failed_analysis(SourceFailure::Missing);
    analysis.executable = vec![1; 128];
    let retained_bytes = analysis.estimated_retained_bytes();
    let mut cache = AnalysisCache::default();

    cache.insert_with_limits(
        AnalysisKey([1; 32]),
        CachedAnalysis::new(analysis.clone()),
        CacheLimits {
            entries: 2,
            bytes: retained_bytes - 1,
        },
    );
    assert!(cache.entries.is_empty());

    let limits = CacheLimits {
        entries: 2,
        bytes: retained_bytes * 2 - 1,
    };
    cache.insert_with_limits(
        AnalysisKey([1; 32]),
        CachedAnalysis::new(analysis.clone()),
        limits,
    );
    cache.insert_with_limits(AnalysisKey([2; 32]), CachedAnalysis::new(analysis), limits);
    assert_eq!(cache.entries.len(), 1);
    assert!(!cache.entries.contains_key(&AnalysisKey([1; 32])));
    assert!(cache.entries.contains_key(&AnalysisKey([2; 32])));
    assert!(cache.retained_bytes <= limits.bytes);
}

#[test]
fn debug_folding_preserves_optimization_specific_cached_obligations() {
    let source = "if __debug__:\n    enabled = 1\nelse:\n    disabled = 2\n";
    for (debug_enabled, expected_lines) in [(true, vec![1, 2]), (false, vec![1, 4])] {
        let result = analyze(
            source,
            &[],
            true,
            PythonSemantics {
                class_bare_annotations_execute: false,
                debug_enabled,
            },
        )
        .expect("valid debug condition");
        assert_eq!(result.executable, expected_lines);
        assert!(result.possible_arcs.is_empty());
    }
}

#[test]
fn mixed_folded_conditions_keep_structural_branches_separate_from_compiled_lines() {
    for (condition, debug_enabled, executable, arcs) in [
        ("__debug__ and flag", false, vec![1, 2, 4, 5], vec![]),
        ("flag and not __debug__", true, vec![1, 2, 4, 5], vec![]),
        ("False and flag", true, vec![1, 2, 4, 5], vec![]),
        (
            "__debug__ or flag",
            true,
            vec![1, 2, 3, 5],
            vec![(2, -1), (2, 3)],
        ),
        (
            "flag or not __debug__",
            false,
            vec![1, 2, 3, 5],
            vec![(2, -1), (2, 3)],
        ),
        (
            "True or flag",
            true,
            vec![1, 2, 3, 5],
            vec![(2, -1), (2, 3)],
        ),
    ] {
        let source = format!(
            "def choose(flag):\n    if {condition}:\n        return 1\n    return 0\nchoose(True)\n"
        );
        let result = analyze(
            &source,
            &[],
            true,
            PythonSemantics {
                class_bare_annotations_execute: false,
                debug_enabled,
            },
        )
        .unwrap();
        assert_eq!(result.executable, executable, "{condition}");
        assert_eq!(result.possible_arcs, arcs, "{condition}");
        assert!(result.no_branch.is_empty(), "{condition}");
    }
}

#[test]
fn compiler_removed_body_falls_through_to_structural_exit_after_else_return() {
    let result = analyze(
        "def choose(flag):\n    if __debug__ and flag:\n        value = 1\n    else:\n        return 0\n    return value\nchoose(True)\n",
        &[],
        true,
        PythonSemantics {
            class_bare_annotations_execute: false,
            debug_enabled: false,
        },
    )
    .unwrap();
    assert_eq!(result.executable, vec![1, 2, 5, 7]);
    assert_eq!(result.possible_arcs, vec![(2, -1), (2, 5)]);
    assert!(result.no_branch.is_empty());
}
