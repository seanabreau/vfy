use super::{CollectionArgument, parameter_indices};

fn argument(parts: &[&str], parameter: Option<&str>) -> CollectionArgument {
    CollectionArgument {
        path: "/tests/test_cases.py".to_owned(),
        parts: parts.iter().map(|part| (*part).to_owned()).collect(),
        parametrization: parameter.map(str::to_owned),
    }
}

fn select(arguments: &[CollectionArgument], ids: &[&str]) -> Option<Vec<usize>> {
    parameter_indices(
        arguments,
        "/tests/test_cases.py",
        &[],
        "test_case",
        &ids.iter().map(|id| (*id).to_owned()).collect::<Vec<_>>(),
    )
}

#[test]
fn selection_retains_generation_order_and_deduplicates_allocation_only() {
    let arguments = [
        argument(&["test_case"], Some("[second]")),
        argument(&["test_case"], Some("[first]")),
        argument(&["test_case"], Some("[second]")),
    ];
    assert_eq!(
        select(&arguments, &["first", "second", "third"]),
        Some(vec![0, 1])
    );
    assert_eq!(
        select(&arguments, &["first", "second", "second", "third"]),
        Some(vec![0, 1, 2])
    );
}

#[test]
fn broad_overlapping_and_missing_selectors_require_expansion() {
    for arguments in [
        vec![argument(&[], None), argument(&["test_case"], Some("[one]"))],
        vec![
            argument(&["test_case"], None),
            argument(&["test_case"], Some("[one]")),
        ],
        vec![argument(&["test_case"], Some("[missing]"))],
    ] {
        assert_eq!(select(&arguments, &["one", "two"]), None);
    }
    assert_eq!(select(&[], &["one", "two"]), None);
}

#[test]
fn parameter_suffixes_match_literally_including_nested_brackets_and_colons() {
    assert_eq!(
        select(
            &[argument(&["test_case"], Some("[one]"))],
            &["one", "two[three]"]
        ),
        Some(vec![0])
    );
    let arguments = [
        argument(&["test_case"], Some("[one::two]")),
        argument(&["test_case"], Some("[rgb[38:2:1:2:3m]")),
    ];
    assert_eq!(
        select(&arguments, &["plain", "one::two", "rgb[38:2:1:2:3m"]),
        Some(vec![1, 2])
    );
    assert_eq!(
        select(
            &[argument(&["test_case"], Some("[missing[part]]"))],
            &["plain", "one::two", "rgb[38:2:1:2:3m"]
        ),
        None
    );
}

#[test]
fn class_names_and_lexical_paths_are_part_of_selection_identity() {
    let arguments = [argument(&["TestCases", "test_case"], Some("[π]"))];
    let ids = vec!["π".to_owned(), "other".to_owned()];
    assert_eq!(
        parameter_indices(
            &arguments,
            "/tests/test_cases.py",
            &["TestCases"],
            "test_case",
            &ids
        ),
        Some(vec![0])
    );
    assert_eq!(
        parameter_indices(
            &arguments,
            "/symlink/test_cases.py",
            &["TestCases"],
            "test_case",
            &ids
        ),
        Some(vec![])
    );
    assert_eq!(
        parameter_indices(
            &arguments,
            "/tests/test_cases.py",
            &["OtherCases"],
            "test_case",
            &ids
        ),
        Some(vec![])
    );
}
