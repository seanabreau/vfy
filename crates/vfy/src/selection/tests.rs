use std::collections::BTreeMap;

use super::record_durations;

#[test]
fn extension_history_adapter_round_trips_core_history() {
    let directory = tempfile::tempdir().expect("history directory");
    let path = directory.path().join("history.json");
    record_durations(
        path.to_str().expect("history path"),
        vec![("test_a".to_owned(), 2.0)],
    )
    .expect("write history");
    assert_eq!(
        super::read_history(&path).durations,
        BTreeMap::from([("test_a".to_owned(), 2.0)])
    );
}
