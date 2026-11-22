use super::analyze_source;

#[test]
fn discovers_stable_low_noise_mutations() {
    let records = analyze_source(
        "sample.py",
        "def f(x, y):\n    if (\n        x >= 2  # and is not the operator\n        and y != 4\n    ):\n        audit()\n    return x + 1\n",
    );
    let operators: Vec<_> = records.iter().map(|record| record.4.as_str()).collect();
    assert!(operators.contains(&"comparison_negation"));
    assert!(operators.contains(&"comparison_boundary"));
    assert!(operators.contains(&"boolean_operator"));
    assert!(operators.contains(&"binary_operator"));
    assert!(operators.contains(&"remove_call"));
    assert!(records.iter().all(|record| record.2.len() == 64));
}

#[test]
fn excludes_coverage_pragmas() {
    let records = analyze_source(
        "sample.py",
        "if value:  # pragma: no decision\n    return 1\n",
    );
    assert!(records.is_empty());
}

#[test]
fn comparison_replacements_are_exact() {
    let records = analyze_source(
        "comparisons.py",
        "a == b\na != b\na < b\na <= b\na > b\na >= b\na is b\na is not b\na in b\na not in b\n",
    );
    let negations: Vec<_> = records
        .iter()
        .filter(|record| record.4 == "comparison_negation")
        .map(|record| (record.5.as_str(), record.6.as_str()))
        .collect();
    assert_eq!(
        negations,
        vec![
            ("==", "!="),
            ("!=", "=="),
            ("<", ">="),
            ("<=", ">"),
            (">", "<="),
            (">=", "<"),
            ("is", "is not"),
            ("is not", "is"),
            ("in", "not in"),
            ("not in", "in"),
        ]
    );
    let boundaries: Vec<_> = records
        .iter()
        .filter(|record| record.4 == "comparison_boundary")
        .map(|record| (record.5.as_str(), record.6.as_str()))
        .collect();
    assert_eq!(
        boundaries,
        vec![("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")]
    );
}

#[test]
fn excludes_multiline_header_pragmas() {
    let records = analyze_source(
        "sample.py",
        "if (\n    value\n):  # pragma: no decision\n    return value + 1\n",
    );
    assert!(records.is_empty());
}

#[test]
fn records_the_smallest_safe_callable_owner() {
    let records = analyze_source(
        "owners.py",
        "def outer(value):\n    def inner(item):\n        return item >= 2\n    return inner(value)\n\nclass Box:\n    def value(self, item):\n        return item >= 2\n\n@decorator\ndef wrapped(item):\n    return item >= 2\n",
    );
    let nested = records
        .iter()
        .find(|record| record.9 == 3 && record.4 == "comparison_boundary")
        .expect("nested function mutation");
    let nested_owner: serde_json::Value = serde_json::from_str(&nested.11).unwrap();
    assert_eq!(nested_owner["kind"], "function");
    assert_eq!(nested_owner["qualname"], "outer");
    assert_eq!(nested_owner["fast_eligible"], true);

    let method = records
        .iter()
        .find(|record| record.9 == 8 && record.4 == "comparison_boundary")
        .expect("method mutation");
    let method_owner: serde_json::Value = serde_json::from_str(&method.11).unwrap();
    assert_eq!(method_owner["qualname"], "Box.value");
    assert_eq!(method_owner["fast_eligible"], true);

    let decorated = records
        .iter()
        .find(|record| record.9 == 12 && record.4 == "comparison_boundary")
        .expect("decorated function mutation");
    let decorated_owner: serde_json::Value = serde_json::from_str(&decorated.11).unwrap();
    assert_eq!(decorated_owner["qualname"], "wrapped");
    assert_eq!(decorated_owner["fast_eligible"], false);
}

#[test]
fn records_definition_time_and_body_execution_phases() {
    let records = analyze_source(
        "definition_phase.py",
        concat!(
            "module_value = 2\n\n",
            "def value(argument=1):\n    return argument + 1\n"
        ),
    );
    let default = records
        .iter()
        .find(|record| record.4 == "integer_plus_one" && record.9 == 3)
        .expect("default argument mutation");
    let default_owner: serde_json::Value = serde_json::from_str(&default.11).unwrap();
    assert_eq!(default_owner["phase"], "definition");
    assert_eq!(default_owner["fast_eligible"], false);

    let body = records
        .iter()
        .find(|record| record.4 == "binary_operator")
        .expect("function-body mutation");
    let body_owner: serde_json::Value = serde_json::from_str(&body.11).unwrap();
    assert_eq!(body_owner["phase"], "body");

    let module = records
        .iter()
        .find(|record| record.4 == "integer_plus_one" && record.9 == 1)
        .expect("module assignment mutation");
    let module_owner: serde_json::Value = serde_json::from_str(&module.11).unwrap();
    assert_eq!(module_owner["phase"], "definition");
}

#[test]
fn keeps_nested_class_body_mutations_portable() {
    let records = analyze_source(
        "nested_class.py",
        concat!(
            "def outer(value):\n",
            "    class Inner:\n",
            "        limit = value + 1\n",
            "        def read(self):\n",
            "            return 43\n",
            "    return Inner.limit\n"
        ),
    );
    let mutation = records
        .iter()
        .find(|record| record.4 == "integer_plus_one" && record.9 == 3)
        .expect("nested class-body mutation");
    let owner: serde_json::Value = serde_json::from_str(&mutation.11).unwrap();
    assert_eq!(owner["qualname"], "outer");
    assert_eq!(owner["fast_eligible"], false);
    assert_eq!(owner["phase"], "definition");

    let method_mutation = records
        .iter()
        .find(|record| record.4 == "integer_plus_one" && record.9 == 5)
        .expect("nested class method-body mutation");
    let method_owner: serde_json::Value = serde_json::from_str(&method_mutation.11).unwrap();
    assert_eq!(method_owner["qualname"], "outer");
    assert_eq!(method_owner["phase"], "body");
}

#[test]
fn keeps_methods_of_decorated_classes_portable() {
    let records = analyze_source(
        "decorated_class.py",
        "@decorator\nclass Box:\n    def value(self, item):\n        return item >= 2\n",
    );
    let mutation = records
        .iter()
        .find(|record| record.4 == "comparison_boundary")
        .expect("decorated-class method mutation");
    let owner: serde_json::Value = serde_json::from_str(&mutation.11).unwrap();
    assert_eq!(owner["qualname"], "Box.value");
    assert_eq!(owner["fast_eligible"], false);
}

#[test]
fn universal_newlines_keep_mutation_locations_and_exclusions() {
    for newline in ["\n", "\r\n", "\r"] {
        let source = ["a >= 1  # pragma: no cover", "é >= 2", ""].join(newline);
        let records = analyze_source("newlines.py", &source);
        assert!(!records.is_empty());
        assert!(records.iter().all(|record| record.9 == 2));
        let comparison = records
            .iter()
            .find(|record| record.4 == "comparison_negation")
            .expect("the second line has a comparison mutation");
        assert_eq!(comparison.10, 3);
        assert_eq!(&source[comparison.7 as usize..comparison.8 as usize], ">=");
    }
}
