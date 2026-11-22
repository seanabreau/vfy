use super::body_isolated;
use std::collections::{BTreeMap, BTreeSet};
use vfy_core::mutation::{ExecutionOwner, MutationInput, MutationPhase, OwnerKind};

#[test]
fn body_boundary_cannot_hide_a_collection_executed_mutation() {
    let directory = tempfile::tempdir().expect("source directory");
    let path = directory.path().join("subject.py");
    let source = "def value(flag):\n    if flag:\n        return 1\n    return 2\n";
    std::fs::write(&path, source).expect("subject source");
    let start = u32::try_from(source.find('1').expect("mutated literal")).expect("source offset");
    let mut input = MutationInput {
        id: String::new(),
        path: path.to_string_lossy().into_owned(),
        source_hash: String::new(),
        kind: "value".to_owned(),
        operator: "integer_plus_one".to_owned(),
        original: "1".to_owned(),
        replacement: "2".to_owned(),
        start,
        end: start + 1,
        line: 3,
        column: 15,
        owner: ExecutionOwner {
            kind: OwnerKind::Function,
            qualname: "value".to_owned(),
            start: 0,
            end: u32::try_from(source.len()).expect("source length"),
            body_start: Some(u32::try_from(source.find("if flag").expect("body")).expect("offset")),
            fast_eligible: true,
            phase: MutationPhase::Body,
        },
    };
    let mut lines = BTreeMap::from([(input.path.clone(), BTreeSet::from([1]))]);
    assert!(body_isolated(&input, &lines).expect("isolated body"));
    lines.insert(input.path.clone(), BTreeSet::from([1, 2, 3]));
    assert!(!body_isolated(&input, &lines).expect("collection call"));
    input.owner.body_start =
        Some(u32::try_from(source.find("return 2").expect("later branch")).expect("source offset"));
    assert!(!body_isolated(&input, &lines).expect("invalid advanced boundary"));
}
