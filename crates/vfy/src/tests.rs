use super::uses_sysmon_branch_arcs;

#[test]
fn selects_coverage_branch_arc_convention() {
    assert!(!uses_sysmon_branch_arcs(
        "cpython",
        (3, 14, 0, "alpha", 5),
        None
    ));
    assert!(uses_sysmon_branch_arcs(
        "cpython",
        (3, 14, 0, "alpha", 6),
        None
    ));
    assert!(uses_sysmon_branch_arcs(
        "cpython",
        (3, 14, 0, "final", 0),
        None
    ));
    assert!(!uses_sysmon_branch_arcs(
        "pypy",
        (3, 14, 0, "final", 0),
        None
    ));
    assert!(!uses_sysmon_branch_arcs(
        "cpython",
        (3, 14, 0, "final", 0),
        Some("ctrace")
    ));
}
