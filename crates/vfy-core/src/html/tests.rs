use std::borrow::Cow;

use proptest::prelude::*;

use super::escape;

#[test]
fn escaping_preserves_unicode_and_borrows_unmodified_text() {
    for value in ["", "plain text", "é🦀\0\t\n"] {
        let escaped = escape(value);
        assert!(matches!(escaped, Cow::Borrowed(_)));
        assert_eq!(escaped, value);
    }
    assert_eq!(
        escape("\0é<&>\"'🦀&amp;"),
        "\0é&lt;&amp;&gt;&quot;&#x27;🦀&amp;amp;"
    );
}

proptest! {
    #[test]
    fn escaping_matches_sequential_replacement_for_arbitrary_unicode(
        characters in prop::collection::vec(
            prop_oneof![
                any::<char>(),
                prop::sample::select(vec!['&', '<', '>', '"', '\'']),
            ],
            0..256,
        ),
    ) {
        let value: String = characters.into_iter().collect();
        let expected = value
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('"', "&quot;")
            .replace('\'', "&#x27;");
        prop_assert_eq!(escape(&value), expected);
    }
}
