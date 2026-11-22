use std::collections::BTreeMap;

use serde_json::{Value, json};

use super::{read_mutation_cache, write_mutation_cache};

fn result(id: &str) -> Value {
    json!({"id": id, "path": "example.py", "source_hash": "hash", "kind": "binary",
        "operator": "replace", "original": "+", "replacement": "-", "start": 1,
        "end": 2, "line": 1, "column": 1, "status": "protected", "tests": ["test.py::test"],
        "killer": "test.py::test", "duration_ms": 1.0, "returncode": 1, "detail": "detected"})
}

#[test]
fn invalid_records_fail_even_when_fingerprint_changes() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let mut invalid = result("bad");
    invalid["duration_ms"] = json!(-1.0);
    std::fs::write(&path, json!({"schema": "vfy.mutation", "fingerprint": "same", "mutations": [result("good"), invalid]}).to_string()).expect("seed cache");
    assert!(read_mutation_cache(&path, "same", None).is_err());
    assert!(read_mutation_cache(&path, "changed", None).is_err());
}

#[test]
fn incremental_cache_preserves_other_entries_and_rejects_stale_evidence() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let fingerprints = BTreeMap::from([
        ("one".to_owned(), "a".to_owned()),
        ("two".to_owned(), "b".to_owned()),
    ]);
    for id in ["one", "two"] {
        let report =
            json!({"schema": "vfy.mutation", "fingerprint": id, "mutations": [result(id)]});
        write_mutation_cache(&path, &report.to_string(), Some(&fingerprints))
            .expect("commit result");
    }
    assert_eq!(
        read_mutation_cache(&path, "irrelevant", Some(&fingerprints))
            .expect("read cache")
            .len(),
        2
    );
    let changed = BTreeMap::from([
        ("one".to_owned(), "changed".to_owned()),
        ("two".to_owned(), "b".to_owned()),
    ]);
    assert_eq!(
        read_mutation_cache(&path, "irrelevant", Some(&changed))
            .expect("read cache")
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["two"]
    );
}

#[test]
fn rejected_report_preserves_destination_bytes() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    std::fs::write(&path, b"existing cache").expect("seed destination");
    for owner in [
        json!({"kind": "function", "start": 20, "end": 10}),
        json!({"start": 2, "end": 20, "body_start": 1}),
        json!({"start": 0, "end": 20, "body_start": 20}),
        json!({"start": 0, "end": 20, "body_start": 2, "phase": "body"}),
    ] {
        let mut invalid = result("bad");
        invalid["owner"] = owner;
        let report =
            json!({"schema": "vfy.mutation", "fingerprint": "same", "mutations": [invalid]});
        assert!(write_mutation_cache(&path, &report.to_string(), None).is_err());
        assert_eq!(
            std::fs::read(&path).expect("read destination"),
            b"existing cache"
        );
    }
}

#[test]
fn body_boundaries_preserve_legacy_and_definition_cache_records() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let mut body = result("body");
    body["owner"] = json!({"start": 0, "end": 20, "body_start": 1, "phase": "body"});
    let mut definition = result("definition");
    definition["owner"] = json!({"start": 0, "end": 20, "body_start": 8, "phase": "definition"});
    let report = json!({"schema": "vfy.mutation", "fingerprint": "same", "mutations": [
        result("legacy"), body, definition
    ]});
    write_mutation_cache(&path, &report.to_string(), None).expect("valid boundary metadata");
    assert_eq!(
        read_mutation_cache(&path, "same", None)
            .expect("read cache")
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["body", "definition", "legacy"]
    );
}

#[test]
fn invalid_existing_cache_is_never_replaced() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let fingerprints = BTreeMap::from([("one".to_owned(), "a".to_owned())]);
    let report =
        json!({"schema": "vfy.mutation", "fingerprint": "new", "mutations": [result("one")]});
    let bad = [
        "{bad".to_owned(),
        json!({"schema": "wrong.schema", "entries": {}}).to_string(),
        json!({"schema": "vfy.mutation", "entries": {"one": {"fingerprint": "a", "result": result("other")}}}).to_string(),
        json!({"schema": "vfy.mutation", "entries": {"one": {"fingerprint": "a", "result": {}}}}).to_string(),
    ];
    assert!(
        read_mutation_cache(&path, "new", Some(&fingerprints))
            .expect("missing cache")
            .is_empty()
    );
    for bytes in bad {
        std::fs::write(&path, &bytes).expect("seed cache");
        assert!(read_mutation_cache(&path, "new", Some(&fingerprints)).is_err());
        for map in [None, Some(&fingerprints)] {
            assert!(write_mutation_cache(&path, &report.to_string(), map).is_err());
            assert_eq!(std::fs::read(&path).expect("read cache"), bytes.as_bytes());
        }
    }
    assert!(read_mutation_cache(directory.path(), "new", None).is_err());
}

#[test]
fn fingerprint_mismatch_is_a_cache_miss() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let report =
        json!({"schema": "vfy.mutation", "fingerprint": "old", "mutations": [result("one")]});
    write_mutation_cache(&path, &report.to_string(), None).expect("write cache");
    assert!(
        read_mutation_cache(&path, "new", None)
            .expect("read cache")
            .is_empty()
    );
}

#[test]
fn entry_without_fingerprint_is_rejected_and_destination_preserved() {
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let fingerprints = BTreeMap::from([("one".to_owned(), "a".to_owned())]);
    let first = json!({"schema": "vfy.mutation", "fingerprint": "f", "mutations": [result("one")]});
    write_mutation_cache(&path, &first.to_string(), Some(&fingerprints)).expect("seed cache");
    let before = std::fs::read(&path).expect("read cache");
    let report = json!({"schema": "vfy.mutation", "fingerprint": "f",
        "mutations": [result("one"), result("two")]});
    let error = write_mutation_cache(&path, &report.to_string(), Some(&fingerprints))
        .expect_err("missing fingerprint");
    assert!(error.contains("two"), "{error}");
    assert_eq!(std::fs::read(&path).expect("read cache"), before);
}
