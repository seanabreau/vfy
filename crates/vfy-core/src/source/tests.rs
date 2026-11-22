use super::SourceLocator;
use super::decode;

#[test]
fn locates_ascii_and_exclusive_multiline_ranges() {
    let locator = SourceLocator::new("alpha\nbeta");
    assert_eq!(locator.line(0), 1);
    assert_eq!(locator.byte_column(4), 4);
    assert_eq!(locator.line_text(2), "beta");
    assert_eq!(locator.range_position(2..8), (1, 2, 2, 2));
    assert_eq!(locator.range_position(0..6), (1, 0, 2, 0));
}

#[test]
fn columns_count_utf8_bytes() {
    let locator = SourceLocator::new("aé日\nz");
    assert_eq!(locator.byte_column("aé".len()), 3);
    assert_eq!(locator.range_position(1.."aé日".len()), (1, 1, 1, 6));
}

#[test]
fn empty_input_and_eof_have_a_position() {
    let empty = SourceLocator::new("");
    assert_eq!(empty.range_position(0..0), (1, 0, 1, 0));
    assert_eq!(empty.line_text(1), "");

    let source = SourceLocator::new("abc");
    assert_eq!(source.range_position(3..3), (1, 3, 1, 3));
    assert_eq!(source.range_position(99..99), (1, 3, 1, 3));
}

#[test]
fn terminal_and_repeated_newlines_create_empty_lines() {
    let locator = SourceLocator::new("a\n\n");
    assert_eq!(locator.line_text(1), "a");
    assert_eq!(locator.line_text(2), "");
    assert_eq!(locator.line_text(3), "");
    assert_eq!(locator.range_position(1..3), (1, 1, 3, 0));
    assert_eq!(locator.range_position(2..2), (2, 0, 2, 0));
}

#[test]
fn decodes_cookies_without_windows_latin1_substitution() {
    assert_eq!(
        decode(b"# coding: latin-1\ntext = '\xe9\x80'\n".to_vec()).unwrap(),
        "# coding: latin-1\ntext = 'é\u{80}'\n"
    );
    assert_eq!(
        decode(b"#!/usr/bin/python\n# coding=cp1252\ntext = '\x80'\n".to_vec()).unwrap(),
        "#!/usr/bin/python\n# coding=cp1252\ntext = '€'\n"
    );
}

#[test]
fn cookie_requires_a_comment_in_the_first_two_physical_lines() {
    assert_eq!(
        decode(b"text = 1\n# coding: latin-1\n# \xe9".to_vec()).unwrap(),
        "text = 1\n# coding: latin-1\n# �"
    );
    assert!(decode(b"\xef\xbb\xbf# coding: latin-1\n".to_vec()).is_err());
    assert!(decode(b"# coding: no-such-codec\n".to_vec()).is_err());
    assert_eq!(
        decode(b"\xef\xbb\xbf# coding: utf-8\nx = 1\n".to_vec()).unwrap(),
        "# coding: utf-8\nx = 1\n"
    );
}

#[test]
fn universal_newlines_preserve_byte_offsets() {
    let locator = SourceLocator::new("é\r\n日\rx\n");
    assert_eq!(locator.range_position(4..7), (2, 0, 2, 3));
    assert_eq!(locator.range_position(8..10), (3, 0, 4, 0));
    assert_eq!(locator.line_text(1), "é");
    assert_eq!(locator.line_text(2), "日");
    assert_eq!(locator.line_text(3), "x");
    assert_eq!(locator.line_start(4), Some(10));
    for line in [i32::MIN, -1, 0, 5, i32::MAX] {
        assert_eq!(locator.line_start(line), None);
        assert_eq!(locator.line_text(line), "");
    }
}

#[test]
fn decoding_reuses_owned_utf8_buffer_and_preserves_newline_and_lossy_rules() {
    let source = b"\xef\xbb\xbf# coding: utf-8\r\nvalue = 1\r\r\n".to_vec();
    let allocation = source.as_ptr();
    let decoded = decode(source).unwrap();
    assert_eq!(decoded, "# coding: utf-8\nvalue = 1\n\n");
    assert_eq!(decoded.as_ptr(), allocation);
    assert_eq!(
        decode(b"# coding: utf-8\r\nvalue = '\xff'\r".to_vec()).unwrap(),
        "# coding: utf-8\nvalue = '\u{fffd}'\n"
    );
    assert!(decode(b"\xff\nvalue = 1\n".to_vec()).is_err());
}
