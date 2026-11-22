use super::*;

#[test]
fn equal_display_ids_resolve_separate_canonical_cases_after_reordering() {
    let mut first = CaseResult::new("same.py::test_same".to_owned(), 0.0, Vec::new());
    first.set_test_id(TestId::from_index(7));
    let mut second = CaseResult::new("same.py::test_same".to_owned(), 0.0, Vec::new());
    second.set_test_id(TestId::from_index(2));
    let cases = [second, first];
    assert_eq!(case_for_test(&cases, TestId::from_index(7)), Ok(1));
    assert_eq!(case_for_test(&cases, TestId::from_index(2)), Ok(0));
    assert!(case_for_test(&cases, TestId::from_index(3)).is_err());
}

#[test]
fn duplicate_source_observations_are_rejected_instead_of_guessed() {
    let mut case = CaseResult::new("same.py::test_same".to_owned(), 0.0, Vec::new());
    case.set_test_id(TestId::from_index(4));
    assert!(case_for_test(&[case.clone(), case], TestId::from_index(4)).is_err());
}

#[test]
fn source_index_roundtrips_without_changing_unobserved_case_wire() {
    let mut case = CaseResult::new("same.py::test_same".to_owned(), 0.0, Vec::new());
    let ordinary = serde_json::to_value(&case).unwrap();
    assert!(ordinary.get("test_index").is_none());
    case.set_test_id(TestId::from_index(9));
    let wire = serde_json::to_value(&case).unwrap();
    assert_eq!(wire["test_index"], 9);
    let decoded: CaseResult = serde_json::from_value(wire).unwrap();
    assert_eq!(decoded.test_id(), Some(TestId::from_index(9)));
    assert_eq!(decoded.nodeid, case.nodeid);
}
