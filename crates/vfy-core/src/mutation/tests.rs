use super::{MutationIdentity, source_hash};

fn identity<'a>() -> MutationIdentity<'a> {
    MutationIdentity {
        path: "module.py",
        digest: "digest",
        kind: "value",
        operator: "flip",
        start: 1,
        end: 2,
        replacement: "False",
    }
}

#[test]
fn source_hash_is_stable_and_hex_encoded() {
    assert_eq!(source_hash("value = 1\n").len(), 64);
    assert_ne!(source_hash("value = 1\n"), source_hash("value = 2\n"));
}

#[test]
fn mutation_key_changes_when_any_identity_field_changes() {
    let base = identity().key();
    assert!(base.starts_with("vfy:mutation:"));
    assert_ne!(
        base,
        MutationIdentity {
            digest: "other",
            ..identity()
        }
        .key()
    );
    assert_ne!(
        base,
        MutationIdentity {
            end: 3,
            ..identity()
        }
        .key()
    );
    assert_ne!(
        base,
        MutationIdentity {
            replacement: "True",
            ..identity()
        }
        .key()
    );
}

fn fingerprint_request(test: &str) -> serde_json::Value {
    serde_json::json!({
        "version": "test", "implementation": "cpython", "python_version": "3.12.0",
        "mutations": [{"id":"mutation", "path":"/project/subject.py",
            "source_hash":"source", "kind":"integer", "operator":"plus_one",
            "original":"1", "replacement":"2", "start":1, "end":2,
            "line":1, "column":0}],
        "tests":[test], "pytest_args":["-q"], "timeout":"1.0",
        "evidence":{"mutation":[test]}, "scopes":{"mutation":"selected"}, "suite_state":[["/project/subject.py","source"]]
    })
}

#[test]
fn fresh_identity_binds_selected_scope_and_complete_collection_order() {
    use super::{fresh_full_suite_mutation_fingerprints, mutation_fingerprints};
    let first = fingerprint_request("test_one.py::test_first").to_string();
    let other = fingerprint_request("test_two.py::test_second").to_string();
    let collection = vec![
        "test_one.py::test_first".to_owned(),
        "test_two.py::test_second".to_owned(),
    ];
    let full = fresh_full_suite_mutation_fingerprints(&first, &collection, "actual-inputs")
        .expect("a complete actual collection has a full-suite identity");
    assert_ne!(
        full,
        fresh_full_suite_mutation_fingerprints(&other, &collection, "actual-inputs")
            .expect("another requested selection has its own identity")
    );
    let mut requested = fingerprint_request("test_one.py::test_first");
    requested["evidence"]["mutation"] = serde_json::json!(&collection);
    let ordered = fresh_full_suite_mutation_fingerprints(
        &requested.to_string(),
        &collection,
        "actual-inputs",
    )
    .expect("selected scope follows actual collection order");
    requested["evidence"]["mutation"] = serde_json::json!([&collection[1], &collection[0]]);
    assert_eq!(
        ordered,
        fresh_full_suite_mutation_fingerprints(
            &requested.to_string(),
            &collection,
            "actual-inputs"
        )
        .expect("request priority cannot change native assignment order")
    );
    let selected = mutation_fingerprints(&first).expect("existing selected identity");
    assert_ne!(full.0, selected.0);
    assert_ne!(full.1["mutation"], selected.1["mutation"]);
    let mut reversed = collection;
    reversed.reverse();
    let changed = fresh_full_suite_mutation_fingerprints(&first, &reversed, "actual-inputs")
        .expect("another complete order has its own identity");
    assert_ne!(full.0, changed.0);
    assert_ne!(full.1["mutation"], changed.1["mutation"]);
}

#[test]
fn fresh_full_suite_identity_includes_source_arguments_and_actual_execution_inputs() {
    use super::fresh_full_suite_mutation_fingerprints;
    let input = fingerprint_request("test_one.py::test_first");
    let collection = vec!["test_one.py::test_first".to_owned()];
    let identity = |input: &serde_json::Value, actual| {
        fresh_full_suite_mutation_fingerprints(&input.to_string(), &collection, actual)
            .expect("valid fingerprint fixture")
    };
    let original = identity(&input, "actual-config-and-environment");
    for (field, value) in [
        ("pytest_args", serde_json::json!(["-q", "-k", "changed"])),
        (
            "suite_state",
            serde_json::json!([["/project/subject.py", "edited"]]),
        ),
        ("timeout", serde_json::json!("2.0")),
        ("timeout_policy", serde_json::json!("fixed")),
    ] {
        let mut changed = input.clone();
        changed[field] = value;
        let altered = identity(&changed, "actual-config-and-environment");
        assert_ne!(original.0, altered.0, "changed {field}");
        assert_ne!(
            original.1["mutation"], altered.1["mutation"],
            "changed {field}"
        );
    }
    assert_ne!(
        original,
        identity(&input, "changed-actual-config-or-environment")
    );
}

#[test]
fn fresh_full_suite_identity_rejects_incomplete_or_fabricated_collection_scope() {
    use super::fresh_full_suite_mutation_fingerprints;
    let mut input = fingerprint_request("test_one.py::test_first");
    for collection in [Vec::new(), vec![String::new()], vec!["same".to_owned(); 2]] {
        assert!(
            fresh_full_suite_mutation_fingerprints(&input.to_string(), &collection, "actual")
                .is_err()
        );
    }
    let collection = vec!["test_one.py::test_first".to_owned()];
    assert!(fresh_full_suite_mutation_fingerprints(&input.to_string(), &collection, "").is_err());
    input["evidence"] = serde_json::json!({});
    assert!(
        fresh_full_suite_mutation_fingerprints(&input.to_string(), &collection, "actual").is_err()
    );
}

#[test]
fn mutation_cache_identity_distinguishes_empty_scopes() {
    use super::{
        fresh_full_suite_mutation_fingerprints, mutation_fingerprints, read_mutation_cache,
    };
    let mut request = fingerprint_request("test_subject.py::test_value");
    request["evidence"]["mutation"] = serde_json::json!([]);
    request["scopes"]["mutation"] = serde_json::json!("unreached");
    let unreached = request.to_string();
    request["scopes"]["mutation"] = serde_json::json!("full_suite");
    let full_suite = request.to_string();
    let direct_unreached = mutation_fingerprints(&unreached).expect("unreached plan");
    let direct_full = mutation_fingerprints(&full_suite).expect("full-suite plan");
    // Produced by the pre-scope fingerprint implementation for this exact request.
    assert_ne!(
        direct_full.0,
        "2b2aa5c4a530a8caba9681c637e0b60854aecbb7e92ffe4aa732e9357bf1d742"
    );
    assert_ne!(
        direct_full.1["mutation"],
        "8d5f05455c7814075a198bdc3ac4dd8bf3393b1228a01e5ad148345090ed554f"
    );
    assert_ne!(direct_unreached.0, direct_full.0);
    assert_ne!(direct_unreached.1["mutation"], direct_full.1["mutation"]);
    let collection = vec!["test_subject.py::test_value".to_owned()];
    let fresh_unreached = fresh_full_suite_mutation_fingerprints(&unreached, &collection, "actual")
        .expect("unreached fresh plan");
    let fresh_full = fresh_full_suite_mutation_fingerprints(&full_suite, &collection, "actual")
        .expect("full-suite fresh plan");
    assert_ne!(
        fresh_full.1["mutation"],
        "ee572c7485f2172ca65d0faf398e8790bc82b0ce0e076ad8c9a65ffa634690c2"
    );
    assert_ne!(fresh_unreached.0, fresh_full.0);
    assert_ne!(fresh_unreached.1["mutation"], fresh_full.1["mutation"]);
    let directory = tempfile::tempdir().expect("cache directory");
    let path = directory.path().join("mutations.json");
    let mut result = request["mutations"][0].clone();
    result["status"] = serde_json::json!("unreached");
    result["killer"] = serde_json::Value::Null;
    result["returncode"] = serde_json::Value::Null;
    for (old, new) in [
        (
            "8d5f05455c7814075a198bdc3ac4dd8bf3393b1228a01e5ad148345090ed554f",
            &direct_full.1,
        ),
        (
            "ee572c7485f2172ca65d0faf398e8790bc82b0ce0e076ad8c9a65ffa634690c2",
            &fresh_full.1,
        ),
    ] {
        let cache = serde_json::json!({"schema":"vfy.mutation", "entries": {
            "mutation": {"fingerprint": old, "result": result}
        }});
        std::fs::write(&path, cache.to_string()).expect("write old cache");
        let old_map = std::collections::BTreeMap::from([("mutation".to_owned(), old.to_owned())]);
        assert_eq!(
            read_mutation_cache(&path, "unused", Some(&old_map))
                .expect("read cache")
                .len(),
            1
        );
        assert!(
            read_mutation_cache(&path, "unused", Some(new))
                .expect("read cache")
                .is_empty()
        );
    }
    request
        .as_object_mut()
        .expect("request object")
        .remove("scopes");
    assert!(mutation_fingerprints(&request.to_string()).is_err());
    assert!(
        fresh_full_suite_mutation_fingerprints(&request.to_string(), &collection, "actual")
            .is_err()
    );
}
