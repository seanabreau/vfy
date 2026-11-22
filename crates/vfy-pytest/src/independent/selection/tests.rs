use super::selection_mask;

#[test]
fn selected_tests_retain_the_project_collection_order() {
    let nodeids = ["test_b", "test_a", "test_c"].map(str::to_owned);
    let selected = ["test_c", "test_b"].map(str::to_owned);
    assert_eq!(
        selection_mask(&nodeids, &selected).expect("collected selection"),
        [true, false, true],
    );
}

#[test]
fn invalid_selection_is_rejected_before_filtering() {
    let nodeids = ["test_a"].map(str::to_owned);
    for selected in [vec!["test_a", "test_a"], vec!["test_missing"]] {
        let selected = selected.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert!(selection_mask(&nodeids, &selected).is_err());
    }
}
