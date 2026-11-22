use super::{
    BackslashEscaping, ParameterRow, escape_identifier, finalize_fixture_identifiers,
    parameter_suffix, unique_identifiers,
};

#[test]
fn escaped_parameter_ids_distinguish_surrogates_and_replacement_characters() {
    assert_eq!(
        escape_identifier(
            [0xdcbf, 0xdc80, 0xfffd, 0xd800, 0xdc00].into_iter(),
            BackslashEscaping::Escape
        ),
        "\\udcbf\\udc80\\ufffd\\ud800\\udc00"
    );
}

#[test]
fn stacked_parameter_suffix_preserves_literal_brackets_and_empty_ids() {
    let ids = vec![String::new(), "[a]".to_owned(), String::new()];
    assert_eq!(parameter_suffix(&ids), "[-[a]-]");
}

#[test]
fn fixture_parameter_suffix_uses_the_complete_ordered_id_list() {
    let mut rows = [ParameterRow {
        suffix: "[[a]]".to_owned(),
        idlist: vec!["[a]".to_owned()],
        fixture_ids: vec!["fixture]".to_owned()],
        ..ParameterRow::default()
    }];

    finalize_fixture_identifiers(&mut rows, &[]);

    assert_eq!(rows[0].idlist, ["fixture]", "[a]"]);
    assert_eq!(rows[0].suffix, "[fixture]-[a]]");
}

#[test]
fn duplicate_ids_skip_existing_names_and_preserve_unique_names() {
    let mut unique = ["first".to_owned(), "second".to_owned()];
    unique_identifiers(&mut unique);
    assert_eq!(unique, ["first", "second"]);

    let mut mixed = ["a", "a", "a0", "b1", "b1", "b1_0"].map(str::to_owned);
    unique_identifiers(&mut mixed);
    assert_eq!(mixed, ["a1", "a2", "a0", "b1_1", "b1_2", "b1_0"]);
}
