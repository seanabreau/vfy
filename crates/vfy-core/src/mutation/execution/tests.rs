use super::FreshMutation;
use crate::mutation::{ExecutionOwner, MutationIdentity, MutationInput, source_hash};

fn input() -> MutationInput {
    let source = "def value(default=1):\r\n    return default\r\n";
    let mut input = MutationInput {
        id: String::new(),
        path: "/project/app.py".to_owned(),
        source_hash: source_hash(source),
        kind: "literal".to_owned(),
        operator: "number".to_owned(),
        original: "1".to_owned(),
        replacement: "2".to_owned(),
        start: 18,
        end: 19,
        line: 1,
        column: 19,
        owner: ExecutionOwner::default(),
    };
    input.id = MutationIdentity {
        path: &input.path,
        digest: &input.source_hash,
        kind: &input.kind,
        operator: &input.operator,
        start: input.start,
        end: input.end,
        replacement: &input.replacement,
    }
    .key();
    input
}

#[test]
fn fresh_import_replacement_preserves_unmodified_bytes() {
    let mutation = FreshMutation::new(input()).expect("the actual catalog identity is valid");
    assert_eq!(
        mutation
            .patch(b"def value(default=1):\r\n    return default\r\n")
            .expect("the exact source range is valid"),
        b"def value(default=2):\r\n    return default\r\n",
    );
}

#[test]
fn stale_source_or_tampered_identity_never_produces_a_replacement() {
    let mutation = FreshMutation::new(input()).expect("the actual catalog identity is valid");
    assert!(
        mutation
            .patch(b"def value(default=9):\r\n    return default\r\n")
            .is_err()
    );
    let mut changed = input();
    changed.replacement = "3".to_owned();
    assert!(FreshMutation::new(changed).is_err());
}

#[test]
fn collection_executed_body_requires_fresh_replay_even_without_code_replacement() {
    use crate::mutation::{MutationPhase, OwnerKind, mutation_body_isolated};
    use std::collections::{BTreeMap, BTreeSet};

    let source = b"def value():\n    return 1\n\ncached = value()\n";
    let directory = tempfile::tempdir().expect("real mutation source");
    let path = directory.path().join("app.py");
    std::fs::write(&path, source).expect("source bytes match the mutation owner");
    let mut input = input();
    input.path = crate::glob_path(&crate::absolute_path(&path));
    input.start = 24;
    input.end = 25;
    input.owner = ExecutionOwner {
        kind: OwnerKind::Function,
        qualname: "value".to_owned(),
        start: 0,
        end: 25,
        body_start: Some(17),
        fast_eligible: false,
        phase: MutationPhase::Body,
    };
    let mut lines = BTreeMap::from([(input.path.clone(), BTreeSet::from([1, 4]))]);
    assert!(mutation_body_isolated(&input, source, &lines).expect("unexecuted body"));
    lines.get_mut(&input.path).unwrap().insert(2);
    assert!(!mutation_body_isolated(&input, source, &lines).expect("import-time body call"));
}

#[test]
#[cfg(unix)]
fn isolation_declines_unmeasured_hardlinks_and_missing_source_identities() {
    use crate::mutation::{MutationPhase, OwnerKind, mutation_body_isolated};
    use std::collections::{BTreeMap, BTreeSet};

    let directory = tempfile::tempdir().expect("real source identity");
    let path = directory.path().join("app.py");
    let alias = directory.path().join("alias.py");
    let source = b"def value():\n    return 1\n";
    std::fs::write(&path, source).expect("single-link source");
    let mut input = input();
    input.path = crate::glob_path(&crate::absolute_path(&path));
    input.start = 24;
    input.end = 25;
    input.owner = ExecutionOwner {
        kind: OwnerKind::Function,
        qualname: "value".to_owned(),
        start: 0,
        end: 25,
        body_start: Some(17),
        fast_eligible: true,
        phase: MutationPhase::Body,
    };
    let lines = BTreeMap::from([(input.path.clone(), BTreeSet::from([1]))]);
    assert!(mutation_body_isolated(&input, source, &lines).expect("observed single-link source"));
    std::fs::hard_link(&path, &alias).expect("an alias omitted from the coverage source scope");
    assert!(!mutation_body_isolated(&input, source, &lines).expect("unmeasured physical alias"));
    std::fs::remove_file(alias).expect("remove the unmeasured alias");
    assert!(mutation_body_isolated(&input, source, &lines).expect("rechecked single-link source"));
    std::fs::remove_file(path).expect("remove the source identity");
    assert!(
        !mutation_body_isolated(&input, source, &lines).expect("missing metadata fails closed")
    );
}

#[test]
fn relative_collection_paths_preserve_isolation_and_merge_observed_aliases() {
    use std::collections::BTreeMap;

    use crate::data::CoverageData;
    use crate::mutation::{
        MutationPhase, OwnerKind, mutation_body_isolated, mutation_unattributed_lines,
    };

    let source = b"def value():\n    return 1\n\ncached = value()\n";
    let directory = tempfile::tempdir().expect("real collection source");
    let path = directory.path().join("app.py");
    std::fs::write(&path, source).expect("source bytes match the mutation owner");
    let absolute = crate::glob_path(&crate::absolute_path(&path));
    let relative = crate::glob_path(&crate::relative_path(
        &std::env::current_dir().expect("collection working directory"),
        &path,
    ));
    let mut input = input();
    input.path.clone_from(&absolute);
    input.start = 24;
    input.end = 25;
    input.owner = ExecutionOwner {
        kind: OwnerKind::Function,
        qualname: "value".to_owned(),
        start: 0,
        end: 25,
        body_start: Some(17),
        fast_eligible: true,
        phase: MutationPhase::Body,
    };
    for arcs in [false, true] {
        for observed in [false, true] {
            for spelling in [&absolute, &relative] {
                let mut data = CoverageData::new(arcs);
                if arcs {
                    data.add_arcs("", spelling, vec![(1, 4)]);
                    if observed {
                        data.add_arcs("", spelling, vec![(2, -2)]);
                    }
                } else {
                    data.add_lines(
                        "",
                        spelling,
                        if observed { vec![1, 2, 4] } else { vec![1, 4] },
                    );
                }
                // The other spelling must merge without discarding body evidence.
                data.add_lines("", &absolute, vec![1]);
                let lines = mutation_unattributed_lines(&data);
                assert_eq!(lines.len(), 1);
                assert_eq!(
                    mutation_body_isolated(&input, source, &lines).expect("valid owner"),
                    !observed,
                    "arcs={arcs}, observed={observed}, spelling={spelling}",
                );
            }
        }
    }
    assert!(!mutation_body_isolated(&input, source, &BTreeMap::new()).expect("missing evidence"));
}
