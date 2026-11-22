use crate::assertion_diagnostics::AssertionVerbosity;

fn equality(left: &str, right: &str, left_repr: &str, right_repr: &str) -> String {
    super::equality(
        left,
        right,
        left_repr,
        right_repr,
        AssertionVerbosity::Normal,
        super::super::AssertionTruncation::default(),
    )
}

#[test]
fn short_string_diff_and_shared_context_preserve_pytest_layout() {
    assert_eq!(
        equality("foo", "FOO", "'foo'", "'FOO'"),
        "'foo' == 'FOO'\n  \n  - FOO\n  + foo"
    );
    let actual = format!("{}x{}", "b".repeat(50), "a".repeat(100));
    let expected = format!("{}y{}", "b".repeat(50), "a".repeat(100));
    let result = equality(&actual, &expected, "left", "right");
    assert!(result.contains("Skipping 40 identical leading characters"));
    assert!(result.contains("Skipping 91 identical trailing characters"));
    assert!(result.contains("- bbbbbbbbbbyaaaaaaaaa\n  ?           ^"));
}

#[test]
fn text_context_matches_the_stock_prefix_and_unequal_length_boundaries() {
    let prepared = super::prepare_difference(&"b".repeat(50), &"b".repeat(51), 0);
    assert_eq!(prepared.left, "b".repeat(11));
    assert_eq!(prepared.right, "b".repeat(12));
    assert_eq!(
        prepared.notices,
        ["Skipping 39 identical leading characters in diff, use -v to show"]
    );
    let prepared = super::prepare_difference(
        &format!("x{}", "a".repeat(100)),
        &format!("longer{}", "a".repeat(100)),
        0,
    );
    assert!(prepared.notices.is_empty());
    assert_eq!(prepared.left.len(), 101);
    assert_eq!(prepared.right.len(), 106);
}

#[test]
fn text_context_requests_representations_for_python_whitespace() {
    let prepared = super::prepare_difference("\x1c", "\t", 0);
    assert!(prepared.escape_whitespace);
    let prepared = super::prepare_difference("left", "right", 1);
    assert!(!prepared.escape_whitespace);
}

#[test]
fn unequal_replacement_blocks_list_the_shorter_side_first() {
    assert_eq!(
        equality("a\nQ\nd", "a\nx\ny\nd", "'l'", "'r'"),
        "'l' == 'r'\n  \n    a\n  + Q\n  - x\n  - y\n    d"
    );
    assert_eq!(
        equality("a\nQ\nR\nd", "a\nx\nd", "'l'", "'r'"),
        "'l' == 'r'\n  \n    a\n  - x\n  + Q\n  + R\n    d"
    );
}
