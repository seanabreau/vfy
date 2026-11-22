//! Rendering preserves the prefix yielded before a Mapping protocol fails.

use super::{ContainerComparison, DictionaryDetail, DictionaryEnding, Side};
use crate::test_diagnostics::{DiagnosticCrash, DiagnosticLocation};

#[test]
fn mapping_failure_keeps_completed_details_and_pending_heading() {
    let rendered = ContainerComparison::Dictionary {
        left: "environ({'a': 1})".to_owned(),
        right: "{}".to_owned(),
        details: vec![
            DictionaryDetail::Identical(1),
            DictionaryDetail::Extra {
                side: Side::Left,
                count: 1,
                items: None,
            },
        ],
        ending: DictionaryEnding::Failure(Some(DiagnosticCrash {
            location: DiagnosticLocation {
                path: "mapping.py".to_owned(),
                line: 12,
            },
            text: "RuntimeError: lookup failed\ncontinuation".to_owned(),
        })),
    }
    .render();
    assert_eq!(
        rendered,
        "environ({'a': 1}) == {}\n  \n  Omitting 1 identical items, use -vv to show\n  Left contains 1 more item:\n  (pytest_assertion plugin: representation of details failed: mapping.py:12: RuntimeError: lookup failed.\n   Probably an object has a faulty __repr__.)"
    );
}

#[test]
fn completed_dictionary_preserves_the_default_explanation() {
    let rendered = ContainerComparison::Dictionary {
        left: "{'a': 1}".to_owned(),
        right: "{'a': 2}".to_owned(),
        details: vec![DictionaryDetail::Different(vec![(
            "{'a': 1}".to_owned(),
            "{'a': 2}".to_owned(),
        )])],
        ending: DictionaryEnding::Hint,
    }
    .render();
    assert_eq!(
        rendered,
        "{'a': 1} == {'a': 2}\n  \n  Differing items:\n  {'a': 1} != {'a': 2}\n  Use -v to get more diff"
    );
}

#[test]
fn unsuccessful_iterability_probe_does_not_add_a_hint() {
    let rendered = ContainerComparison::Dictionary {
        left: "left".to_owned(),
        right: "right".to_owned(),
        details: vec![DictionaryDetail::Identical(2)],
        ending: DictionaryEnding::Silent,
    }
    .render();
    assert_eq!(
        rendered,
        "left == right\n  \n  Omitting 2 identical items, use -vv to show"
    );
}
