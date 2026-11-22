use super::{KeywordExpression, MarkerExpression};

fn selections(expression: &str, nodeids: &[&str]) -> Vec<bool> {
    let expression = KeywordExpression::parse(expression).expect("valid keyword expression");
    nodeids
        .iter()
        .map(|nodeid| expression.matches(nodeid))
        .collect()
}

#[test]
fn boolean_precedence_and_parentheses_preserve_selection() {
    let nodeids = [
        "test_api.py::test_fast",
        "test_api.py::test_slow",
        "test_cli.py::test_fast",
        "test_cli.py::test_slow",
    ];
    assert_eq!(
        selections("api or cli and not slow", &nodeids),
        [true, true, true, false]
    );
    assert_eq!(
        selections("(api or cli) and not slow", &nodeids),
        [true, false, true, false]
    );
    assert_eq!(
        selections("not (api or slow)", &nodeids),
        [false, false, true, false]
    );
    assert_eq!(
        selections("not not api", &nodeids),
        [true, true, false, false]
    );
}

#[test]
fn names_match_case_insensitively_and_keep_nodeid_punctuation() {
    assert_eq!(
        selections(
            "TEST_API.PY::TEST_CASE[value-1] or ÜBER",
            &[
                "tests/test_api.py::test_case[value-1]",
                "tests/test_api.py::test_case[value-2]",
                "test_über.py::test_value",
            ],
        ),
        [true, false, true]
    );
    assert_eq!(
        selections(
            "C:\\suite/test+value.py",
            &["c:\\suite/test+value.py::test"]
        ),
        [true]
    );
    assert_eq!(
        selections(
            "candy or nothing",
            &["test_candy", "test_nothing", "test_other"]
        ),
        [true, true, false]
    );
    assert_eq!(
        selections("AND", &["test_and", "test_other"]),
        [true, false]
    );
}

#[test]
fn empty_keyword_filters_select_every_node() {
    for expression in ["", " ", "\t \t"] {
        assert_eq!(selections(expression, &["test_a", "test_b"]), [true, true]);
    }
}

#[test]
fn keywords_match_separate_names_without_joining_parent_boundaries() {
    let names = [
        "ParentToken",
        "test_named.py",
        "TestClassToken",
        "MarkToken",
        "FunctionToken",
        "ExtraToken",
    ]
    .map(str::to_owned);
    for (source, expected) in [
        ("named.PY and classtoken", true),
        ("mArKtOk and FUNCTIONTOKEN and extratoken", true),
        ("ParentToken/test_named.py", false),
        ("MarkToken and not FunctionToken", false),
    ] {
        assert_eq!(
            KeywordExpression::parse(source)
                .expect("keyword expression")
                .matches_keywords(&names),
            expected,
            "{source}"
        );
    }
}

#[test]
fn marker_names_match_exactly_and_preserve_case_under_boolean_operators() {
    let names = ["Module", "Fast", "Slow"].map(str::to_owned);
    for (source, expected) in [
        ("Fast and Slow", true),
        ("Fast and not Slow", false),
        ("fast", false),
        ("Fas", false),
        ("Module and (Fast or absent)", true),
        ("not (fast or absent)", true),
    ] {
        assert_eq!(
            MarkerExpression::parse(source)
                .expect("marker expression")
                .matches_names(&names),
            expected,
            "{source}"
        );
    }
    assert!(
        MarkerExpression::parse("")
            .expect("empty marker option")
            .matches_names(&[])
    );
    assert!(
        !MarkerExpression::parse(" \t ")
            .expect("whitespace marker expression")
            .matches_names(&names)
    );
    assert!(
        MarkerExpression::parse("tag(flag=True)")
            .expect_err("unsupported marker keyword arguments")
            .contains("marker keyword arguments are unsupported")
    );
    assert!(
        MarkerExpression::parse("Fast and")
            .expect_err("invalid marker expression")
            .contains("invalid -m expression")
    );
}

#[test]
fn word_tokens_exclude_combining_marks_accepted_by_rust_alphabetic_checks() {
    for source in ["\u{0345}", "\u{05b0}"] {
        assert!(
            KeywordExpression::parse(source)
                .expect_err("combining mark keyword")
                .contains("unsupported character")
        );
        assert!(
            MarkerExpression::parse(source)
                .expect_err("combining mark marker")
                .contains("unsupported character")
        );
    }
    let numeric_name = "\u{216b}";
    assert!(
        MarkerExpression::parse(numeric_name)
            .expect("Unicode letter number")
            .matches_names(&[numeric_name.to_owned()])
    );
}

#[test]
fn invalid_syntax_and_unsupported_tokens_return_precise_errors() {
    for (expression, expected) in [
        ("one and", "column 8: expected a name"),
        ("one two", "column 5: expected 'and', 'or', or end of input"),
        ("(one or two", "column 12: expected ')'"),
        ("()", "column 2: expected a name"),
        ("one)", "column 4: expected 'and', 'or', or end of input"),
        ("and one", "column 1: expected a name"),
        (
            "one AND two",
            "column 5: expected 'and', 'or', or end of input",
        ),
        ("one && two", "column 5: unsupported character '&'"),
        ("test_*", "column 6: unsupported character '*'"),
        ("'one'", "column 1: unsupported character '\\''"),
        ("one\ntwo", "column 4: unsupported character '\\n'"),
        ("name(x=1)", "column 7: unsupported character '='"),
        ("name()", "column 5: expected 'and', 'or', or end of input"),
        ("é & b", "column 3: unsupported character '&'"),
    ] {
        let error = KeywordExpression::parse(expression).expect_err("invalid keyword expression");
        assert!(error.contains(expected), "{expression:?}: {error:?}");
    }
}

#[test]
fn expression_size_and_nesting_are_bounded_without_restricting_flat_lists() {
    assert!(KeywordExpression::parse(&"x".repeat(super::MAX_EXPRESSION_BYTES + 1)).is_err());
    let nested = format!(
        "{}x{}",
        "(".repeat(super::MAX_NESTING),
        ")".repeat(super::MAX_NESTING)
    );
    assert!(
        KeywordExpression::parse(&nested)
            .expect("maximum nesting")
            .matches("x")
    );
    let excessive = format!("({nested})");
    assert!(
        KeywordExpression::parse(&excessive)
            .expect_err("excessive nesting")
            .contains("nesting exceeds 128")
    );
    let negations = format!("{}x", "not ".repeat(super::MAX_NESTING + 1));
    assert!(KeywordExpression::parse(&negations).is_err());
    let flat = vec!["x"; 4_000].join(" and ");
    assert!(
        KeywordExpression::parse(&flat)
            .expect("flat expression")
            .matches("x")
    );
}
