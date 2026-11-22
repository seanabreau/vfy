use super::{PythonVersion, RequestedCore, RuntimePolicy};

fn policy<'a>(
    implementation: &'a str,
    version: (u8, u8, u8, &'a str, u8),
    requested_core: RequestedCore<'a>,
) -> RuntimePolicy<'a> {
    RuntimePolicy {
        implementation,
        version: PythonVersion {
            major: version.0,
            minor: version.1,
            micro: version.2,
            release_level: version.3,
            serial: version.4,
        },
        requested_core,
    }
}

#[test]
fn selects_the_supported_cpython_branch_arc_core() {
    assert!(
        !policy("cpython", (3, 14, 0, "alpha", 5), RequestedCore::Default,)
            .uses_sysmon_branch_arcs()
    );
    assert!(
        policy("cpython", (3, 14, 0, "alpha", 6), RequestedCore::Default,)
            .uses_sysmon_branch_arcs()
    );
    assert!(
        policy("cpython", (3, 14, 0, "final", 0), RequestedCore::Default,)
            .uses_sysmon_branch_arcs()
    );
    assert!(
        !policy("pypy", (3, 14, 0, "final", 0), RequestedCore::Default,).uses_sysmon_branch_arcs()
    );
    assert!(
        !policy(
            "cpython",
            (3, 14, 0, "final", 0),
            RequestedCore::Named("ctrace"),
        )
        .uses_sysmon_branch_arcs()
    );
}
