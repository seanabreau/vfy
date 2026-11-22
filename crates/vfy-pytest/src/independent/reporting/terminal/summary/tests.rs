use super::normalize;

#[test]
fn report_selection_replaces_resets_and_deduplicates_in_request_order() {
    assert_eq!(normalize("fEaNSFfs", false), "wsf");
    assert_eq!(normalize("pAPX", false), "wPpsxXEf");
    assert_eq!(normalize("N", false), "w");
    assert_eq!(normalize("waw", true), "sxXEf");
    assert_eq!(normalize("ZwZ", false), "Zw");
}
